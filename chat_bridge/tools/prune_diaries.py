#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""日记清理：只留最重要的几篇，其余删掉，省 token。

    python tools/prune_diaries.py --dry-run
    python tools/prune_diaries.py --keep 3

规则：
- 只保留 conv: 开头的真实会话，测试账号（web-*/douyin-*/share-*）的日记直接删；
- 每个会话最多留 --keep 篇（默认 3），按「信息量」排序：
  长度 + 去重后的词数 + 是否提到具体记忆，模板化重复的排后面；
- 开头 12 个字相同的视为重复，只留最早那篇。
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "xiaoyu.db"

GENERIC = ("平静", "顺其自然", "一切都很", "反思", "感恩", "提醒自己", "似乎", "有些许")


def score(content: str) -> float:
    text = (content or "").strip()
    if not text:
        return 0.0
    words = set(re.findall(r"[\u4e00-\u9fa5]{2,}", text))
    generic = sum(1 for word in GENERIC if word in text)
    return len(text) * 0.6 + len(words) * 2 - generic * 12


def main() -> int:
    parser = argparse.ArgumentParser(description="日记清理")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep", type=int, default=3)
    args = parser.parse_args()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("select id, user_id, content, created_at from diaries order by id desc")]

    remove: list[int] = []
    kept: list[dict] = []
    by_user: dict[str, list[dict]] = {}
    for row in rows:
        user_id = str(row["user_id"])
        if not user_id.startswith("conv:"):
            remove.append(int(row["id"]))
            continue
        by_user.setdefault(user_id, []).append(row)

    for user_id, items in by_user.items():
        seen_openings: set[str] = set()
        picked: list[dict] = []
        for row in sorted(items, key=lambda r: score(r["content"]), reverse=True):
            opening = (row["content"] or "").strip()[:12]
            if opening in seen_openings:
                remove.append(int(row["id"]))
                continue
            seen_openings.add(opening)
            if len(picked) < args.keep:
                picked.append(row)
            else:
                remove.append(int(row["id"]))
        kept.extend(picked)

    print(f"日记共 {len(rows)} 篇，保留 {len(kept)} 篇，删除 {len(remove)} 篇")
    if args.dry_run:
        return 0
    for diary_id in remove:
        con.execute("delete from diaries where id=?", (diary_id,))
    con.commit()
    con.close()
    print("清理完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
