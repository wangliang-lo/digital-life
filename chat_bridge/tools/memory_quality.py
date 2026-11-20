#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite 记忆库（books 表）只读质量分析脚本。

用法：
    python memory_quality.py --db data/xiaoyu.db --limit 30
"""

from __future__ import annotations

import argparse
import math
import re
import sqlite3
import sys
import urllib.parse
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


def parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def short_text(text: Any, max_len: int = 48) -> str:
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 1] + "…"


def escape_md(text: Any) -> str:
    return str(text or "").replace("|", "\\|").replace("\n", " ").strip()


def normalize_summary(text: str) -> str:
    raw = str(text or "")
    # 2026-11-16：先去掉"某某说："这个说话人前缀——不然所有
    # "SOLVER-4说：…" 的记忆都会被算成互相重复（实测一次把 32 条聚成一组）。
    raw = re.sub(r"^\s*[^：:\n]{1,12}说[:：]\s*", "", raw)
    clean = re.sub(r"<[^>]+>", " ", raw)
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


def is_hollow_memory(summary: str, content: str) -> tuple[bool, str]:
    """判定空壳记忆：去掉 XML/方括号占位符后有效信息极少，或纯媒体/无法识别占位。"""
    raw = f"{summary} {content}".strip()
    if not raw:
        return True, "摘要与正文均为空"

    stripped = re.sub(r"</?(?:user_turn|assistant_turn)>", " ", raw)
    stripped = re.sub(r"^[^：:]{1,12}说[:：]\s*", "", stripped.strip())
    core = re.sub(r"\[[^\]]{0,24}\]|（[^）]{0,24}）|\([^)]{0,24}\)", "", stripped)
    core = re.sub(r"[\s\W_]+", "", core)

    placeholder_hints = ("表情", "图片", "看不到", "未看清", "没看清", "发来一个", "语音转写", "未知图片")
    has_hint = any(word in raw for word in placeholder_hints)

    if len(core) == 0:
        return True, "剥离占位符后无有效文字"
    if len(core) <= 8 and has_hint:
        return True, f"仅含占位描述（有效字符 {len(core)} 字）"
    if len(core) <= 2 and not re.search(r"[\u4e00-\u9fa5A-Za-z0-9]{2}", core):
        return True, "无实质语义字符"
    return False, ""


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
    required = ["id", "summary", "content", "mv", "status", "last_accessed", "folder_event", "folder_user", "created_at"]
    select_cols = [col if col in existing_cols else f"'' AS {col}" for col in required]
    sql = f"SELECT {', '.join(select_cols)} FROM books ORDER BY id ASC"
    return [dict(row) for row in conn.execute(sql).fetchall()]


def find_duplicate_groups(books: list[dict[str, Any]], threshold: float = 0.82) -> list[list[dict[str, Any]]]:
    """对非 ghost 记忆按 summary 相似度聚类成组（基于倒排索引 + 并查集控制复杂度）。"""
    candidates = []
    for book in books:
        if str(book.get("status") or "") == "ghost":
            continue
        norm = normalize_summary(str(book.get("summary") or ""))
        if len(norm) < 4:
            continue
        candidates.append({
            **book,
            "_norm": norm,
            "_grams": char_bigrams(norm),
        })

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
    groups.sort(key=lambda g: (-len(g), -max(float(x.get("mv") or 0) for x in g)))
    return groups


def analyze(db_path: Path, limit: int) -> str:
    conn = open_readonly_db(db_path)
    try:
        books = load_books(conn)
    finally:
        conn.close()

    now = datetime.now()
    total_count = len(books)
    status_counter = Counter(str(b.get("status") or "unknown") for b in books)
    event_counter = Counter(str(b.get("folder_event") or "未分类") for b in books)

    dup_groups = find_duplicate_groups(books, threshold=0.82)

    hollow_items: list[tuple[dict[str, Any], str]] = []
    for book in books:
        if str(book.get("status") or "") == "ghost":
            continue
        hollow, reason = is_hollow_memory(str(book.get("summary") or ""), str(book.get("content") or ""))
        if hollow:
            hollow_items.append((book, reason))

    cutoff_60d = now - timedelta(days=60)
    stale_items: list[tuple[dict[str, Any], int]] = []
    for book in books:
        dt = parse_dt(book.get("last_accessed")) or parse_dt(book.get("created_at"))
        if dt is not None and dt < cutoff_60d:
            days = (now - dt).days
            stale_items.append((book, days))
    stale_items.sort(key=lambda pair: (-pair[1], float(pair[0].get("mv") or 0)))

    cleanup_candidates: dict[int, dict[str, Any]] = {}

    for book, reason in hollow_items:
        bid = int(book["id"])
        cleanup_candidates[bid] = {
            "id": bid,
            "priority": 1,
            "reason": f"空壳占位（{reason}）",
            "status": book.get("status") or "",
            "mv": float(book.get("mv") or 0.0),
            "summary": short_text(book.get("summary") or book.get("content") or "", 40),
        }

    for grp in dup_groups:
        ordered = sorted(
            grp,
            key=lambda b: (float(b.get("mv") or 0.0), str(b.get("last_accessed") or ""), int(b["id"])),
            reverse=True,
        )
        keeper = ordered[0]
        for dup in ordered[1:]:
            bid = int(dup["id"])
            tag = f"重复记忆（与 #{keeper['id']} 高度相似）"
            if bid in cleanup_candidates:
                cleanup_candidates[bid]["reason"] += f"；{tag}"
            else:
                cleanup_candidates[bid] = {
                    "id": bid,
                    "priority": 2,
                    "reason": tag,
                    "status": dup.get("status") or "",
                    "mv": float(dup.get("mv") or 0.0),
                    "summary": short_text(dup.get("summary") or "", 40),
                }

    for book, days in stale_items:
        bid = int(book["id"])
        mv = float(book.get("mv") or 0.0)
        status = str(book.get("status") or "")
        if status == "suppressed" or mv >= 30.0:
            continue
        tag = f"冷门低价值（{days} 天未访问且 MV={mv:.1f}）"
        if bid in cleanup_candidates:
            cleanup_candidates[bid]["reason"] += f"；{tag}"
        else:
            cleanup_candidates[bid] = {
                "id": bid,
                "priority": 3,
                "reason": tag,
                "status": status,
                "mv": mv,
                "summary": short_text(book.get("summary") or "", 40),
            }

    cleanup_list = sorted(
        cleanup_candidates.values(),
        key=lambda item: (item["priority"], item["mv"], item["id"]),
    )

    lines: list[str] = [
        "# 记忆库（`books`）质量分析报告",
        "",
        f"- **数据库路径**：`{db_path}`",
        f"- **分析时间**：{now.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- **记忆总条数**：{total_count} 条",
        "- **状态分布**：" + ("，".join(f"`{k}`: {v}" for k, v in status_counter.most_common()) or "无"),
        f"- **展示上限 (`--limit`)**：每节最多展示 {limit} 条",
        "",
        "---",
        "",
        f"## 1. 重复记忆（共 {len(dup_groups)} 组，涉及 {sum(len(g) for g in dup_groups)} 条）",
        "",
    ]

    if not dup_groups:
        lines.append("未检测到高相似度重复记忆。")
    else:
        for idx, grp in enumerate(dup_groups[:limit], start=1):
            ordered = sorted(
                grp,
                key=lambda b: (float(b.get("mv") or 0.0), str(b.get("last_accessed") or ""), int(b["id"])),
                reverse=True,
            )
            lines.append(f"### 重复组 #{idx}（共 {len(ordered)} 条）")
            lines.append("| ID | 建议 | 状态 | MV | 事件 | 摘要 (`summary`) | 最近访问 |")
            lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
            for pos, item in enumerate(ordered):
                role = "保留主条" if pos == 0 else "冗余副本"
                lines.append(
                    f"| {item['id']} | {role} | `{escape_md(item.get('status'))}` | "
                    f"{float(item.get('mv') or 0):.1f} | {escape_md(item.get('folder_event'))} | "
                    f"{escape_md(short_text(item.get('summary'), 44))} | "
                    f"{escape_md(str(item.get('last_accessed') or '')[:16])} |"
                )
            lines.append("")

    lines.extend([
        "---",
        "",
        f"## 2. 空壳记忆（共 {len(hollow_items)} 条）",
        "",
    ])
    if not hollow_items:
        lines.append("未检测到空壳或纯占位记忆。")
    else:
        lines.append("| ID | 状态 | MV | 判定理由 | 摘要 (`summary`) | 正文片段 (`content`) |")
        lines.append("| :--- | :--- | :--- | :--- | :--- | :--- |")
        for book, reason in hollow_items[:limit]:
            lines.append(
                f"| {book['id']} | `{escape_md(book.get('status'))}` | {float(book.get('mv') or 0):.1f} | "
                f"{escape_md(reason)} | {escape_md(short_text(book.get('summary'), 28))} | "
                f"{escape_md(short_text(book.get('content'), 36))} |"
            )
        lines.append("")

    lines.extend([
        "---",
        "",
        f"## 3. 超过 60 天未被检索的记忆（共 {len(stale_items)} 条）",
        "",
    ])
    if not stale_items:
        lines.append("没有超过 60 天未访问的记忆。")
    else:
        lines.append("| ID | 未访问天数 | 状态 | MV | 人物 | 事件 | 摘要 (`summary`) | 最近访问 |")
        lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
        for book, days in stale_items[:limit]:
            lines.append(
                f"| {book['id']} | {days} 天 | `{escape_md(book.get('status'))}` | "
                f"{float(book.get('mv') or 0):.1f} | {escape_md(book.get('folder_user'))} | "
                f"{escape_md(book.get('folder_event'))} | {escape_md(short_text(book.get('summary'), 34))} | "
                f"{escape_md(str(book.get('last_accessed') or '')[:10])} |"
            )
        lines.append("")

    lines.extend([
        "---",
        "",
        "## 4. `folder_event` 分布计数",
        "",
        "| 事件分类 (`folder_event`) | 记忆条数 | 占比 |",
        "| :--- | :--- | :--- |",
    ])
    if not event_counter:
        lines.append("| （空库） | 0 | 0.0% |")
    else:
        for event, count in event_counter.most_common():
            pct = (count / total_count * 100.0) if total_count else 0.0
            lines.append(f"| {escape_md(event)} | {count} | {pct:.1f}% |")
    lines.append("")

    lines.extend([
        "---",
        "",
        f"## 5. 建议清理清单（共 {len(cleanup_list)} 条，展示前 {min(limit, len(cleanup_list))} 条）",
        "",
    ])
    if not cleanup_list:
        lines.append("当前记忆库整体健康，暂无建议清理条目。")
    else:
        lines.append("| ID | 状态 | MV | 清理理由 | 摘要片段 |")
        lines.append("| :--- | :--- | :--- | :--- | :--- |")
        for item in cleanup_list[:limit]:
            lines.append(
                f"| {item['id']} | `{escape_md(item['status'])}` | {item['mv']:.1f} | "
                f"{escape_md(item['reason'])} | {escape_md(item['summary'])} |"
            )
        lines.append("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读分析 SQLite 记忆库（books 表）质量并输出 Markdown 报告")
    parser.add_argument("--db", default="xiaoyu.db", help="SQLite 数据库路径（默认：xiaoyu.db）")
    parser.add_argument("--limit", type=int, default=30, help="每个章节最多展示的条目/组数（默认：30）")
    args = parser.parse_args(argv)

    limit = max(1, int(args.limit))
    try:
        report = analyze(Path(args.db), limit=limit)
    except Exception as exc:  # noqa: BLE001
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    sys.stdout.write(report + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
