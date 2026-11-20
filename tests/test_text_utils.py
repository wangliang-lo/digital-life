"""dl_server.text_utils 拆分后的回归测试：函数行为 + dl_api 仍然接得上。"""
import sys
from pathlib import Path

BRIDGE = str(Path(__file__).resolve().parent.parent / "chat_bridge")
if BRIDGE not in sys.path:
    sys.path.insert(0, BRIDGE)

import dl_api  # noqa: E402
from dl_server import text_utils  # noqa: E402

NAMES = [
    "strip_stage_directions", "clean_partial_tags", "_is_bare_ack", "_ascii_word_char",
    "shape_reply", "split_bubbles", "_part_of_day", "clean_incoming_text",
    "human_reply_delay_ms", "_make_delay_fn", "_WEEKDAYS",
]


def test_split_bubbles():
    assert text_utils.split_bubbles("第一句|||第二句", 20) == ["第一句", "第二句"]


def test_strip_stage_directions():
    assert text_utils.strip_stage_directions("（想了想）你好") == "你好"


def test_clean_partial_tags_drops_unclosed():
    assert "[STICKER" not in text_utils.clean_partial_tags("嗯\n[STICKER: 难过")


def test_clean_partial_tags_keeps_complete():
    assert "[GOMOKU: 5,5]" in text_utils.clean_partial_tags("我下 [GOMOKU: 5,5]")


def test_dl_api_reexports_are_same_objects():
    for name in NAMES:
        assert getattr(dl_api, name) is getattr(text_utils, name), name


def test_dl_api_behaviour_matches():
    assert dl_api.split_bubbles("第一句|||第二句", 20) == ["第一句", "第二句"]
    assert dl_api.strip_stage_directions("（想了想）你好") == "你好"
    assert "[STICKER" not in dl_api.clean_partial_tags("嗯\n[STICKER: 难过")
    assert "[GOMOKU: 5,5]" in dl_api.clean_partial_tags("我下 [GOMOKU: 5,5]")
    assert dl_api.shape_reply(["你好"], [], [], "web", 0.5) == text_utils.shape_reply(["你好"], [], [], "web", 0.5)


def test_constants_travelled_with_functions():
    # 上次翻车：_WEEKDAYS 被连带删掉，测试没覆盖，真跑 500
    assert text_utils._WEEKDAYS == "一二三四五六日"
    assert text_utils._is_bare_ack("嗯嗯")
    assert not text_utils._is_bare_ack("我们去吃饭吧今天")
    assert text_utils.clean_incoming_text("") == ""
    assert text_utils.clean_incoming_text("{}") == "[对方发来一个表情或图片]"
    assert text_utils._part_of_day(8)
