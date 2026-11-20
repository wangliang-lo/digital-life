"""记忆检索的体检：用她真实的记忆做"还记不记得"测试。

用法：
    python tools/memory_eval.py            # 抽 40 条测
    python tools/memory_eval.py --n 80

做法：从库里随机抽一条记忆，用里面最有辨识度的词造一个问题，
再看她检索时能不能把这条找回来（top1 / top5 命中率）。
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))

from memory import LibraryMemory  # noqa: E402

DB = ROOT / "data" / "xiaoyu.db"
USER = "conv:9e52b4549f424e730d8871644385bcce63768cc74b8f8ec7ffa895f62ca73eeb"
STOP = set("的了是我你他她它这那吗呢吧啊和跟就都很还没在有说过要会能不上下来去给对把被让又也但所以因为如果什么怎么")


def keywords(text: str, limit: int = 2) -> list[str]:
    words = re.findall(r"[\u4e00-\u9fff]{2,6}", text)
    scored = sorted({w for w in words if not (set(w) & STOP) and len(w) >= 2},
                    key=lambda w: (-len(w), w))
    return scored[:limit]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=40)
    parser.add_argument("--k", type=int, default=5)
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    all_ids = [int(r[0]) for r in conn.execute(
        "SELECT id FROM books WHERE status != 'suppressed' AND length(summary) >= 12 "
        "AND folder_event NOT IN ('短期回忆') ORDER BY id").fetchall()]
    step = max(1, len(all_ids) // (args.n * 3))
    picked = all_ids[::step][: args.n * 2]
    rows = conn.execute(
        "SELECT id, summary, content, folder_user, status FROM books WHERE id IN (%s) ORDER BY id"
        % ",".join(str(i) for i in picked)
    ).fetchall()
    conn.close()

    # 用"稀有词"造问题：满库都有的词（小鱼、晚安）测不出检索好坏
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    all_summaries = [str(row[0] or "") for row in conn.execute(
        "SELECT summary FROM books WHERE status != 'suppressed'").fetchall()]
    conn.close()

    def rare_keywords(text: str, limit: int = 2) -> list[str]:
        words = re.findall(r"[\u4e00-\u9fff]{2,6}", text)
        uniq = {w for w in words if not (set(w) & STOP) and len(w) >= 2}
        scored = []
        for word in uniq:
            df = sum(1 for s in all_summaries if word in s)
            if df <= max(3, len(all_summaries) // 50):
                scored.append((df, -len(word), word))
        scored.sort()
        return [word for _, _, word in scored[:limit]]

    lib = LibraryMemory(DB, user_id=USER)
    hits1 = hits5 = total = 0
    misses: list[tuple[str, list[str], str]] = []

    for book_id, summary, content, folder_user, status in rows:
        if total >= args.n:
            break
        kw = rare_keywords(str(summary or ""))
        if not kw:
            continue
        query = "你还记得" + "、".join(kw) + "吗"
        try:
            result = lib.retrieve(query=query, now=datetime.now(), impulse=55)
        except Exception as exc:  # noqa: BLE001
            print("检索异常:", exc)
            continue
        got = [int(getattr(hit, "book_id", 0) or 0) for hit in result.hits[: args.k]]
        total += 1
        if got[:1] and got[0] == book_id:
            hits1 += 1
        if book_id in got:
            hits5 += 1
        else:
            misses.append((str(summary)[:40], kw, "/".join(str(g) for g in got[:3])))

    print(f"样本 {total} 条")
    print(f"top1 命中 {hits1} ({hits1 / max(1, total):.0%})")
    print(f"top{args.k} 命中 {hits5} ({hits5 / max(1, total):.0%})")
    print("\n漏掉的例子（前 8 条）：")
    for summary, kw, got in misses[:8]:
        print(f"  - {summary} | 关键词 {kw} | 实际拿到 {got}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
