# -*- coding: utf-8 -*-
"""intent_router.py 的单元测试（只读被测文件，不联网、不读真实 data/）。

被测：chat_bridge/intent_router.py
  route(message, timeout) -> list[dict]   失败一律 []
  _parse(raw)             -> list[dict]
  snapshot()              -> dict         读 data/ 下的 json

所有网络调用（_llm）在测试里都被替换掉；snapshot 的 data 目录
通过 monkeypatch 模块的 __file__ 指向 tmp_path（源码里是
`Path(__file__).resolve().parent / "data"`，没有可改的路径常量）。

运行：C:/Users/Lenovo/anaconda3/python.exe -m pytest tests/test_intent_router.py -q
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "chat_bridge" / "intent_router.py"


@pytest.fixture(scope="module")
def router() -> Any:
    """按文件路径加载被测模块，不污染 sys.path，也不依赖包结构。"""
    assert MODULE_PATH.is_file(), f"被测文件不存在: {MODULE_PATH}"
    spec = importlib.util.spec_from_file_location("_intent_router_under_test", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def isolated(router: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """把模块的 __file__ 挪进 tmp_path，使 snapshot() 读到的是一个空的 data/。

    返回被测模块本身。这样任何测试都不会碰到真实的 chat_bridge/data。
    """
    pkg = tmp_path / "pkg"
    (pkg / "data").mkdir(parents=True)
    monkeypatch.setattr(router, "__file__", str(pkg / "intent_router.py"), raising=False)
    return router


def _spy_llm(monkeypatch: pytest.MonkeyPatch, router: Any, replies: list[Any], calls: list[Any]) -> None:
    """把 _llm 换成一个按顺序吐 replies 的假函数，并记录调用。"""
    seq = iter(replies)

    def fake_llm(prompt: str, timeout: float) -> str:
        calls.append((prompt, timeout))
        item = next(seq)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(router, "_llm", fake_llm)


# ---------------------------------------------------------------- 1. 空 / 超长
@pytest.mark.parametrize("message", ["", "   ", "\n\t  \n", "喝" * 301, "x" * 1000])
def test_empty_or_overlong_message_returns_empty_without_llm(
    isolated: Any, monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    calls: list[Any] = []
    _spy_llm(monkeypatch, isolated, [json.dumps({"actions": [{"type": "water"}]})], calls)

    assert isolated.route(message) == []
    assert calls == [], "空消息/超长消息不应调用模型"


def test_exactly_300_chars_still_reaches_model(isolated: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """边界：源码是 len(said) > 300 才丢弃，正好 300 字必须照常走模型。"""
    calls: list[Any] = []
    _spy_llm(monkeypatch, isolated, [json.dumps({"actions": [{"type": "water"}]})], calls)

    assert isolated.route("水" * 300) == [{"type": "water"}]
    assert len(calls) == 1


# ---------------------------------------------------------------- 2. 正常解析
def test_route_returns_actions_from_model(isolated: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    _spy_llm(monkeypatch, isolated, [json.dumps({"actions": [{"type": "water"}]})], calls)

    actions = isolated.route("我想给花浇水")

    assert actions == [{"type": "water"}]
    assert len(calls) == 1
    prompt, timeout = calls[0]
    assert "我想给花浇水" in prompt, "她的原话必须进 prompt"
    assert timeout == isolated.DEFAULT_TIMEOUT


def test_route_passes_extra_fields_through(isolated: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    payload = {"actions": [{"type": "tic_move", "pos": 5}]}
    _spy_llm(monkeypatch, isolated, [json.dumps(payload)], calls)

    assert isolated.route("我下中间") == [{"type": "tic_move", "pos": 5}]


def test_route_accepts_json_wrapped_in_prose(isolated: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = '好的，她想去院子：{"actions":[{"type":"go","room":"garden"}]}'
    calls: list[Any] = []
    _spy_llm(monkeypatch, isolated, [raw], calls)

    assert isolated.route("我去院子里") == [{"type": "go", "room": "garden"}]


# ---------------------------------------------------------------- 3. 垃圾输出
@pytest.mark.parametrize("raw", ["哈哈", "", "   ", "{bad json", "null", "没有动作", "```"])
def test_garbage_model_output_returns_empty(isolated: Any, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    calls: list[Any] = []
    _spy_llm(monkeypatch, isolated, [raw], calls)

    assert isolated.route("随便说点什么") == []  # 不抛异常
    assert len(calls) == 1


@pytest.mark.parametrize(
    "raw",
    [
        json.dumps({"actions": "water"}),   # actions 不是数组
        json.dumps({"actions": []}),        # 合法但空
        json.dumps({"nothing": 1}),         # 没有 actions 键
        json.dumps({"actions": None}),
    ],
)
def test_wellformed_json_without_action_list_returns_empty(
    isolated: Any, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    calls: list[Any] = []
    _spy_llm(monkeypatch, isolated, [raw], calls)

    assert isolated.route("嗯") == []


# ---------------------------------------------------------------- 4. 异常 / 超时
@pytest.mark.parametrize(
    "exc",
    [TimeoutError("timed out"), OSError("connection reset"), ValueError("weird"), RuntimeError("boom")],
)
def test_llm_exception_returns_empty(isolated: Any, monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    calls: list[Any] = []
    _spy_llm(monkeypatch, isolated, [exc], calls)

    assert isolated.route("我去画画") == []  # 不许把异常抛给调用方


# ---------------------------------------------------------------- 5. actions 结构过滤
def test_actions_filtering_keeps_only_dicts_with_non_empty_type(
    isolated: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = json.dumps({"actions": [{}, {"type": "plant"}, "junk", {"type": ""}]})
    calls: list[Any] = []
    _spy_llm(monkeypatch, isolated, [raw], calls)

    # 源码真实行为：非 dict 跳过、type 为空跳过、剩下的只保留 type 字符串
    assert isolated.route("种点小葱") == [{"type": "plant"}]


def test_parse_normalises_type_case_and_whitespace(isolated: Any) -> None:
    raw = json.dumps({"actions": [{"type": " WATER "}, {"type": "Plant", "name": "小葱"}]})

    assert isolated._parse(raw) == [{"type": "water"}, {"type": "plant", "name": "小葱"}]


def test_parse_keeps_at_most_four_actions(isolated: Any) -> None:
    """源码 actions[:4] 静默截断——记录真实行为，不是期望的语义。"""
    raw = json.dumps({"actions": [{"type": t} for t in ("water", "plant", "harvest", "rest", "go", "draw")]})

    assert [a["type"] for a in isolated._parse(raw)] == ["water", "plant", "harvest", "rest"]


@pytest.mark.parametrize("raw", ["", "   ", "哈哈", "{bad json", "[1,2,3]"])
def test_parse_never_raises(isolated: Any, raw: str) -> None:
    assert isolated._parse(raw) == []


# ---------------------------------------------------------------- 6. snapshot 容错
def test_snapshot_on_empty_data_dir_returns_dict(isolated: Any) -> None:
    state = isolated.snapshot()

    assert isinstance(state, dict)
    assert state == {}


def test_snapshot_ignores_corrupt_files(isolated: Any, tmp_path: Path) -> None:
    data = Path(isolated.__file__).resolve().parent / "data"
    (data / "tictactoe.json").write_text("{not json", encoding="utf-8")
    (data / "garden.json").write_text("[]", encoding="utf-8")  # json 合法但不是 dict

    state = isolated.snapshot()

    assert isinstance(state, dict)
    assert "棋局" not in state          # 坏文件被吞掉，不影响其它部分
    assert "院子" not in state
    assert "当前房间" not in state


def test_snapshot_reads_good_data_from_tmp_dir(isolated: Any) -> None:
    data = Path(isolated.__file__).resolve().parent / "data"
    (data / "tictactoe.json").write_text(
        json.dumps({"board": ["her", "", "other", "", "", "", "", "", ""], "opponent": "toy", "turn": "her"}),
        encoding="utf-8",
    )
    (data / "garden.json").write_text(
        json.dumps({"plots": [{"name": "小葱"}, {"name": "番茄"}]}), encoding="utf-8"
    )
    (data / "space_state.json").write_text(json.dumps({"room": "garden"}), encoding="utf-8")

    state = isolated.snapshot()

    assert state["棋局"]["对手"] == "toy"
    assert state["棋局"]["她占"] == ["左上"]
    assert state["棋局"]["对方占"] == ["右上"]
    assert state["院子"] == ["小葱", "番茄"]
    assert state["当前房间"] == "garden"


def test_route_uses_snapshot_state_in_prompt(isolated: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    data = Path(isolated.__file__).resolve().parent / "data"
    (data / "space_state.json").write_text(json.dumps({"room": "easel"}), encoding="utf-8")
    calls: list[Any] = []
    _spy_llm(monkeypatch, isolated, [json.dumps({"actions": [{"type": "draw"}]})], calls)

    assert isolated.route("我想画画") == [{"type": "draw"}]
    assert "easel" in calls[0][0]
