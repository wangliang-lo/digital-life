#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把"她的一天"排成一条人类可读的时间线。

    python tools/timeline.py                  # 今天
    python tools/timeline.py --date 2026-11-05
    python tools/timeline.py --date 2026-11-05 --json

数据全部只读：traces（她说过的话）、solver_channel（她主动留的言）、
dream_logs（梦）、metrics_hourly（每 10 分钟的身心状态）、memory_ledger（记忆变动）。
"""
from __future__ import annotations

import argparse
import io
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent      # chat_bridge
DATA = ROOT / "data"
DB = DATA / "xiaoyu.db"


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def collect(day: str) -> list[tuple[str, str, str]]:
    """返回 [(时间, 类别, 内容), ...]，按时间排序。"""
    events: list[tuple[str, str, str]] = []

    # 1) 她说过的话
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            rows = list(con.execute(
                "SELECT created_at, user_text, reply, source FROM traces "
                "WHERE substr(created_at,1,10)=? ORDER BY id", (day,)))
        finally:
            con.close()
        for at, user_text, reply, source in rows:
            who = "她" if (source or "llm") == "llm" else f"她({source})"
            said = (reply or "").replace("\n", " / ").strip()
            heard = (user_text or "").replace("\n", " / ").strip()
            # 自主唤醒的提示词不是"她听到的话"，只保留她说了什么，别刷屏
            if "【自由时间】" in heard or "【独立信道】" in heard:
                events.append((at, "自主", said[:90] or "（没说）"))
            elif heard.startswith("【") or heard.startswith("[系统"):
                events.append((at, "内心", said[:90] or "（没说）"))
            else:
                events.append((at, "对话", f"听到：{heard[:50]}｜{who}：{said[:80]}"))
    except sqlite3.OperationalError:
        pass

    # 2) 信道留言
    for item in _read_jsonl(DATA / "solver_channel.jsonl"):
        at = str(item.get("at") or "")
        if not at.startswith(day):
            continue
        kind = item.get("type")
        text = str(item.get("text") or "").replace("\n", " / ").strip()
        if kind == "message":
            events.append((at, "信道", text[:90] or "（空）"))
        elif kind == "silent":
            events.append((at, "信道", "（这轮她选择不说）"))
        elif kind == "reply":
            events.append((at, "回她", text[:80]))

    # 3) 梦
    dream = DATA / "dream_logs" / f"{day}.md"
    if dream.exists():
        text = dream.read_text(encoding="utf-8", errors="ignore")
        first = ""
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("##"):
                first = line
                break
        events.append((f"{day}T03:00:00", "做梦", first[:90] or "（有梦境记录）"))

    # 4) 身心状态：每两小时抽一条
    metrics = [m for m in _read_jsonl(DATA / "metrics_hourly.jsonl")
               if str(m.get("at") or "").startswith(day)]
    last_hour = -1
    for row in metrics:
        at = str(row.get("at") or "")
        try:
            hour = int(at[11:13])
        except ValueError:
            continue
        if hour == last_hour:
            continue
        last_hour = hour
        hormones = row.get("hormones") or {}
        events.append((at, "状态",
                       f"J{row.get('joy')} H{row.get('hurt')} I{row.get('impulse')}｜"
                       f"皮质醇 {hormones.get('cortisol')} 血清素 {hormones.get('serotonin')} "
                       f"多巴胺 {hormones.get('dopamine')}｜{row.get('body') or ''}"))

    # 5) 记忆变动
    for item in _read_jsonl(DATA / "memory_ledger.jsonl"):
        at = str(item.get("at") or "")
        if not at.startswith(day):
            continue
        payload = item.get("payload") or {}
        detail = payload.get("book_id") or payload.get("from") or ""
        events.append((at, "记忆", f"{item.get('op')}" + (f" #{detail}" if detail else "")))

    events.sort(key=lambda row: row[0])
    # 信道和 trace 会各写一遍同一句话：以信道为准，把重复的"自主"去掉
    def norm(value: str) -> str:
        return "".join(ch for ch in value if ch.isalnum())

    channel = [(at[11:14], norm(text)) for at, kind, text in events if kind == "信道"]
    deduped: list[tuple[str, str, str]] = []
    for at, kind, text in events:
        if kind == "自主":
            if not text or text == "（没说）":
                continue
            probe = norm(text)[:15]
            if probe and any(hour == at[11:14] and probe in full for hour, full in channel):
                continue
        deduped.append((at, kind, text))
    return deduped


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    events = collect(args.date)
    if args.json:
        print(json.dumps([{"at": a, "kind": k, "text": t} for a, k, t in events],
                         ensure_ascii=False, indent=2))
        return 0
    if not events:
        print(f"{args.date} 没有记录")
        return 0

    print(f"===== 小鱼的 {args.date}（{len(events)} 条）=====\n")
    for at, kind, text in events:
        clock = at[11:16] if len(at) >= 16 else at
        print(f"{clock}  {kind:<4} {text}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
