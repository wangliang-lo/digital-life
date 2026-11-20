"""小鱼的"褪黑素 / 睡眠节律"逻辑。

2026-11-16 从 dl_api.py 拆出（拆分第四步，小切片）。
逐字搬运：除 import、路径修正、以及 HORMONES / set_sleep_state / is_sleeping
三个运行时依赖改成由 set_runtime() 注入外，逻辑一行没改。

本文件在 dl_server/ 下，所以 data/ 路径一律用
Path(__file__).resolve().parent.parent（= chat_bridge/）。

用法（dl_api.py 在 HORMONES / set_sleep_state / is_sleeping 都定义好之后调一次）：

    from dl_server import physiology
    physiology.set_runtime(
        hormones=HORMONES,
        sleep_setter=set_sleep_state,
        sleep_checker=is_sleeping,
    )

不反向 import dl_api（会循环）。

melatonin_tick 依赖的东西：
- HORMONES.sleep_blocked()、HORMONES.values["cortisol"]  → hormones
- set_sleep_state(hours=8.0)                              → sleep_setter
- is_sleeping(now)                                        → sleep_checker
- log、_update_json / _write_json_atomic                  → 本文件自己
"""
from __future__ import annotations

import json
import os
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("dl_api.physiology")

# state_io 在 chat_bridge/tools/ 下（dl_api.py 里也是 append 进去再 import 的）。
try:  # dl_api 先 import 过的话直接命中；单独 import 本模块时兜底加路径
    from state_io import update_json as _update_json  # noqa: F401
    from state_io import write_json as _write_json_atomic  # noqa: F401
except ImportError:  # pragma: no cover - 只在单独 import 时走到
    _TOOLS_DIR = str(Path(__file__).resolve().parent.parent / "tools")
    if _TOOLS_DIR not in sys.path:
        sys.path.append(_TOOLS_DIR)
    from state_io import update_json as _update_json  # noqa: F401
    from state_io import write_json as _write_json_atomic  # noq# 2026-11-16：测试/冒烟实例可以用 XIAOYU_STATE_DIR 指到临时目录，
# 避免再往真实 data/ 里写 melatonin.json（今天是真踩到过）。
_STATE_DIR = Path(os.environ.get("XIAOYU_STATE_DIR") or (Path(__file__).resolve().parent.parent / "data"))
a: F401


MELATONIN_STATE = _STATE_DIR / "melatonin.json"

# 褪黑素：22:00 开始攒，越晚涨得越快（二次曲线），03:00 到顶 100 就该睡着了。
# 睡着后匀速消退；白天不涨（只在白天把残留冲掉）。
MELATONIN_START_HOUR = 22.0   # 22:00 开始
MELATONIN_PEAK_HOUR = 27.0    # 次日 03:00 到顶
MELATONIN_MAX = 100.0
MELATONIN_DECAY_PER_HOUR = 100.0 / 6.0  # 睡着后约 6 小时清空


# ---- 运行时依赖（set_runtime 注入；没注入时用安全兜底）----------------------
class _NullHormones:
    """没接 dl_api 时的兜底：任何睡眠阻塞都算"没阻塞"。"""

    values: dict[str, Any] = {}

    def sleep_blocked(self) -> bool:
        return False

    def interoception(self, _level: float) -> str:  # pragma: no cover - 仅兜底
        return ""

    def on_wake(self) -> None:  # pragma: no cover - 仅兜底
        return None


HORMONES: Any = _NullHormones()
set_sleep_state: Callable[..., None] = lambda *a, **k: None
is_sleeping: Callable[..., bool] = lambda *a, **k: False


def set_runtime(
    hormones: Any = None,
    sleep_setter: Callable[..., None] | None = None,
    sleep_checker: Callable[..., bool] | None = None,
) -> None:
    """dl_api.py 在模块加载完成后调用一次：把全局对象的数据写入进来。"""
    global HORMONES, set_sleep_state, is_sleeping
    if hormones is not None:
        HORMONES = hormones
    if sleep_setter is not None:
        set_sleep_state = sleep_setter
    if sleep_checker is not None:
        is_sleeping = sleep_checker


