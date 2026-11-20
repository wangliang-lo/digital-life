#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把数字生命的「书」同步进抖音助手的共享记忆库。

数字生命的记忆在 data/xiaoyu.db 的 books 表里；抖音助手用的是
~/.dsh/douyin-browser/memory/shared.sqlite 的 memory_entries 表。
这个脚本把前者按 important-first 写进后者，external_key 去重，可以反复跑。

    python tools/sync_memory.py                 # 同步一次
    python tools/sync_memory.py --watch 60      # 每 60 秒增量同步
    python tools/sync_memory.py --user douyin-张三 --min-mv 20 --limit 80

只写 memory_entries，不碰助手的人格卡、兴趣和观看记录。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIGITAL_DB = ROOT / "data" / "xiaoyu.db"
DEFAULT_SHARED_DB = Path.home() / ".dsh" / "douyin-browser" / "memory" / "shared.sqlite"


def read_books(db_path: Path, user: str = "", min_mv: float = 0.0, limit: int = 50) -> list[dict]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        sql = (
            "SELECT id, user_id, summary, folder_event, mv, status "
            "FROM books WHERE status IN ('detail','summary') AND mv >= ?"
        )
        params: list[object] = [min_mv]
        if user:
            sql += " AND user_id = ?"
            params.append(user)
        sql += " ORDER BY mv DESC, id DESC LIMIT ?"
        params.append(limit)
        return [dict(row) for row in con.execute(sql, params)]
    finally:
        con.close()


def sync(digital_db: Path, shared_db: Path, user: str, min_mv: float, limit: int) -> int:
    books = read_books(digital_db, user=user, min_mv=min_mv, limit=limit)
    if not books:
        print("没有可同步的记忆（检查 --min-mv / --user）")
        return 0

    con = sqlite3.connect(str(shared_db), timeout=10)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        now_ms = int(time.time() * 1000)
        written = 0
        for book in books:
            summary = str(book.get("summary") or "").strip()
            if not summary:
                continue
            content = summary[:500]
            key = f"digital-life:{book['user_id']}:{book['id']}"
            metadata = json.dumps(
                {
                    "sourceType": "private",
                    "privacy": "internal-reference",
                    "confirmed": False,
                    "inferred": False,
                    "isUserFact": False,
                    "digitalLifeUser": book["user_id"],
                    "digitalLifeBookId": book["id"],
                    "event": book.get("folder_event") or "",
                    "mv": round(float(book.get("mv") or 0), 1),
                },
                ensure_ascii=False,
            )
            con.execute(
                """
                INSERT INTO memory_entries
                    (kind, content, source, sources_json, video_id, url, author, title,
                     metadata_json, created_at, updated_at, origin, status, external_key)
                VALUES ('note', ?, 'digital-life', ?, NULL, NULL, NULL, NULL,
                        ?, ?, ?, 'video', 'recorded-note', ?)
                ON CONFLICT(external_key) DO UPDATE SET
                    content = excluded.content,
                    metadata_json = excluded.metadata_json,
                    origin = excluded.origin,
                    updated_at = excluded.updated_at
                WHERE memory_entries.content IS NOT excluded.content
                   OR memory_entries.metadata_json IS NOT excluded.metadata_json
                   OR memory_entries.origin IS NOT excluded.origin
                """,
                (content, json.dumps(["digital-life"]), metadata, now_ms, now_ms, key),
            )
            written += 1
        con.commit()
        print(f"同步完成：{written} 条（{shared_db}）")
        return written
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="数字生命 → 抖音助手 记忆同步")
    parser.add_argument("--digital-db", default=str(DEFAULT_DIGITAL_DB))
    parser.add_argument("--shared-db", default=str(DEFAULT_SHARED_DB))
    parser.add_argument("--user", default="", help="只同步某个 user_id（默认全部）")
    parser.add_argument("--min-mv", type=float, default=0.0, help="只同步重要度 >= 这个值的书")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--watch", type=int, default=0, help="每 N 秒重复同步（0=只跑一次）")
    args = parser.parse_args()

    digital_db = Path(args.digital_db)
    shared_db = Path(args.shared_db)
    if not digital_db.exists():
        print(f"找不到数字生命数据库：{digital_db}")
        return 2
    if not shared_db.exists():
        print(f"找不到抖音助手的共享记忆库：{shared_db}")
        print("先启动一次抖音助手（它会创建这个文件），或检查 --shared-db。")
        return 2

    if args.watch <= 0:
        sync(digital_db, shared_db, args.user, args.min_mv, args.limit)
        return 0

    print(f"每 {args.watch}s 同步一次，Ctrl+C 退出")
    while True:
        try:
            sync(digital_db, shared_db, args.user, args.min_mv, args.limit)
        except Exception as exc:  # noqa: BLE001
            print("同步失败：", exc)
        time.sleep(max(5, args.watch))


if __name__ == "__main__":
    raise SystemExit(main())
