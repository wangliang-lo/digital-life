#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记忆库（books 表）每周维护脚本。

只做三类处理，且绝不 DELETE：
  1. 空壳记忆        -> status='suppressed'
  2. 完全重复        -> status='suppressed'（保留 mv 最高的一条）
  3. 超 120 天且 mv < 20 的 summary 记录 -> status='ghost'

用法：
    python memory_maintenance.py              # 默认 dry-run，只分析
    python memory_maintenance.py --apply      # 真正执行
    python memory_maintenance.py --db data/xiaoyu.db --apply

产物（都放在数据库所在目录）：
    memory_maintenance_YYYYMMDD.jsonl   备份清单（每行：id / 原因 / 处理结果，另附原状态便于恢复）
    memory_weekly_YYYYMMDD.md           周报
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# 复用只读分析工具里的相似度分组（疑似重复组统计）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_quality import find_duplicate_groups, normalize_summary, parse_dt  # noqa: E402

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "xiaoyu.db"

# 空壳判定的提示词与阈值
PLACEHOLDER_HINTS = ("表情", "图片", "看不到", "未看清", "没看清", "发来一个", "语音转写", "未知图片")
HOLLOW_MAX_CHARS = 8

# 降级规则
STALE_DAYS = 120
STALE_MV = 20

# 处理类别
CAT_HOLLOW = "hollow"
CAT_DUP = "duplicate"
CAT_STALE = "stale"
CAT_LABELS = {CAT_HOLLOW: "空壳记忆", CAT_DUP: "完全重复", CAT_STALE: "过期降级"}

# 只处理这些状态的记录（已 ghost / suppressed 的不再动）
ACTIVE_STATUSES = ("detail", "summary")


def effective_len(summary: str, content: str) -> int:
    """剥离说话人前缀、标签、方括号/圆括号占位符和标点后的有效字符数。"""
    raw = f"{summary} {content}".strip()
    text = re.sub(r"</?(?:user_turn|assistant_turn)>", " ", raw)
    text = re.sub(r"^[^：:]{1,12}说[:：]\s*", "", text.strip())
    text = re.sub(r"\[[^\]]{0,24}\]|（[^）]{0,24}）|\([^)]{0,24}\)", "", text)
    return len(re.sub(r"[\s\W_]+", "", text))


def is_hollow(summary: str, content: str) -> bool:
    """空壳：有效字符 <= 8 且含"表情/图片/看不到"等提示词。"""
    raw = f"{summary} {content}"
    if not any(w in raw for w in PLACEHOLDER_HINTS):
        return False
    return effective_len(summary, content) <= HOLLOW_MAX_CHARS


def dup_key(book: dict[str, Any]) -> tuple[str, str]:
    """完全重复的判定键：summary 与 content 归一化后都一致。"""
    return normalize_summary(book.get("summary")), normalize_summary(book.get("content"))


def plan_actions(books: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """纯函数：根据当前记录计算处理计划，不碰数据库。

    每条记录最多出现一次，优先级：空壳 > 重复 > 过期降级。
    返回 [{id, category, reason, old_status, new_status}]
    """
    plan: dict[int, dict[str, Any]] = {}
    active = [b for b in books if str(b.get("status") or "") in ACTIVE_STATUSES]

    # 1) 空壳
    for b in active:
        if is_hollow(str(b.get("summary") or ""), str(b.get("content") or "")):
            plan[b["id"]] = {
                "id": b["id"], "category": CAT_HOLLOW,
                "reason": f"空壳记忆（有效字符 {effective_len(str(b.get('summary') or ''), str(b.get('content') or ''))}）",
                "old_status": b["status"], "new_status": "suppressed",
            }

    # 2) 完全重复：同组保留 mv 最高（并列取 id 最小）
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for b in active:
        if b["id"] in plan:
            continue
        key = dup_key(b)
        if not key[0] and not key[1]:
            continue  # 两边归一化后都为空，不当重复处理
        groups[key].append(b)
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda x: (-float(x.get("mv") or 0), x["id"]))
        keeper = members[0]["id"]
        for b in members[1:]:
            plan[b["id"]] = {
                "id": b["id"], "category": CAT_DUP,
                "reason": f"与 #{keeper} 完全重复（保留 mv 更高者）",
                "old_status": b["status"], "new_status": "suppressed",
            }

    # 3) 超 120 天且 mv < 20 的 summary -> ghost
    cutoff = now - timedelta(days=STALE_DAYS)
    for b in active:
        if b["id"] in plan or b["status"] != "summary":
            continue
        if float(b.get("mv") or 0) >= STALE_MV:
            continue
        ts = parse_dt(b.get("last_accessed")) or parse_dt(b.get("created_at"))
        if ts is not None and ts < cutoff:
            plan[b["id"]] = {
                "id": b["id"], "category": CAT_STALE,
                "reason": f"超过 {STALE_DAYS} 天未访问且 mv<{STALE_MV}",
                "old_status": b["status"], "new_status": "ghost",
            }

    return sorted(plan.values(), key=lambda x: x["id"])


