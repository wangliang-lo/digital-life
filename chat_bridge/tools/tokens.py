#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""看 token 花在哪儿：按天汇总、按来源看、缓命中率。

    python tools/tokens.py            # 今天
    python tools/tokens.py --days 7   # 最近 7 天
"""
from __future__ import annotations

import argparse
import io
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "xiaoyu.db"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--db", default=str(DB))
    args = parser.parse_args()

    since = (date.today() - timedelta(days=args.days - 1)).isoformat()
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        rows = list(con.execute(
            "SELECT substr(created_at,1,10) AS day, source, COUNT(*), "
            "SUM(prompt_tokens), SUM(completion_tokens), SUM(cached_tokens) "
            "FROM traces WHERE substr(created_at,1,10) >= ? "
            "GROUP BY day, source ORDER BY day, source", (since,)))
    except sqlite3.OperationalError:
        print("traces 表还没有 token 列（重启 dl_api 后会自动迁移）")
        return 1

    if not rows:
        print(f"{since} 之后没有记录")
        return 0

    print(f"{'日期':12} {'来源':10} {'轮数':>5} {'输入':>9} {'输出':>7} {'缓存命中':>8} {'命中率':>7}")
    total_in = total_out = total_cached = 0
    for day, source, count, prompt, completion, cached in rows:
        prompt = prompt or 0
        completion = completion or 0
        cached = cached or 0
        rate = (cached / prompt * 100) if prompt else 0.0
        print(f"{day:12} {source or '-':10} {count:5d} {prompt:9d} {completion:7d} {cached:8d} {rate:6.1f}%")
        total_in += prompt
        total_out += completion
        total_cached += cached
    rate = (total_cached / total_in * 100) if total_in else 0.0
    print(f"{'合计':12} {'':10} {'':5} {total_in:9d} {total_out:7d} {total_cached:8d} {rate:6.1f}%")
    print("\n（缓存命中率 = 命中缓存的输入 token / 全部输入 token；"
          "它越高，说明提示词前缀越稳定、越省钱）")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
