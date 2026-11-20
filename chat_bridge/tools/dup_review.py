#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite 记忆库（books 表）重复记忆交互式抽样核对工具（只读库，决策写 JSONL）。

用法：
    python dup_review.py --db data/xiaoyu.db --limit 20 --threshold 0.82
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import sys
import urllib.parse
from collections import defaultdict
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


def short_text(text: Any, max_len: int) -> str:
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 1] + "…"


def normalize_summary(text: str) -> str:
    clean = re.sub(r"<[^>]+>", " ", str(text or "")).strip()
    clean = re.sub(r"^[^：:]{1,12}说[:：]\s*", "", clean)
    clean = re.sub(r"[\s\W_]+", "", clean).lower()
    return clean


def char_bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text} if text else set()
    return {text[i : i + 2] for i in range(len(text) - 1)}


def summary_similarity(left_norm: str, left_grams: set[str], right_norm: str, right_grams: set[str]) -> float:
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    if left_grams and right_grams:
        bigram_sim = len(left_grams & right_grams) / math.sqrt(len(left_grams) * len(right_grams))
    else:
        bigram_sim = 0.0
    seq_sim = SequenceMatcher(None, left_norm, right_norm).ratio()
    return max(bigram_sim, seq_sim)


def open_readonly_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"数据库文件不存在：{db_path}")
    resolved = db_path.resolve().as_posix()
    uri = f"file:{urllib.parse.quote(resolved)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_books(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cols_info = conn.execute("PRAGMA table_info(books)").fetchall()
    existing_cols = {str(row["name"]) for row in cols_info}
    required = ["id", "summary", "content", "mv", "status", "last_accessed", "created_at"]
    select_cols = [col if col in existing_cols else f"'' AS {col}" for col in required]
    sql = f"SELECT {', '.join(select_cols)} FROM books ORDER BY id ASC"
    return [dict(row) for row in conn.execute(sql).fetchall()]


def find_duplicate_groups(books: list[dict[str, Any]], threshold: float = 0.82) -> list[list[dict[str, Any]]]:
    candidates = []
    for book in books:
        if str(book.get("status") or "") == "ghost":
            continue
        norm = normalize_summary(str(book.get("summary") or ""))
        if len(norm) < 4:
            continue
        candidates.append({**book, "_norm": norm, "_grams": char_bigrams(norm)})

    n = len(candidates)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    gram_index: dict[str, list[int]] = defaultdict(list)
    for idx, item in enumerate(candidates):
        for gram in item["_grams"]:
            gram_index[gram].append(idx)

    compared: set[tuple[int, int]] = set()
    for indices in gram_index.values():
        if len(indices) > 120:
            continue
        for i in range(len(indices)):
            for j in range(i + 1, len(indices)):
                a, b = indices[i], indices[j]
                if a > b:
                    a, b = b, a
                if (a, b) in compared:
                    continue
                compared.add((a, b))
                la, lb = len(candidates[a]["_norm"]), len(candidates[b]["_norm"])
                if min(la, lb) / max(la, lb) < 0.65:
                    continue
                sim = summary_similarity(
                    candidates[a]["_norm"],
                    candidates[a]["_grams"],
                    candidates[b]["_norm"],
                    candidates[b]["_grams"],
                )
                if sim >= threshold:
                    union(a, b)

    clusters: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for idx, item in enumerate(candidates):
        clusters[find(idx)].append(item)

    groups = [grp for grp in clusters.values() if len(grp) >= 2]
    for grp in groups:
        grp.sort(
            key=lambda b: (float(b.get("mv") or 0.0), str(b.get("last_accessed") or ""), int(b["id"])),
            reverse=True,
        )
    groups.sort(key=lambda g: (-len(g), -float(g[0].get("mv") or 0.0)))
    return groups


def make_group_id(group: list[dict[str, Any]]) -> str:
    ids_str = ",".join(str(b["id"]) for b in sorted(group, key=lambda x: int(x["id"])))
    digest = hashlib.sha1(ids_str.encode("utf-8")).hexdigest()[:10]
    return f"grp_{digest}"


