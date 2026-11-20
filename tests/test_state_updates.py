# -*- coding: utf-8 -*-
"""读-改-写 → update_json：并发下不丢更新。

2026-11-16：原来 `_write_talk_state` / `_melatonin_save` 都是「读整份 → 改几个键 → 写整份」，
两个线程/进程同时来就会互相覆盖（A 读了旧值，B 写完，A 再把自己的旧值写回去）。
`tools/state_io.update_json` 有线程锁 + 跨进程 .lock 文件，改成只 patch 自己那几个键之后
就不会再抹掉别人刚写的键。

跑法：
    python -m pytest tests/test_state_updates.py -v
"""
from __future__ import annotations

import json
import sys
import threading
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "chat_bridge"))
sys.path.insert(0, str(ROOT / "chat_bridge" / "tools"))

import state_io  # noqa: E402

CONCURRENCY_ROUNDS = 200


# ---------------------------------------------------------------- update_json


def test_two_threads_different_keys_both_survive(tmp_path):
    """两个线程各更新同一文件的不同键，各 200 次：两个键最后都在，且是各自最后一次的值。"""
    path = tmp_path / "state.json"
    errors: list[BaseException] = []

    def worker(key: str) -> None:
        try:
            for i in range(CONCURRENCY_ROUNDS):
                state_io.update_json(path, {key: i})
        except BaseException as exc:  # noqa: BLE001 - 线程里的异常要带回主线程
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(k,)) for k in ("alpha", "beta")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发写抛异常：{errors!r}"
    final = json.loads(path.read_text(encoding="utf-8"))
    assert final == {"alpha": CONCURRENCY_ROUNDS - 1, "beta": CONCURRENCY_ROUNDS - 1}


def test_nested_keys_do_not_clobber_each_other(tmp_path):
    """读改写只合并顶层键：别人写的 forced_sleep_* 这种旁路键不能被顺手抹掉。"""
    path = tmp_path / "melatonin.json"
    state_io.write_json(path, {"forced_sleep_day": "2026-11-16"})
    for _ in range(50):
        state_io.update_json(path, {"level": 42.0})
        state_io.update_json(path, {"forced_sleep_defer_until": "2026-11-17T03:45:00"})
    final = json.loads(path.read_text(encoding="utf-8"))
    assert final["forced_sleep_day"] == "2026-11-16"
    assert final["forced_sleep_defer_until"] == "2026-11-17T03:45:00"
    assert final["level"] == 42.0


# ------------------------------------------------------------ _write_talk_state


@pytest.fixture()
def talk_state(tmp_path, monkeypatch):
    """把 dl_api 的 talk_state 指到临时目录（模块常量是运行时读取的）。"""
    import dl_api  # noqa: PLC0415 - 需要在 sys.path 设好之后再导入

    path = tmp_path / "talk_state.json"
    monkeypatch.setattr(dl_api, "TALK_STATE", path)
    dl_api._reset_talk_cache()
    yield dl_api, path
    dl_api._reset_talk_cache()


def test_write_talk_state_concurrent_stays_valid_json(talk_state):
    """并发调用后文件仍是合法 JSON，两个线程的键都在，最后一次的字段也写进去了。"""
    dl_api, path = talk_state
    errors: list[BaseException] = []

    def worker(key: str) -> None:
        try:
            for i in range(CONCURRENCY_ROUNDS):
                dl_api._write_talk_state(**{key: i})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(k,)) for k in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发写抛异常：{errors!r}"
    final = json.loads(path.read_text(encoding="utf-8"))
    assert final["a"] == CONCURRENCY_ROUNDS - 1
    assert final["b"] == CONCURRENCY_ROUNDS - 1

    # 最后一次写入的字段必须在，而且立刻能读到（不再等 mtime 变化）
    dl_api._write_talk_state(quiet_reason="最后一次")
    assert json.loads(path.read_text(encoding="utf-8"))["quiet_reason"] == "最后一次"
    assert dl_api._read_talk_state()["quiet_reason"] == "最后一次"


def test_write_talk_state_patches_only_its_keys(talk_state):
    """写 quiet_* 不该动 sulk_day：以前整份回写会把它抹掉。"""
    dl_api, path = talk_state
    dl_api._write_talk_state(sulk_day="2026-11-16", sulk_spoke=True)
    dl_api._write_talk_state(quiet_until="2026-11-16T22:00:00", quiet_reason="想安静")
    final = json.loads(path.read_text(encoding="utf-8"))
    assert final["sulk_day"] == "2026-11-16"
    assert final["sulk_spoke"] is True
    assert final["quiet_reason"] == "想安静"


def test_read_talk_state_sees_external_write_despite_coarse_mtime(talk_state, monkeypatch):
    """缓存失效不再看 float 秒的 st_mtime：同一秒里别人改过文件，也要读到新内容。

    把 st_mtime 冻住（模拟 Windows 的粗粒度 mtime），只留 st_mtime_ns 是真实的。
    用旧实现（_TALK_CACHE["mtime"] 只比 st_mtime）这里会读回旧值。
    """
    dl_api, path = talk_state
    real_stat = Path.stat
    frozen_mtime = 1_700_000_000.0  # 同一个"秒"

    def coarse_stat(self, *args, **kwargs):
        st = real_stat(self, *args, **kwargs)
        if Path(self) == path:
            # 只改秒级字段；ns 仍然是真实的，模拟"精度更高但秒级相同"的情况
            return _StatView(st, frozen_mtime)
        return st

    monkeypatch.setattr(Path, "stat", coarse_stat)

    state_io.write_json(path, {"quiet_reason": "旧"})
    assert dl_api._read_talk_state()["quiet_reason"] == "旧"

    # 别的进程/线程直接写盘（不走 _write_talk_state，缓存不会自己更新）
    state_io.write_json(path, {"quiet_reason": "新值更长一点"})  # 顺便改 size

    assert dl_api._read_talk_state()["quiet_reason"] == "新值更长一点"