def load_books(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    cols = ("id", "summary", "content", "mv", "status", "last_accessed", "created_at")
    rows = conn.execute(f"SELECT {', '.join(cols)} FROM books ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def write_manifest(path: Path, plan: list[dict[str, Any]], results: dict[int, str]) -> None:
    """写备份清单：每行 id / 原因 / 处理结果（另带原状态与目标状态，便于恢复）。"""
    with path.open("w", encoding="utf-8") as f:
        for item in plan:
            row = {
                "id": item["id"],
                "reason": item["reason"],
                "result": results.get(item["id"], "pending"),
                "old_status": item["old_status"],
                "target_status": item["new_status"],
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def apply_plan(conn: sqlite3.Connection, plan: list[dict[str, Any]]) -> dict[int, str]:
    """逐条 UPDATE status（绝不 DELETE）。唯一索引冲突时回退为 ghost。

    返回 {id: 结果}，结果取值：suppressed / ghost / ghost(冲突回退) / failed:<原因>
    """
    results: dict[int, str] = {}
    for item in plan:
        rid, target = item["id"], item["new_status"]
        # 只在状态仍是原状态时更新，避免覆盖期间被别处改过的记录
        sql = "UPDATE books SET status=? WHERE id=? AND status=?"
        try:
            conn.execute("SAVEPOINT m")
            conn.execute(sql, (target, rid, item["old_status"]))
            conn.execute("RELEASE m")
            results[rid] = target
        except sqlite3.IntegrityError:
            # idx_books_dedup（status != 'ghost'）冲突：回退为 ghost，ghost 不在唯一索引范围内
            conn.execute("ROLLBACK TO m")
            conn.execute("RELEASE m")
            try:
                conn.execute("SAVEPOINT m")
                conn.execute(sql, ("ghost", rid, item["old_status"]))
                conn.execute("RELEASE m")
                item["new_status"] = "ghost"
                results[rid] = "ghost(冲突回退)"
            except sqlite3.Error as e:  # 兜底：不崩
                conn.execute("ROLLBACK TO m")
                conn.execute("RELEASE m")
                results[rid] = f"failed:{e}"
        except sqlite3.Error as e:
            conn.execute("ROLLBACK TO m")
            conn.execute("RELEASE m")
            results[rid] = f"failed:{e}"
    return results


def build_report(
    total: int,
    status_counter: Counter,
    plan: list[dict[str, Any]],
    results: dict[int, str],
    remaining_groups: int,
    applied: bool,
    now: datetime,
) -> str:
    cat_counter = Counter(p["category"] for p in plan)
    lines = [
        f"# 记忆库周报 {now:%Y-%m-%d}",
        "",
        f"- 运行模式：{'已执行 (--apply)' if applied else 'dry-run（未写库）'}",
        f"- 总条数：{total}",
        "",
        "## 各状态条数（处理前）",
        "",
    ]
    for st, n in sorted(status_counter.items(), key=lambda x: -x[1]):
        lines.append(f"- {st}：{n}")
    lines += ["", f"## 本次{'处理' if applied else '将处理'}条数：{len(plan)}", ""]
    for cat in (CAT_HOLLOW, CAT_DUP, CAT_STALE):
        lines.append(f"- {CAT_LABELS[cat]}：{cat_counter.get(cat, 0)}")
    if applied:
        fallback = sum(1 for r in results.values() if "冲突回退" in r)
        failed = sum(1 for r in results.values() if r.startswith("failed"))
        lines += ["", f"- 其中唯一索引冲突回退为 ghost：{fallback}", f"- 失败：{failed}"]
    lines += ["", f"## 剩余疑似重复组数：{remaining_groups}", ""]
    return "\n".join(lines)


def run(db_path: Path, apply: bool = False, now: datetime | None = None) -> dict[str, Any]:
    """主流程，返回摘要字典（便于测试）。"""
    now = now or datetime.now()
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"数据库文件不存在：{db_path}")
    out_dir = db_path.parent
    stamp = now.strftime("%Y%m%d")
    manifest_path = out_dir / f"memory_maintenance_{stamp}.jsonl"
    report_path = out_dir / f"memory_weekly_{stamp}.md"

    conn = sqlite3.connect(str(db_path))
    try:
        books = load_books(conn)
        status_counter = Counter(str(b.get("status") or "unknown") for b in books)
        plan = plan_actions(books, now)
        results: dict[int, str] = {}

        if apply and plan:
            # 先写备份清单（pending），再动库
            write_manifest(manifest_path, plan, {})
            results = apply_plan(conn, plan)
            conn.commit()
            write_manifest(manifest_path, plan, results)
        elif apply:
            write_manifest(manifest_path, plan, {})

        # 剩余疑似重复组：在"处理后"的状态上计算（dry-run 用模拟状态）
        final_status = {pid: (results.get(pid, p["new_status"]).split("(")[0]) for pid, p in
                        ((p["id"], p) for p in plan) if not results.get(pid, "").startswith("failed")}
        after = [{**b, "status": final_status.get(b["id"], b["status"])} for b in books]
        remaining = len(find_duplicate_groups(after))
    finally:
        conn.close()

    report = build_report(len(books), status_counter, plan, results, remaining, apply, now)
    report_path.write_text(report, encoding="utf-8")
    return {
        "total": len(books), "planned": len(plan), "plan": plan, "results": results,
        "remaining_groups": remaining, "manifest": manifest_path if apply else None,
        "report": report_path, "applied": apply,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="记忆库每周维护（默认 dry-run）")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="数据库路径")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只分析不写库（默认）")
    mode.add_argument("--apply", action="store_true", help="真正执行（仅改 status，不删除）")
    args = ap.parse_args(argv)

    summary = run(args.db, apply=args.apply)
    cats = Counter(p["category"] for p in summary["plan"])
    verb = "已处理" if args.apply else "将处理"
    print(f"{verb} {summary['planned']} 条："
          + "，".join(f"{CAT_LABELS[c]} {cats.get(c, 0)}" for c in (CAT_HOLLOW, CAT_DUP, CAT_STALE)))
    print(f"剩余疑似重复组：{summary['remaining_groups']}")
    if summary["manifest"]:
        print(f"备份清单：{summary['manifest']}")
    print(f"周报：{summary['report']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