def load_reviewed_groups(out_path: Path) -> set[str]:
    reviewed: set[str] = set()
    if not out_path.exists():
        return reviewed
    try:
        for line in out_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if isinstance(record, dict) and record.get("action") in ("k", "m"):
                gid = str(record.get("group_id") or "")
                if gid:
                    reviewed.add(gid)
    except Exception:  # noqa: BLE001
        pass
    return reviewed


def append_decision(out_path: Path, record: dict[str, Any]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def review_loop(groups: list[list[dict[str, Any]]], limit: int, out_path: Path) -> None:
    reviewed = load_reviewed_groups(out_path)
    pending = [g for g in groups if make_group_id(g) not in reviewed]

    print("=" * 72)
    print(f"共检测到 {len(groups)} 组重复记忆（已审 {len(reviewed)} 组，待审 {len(pending)} 组）")
    print(f"本次抽样上限：{limit} 组 | 决策输出文件：{out_path}")
    print("=" * 72)

    if not pending:
        print("没有需要继续核对的重复组。")
        return

    target_groups = pending[:limit]
    saved_count = 0

    for idx, grp in enumerate(target_groups, start=1):
        gid = make_group_id(grp)
        keeper = grp[0]
        drop_candidates = grp[1:]

        print(f"\n[{idx}/{len(target_groups)}] 组标识：{gid}（组内共 {len(grp)} 条）")
        print("-" * 72)
        for pos, item in enumerate(grp, start=1):
            tag = " [★MV最高]" if pos == 1 else ""
            summary_str = short_text(item.get("summary") or "（无摘要）", 60)
            content_str = short_text(item.get("content") or "（无正文）", 80)
            print(
                f"  ({pos}) ID={item['id']} | MV={float(item.get('mv') or 0):.1f} | "
                f"状态={item.get('status') or 'unknown'}{tag}"
            )
            print(f"      摘要：{summary_str}")
            print(f"      正文：{content_str}")

        while True:
            try:
                choice = input("\n请选择操作 [k=全部保留 / m=仅保留MV最高并合并其余 / s=跳过 / q=退出]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                choice = "q"
                print()

            if choice == "q":
                print(f"\n已退出审核。本次共记录 {saved_count} 条决策到 {out_path}。")
                return
            if choice == "s":
                record = {
                    "group_id": gid,
                    "action": "s",
                    "keep_id": None,
                    "drop_ids": [],
                    "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                }
                append_decision(out_path, record)
                print("  -> 已跳过（不计入已审，下次运行仍会出现）。")
                break
            if choice == "k":
                record = {
                    "group_id": gid,
                    "action": "k",
                    "keep_id": None,
                    "drop_ids": [],
                    "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                }
                append_decision(out_path, record)
                saved_count += 1
                print("  -> 已记录：全部保留。")
                break
            if choice == "m":
                keep_id = int(keeper["id"])
                drop_ids = [int(x["id"]) for x in drop_candidates]
                record = {
                    "group_id": gid,
                    "action": "m",
                    "keep_id": keep_id,
                    "drop_ids": drop_ids,
                    "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                }
                append_decision(out_path, record)
                saved_count += 1
                print(f"  -> 已记录：保留 ID={keep_id}，标记合并 {drop_ids}。")
                break

            print("  输入无效，请输入 k / m / s / q。")

    print(f"\n本次抽样核对完成！共记录 {saved_count} 条有效决策到 {out_path}。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重复记忆交互式抽样核对工具（只读库，不执行任何写入）")
    parser.add_argument("--db", default="xiaoyu.db", help="SQLite 数据库路径（默认：xiaoyu.db）")
    parser.add_argument("--limit", type=int, default=20, help="本次最多抽样核对的组数（默认：20）")
    parser.add_argument("--threshold", type=float, default=0.82, help="summary 相似度阈值（默认：0.82）")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    limit = max(1, int(args.limit))
    threshold = max(0.0, min(1.0, float(args.threshold)))
    out_path = Path("dup_decisions.jsonl")

    try:
        conn = open_readonly_db(db_path)
        try:
            books = load_books(conn)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    groups = find_duplicate_groups(books, threshold=threshold)
    review_loop(groups, limit=limit, out_path=out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
