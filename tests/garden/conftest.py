# -*- coding: utf-8 -*-
"""共用夹具。

三件事：
1. 用源码 exec 出被测模块（不 import 生产包，也不写 __pycache__ 到 C:\\aihuman）。
2. 把 garden.STATE / garden.LOG 重定向到 pytest 的临时目录。
3. 把“今天”换成可推进的 FakeClock —— 被测模块里所有日期都走 _today()，
   所以替换这一个函数就够了，不用改生产文件。
"""
from __future__ import annotations

import hashlib
import json
import types
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GARDEN_SRC = ROOT / "chat_bridge" / "tools" / "garden.py"
PROD_STATE = Path(r"PROJECT_ROOT\chat_bridge\data\garden.json")
PROD_LOG = Path(r"PROJECT_ROOT\chat_bridge\data\garden_log.jsonl")
PROD_GUARD = (PROD_STATE, PROD_LOG)

# 测试里用的“今天”，跟真实日期无关，保证任何一天跑都一个结果
BASE_DAY = date(2026, 11, 1)


def load_garden_module() -> types.ModuleType:
    """把 garden.py 的源码编译进一个全新模块，避免污染 sys.modules 与磁盘。"""
    source = GARDEN_SRC.read_text(encoding="utf-8")
    module = types.ModuleType("garden_under_test")
    module.__file__ = str(GARDEN_SRC)
    exec(compile(source, str(GARDEN_SRC), "exec"), module.__dict__)  # noqa: S102
    return module


class FakeClock:
    """可伪装的“今天”。只有调用 advance()/set() 才会变。"""

    def __init__(self, day: date = BASE_DAY) -> None:
        self.day = day

    def today(self) -> date:
        return self.day

    def set(self, day: date) -> None:
        self.day = day

    def advance(self, days: int) -> None:
        self.day = self.day + timedelta(days=days)


def _fingerprint(path: Path):
    try:
        stat = path.stat()
    except FileNotFoundError:
        return ("missing",)
    return (stat.st_size, stat.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())


@pytest.fixture(autouse=True)
def _production_files_untouched():
    """每个用例前后对比生产状态文件的指纹，任何写入都会让用例变红。"""
    before = {path: _fingerprint(path) for path in PROD_GUARD}
    yield
    after = {path: _fingerprint(path) for path in PROD_GUARD}
    changed = [str(path) for path in PROD_GUARD if before[path] != after[path]]
    assert not changed, f"生产状态文件被改动了（测试绝不能写这里）：{changed}"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def garden(tmp_path, monkeypatch, clock):
    """被测模块，状态文件和日志都落在 tmp_path 里，时间由 clock 控制。"""
    module = load_garden_module()
    state_path = tmp_path / "garden.json"
    log_path = tmp_path / "garden_log.jsonl"
    monkeypatch.setattr(module, "STATE", state_path)
    monkeypatch.setattr(module, "LOG", log_path)
    monkeypatch.setattr(module, "_today", clock.today)
    # 天气随机只影响附加描述，关掉它让断言稳定
    monkeypatch.setattr(
        module,
        "random",
        types.SimpleNamespace(random=lambda: 0.99, choice=lambda seq: seq[0]),
    )
    # 兜底：确认重定向真的生效了，否则宁可炸在这个 fixture 里
    assert module.STATE == state_path, "STATE 重定向失败，拒绝跑测试"
    assert str(module.STATE).startswith(str(tmp_path)), "状态文件跑到临时目录外面了"
    return module


@pytest.fixture
def plot():
    """构造一条 plots 记录。watered 缺省等于 planted。"""

    def _plot(name, planted, watered=None, stage=0, thirsty=False):
        return {
            "name": name,
            "planted": planted.isoformat(),
            "watered": (watered or planted).isoformat(),
            "stage": stage,
            "thirsty": thirsty,
        }

    return _plot


@pytest.fixture
def seed(garden):
    """直接把状态文件写成我们想要的样子，用来测只读/只推进入口。"""

    def _seed(plots=(), harvested=(), died=()):
        payload = {
            "plots": list(plots),
            "last_seen": "",
            "harvested": list(harvested),
            "died": list(died),
        }
        garden.STATE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    return _seed


@pytest.fixture
def state(garden):
    """读回磁盘上的状态文件（而不是内存里那个 dict）。"""

    def _state() -> dict:
        return json.loads(garden.STATE.read_text(encoding="utf-8"))

    return _state
