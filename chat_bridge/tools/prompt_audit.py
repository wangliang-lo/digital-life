#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""提示词体检：把 persona 拆成"块"，量每一块占多少字/token，找出该删的。

    python tools/prompt_audit.py                 # 主人私聊场景
    python tools/prompt_audit.py --scene group   # 群里
    python tools/prompt_audit.py --dump          # 连原文一起打出来

2026-11-05：爸爸说"token 低也是研究方向，删掉没用的提示词，但要准"。
那就先量，再删——不量就删是瞎删。
"""
from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import dl_api  # noqa: E402


SCENES = {
    "owner": dict(channel="web", speaker="爸爸", is_owner=True),
    "group": dict(channel="chat_group", speaker="阿哦", is_owner=False, group="一家人"),
    "stranger": dict(channel="web_guest", speaker="路人甲", is_owner=False),
}


def estimate_tokens(text: str) -> int:
    """粗估：中文约 1 字 = 1 token，英文 4 字符 = 1 token。"""
    han = len(re.findall(r"[\u4e00-\u9fa5]", text))
    other = len(text) - han
    return han + other // 4


def split_blocks(prompt: str) -> list[tuple[str, str]]:
    """按 【标题】 切块；开头没有标题的部分算"基础人格"。"""
    parts = re.split(r"(?m)^(?=【)", prompt)
    blocks: list[tuple[str, str]] = []
    for part in parts:
        part = part.strip("\n")
        if not part.strip():
            continue
        match = re.match(r"【([^】]{1,20})】", part)
        name = match.group(1) if match else "基础人格（无标题）"
        blocks.append((name, part))
    return blocks


def build(scene: str) -> str:
    kwargs = SCENES[scene]
    return dl_api.persona_with_time(dl_api.DEFAULT_PERSONA, **kwargs)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scene", default="owner", choices=list(SCENES))
    parser.add_argument("--dump", action="store_true")
    parser.add_argument("--top", type=int, default=0, help="只看最大的几个块")
    args = parser.parse_args()

    prompt = build(args.scene)
    blocks = split_blocks(prompt)
    total_chars = len(prompt)
    total_tokens = estimate_tokens(prompt)

    print(f"场景：{args.scene}　总长 {total_chars} 字 ≈ {total_tokens} token"
          f"（{len(blocks)} 块）\n")
    rows = []
    for name, text in blocks:
        chars = len(text)
        rows.append((chars, name, text))
    rows.sort(reverse=True)
    if args.top:
        rows = rows[:args.top]
    print(f"{'字':>6} {'占比':>6} {'≈token':>7}  块")
    for chars, name, text in rows:
        share = chars / total_chars * 100 if total_chars else 0
        print(f"{chars:6d} {share:5.1f}% {estimate_tokens(text):7d}  {name}")
    if args.dump:
        print("\n" + "=" * 70)
        for chars, name, text in rows:
            print(f"\n----- {name}（{chars} 字）-----")
            print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