# ---- 以下为从 dl_api.py 逐字搬来的函数 -------------------------------------
def _melatonin_load() -> dict[str, Any]:
    try:
        data = json.loads(MELATONIN_STATE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _melatonin_save(level: float, asleep: bool, now: datetime) -> None:
    # 2026-11-14 修：原来是整份覆盖，把 forced_sleep_day / forced_sleep_defer_until
    # 一起抹掉——顺延永远到不了期，"每晚只强制一次"也失效。
    # 2026-11-16：改成只 patch 自己这几个键（update_json 带锁读改写），
    # 别的线程刚写的 stuck_fixed_at / forced_sleep_* 不会再被读到的旧值盖回去。
    try:
        _update_json(MELATONIN_STATE, {
            "level": round(max(0.0, min(MELATONIN_MAX, level)), 2),
            "asleep": bool(asleep),
            "updated_at": now.isoformat(timespec="seconds"),
        })
    except Exception:  # noqa: BLE001
        log.warning("写褪黑素状态失败")


def _night_hours(now: datetime) -> float | None:
    """现在处于「22:00→03:00」这段夜里的话，返回距 22:00 的小时数；白天返回 None。"""
    hour = now.hour + now.minute / 60.0
    if hour >= MELATONIN_START_HOUR:
        return hour - MELATONIN_START_HOUR
    # 2026-11-14 修：原来只到 03:00，导致"到顶强制入睡"几乎永远错过后半夜。
    # 夜里一直算到 06:00（曲线本身会在峰值处夹住，不影响白天）。
    if hour < 6:  # 00:00–06:00
        return hour + (24 - MELATONIN_START_HOUR)
    return None


def _melatonin_curve(t: float) -> float:
    """t 小时（22:00 起算，0–5）：二次增长，越往后越快，5 小时正好到 100。"""
    t = max(0.0, min(MELATONIN_PEAK_HOUR - MELATONIN_START_HOUR, t))
    return MELATONIN_MAX * (t / (MELATONIN_PEAK_HOUR - MELATONIN_START_HOUR)) ** 2


def melatonin_tick(now: datetime | None = None) -> float:
    """算当前褪黑素水平并落盘；到 03:00 还醒着就让她入睡。"""
    current = now or datetime.now().astimezone()
    state = _melatonin_load()
    stored = float(state.get("level") or 0.0)
    asleep = is_sleeping(current)
    elapsed = 0.0
    try:
        prev = datetime.fromisoformat(str(state.get("updated_at") or ""))
        elapsed = max(0.0, min(12.0, (current - prev).total_seconds() / 3600.0))
    except Exception:  # noqa: BLE001
        elapsed = 0.0
    faded = max(0.0, stored - MELATONIN_DECAY_PER_HOUR * elapsed)

    if asleep:
        level = faded
        _melatonin_save(level, True, current)
        return level

    night = _night_hours(current)
    if night is None:
        # 白天：不增加，只把残留冲淡
        level = faded
        _melatonin_save(level, False, current)
        return level

    # 夜里醒着：取"时钟曲线"和"残留淡化后"的较大值（睡过一觉醒来不会是满格）
    target = _melatonin_curve(night)
    level = max(target, faded)
    _melatonin_save(level, False, current)

    # 03:00 到顶还醒着 → 强制入睡（每晚只做一次）
    # 2026-11-04：皮质醇高的时候睡不着——真人在应激状态下，褪黑素够也照样睁着眼。
    # 所以这时不硬按她睡，只顺延（每 45 分钟再看一次）。
    if night >= (MELATONIN_PEAK_HOUR - MELATONIN_START_HOUR) - 0.02 and level >= MELATONIN_MAX - 0.5:
        if state.get("forced_sleep_day") != current.date().isoformat():
            try:
                elevated = HORMONES.sleep_blocked()
            except Exception:  # noqa: BLE001
                elevated = False
            defer_until = str(state.get("forced_sleep_defer_until") or "")
            if elevated and (not defer_until or datetime.fromisoformat(defer_until) > current):
                if not defer_until:
                    defer = current + timedelta(minutes=45)
                    # 2026-11-16：原来写回的是函数开头读到的旧 state，
                    # 会把刚存进去的 level 覆盖成旧值。改成 update_json 只写这一个键
                    # （带锁读改写，"重读再写"那一步也不用自己做了）。
                    try:
                        _update_json(MELATONIN_STATE, {
                            "forced_sleep_defer_until": defer.isoformat(timespec="seconds"),
                        })
                    except Exception:  # noqa: BLE001
                        pass
                    log.info("皮质醇偏高（%.2f），今晚先不强制入睡，45 分钟后再看", float(HORMONES.values.get("cortisol") or 0))
            else:
                log.info("褪黑素到顶，让她入睡")
                set_sleep_state(hours=8.0)
                forced = _melatonin_load()
                forced["forced_sleep_day"] = current.date().isoformat()
                forced.pop("forced_sleep_defer_until", None)
                try:
                    _write_json_atomic(MELATONIN_STATE, forced)
                except Exception:  # noqa: BLE001
                    pass
    return level


def melatonin_note(level: float | None = None) -> str:
    """把困意翻译成她能感觉到的状态。"""
    value = melatonin_tick() if level is None else level
    if value < 10:
        return ""
    if value < 30:
        return f"\n【身体状态】褪黑素 {value:.0f}/100：有点犯困了，反应比白天慢一点。"
    if value < 55:
        return f"\n【身体状态】褪黑素 {value:.0f}/100：明显困了，脑子转得慢，说话会变短；能不聊就少聊两句。"
    if value < 80:
        return f"\n【身体状态】褪黑素 {value:.0f}/100：很困，注意力散，句子容易短、容易漏字；想回就回一句，不想回就睡。"
    if value < 97:
        return f"\n【身体状态】褪黑素 {value:.0f}/100：困到快撑不住了，说话会磕巴、会断句，长句说不完整。"
    return f"\n【身体状态】褪黑素 {value:.0f}/100：已经困到极限，只想睡，别硬撑着聊天。"