class _StatView:
    """把 os.stat_result 包一层，只覆盖 st_mtime，其余字段透传。"""

    def __init__(self, st, mtime: float) -> None:
        self._st = st
        self.st_mtime = mtime

    def __getattr__(self, name: str):
        return getattr(self._st, name)


# ------------------------------------------------------------------- 褪黑素


@pytest.fixture()
def melatonin(tmp_path, monkeypatch):
    import dl_api  # noqa: PLC0415

    path = tmp_path / "melatonin.json"
    from dl_server import physiology  # noqa: PLC0415

    monkeypatch.setattr(dl_api, "MELATONIN_STATE", path)  # dl_api 里的校正段还在用
    monkeypatch.setattr(physiology, "MELATONIN_STATE", path)  # 搬家后 tick/save 读这个
    yield dl_api, path
    monkeypatch.undo()


def test_melatonin_save_keeps_other_thread_defer_key(melatonin):
    """别的线程刚写了 forced_sleep_defer_until，再调 _melatonin_save，这个键不能丢。"""
    dl_api, path = melatonin
    from dl_server import physiology  # noqa: PLC0415
    state_io.write_json(path, {
        "level": 80.0,
        "asleep": False,
        "updated_at": "2026-11-16T02:50:00",
        "forced_sleep_day": "2026-11-15",
    })
    # 模拟并发的另一路写入
    state_io.update_json(path, {"forced_sleep_defer_until": "2026-11-16T03:45:00"})

    dl_api._melatonin_save(12.5, False, datetime(2026, 11, 16, 3, 10))

    final = json.loads(path.read_text(encoding="utf-8"))
    assert final["forced_sleep_defer_until"] == "2026-11-16T03:45:00", "顺延时间被整份回写抹掉了"
    assert final["forced_sleep_day"] == "2026-11-15", "「每晚只做一次」的标记不能丢"
    assert final["level"] == 12.5
    assert final["asleep"] is False
    assert final["updated_at"] == "2026-11-16T03:10:00"


def test_melatonin_save_clamps_and_tolerates_missing_file(melatonin):
    dl_api, path = melatonin
    from dl_server import physiology  # noqa: PLC0415
    dl_api._melatonin_save(999.0, True, datetime(2026, 11, 16, 1, 0))
    assert json.loads(path.read_text(encoding="utf-8"))["level"] == dl_api.MELATONIN_MAX
    dl_api._melatonin_save(-5.0, False, datetime(2026, 11, 16, 1, 5))
    assert json.loads(path.read_text(encoding="utf-8"))["level"] == 0.0


def test_melatonin_tick_defer_writes_only_the_defer_key(melatonin, monkeypatch):
    """03:00 皮质醇高 → 只写 forced_sleep_defer_until，不碰其他键、也不整份覆盖。"""
    dl_api, path = melatonin
    from dl_server import physiology  # noqa: PLC0415
    state_io.write_json(path, {
        "level": 100.0,
        "asleep": False,
        "updated_at": "2026-11-16T02:59:00",
        "forced_sleep_day": "2026-11-15",
        "stuck_fixed_at": "2026-11-16T01:00:00",
    })
    monkeypatch.setattr(physiology, "is_sleeping", lambda *a, **k: False)
    monkeypatch.setattr(dl_api.HORMONES, "sleep_blocked", lambda *a, **k: True)
    # 她确实睡下去的话会调这个：这里断言没被调用
    monkeypatch.setattr(physiology, "set_sleep_state",
                        lambda *a, **k: pytest.fail("皮质醇高时不该强制入睡"))

    level = dl_api.melatonin_tick(datetime(2026, 11, 16, 3, 0))

    final = json.loads(path.read_text(encoding="utf-8"))
    assert level == pytest.approx(100.0)
    assert final["level"] == pytest.approx(100.0, abs=0.01)
    assert final["forced_sleep_defer_until"] == "2026-11-16T03:45:00"
    assert final["forced_sleep_day"] == "2026-11-15", "顺延那天不能标成「已强制入睡」"
    assert final["stuck_fixed_at"] == "2026-11-16T01:00:00"
    assert "forced_sleep_day" in final and final["forced_sleep_day"] != "2026-11-16"


def test_melatonin_tick_forced_sleep_still_marks_the_day(melatonin, monkeypatch):
    """不触发顺延时语义不变：记 forced_sleep_day 并清掉 defer（每晚只做一次）。"""
    dl_api, path = melatonin
    from dl_server import physiology  # noqa: PLC0415
    state_io.write_json(path, {
        "level": 100.0,
        "asleep": False,
        "updated_at": "2026-11-16T02:59:00",
        "forced_sleep_defer_until": "2026-11-16T02:50:00",  # 已经过期 → 不再顺延
    })
    monkeypatch.setattr(physiology, "is_sleeping", lambda *a, **k: False)
    monkeypatch.setattr(dl_api.HORMONES, "sleep_blocked", lambda *a, **k: True)
    slept: list[dict] = []
    monkeypatch.setattr(physiology, "set_sleep_state", lambda **k: slept.append(k))

    dl_api.melatonin_tick(datetime(2026, 11, 16, 3, 0))

    assert slept == [{"hours": 8.0}]
    final = json.loads(path.read_text(encoding="utf-8"))
    assert final["forced_sleep_day"] == "2026-11-16"
    assert "forced_sleep_defer_until" not in final
