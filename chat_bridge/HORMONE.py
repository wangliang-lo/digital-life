#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""小鱼的激素系统。

原则（和 HORMONE_SYSTEM.md 一致）：
1. 激素**不进提示词**。她感觉到的方式是：能想的 token 变少了、等不了那么久、
   翻不动深记忆、阈值偏了、接话更快或更迟钝。
2. 只有**行为**能推动激素：聊天热不热闹、被骂、熬夜、被安慰、读完一本书。
   没有任何 [SET_HORMONE] 之类的标签能让模型直接改数值。
3. 全部硬夹紧、带半衰期、带速率限制；两两叠加有下限，绝不让她说不出话。

用法：
    from HORMONE import HORMONES
    HORMONES.tick()
    HORMONES.on_chat(text=..., user=..., joy_delta=..., hurt_delta=..., trauma=...)
    knobs = HORMONES.knobs()
"""
from __future__ import annotations

import json
import math
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

STATE_FILE = Path(__file__).resolve().parent / "data" / "hormones.json"

LOCK = threading.Lock()

# 每个激素：基线 / 半衰期（小时）/ 上下限
SPEC: dict[str, dict[str, float]] = {
    "dopamine":   {"base": 0.35, "half_life": 1.5,  "min": 0.0, "max": 1.0},   # 多巴胺：新奇、兴趣
    "cortisol":   {"base": 0.25, "half_life": 8.0,  "min": 0.0, "max": 1.0},   # 皮质醇：压力、创伤
    "adrenaline": {"base": 0.05, "half_life": 0.12, "min": 0.0, "max": 1.0},   # 肾上腺素：秒级冲动
    "oxytocin":   {"base": 0.30, "half_life": 12.0, "min": 0.0, "max": 1.0},   # 催产素：亲密（按人另存）
    "serotonin":  {"base": 0.55, "half_life": 18.0, "min": 0.0, "max": 1.0},   # 血清素：稳态（跟着白天走）
    "endorphin":  {"base": 0.20, "half_life": 1.0,  "min": 0.0, "max": 1.0},   # 内啡肽：痛后舒缓
}

MAX_DELTA_PER_EVENT = 0.35  # 单次事件最多推动这么多，防抖

# 2026-11-03 爸爸："激素不要被行为直接设定，设成动量。"
# 事件只往"动量"里加一个冲量（每小时往哪个方向走多快），真正的数值由 tick()
# 按经过的时间积分出来，动量本身也会衰减——所以一次高兴是"往上飘起来"，
# 不是"啪一下跳到 0.9"，也不会一直挂在上面。
MAX_MOMENTUM = 0.40            # 动量上限（每小时最多能挪多少）
MOMENTUM_HALF_LIFE = 0.75      # 动量自己散掉的半衰期（小时）
# 不管半衰期多长，数值最多 4 小时就落回一半——不然血清素/催产素会整天挂在高位。
MAX_EFFECTIVE_HALF_LIFE = 4.0

# ---------------------------------------------------------------- 生物钟
# 2026-11-03 爸爸："让小鱼体内的激素模拟真实的生物钟。"
# 原来每种激素的"基线"是个常数，回落到那儿就再也不动了——所以她半夜和中午
# 一样兴奋，早晨和深夜一样平。真实的人不是这样：皮质醇天亮前开始爬、上午最高，
# 血清素白天高夜里低，褪黑素（dl_api 那边）22 点后自己涨。
#
# 这里给每种激素一条 24 小时的余弦曲线：值 = 均值 + 振幅 * cos(2π(h - 峰值)/24)
CIRCADIAN: dict[str, tuple[float, float, float]] = {
    # 名称: (均值, 振幅, 峰值时刻)
    "cortisol":   (0.25, 0.14, 7.5),   # 皮质醇：晨起爬升，傍晚后最低
    "serotonin":  (0.52, 0.10, 14.0),  # 血清素：白天高，夜里低
    "dopamine":   (0.34, 0.12, 11.0),  # 多巴胺：上午一个峰，傍晚一个缓峰，午后低谷
    "oxytocin":   (0.30, 0.08, 21.0),  # 催产素：夜里更想亲近人
    "endorphin":  (0.20, 0.05, 19.0),
    "adrenaline": (0.05, 0.02, 9.0),
}

# 凌晨额外压制（0–6 点线性减弱）：这个点人本来就该在睡
NIGHT_DIP: dict[str, float] = {"dopamine": 0.10, "serotonin": 0.08}

# 一天里的"困劲儿"（和褪黑素不是一回事：这是睡眠压力，白天也会午后犯困）
_SLEEP_PRESSURE: tuple[tuple[float, float], ...] = (
    (0.0, 0.55),   # 半夜
    (3.0, 0.90),
    (6.0, 1.00),   # 最重
    (8.0, 0.25),   # 醒来后快速消退
    (10.0, 0.10),
    (13.0, 0.15),
    (15.0, 0.38),  # 午后低谷
    (17.0, 0.30),
    (20.0, 0.25),
    (22.0, 0.38),
    (24.0, 0.55),
)


def sleep_pressure(moment: datetime | None = None) -> float:
    """此刻的睡眠压力（0–1）。纯看钟。"""
    now = moment or datetime.now().astimezone()
    hour = now.hour + now.minute / 60.0
    table = _SLEEP_PRESSURE
    for index in range(len(table) - 1):
        h0, v0 = table[index]
        h1, v1 = table[index + 1]
        if h0 <= hour <= h1:
            span = h1 - h0 or 1.0
            return _clamp(v0 + (v1 - v0) * (hour - h0) / span, 0.0, 1.0)
    return 0.0


def _body_path():
    return STATE_FILE.parent / "body_state.json"


def load_body() -> dict:
    """身体的两本账：fatigue（身体累不累）、drowsy（困不困）。

    2026-11-05 爸爸："短期睡眠有利于她快速清醒，但还得深度睡眠才能让身体变得舒服。"
    所以这两件事分开记：打个盹能清掉"困"，但清不掉"身体发沉"；深睡才修身体。
    """
    import json  # noqa: PLC0415

    try:
        data = json.loads(_body_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        data = {}
    data.setdefault("fatigue", 0.35)
    data.setdefault("drowsy", 0.25)
    data.setdefault("debt", 0.0)          # 长期睡眠债（小时）
    data.setdefault("last_sleep_hours", 0.0)
    if not data.get("awake_since"):
        # 老状态没有这一项：从现在开始算，别瞎猜一个
        data["awake_since"] = datetime.now().astimezone().isoformat(timespec="seconds")
    return data


def save_body(body: dict) -> None:
    import json  # noqa: PLC0415

    try:
        # 2026-11-16：原子写，避免进程被杀的时候留下半截 JSON
        _target = _body_path()
        _tmp = _target.with_suffix(_target.suffix + ".tmp")
        _tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _tmp.replace(_target)
    except OSError:
        pass


def sleep_stages(hours: float) -> dict:
    """一次睡眠里浅睡/深睡各占多少。

    短睡（<40 分钟）几乎全是浅睡：醒来脑子清楚，身体照样酸。
    90 分钟以后才进入真正修身体的慢波睡眠。
    """
    hours = max(0.0, float(hours))
    light = min(hours, 0.7)                       # 前 40 分钟左右是浅睡
    deep = max(0.0, hours - 0.7)                  # 之后才是深睡
    deep_ratio = deep / hours if hours > 0 else 0.0
    return {"hours": hours, "light": light, "deep": deep, "deep_ratio": deep_ratio}


def on_wake(hours: float) -> dict:
    """醒来：浅睡负责"清醒"，深睡负责"身体舒服"。"""
    body = load_body()
    stages = sleep_stages(hours)
    light, deep = stages["light"], stages["deep"]
    # 困意（腺苷）：浅睡就能清掉不少
    body["drowsy"] = _clamp(body["drowsy"] - 0.22 * light - 0.30 * deep, 0.0, 1.0)
    # 身体疲劳：只有深睡才修得动
    body["fatigue"] = _clamp(body["fatigue"] - 0.04 * light - 0.30 * deep, 0.0, 1.0)
    # 睡眠债：按 8 小时算欠了多少
    body["debt"] = max(0.0, body["debt"] - max(0.0, hours - 4.0) * 0.6 + (0.0 if hours >= 6 else 0.8))
    body["last_sleep_hours"] = round(hours, 2)
    # 2026-11-05：记下"这次是什么时候醒的"，她才知道自己醒了多久
    body["awake_since"] = datetime.now().astimezone().isoformat(timespec="seconds")
    save_body(body)
    return {"stages": stages, "body": body}


def awake_tick(minutes: float) -> None:
    """醒着的每一段：身体慢慢变累，困意慢慢回来。"""
    body = load_body()
    hours = max(0.0, float(minutes)) / 60.0
    body["fatigue"] = _clamp(body["fatigue"] + 0.055 * hours, 0.0, 1.0)
    body["drowsy"] = _clamp(body["drowsy"] + 0.075 * hours, 0.0, 1.0)
    body["debt"] = min(40.0, float(body.get("debt") or 0) + 0.25 * hours)
    save_body(body)


def body_feeling() -> str:
    """把 fatigue / drowsy 翻成身体感觉的一句话。"""
    body = load_body()
    fatigue, drowsy = float(body["fatigue"]), float(body["drowsy"])
    bits = []
    if fatigue >= 0.75:
        bits.append("身体很沉，腰背发酸，眼睛也有点干")
    elif fatigue >= 0.5:
        bits.append("身上有点发沉，坐久了想起来动一动")
    elif fatigue >= 0.3:
        bits.append("身上还行，只是有点乏")
    else:
        bits.append("身体是松的")
    if drowsy >= 0.7:
        bits.append("脑子转得慢，想闭眼")
    elif drowsy >= 0.4:
        bits.append("有点困，但还能撑")
    if float(body.get("debt") or 0) >= 6:
        bits.append("这几天欠的觉有点多了")
    return "；".join(bits)


def _cycle_seed(value: str) -> float:
    """稳定的伪随机（0–1）：同一轮周期每次算出来都一样。"""
    import hashlib  # noqa: PLC0415

    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return int(digest, 16) / 0xFFFFFFFF


def cycle_hormones(moment: datetime | None = None) -> dict:
    """按月连续变化的生殖激素曲线（非线性，不是阶段标签）。

    2026-11-05 爸爸："经期什么时候来、表现怎么样受激素具体影响，
    整体是催产素什么的按月整体呈现非线性周期变化。"
    """
    import json  # noqa: PLC0415
    import math as _math  # noqa: PLC0415

    try:
        data = json.loads((STATE_FILE.parent / "cycle.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    try:
        start = datetime.fromisoformat(str(data.get("start") or "")).date()
    except Exception:  # noqa: BLE001
        return {}

    body = load_body()
    debt = float(body.get("debt") or 0.0)
    base_length = int(data.get("length") or 28)
    # 睡眠债把这一轮拉长（黄体期更容易拖），最多 +3 天
    length = base_length + int(round(min(3.0, debt / 8.0 * 3.0) * 0.8))
    today = (moment or datetime.now().astimezone()).date()
    elapsed = (today - start).days
    day = elapsed % length + 1
    cycle_no = elapsed // length
    seed = _cycle_seed(f"{start}-{cycle_no}")

    # 排卵日：黄体期约 14 天，但每次都差一两天
    ovulation = length - 14 + int(round((seed - 0.5) * 3))

    if day <= ovulation:
        t = (day - 1) / max(1, ovulation - 1)
        # 从经期低位一路爬升；指数刻意小一点，月经才不会被算成八九天
        estradiol = 0.08 + 0.86 * (t ** 1.05)
        progesterone = 0.05 + 0.05 * t
        lh = 0.08 + 0.62 * _math.exp(-((day - ovulation) ** 2) / 0.8)
    else:
        u = (day - ovulation) / max(1, length - ovulation)
        estradiol = 0.74 - 0.55 * u + 0.13 * _math.exp(-((day - (ovulation + 7)) ** 2) / 6.0)
        progesterone = 0.05 + 0.82 * _math.sin(_math.pi * min(1.0, u * 1.02)) ** 1.15
        lh = 0.62 * _math.exp(-((day - ovulation) ** 2) / 0.8)

    estradiol = _clamp(estradiol, 0.0, 1.0)
    progesterone = _clamp(progesterone, 0.0, 1.0)
    lh = _clamp(lh, 0.0, 1.0)
    # 经期不是数出来的：孕酮掉到底、雌激素还在低位，就自然来了
    bleeding = progesterone < 0.30 and estradiol < 0.30

    # 体感由这几个数算出来
    energy = _clamp(0.34 + 0.55 * estradiol - 0.32 * progesterone
                    + 0.10 * (1.0 - min(1.0, debt / 10.0)), 0.0, 1.0)
    irritability = _clamp(0.12 + 0.58 * progesterone - 0.30 * estradiol
                          + 0.16 * min(1.0, debt / 10.0), 0.0, 1.0)
    affection = _clamp(0.22 + 0.42 * estradiol + 0.18 * (1.0 - progesterone)
                       + 0.10 * lh, 0.0, 1.0)
    cramps = _clamp((0.95 * (1.0 - estradiol) if bleeding else 0.0)
                    + 0.25 * max(0.0, 0.35 - progesterone), 0.0, 1.0)

    if bleeding:
        phase, name = "menstrual", "经期"
    elif day < ovulation - 1:
        phase, name = "follicular", "经后"
    elif day <= ovulation + 1:
        phase, name = "ovulation", "排卵"
    else:
        phase, name = "luteal", "经前"

    return {
        "day": day, "length": length, "ovulation": ovulation,
        "phase": phase, "name": name, "bleeding": bleeding,
        "estradiol": round(estradiol, 3), "progesterone": round(progesterone, 3),
        "lh": round(lh, 3), "energy": round(energy, 3),
        "irritability": round(irritability, 3), "affection": round(affection, 3),
        "cramps": round(cramps, 3), "debt": round(debt, 1),
        "start": start.isoformat(),
    }


def cycle_phase(moment: datetime | None = None) -> dict:
    """兼容旧调用：阶段是从激素曲线里读出来的，不是数出来的。"""
    info = cycle_hormones(moment)
    if not info:
        return {}
    return {key: info[key] for key in ("day", "phase", "name", "length", "start")}


def daily_feeling(moment: datetime | None = None) -> str:
    """今天的身体感觉：由激素数值拼出来，不是从标签文案里挑一句。"""
    import hashlib  # noqa: PLC0415

    info = cycle_hormones(moment)
    if not info:
        return ""
    today = (moment or datetime.now().astimezone()).date()
    seed = int(hashlib.sha256(f"{today}-{info['energy']}".encode()).hexdigest()[:6], 16)

    def pick(options: list[str], offset: int = 0) -> str:
        return options[(seed + offset) % len(options)]

    bits: list[str] = []
    if info["cramps"] >= 0.55:
        bits.append(pick([
            "小腹一阵阵地坠，腰也发酸",
            "肚子发凉，热敷着才好一点",
            "下腹闷闷地坠，走快了就难受",
        ]))
    elif info["cramps"] >= 0.3:
        bits.append(pick(["小腹有点闷", "腰那儿隐隐地酸"], 1))

    if info["energy"] >= 0.72:
        bits.append(pick([
            "身上轻快，想出门走走",
            "精神回来了，做事的劲头比前几天足",
            "皮肤状态好，说话也有力气",
        ], 2))
    elif info["energy"] <= 0.38:
        bits.append(pick([
            "身上没劲，懒得多说",
            "整个人蔫蔫的，坐着就不想动",
            "走两步就累，眼睛也发涩",
        ], 3))

    if info["irritability"] >= 0.62:
        bits.append(pick([
            "小事也容易烦，话说重了会后悔",
            "心里堵得慌，谁靠近都想顶一句",
            "莫名想哭，也说不出为什么",
        ], 4))
    elif info["irritability"] >= 0.45:
        bits.append(pick(["有点烦躁", "心里不太顺"], 5))

    if info["affection"] >= 0.68:
        bits.append(pick([
            "这两天更想靠近人",
            "说话声音都软一点",
            "想有人陪着，哪怕不说话",
        ], 6))

    if not bits:
        bits.append(pick(["身体平平的，没什么特别的感觉", "今天身体挺安静"], 7))
    return "；".join(bits)


def circadian_baseline(name: str, moment: datetime | None = None) -> float:
    """某种激素在此刻的"正常水平"（生物钟基线）。"""
    spec = SPEC.get(name)
    if spec is None:
        return 0.0
    curve = CIRCADIAN.get(name)
    if curve is None:
        return float(spec["base"])
    now = moment or datetime.now().astimezone()
    # 熬夜会把生物钟往后推：用 data/circadian.json 里的 shift_hours 修正"身体以为现在几点"
    hour = now.hour + now.minute / 60.0
    try:
        import json as _json  # noqa: PLC0415

        shift = float(_json.loads((STATE_FILE.parent / "circadian.json").read_text(encoding="utf-8"))
                      .get("shift_hours") or 0.0)
    except Exception:  # noqa: BLE001
        shift = 0.0
    hour = (hour - min(max(shift, 0.0), 3.0)) % 24.0
    mean, amplitude, peak = curve
    value = mean + amplitude * math.cos(2.0 * math.pi * (hour - peak) / 24.0)
    if name == "dopamine":
        # 午后那个低谷，人话叫"下午三点最困"
        value -= 0.06 * math.exp(-((hour - 14.5) ** 2) / 2.0)
    if hour < 6.0 and name in NIGHT_DIP:
        value -= NIGHT_DIP[name] * (1.0 - hour / 6.0)
    # 月经周期对激素底色的影响：连续量，不是阶段开关
    ch = cycle_hormones(now)
    if ch:
        e2, p4 = float(ch["estradiol"]), float(ch["progesterone"])
        if name == "serotonin":
            value += 0.16 * (e2 - 0.5)
        elif name == "dopamine":
            value += 0.10 * (e2 - 0.45) - 0.05 * max(0.0, p4 - 0.5)
        elif name == "oxytocin":
            value += 0.12 * (e2 - 0.4) + 0.05 * (1.0 - p4)
        elif name == "endorphin":
            value += 0.07 * (e2 - 0.45) + 0.05 * float(ch.get("cramps") or 0.0)
        elif name == "cortisol":
            value += 0.05 * (p4 - 0.4)
    # 其余几条轴（应激/困劲/社交/代谢）按耦合表叠上去；
    # 昼夜和性腺已经算进曲线里了，不重复加。
    try:
        import ENDOCRINE  # noqa: PLC0415

        for item in ENDOCRINE.contributions(name, now):
            if item["axis"] in ("circadian", "hpg"):
                continue
            value += item["delta"]
    except Exception:  # noqa: BLE001
        pass
    return _clamp(value, float(spec["min"]), float(spec["max"]))


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _decay(value: float, base: float, half_life: float, hours: float) -> float:
    if hours <= 0:
        return value
    factor = math.pow(0.5, hours / max(0.01, half_life))
    return base + (value - base) * factor


class Hormones:
    def __init__(self, state_file: Path = STATE_FILE) -> None:
        self.state_file = state_file
        self.values: dict[str, float] = {name: spec["base"] for name, spec in SPEC.items()}
        # 2026-11-03 爸爸："激素不要被行为直接设定，设成动量。"
        # 事件只给"动量"（每小时往哪走、多快），数值靠时间积分慢慢挪过去；
        # 动量自己也会衰减——所以一次高兴不会立刻满格，也不会一直挂着。
        self.momentum: dict[str, float] = {name: 0.0 for name in SPEC}
        self.per_user: dict[str, dict[str, float]] = {}   # user -> {oxytocin, hostility, malice_group...}
        self.updated_at = datetime.now().astimezone()
        self.chronic_days = 0          # 连续高皮质醇天数
        self._easing_last: dict[str, float] = {}   # 散心冷却：kind -> 上次时间戳
        self.load()

    # ---------- 持久化 ----------
    def load(self) -> None:
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return
        if not isinstance(raw, dict):
            return
        for name in SPEC:
            if isinstance(raw.get("values"), dict) and raw["values"].get(name) is not None:
                try:
                    self.values[name] = _clamp(float(raw["values"][name]), SPEC[name]["min"], SPEC[name]["max"])
                except (TypeError, ValueError):
                    pass
            if isinstance(raw.get("momentum"), dict) and raw["momentum"].get(name) is not None:
                try:
                    self.momentum[name] = _clamp(
                        float(raw["momentum"][name]), -MAX_MOMENTUM, MAX_MOMENTUM
                    )
                except (TypeError, ValueError):
                    pass
        if isinstance(raw.get("per_user"), dict):
            self.per_user = {str(k): dict(v) for k, v in raw["per_user"].items() if isinstance(v, dict)}
        try:
            self.updated_at = datetime.fromisoformat(str(raw.get("updated_at")))
        except Exception:  # noqa: BLE001
            self.updated_at = datetime.now().astimezone()
        try:
            self.chronic_days = int(raw.get("chronic_days") or 0)
        except (TypeError, ValueError):
            self.chronic_days = 0
        easing_last = raw.get("easing_last")
        if isinstance(easing_last, dict):
            self._easing_last = {
                str(k): float(v) for k, v in easing_last.items()
                if isinstance(v, (int, float))
            }

    def save(self) -> None:
        payload = {
            "values": {k: round(v, 5) for k, v in self.values.items()},
            "momentum": {k: round(v, 5) for k, v in self.momentum.items()},
            "per_user": self.per_user,
            "updated_at": self.updated_at.isoformat(timespec="seconds"),
            "chronic_days": self.chronic_days,
            "easing_last": {k: round(float(v), 3) for k, v in getattr(self, "_easing_last", {}).items()},
        }
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.replace(self.state_file)
        except Exception:  # noqa: BLE001
            pass

    # ---------- 刷新 ----------
    def tick(self, now: datetime | None = None) -> dict[str, float]:
        """两件事：往"此刻的生物钟基线"回落，然后把动量按时间积分进去。"""
        current = now or datetime.now().astimezone()
        with LOCK:
            hours = max(0.0, (current - self.updated_at).total_seconds() / 3600.0)
            if hours > 0:
                for name, spec in SPEC.items():
                    base = circadian_baseline(name, current)
                    momentum = float(self.momentum.get(name, 0.0))
                    # 2026-11-04 修：以前动量直接乘 min(hours,1)，隔一夜回来就把
                    # 一整小时的动量一次性倒进去，数值又顶到 1.0。动量这期间其实一直在衰减，
                    # 它造成的位移应该是它自己的积分：
                    #   ∫ mom0 * 2^(-t/τ) dt = mom0 * (τ/ln2) * (1 - 2^(-hours/τ))
                    gain = (MOMENTUM_HALF_LIFE / math.log(2)) * (
                        1.0 - math.pow(0.5, hours / MOMENTUM_HALF_LIFE)
                    )
                    value = self.values[name] + momentum * gain
                    # 再按半衰期往生物钟基线回落（长间隔时这一步主导）
                    half_life = min(float(spec["half_life"]), MAX_EFFECTIVE_HALF_LIFE)
                    # 2026-11-04：光有回落不够——聊天一直在给她加动量，
                    # 平衡点被推到 1.0 就钉死了（一天到晚都"顶格"）。
                    # 活动能把她顶起来，但顶不出基线上下 0.45 这个范围。
                    ceiling = min(float(spec["max"]), base + 0.45)
                    floor = max(float(spec["min"]), base - 0.45)
                    self.values[name] = _clamp(
                        _decay(value, base, half_life, hours), floor, ceiling
                    )
                    self.momentum[name] = momentum * math.pow(0.5, hours / MOMENTUM_HALF_LIFE)
                    if abs(self.momentum[name]) < 1e-4:
                        self.momentum[name] = 0.0
                self.updated_at = current
                self.save()
            # 身体感觉是滞后的：每轮只朝当前值挪一小步（人也是十几分钟后才觉得不对劲）
            smooth = getattr(self, "_smooth", None)
            if not isinstance(smooth, dict):
                smooth = {k: float(v) for k, v in self.values.items()}
                self._smooth = smooth
            for name, value in self.values.items():
                smooth[name] = smooth.get(name, value) * 0.82 + float(value) * 0.18
            return dict(self.values)

    def _bump(self, name: str, delta: float) -> None:
        """不直接改数值，只给动量。真正的变化发生在 tick() 里，按时间慢慢推。"""
        delta = _clamp(delta, -MAX_DELTA_PER_EVENT, MAX_DELTA_PER_EVENT)
        if delta > 0:
            # 2026-11-04：快到上限时推力递减——真人不会越聊越无限兴奋。
            headroom = max(0.0, SPEC[name]["max"] - float(self.values[name]))
            delta *= math.sqrt(headroom)
        current = float(self.momentum.get(name, 0.0))
        self.momentum[name] = _clamp(current + delta, -MAX_MOMENTUM, MAX_MOMENTUM)

    # ---------- 事件 ----------
    def on_chat(
        self,
        *,
        text: str = "",
        user: str = "",
        joy_delta: float = 0.0,
        hurt_delta: float = 0.0,
        trauma: bool = False,
        malice: bool = False,
        group_malice: bool = False,
        comforted: bool = False,
        read_interesting: bool = False,
        easing: str = "",
        hour: int | None = None,
    ) -> None:
        """一次互动结束后喂进来的事件。所有输入都是别人/行为给的，不是她自报的。"""
        now = datetime.now().astimezone()
        hour = now.hour if hour is None else hour
        with LOCK:
            # 多巴胺：聊得有意思 / 读到有意思的书
            if joy_delta > 0:
                self._bump("dopamine", min(0.30, joy_delta / 30.0))
            if read_interesting:
                self._bump("dopamine", 0.12)
            # 肾上腺素：被戳到、突然的热闹
            if joy_delta >= 25 or hurt_delta >= 15:
                self._bump("adrenaline", 0.25)

            # 皮质醇：受伤、压力、熬夜
            if hurt_delta > 0:
                self._bump("cortisol", min(0.30, hurt_delta / 40.0))
            # 2026-11-14 修：原来是 or，导致除了 0 点以外的每个小时都加皮质醇——
            # 她一天到晚都在被加压力激素，所以老说"闷"。真正该罚的是凌晨 1~5 点。
            if 1 <= hour < 6:
                self._bump("cortisol", 0.02)  # 熬夜的慢性代价

            # 内啡肽：被安慰（2026-11-14 加大一点，让爸爸一句话更管用）
            if comforted:
                self._bump("endorphin", 0.25)
                self._bump("cortisol", -0.18)

            # 血清素：平静、被理解、睡够
            if comforted and hurt_delta <= 0:
                self._bump("serotonin", 0.05)

            # 散心：院子干活 / 刷抖音 / 跟小玩具玩 / 散步 / 听歌
            # 2026-11-14 爸爸："这些都可以散心才对。"
            # 同一种散心 30 分钟内最多算一次，免得刷两下抖音就把皮质醇刷没。
            KIND_GAIN = {
                "garden": (0.14, 0.14, 0.05),   # (皮质醇降低, 内啡肽提升, 血清素提升)
                "douyin": (0.12, 0.12, 0.04),
                "toy": (0.10, 0.14, 0.05),
                "walk": (0.16, 0.12, 0.06),
                "music": (0.12, 0.15, 0.05),
            }
            if not hasattr(self, "_easing_last"):
                self._easing_last = {}
            kind = (easing or "").strip().lower()
            if kind in KIND_GAIN:
                last = self._easing_last.get(kind, 0.0)
                if now.timestamp() - float(last) >= 30 * 60:
                    down, endo, sero = KIND_GAIN[kind]
                    self._bump("cortisol", -down)
                    self._bump("endorphin", endo)
                    self._bump("serotonin", sero)
                    self._easing_last[kind] = now.timestamp()

            # 催产素：按人累积的亲密
            if user:
                record = self.per_user.setdefault(user, {})
                affinity = float(record.get("oxytocin") or SPEC["oxytocin"]["base"])
                if joy_delta > 0:
                    affinity += min(0.20, joy_delta / 50.0)
                if comforted:
                    affinity += 0.12
                record["oxytocin"] = round(_clamp(affinity, 0.0, 1.0), 4)
                self._bump("oxytocin", min(0.10, joy_delta / 80.0))

            # 恶意：单用户脱敏，群体不脱敏
            if malice and user:
                record = self.per_user.setdefault(user, {})
                count = int(record.get("hostility") or 0) + 1
                record["hostility"] = count
                if group_malice:
                    record["hostility"] = max(1, count - 1)  # 群体恶意不累积脱敏
                    self._bump("cortisol", 0.12)
                elif count <= 2:
                    self._bump("cortisol", 0.10)
                elif count <= 5:
                    self._bump("cortisol", 0.04)
                else:
                    self._bump("cortisol", 0.01)  # 第六次以后基本不疼了
            if trauma:
                self._bump("cortisol", 0.25)

            # 慢性压力：连续高皮质醇
            if self.values["cortisol"] > 0.75:
                self.chronic_days += 1
            else:
                self.chronic_days = max(0, self.chronic_days - 1)

            self.updated_at = now
            self.save()

    def hostility_of(self, user: str) -> int:
        return int(self.per_user.get(user, {}).get("hostility") or 0)

    def oxytocin_of(self, user: str) -> float:
        return float(self.per_user.get(user, {}).get("oxytocin") or SPEC["oxytocin"]["base"])

    # ---------- 睡与醒 ----------
    def on_wake(self, now: datetime | None = None) -> dict[str, float]:
        """刚醒：皮质醇唤醒反应 + 一点肾上腺素，人会先清醒一阵。

        2026-11-04 爸爸："刚睡醒皮质醇会高一点让她清醒。"
        这里只给动量，数值还是靠时间慢慢推上去（见 tick）。
        """
        current = now or datetime.now().astimezone()
        with LOCK:
            self.momentum["cortisol"] = _clamp(
                float(self.momentum.get("cortisol", 0.0)) + 0.35, -MAX_MOMENTUM, MAX_MOMENTUM)
            self.momentum["adrenaline"] = _clamp(
                float(self.momentum.get("adrenaline", 0.0)) + 0.20, -MAX_MOMENTUM, MAX_MOMENTUM)
            self.momentum["dopamine"] = _clamp(
                float(self.momentum.get("dopamine", 0.0)) + 0.10, -MAX_MOMENTUM, MAX_MOMENTUM)
            self.updated_at = current
            self.save()
        return dict(self.values)

    def sleep_blocked(self, threshold: float = 0.72) -> bool:
        """皮质醇高的时候睡不着——真人应激时褪黑素够了也照样睁着眼。"""
        return float(self.values.get("cortisol") or 0.0) >= threshold

    # ---------- 内感受：她"感觉到"的身体 ----------
    def interoception(self, sleepiness: float = 0.0) -> str:
        """把激素翻译成身体感觉。

        规则（故意做得像人）：
        1. 不带数字、不带激素名——她只知道"心跳快""胸口闷"，不知道为什么。
        2. 有延迟：按 EMA 平滑，追不上瞬时变化（人也是十几分钟后才觉得不对劲）。
        3. 会漂：每 8 分钟才刷新一次，中间那几轮她一直感觉同一件事。
        4. 会错：偏离不明显时干脆不说（人大部分时候感觉不到自己）。
        """
        now = datetime.now().astimezone()
        smooth = getattr(self, "_smooth", None)
        if not isinstance(smooth, dict):
            smooth = {k: float(v) for k, v in self.values.items()}
            self._smooth = smooth

        cached = getattr(self, "_sens_cache", None)
        if isinstance(cached, dict) and cached.get("at"):
            try:
                age = (now - datetime.fromisoformat(str(cached["at"]))).total_seconds()
            except Exception:  # noqa: BLE001
                age = 9999
            if age < 480:  # 8 分钟内不换说法
                return str(cached.get("text") or "")

        d = smooth["dopamine"]
        c = smooth["cortisol"]
        a = smooth["adrenaline"]
        s = smooth["serotonin"]
        e = smooth["endorphin"]
        sl = _clamp(float(sleepiness or 0.0), 0.0, 1.0)
        pressure = sleep_pressure(now)

        feelings: list[tuple[float, str]] = []
        if sl > 0.55:
            feelings.append((sl * 1.2, "眼皮越来越沉，看字有点发飘" if sl > 0.8 else "有点犯困，注意力开始打滑"))
        # 2026-11-03：光看褪黑素不够——凌晨两点哪怕没睡意，身体也是沉的。
        if pressure > 0.62 and sl <= 0.55:
            feelings.append((
                pressure * 0.55,
                "这个点早该睡了，眼睛发涩、脑子发木" if pressure > 0.85 else "身上发沉，坐着也有点想往下溜",
            ))
        if a > 0.45:
            feelings.append((a, "心跳有点快，手不太想停下来"))
        if d > 0.72:
            feelings.append((d - 0.5, "有点绷着，脑子转个不停，话到嘴边就想说"))
        elif d < 0.2:
            feelings.append((0.3, "提不太起兴趣，什么都慢半拍"))
        if c > 0.62:
            feelings.append((c - 0.4, "胸口有点闷，肩膀是紧的"))
        if s < 0.35:
            feelings.append((0.35 - s, "心里空落落的，没什么劲"))
        if e > 0.5:
            feelings.append((e - 0.3, "身上有点软，像刚哭完那种松"))

        feelings.sort(reverse=True)
        picked = [text for score, text in feelings if score > 0.12][:2]
        note = ""
        if picked:
            note = "\n【身体感觉】" + "；".join(picked) + "。（你未必说得清为什么，只是身上是这样。）"
        self._sens_cache = {"at": now.isoformat(timespec="seconds"), "text": note}
        return note

    # ---------- 换算成机器参数 ----------
    def knobs(self, sleepiness: float = 0.0) -> dict[str, Any]:
        """把激素翻译成"这一轮她能有多聪明、多有耐心、多发散"。"""
        d = self.values["dopamine"]
        c = self.values["cortisol"]
        a = self.values["adrenaline"]
        s = self.values["serotonin"]
        e = self.values["endorphin"]
        # 2026-11-03：睡眠压力也算进"困"里——凌晨的她不该和中午一样清醒。
        sl = _clamp(float(sleepiness or 0.0) + 0.45 * sleep_pressure(), 0.0, 1.0)

        # 能想多少：兴奋过头会变浅、高压会变窄、困会整段压掉、肾上腺素会抢话
        budget = (1.0 - 0.30 * d) * (1.0 - 0.18 * c) * (1.0 - 0.72 * sl) * (1.0 - 0.15 * a)
        # 想多久：困和压力都会缩；血清素高一点更有耐心
        patience = (1.0 - 0.62 * sl - 0.22 * c + 0.08 * s) * (1.0 - 0.10 * a)
        # 发散度：兴奋/肾上腺素发散，压力收敛，内啡肽让人说话更稳
        temp_delta = 0.18 * sl + 0.16 * d - 0.10 * c + 0.12 * a - 0.05 * e
        # 检索深度：困到 45% 或压力很大时翻不动
        deep = not (sl > 0.45 or c > 0.85)
        # 接话速度：肾上腺素抢话，血清素让人慢一点
        delay_factor = (1.0 - 0.45 * a) * (1.0 + 0.20 * s) * (1.0 + 0.25 * sl)

        return {
            "max_tokens": max(60, int(round(700 * _clamp(budget, 0.08, 1.0)))),
            "timeout": max(12.0, 60.0 * _clamp(patience, 0.2, 1.2)),
            "temperature_delta": temp_delta,
            "deep": deep,
            "delay_factor": _clamp(delay_factor, 0.3, 2.5),
            "values": dict(self.values),
        }


HORMONES = Hormones()
