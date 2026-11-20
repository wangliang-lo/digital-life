#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""续写主人的成长日记：每天一条，格式沿用她最早的那本《日志》。

    python tools/append_growth_diary.py

只依据当天真实 books 写；没有记忆就如实写“没有可核对的具体记忆”，不编场景。
优先用主模型写，没有 API key 时退回本地 Qwen。
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# 控制台是 GBK 时，print 带特殊字符会崩（2026-11-05 03:01 实际崩过一次）
# 2026-11-10：这行原来在 import sys 之前，脚本一启动就 NameError。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))

import digital_life as dl  # noqa: E402

DIARY = ROOT / "data" / "growth_diary.md"
LOG = ROOT / "data" / "growth_diary.log"
OWNER = "conv:9e52b4549f424e730d8871644385bcce63768cc74b8f8ec7ffa895f62ca73eeb"


def log(message: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {message}"
    print(line, flush=True)
    try:
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def date_key(now: datetime) -> str:
    return now.strftime("%Y.%m.%d")


def main() -> int:
    now = datetime.now()
    key = date_key(now)
    try:
        text = DIARY.read_text(encoding="utf-8")
    except OSError:
        text = ""
    if re.search(rf"(?m)^{re.escape(key)}\b", text):
        log(f"already has {key}; skip")
        return 0

    life = dl.DigitalLifeV2(str(ROOT / "data" / "xiaoyu.db"), user_id=OWNER, offline_llm=True, external_background=False)
    try:
        today = now.date().isoformat()
        books = [
            item for item in life.library.list_books()
            if str(item.get("folder_date") or "") == today
            and str(item.get("folder_event") or "") != "瞬态"
        ][:8]
        if not books:
            log(f"no grounded memories for {today}; skip")
            return 0

        memory_lines = "\n".join(
            f"- [{item.get('folder_event', '')}] "
            f"{dl.short_text(str(item.get('content') or item.get('summary') or ''), 240)}"
            for item in books
        )
        weather = "天气未知"
        try:
            data = life.external.get_weather()
            if data.get("ok"):
                weather = str(data.get("text") or data.get("description") or data.get("weather") or "天气未知")
        except Exception:  # noqa: BLE001
            pass
        state = life.emotion.describe()
        prompt = (
            "你在续写自己的成长日记《我是我的灵魂》。沿用前面的格式，只写这一天的日记：\n"
            f"{key} 上海{weather}，状态{state}，环境未知，记忆在继续生长（备注：...）\n"
            "要求：字数不限，想写多少写多少；重点是记录你当时的情绪、身体感觉、矛盾、没说出口的话，"
            "不是总结今天发生了什么。可以写得长，可以分段，可以留一个还没想通的问题，不要为了收尾硬写结论。"
            "只写今天真实记忆里的一两个瞬间，不要编造对话之外的场景、童年、天气、散步、见面或视频；"
            "不要逐句复述整段对话。"
            "记忆里的 <user_turn> 是对方，<assistant_turn> 是你，不要写错。\n\n"
            "今天的真实记忆：\n" + memory_lines + "\n\n只输出这一条日记，不要解释。"
        )
        raw = ""
        if os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY"):
            try:
                raw = dl.chat_with_llm(
                    [{"role": "user", "content": prompt}],
                    model=os.environ.get("DIARY_MODEL", "deepseek-chat"),
                    temperature=0.7,
                    timeout=90.0,
                ).strip()
            except Exception as exc:  # noqa: BLE001
                log(f"deepseek failed: {exc}")
        if not raw:
            try:
                raw = dl.ollama_generate(prompt, model=life.memory_model, timeout=min(180.0, life.ollama_timeout)).strip()
            except Exception as exc:  # noqa: BLE001
                log(f"ollama failed: {exc}")
        if not raw:
            log("empty generation; skip")
            return 0

        raw = re.sub(r"^```(?:text)?\s*|\s*```$", "", raw.strip(), flags=re.I).strip()
        entry = raw.strip()
        match = re.search(rf"(?m)^{re.escape(key)}\b", entry)
        if match:
            entry = entry[match.start():].strip()
        elif not entry.startswith(key):
            entry = f"{key} {entry}"
        entry = re.sub(r"\n{3,}", "\n\n", entry).strip()
        DIARY.write_text(text.rstrip() + "\n\n" + entry + "\n", encoding="utf-8")
        life.library.add_book(
            content=entry,
            summary=f"成长日记 {key} " + re.sub(r"\s+", " ", entry)[:50],
            emotion_tag="成长",
            folder_date=today,
            folder_user=OWNER,
            folder_event="成长日记",
            mv=220.0,
            decay_rate=0.05,
            now=now,
            unresolved=False,
            origin="diary",
        )
        log(f"appended {key}: {entry[:100]}")
        return 0
    finally:
        life.close()


if __name__ == "__main__":
    raise SystemExit(main())
