#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""给老记忆补 embedding（2026-11-08，批量版）。

背景：线上 `embedding_fn` 一直是 None，1348 条记忆的 embedding 全是 "[]"，
检索只能靠字符 bigram 近似。接上本地 bge-small-zh 之后，这个脚本把旧的一批补上。

    python tools/backfill_embeddings.py               # 补全部空的
    python tools/backfill_embeddings.py --limit 64    # 只补 64 条
    python tools/backfill_embeddings.py --dry-run     # 只看有多少要补
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent        # PROJECT_ROOT\chat_bridge
REPO = HERE.parent                                   # PROJECT_ROOT（digital_life.py 在这）
sys.path.insert(0, str(REPO))

from digital_life import embed_batch  # noqa: E402

DB = HERE / "data" / "xiaoyu.db"
BATCH = 32


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", default=str(DB))
    args = parser.parse_args()

    con = sqlite3.connect(args.db, timeout=30)
    cur = con.cursor()
    total = cur.execute("select count(*) from books").fetchone()[0]
    missing = cur.execute("select count(*) from books where length(embedding) <= 2").fetchone()[0]
    print(f"总共 {total} 条，缺 embedding {missing} 条")
    if args.dry_run or not missing:
        con.close()
        return 0

    todo = args.limit or missing
    done = failed = 0
    started = time.time()
    while done + failed < todo:
        want = min(BATCH, todo - done - failed)
        rows = list(cur.execute(
            "select id, summary, content from books where length(embedding) <= 2 order by id limit ?",
            (want,)))
        if not rows:
            break
        texts = [f"{summary or ''} {content or ''}".strip()[:2000] for _, summary, content in rows]
        vectors = embed_batch(texts)
        if len(vectors) != len(rows):
            failed += len(rows)
            print("  这一批没拿到向量，先停（模型没起来？）")
            break
        for (book_id, _, _), vector in zip(rows, vectors):
            cur.execute("update books set embedding=? where id=?",
                        (json.dumps(vector, ensure_ascii=False), book_id))
        con.commit()
        done += len(rows)
        print(f"  已补 {done} 条，用时 {time.time()-started:.0f}s", flush=True)

    left = cur.execute("select count(*) from books where length(embedding) <= 2").fetchone()[0]
    con.close()
    print(f"完成：补了 {done} 条，失败 {failed}，还剩 {left} 条空的，用时 {time.time()-started:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
