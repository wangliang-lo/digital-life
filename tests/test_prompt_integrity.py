# -*- coding: utf-8 -*-
"""人格提示词防截断测试（2026-11-14）。

真实事故：digital_life._build_messages 曾经用 persona_context[:800] 截断，
排在后面的院子/棋局/对面是谁等段落全部被静默丢掉，她完全不知道那些信息存在。
后来改成 4000，但没有测试盯着 —— 这里把两件事钉住：

1. persona_with_time 拼出来的提示词长度，不超过 _build_messages 真正会用的上限；
2. 关键段落（生日/你在哪/主人是谁）必须真的出现在最终提示词里。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "chat_bridge", ROOT / "chat_bridge" / "tools"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import dl_api  # noqa: E402
import digital_life  # noqa: E402
OWNER_TURN = True


def persona_for(message: str, channel: str = "web") -> str:
    """按真实调用方式设好上下文，再拼人格提示词。"""
    dl_api._TURN_MESSAGE = message
    dl_api._TURN_CHANNEL = channel
    dl_api._CURRENT_SPEAKER = "爸爸"
    dl_api._IS_OWNER_TURN = OWNER_TURN
    return dl_api.persona_with_time("基础人格", digest="")


def persona_limit_in_digital_life() -> int:
    """从 digital_life 源码里读出 persona_context 的切片上限，不硬编码。"""
    source = Path(digital_life.__file__).read_text(encoding="utf-8")
    values = [int(n) for n in re.findall(r"persona_context\[:\s*(\d+)\]", source)]
    assert values, "没在 digital_life._build_messages 里找到 persona_context[:N] 截断"
    return max(values)


def test_prompt_fits_inside_build_messages_limit() -> None:
    text = persona_for("在吗")
    limit = persona_limit_in_digital_life()
    assert len(text) <= limit, f"人格提示词 {len(text)} 字，超过 _build_messages 上限 {limit}"


def test_essential_blocks_survive() -> None:
    always = persona_for("在吗")
    for block in ("【生日】", "【你认得的人】"):
        assert block in always, f"关键段落 {block} 丢了"
    # 位置块是按话题开闸的：聊到「你在哪」才该出现（实际标题是【你此刻】/【这个地方】）
    located = persona_for("你现在在哪儿")
    assert ("【你此刻】" in located) or ("【这个地方】" in located), "问到位置时，位置块没进提示词"


def test_garden_block_appears_when_topic_is_garden() -> None:
    text = persona_for("院子里的小葱浇水了吗")
    assert "【院子】" in text, "聊到院子时，院子块没进提示词"


def test_birthday_block_states_the_real_date() -> None:
    text = persona_for("你生日是哪天")
    assert "2026 年 10 月 28 日" in text, "生日块里没有她的出生日期"
