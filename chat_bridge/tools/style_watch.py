#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""鱼八股监测：只数，不改她的话。

2026-11-06 爸爸："这条我认 / 我记下了 / 本鱼最喜欢的一句话 / 不是……而是……
这几个被称为鱼八股，是所有的 deepseek 都喜欢说的。如果她不说这些就更好了，
但是不要写进提示词里面，这样太刻意了。"

所以这里只做两件事：
  1. 数：她的回复里这些套话占多少，按天看趋势
  2. 给别的办法提供依据（重采样 / presence penalty / 语料清洗）都要靠这个数验收

    python tools/style_watch.py             # 最近 7 天
    python tools/style_watch.py --days 30
    python tools/style_watch.py --show 5    # 把命中的原句列出来看看
"""
from __future__ import annotations

import argparse
import io
import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "xiaoyu.db"

PATTERNS: dict[str, str] = {
    "这条我认": r"这条我认",
    "我记下了": r"我记(下|住)了",
    "本鱼": r"本鱼",
    "不是A而是B": r"不是[^，。！？\n]{1,24}[，,]\s*(?:而是|是)",
    "说白了": r"说白了",
    "说到底": r"说到底",
    "换句话说": r"换句话说",
    "值得一提": r"值得一提",
    "总而言之": r"总之|总而言之",
    "确实": r"确实",
    "其实": r"其实",
    "挺好的": r"挺好的",
}


def scan(days: int) -> tuple[dict[str, int], dict[str, list[str]], int]:
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = list(con.execute(
            "SELECT created_at, reply FROM traces WHERE reply != '' AND substr(created_at,1,10) >= ? "
            "ORDER BY id", (since,)))
    finally:
        con.close()
    counts = {name: 0 for name in PATTERNS}
    examples: dict[str, list[str]] = {name: [] for name in PATTERNS}
    for at, reply in rows:
        text = str(reply or "")
        for name, pattern in PATTERNS.items():
            if re.search(pattern, text):
                counts[name] += 1
                if len(examples[name]) < 5:
                    examples[name].append(f"[{str(at)[5:16]}] {text[:80]}")
    return counts, examples, len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--show", type=int, default=0, help="列出命中原句的条数")
    args = parser.parse_args(argv)

    counts, examples, total = scan(args.days)
    if not total:
        print(f"最近 {args.days} 天没有回复记录")
        return 0
    hits = sum(1 for name in counts if counts[name])
    print(f"最近 {args.days} 天：{total} 条回复\n")
    print(f"{'套话':<12}{'次数':>5}{'占比':>7}")
    for name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        if count:
            print(f"{name:<12}{count:>5}{count / total * 100:>6.1f}%")
    clean = total - sum(1 for _ in range(0))  # 占位，下面用更准的口径
    # 至少命中一条的回复数（去重）
    matched_ids = set()
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        since = (date.today() - timedelta(days=args.days - 1)).isoformat()
        rows = list(con.execute(
            "SELECT id, reply FROM traces WHERE reply != '' AND substr(created_at,1,10) >= ?",
            (since,)))
    finally:
        con.close()
    for row_id, reply in rows:
        text = str(reply or "")
        if any(re.search(p, text) for p in PATTERNS.values()):
            matched_ids.add(row_id)
    print(f"\n至少命中一条：{len(matched_ids)}/{total}（{len(matched_ids) / total * 100:.1f}%）")

    if args.show:
        for name, items in examples.items():
            for item in items[:args.show]:
                print(f"  [{name}] {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
