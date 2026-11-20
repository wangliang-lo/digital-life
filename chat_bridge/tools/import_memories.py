# -*- coding: utf-8 -*-
"""往「记忆图书馆」里灌记忆：一段经历、一本书、一份聊天记录。

为什么要有这个工具：
她的记忆不该从空白开始。空白起步的数字生命，第一句话注定是「你好，我是……」。
先给她一段记不清来历的过去，她说话才有底子。

四种用法（可以叠加）：

    # 1) 导入自带的示例经历（seed/experience.json）
    python tools/import_memories.py --seed

    # 2) 导入同格式的 JSON（自己写的一段经历）
    python tools/import_memories.py --file my_story.json

    # 3) 把一本小说/一篇长文拆成记忆
    python tools/import_memories.py --novel 北回归线.txt

    # 4) 把聊天记录拆成记忆
    python tools/import_memories.py --chat 聊天记录.txt

    # 只看会写什么，不落库
    python tools/import_memories.py --seed --dry-run

导入完建议跑一次：
    python tools/backfill_embeddings.py
（补语义词向量；不补也能用，只是语义检索弱一点）

JSON 格式：
{
  "memories": [
    {"at": "2026-10-09T03:10:00+08:00", "content": "正文",
     "summary": "一句话提要", "emotion": "痛", "event": "明天见",
     "status": "detail", "unresolved": 0}
  ],
  "diaries": [
    {"at": "2026-10-12T02:30:00+08:00", "content": "日记正文"}
  ]
}

status 三档：detail（还清楚）/ summary（只剩梗概）/ ghost（只剩一点感觉）。
unresolved=1 表示这件事她还没想通——会一直挂在她心上。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent.parent
SEED_FILE = BRIDGE / "seed" / "experience.json"
DEFAULT_DB = BRIDGE / "data" / "xiaoyu.db"

sys.stdout.reconfigure(encoding="utf-8")

VALID_STATUS = ("detail", "summary", "ghost")


# ---------------------------------------------------------------- 通用

def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _first_sentence(text: str, limit: int = 60) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        return ""
    m = re.split(r"(?<=[。！？!?])", text)
    out = (m[0] if m and m[0].strip() else text).strip()
    return out[:limit]


def _connect(db: Path) -> sqlite3.Connection:
    assert db.exists(), f"数据库不存在：{db}"
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    return con


def _book_exists(con: sqlite3.Connection, *, user_id: str, at: str,
                 event: str, summary: str) -> bool:
    """同一天、同一件事、同一句提要已经存在就跳过。

    为什么不靠 ON CONFLICT：那条约束带 WHERE status != 'ghost'，
    ghost 状态的记忆不参与去重，重复导入会翻倍。
    """
    row = con.execute(
        "SELECT 1 FROM books WHERE user_id=? AND folder_date=? AND folder_event=? "
        "AND summary=? LIMIT 1",
        (user_id, at[:10], event, summary),
    ).fetchone()
    return row is not None


def _insert_book(con: sqlite3.Connection, *, user_id: str, content: str, summary: str,
                 emotion: str, at: str, event: str, status: str,
                 unresolved: bool, origin: str, decay_rate: float = 1.0) -> bool:
    """写一条记忆；已经存在则跳过并返回 False。"""
    if _book_exists(con, user_id=user_id, at=at, event=event, summary=summary):
        return False
    folder_date = at[:10]
    day_mv = 100.0
    if status == "summary":
        day_mv = 60.0
    elif status == "ghost":
        day_mv = 20.0
    con.execute(
        "INSERT INTO books(user_id, content, summary, emotion_tag, folder_date, "
        "folder_user, folder_event, mv, decay_rate, ghost_hits, embedding, status, "
        "unresolved, last_mentioned, origin, source_id, version, created_at, "
        "last_accessed, weight_source) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(user_id, folder_date, folder_event, summary) "
        "WHERE status != 'ghost' DO UPDATE SET "
        "mv = MIN(220, books.mv + 5), "
        "unresolved = MAX(books.unresolved, excluded.unresolved), "
        "content = CASE WHEN excluded.version > books.version "
        "THEN excluded.content ELSE books.content END",
        (user_id, content, summary, emotion, folder_date, user_id, event,
         day_mv, decay_rate, 0, "[]", status,
         1 if unresolved else 0, "", origin, _digest(summary + content), 1,
         at, at, ""),
    )
    return True


def _diary_exists(con: sqlite3.Connection, *, user_id: str, content: str, at: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM diaries WHERE user_id=? AND created_at=? AND content=? LIMIT 1",
        (user_id, at, content),
    ).fetchone()
    return row is not None


def _insert_diary(con: sqlite3.Connection, *, user_id: str, content: str, at: str,
                  origin: str = "seed") -> bool:
    """写一篇日记；已经存在则跳过并返回 False。"""
    if _diary_exists(con, user_id=user_id, content=content, at=at):
        return False
    con.execute(
        "INSERT INTO diaries(user_id, content, source, created_at, is_discussed) "
        "VALUES(?,?,?,?,0)",
        (user_id, content, origin, at),
    )
    return True


# ---------------------------------------------------------------- 各来源

def load_json_items(path: Path) -> tuple[list[dict], list[dict]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    mems = data.get("memories") or data.get("items") or []
    dias = data.get("diaries") or []
    if not isinstance(mems, list):
        raise SystemExit("memories 必须是数组")
    return mems, dias


def novel_items(path: Path) -> list[dict]:
    """把长文按段落合并成 180~420 字的块，每块当一条记忆。

    章节之间不合并——一章一个单元，太长才在章内再切。
    """
    raw = path.read_text(encoding="utf-8-sig", errors="replace")

    out: list[dict] = []
    base = datetime.now().astimezone()
    idx = 0

    chapters = re.split(r"\n\s*(?=第[0-9一二三四五六七八九十百千]+[章节回])", raw)
    for chapter in chapters:
        paras = [re.sub(r"[ \t]+", " ", p).strip()
                 for p in re.split(r"\n\s*\n", chapter)]
        paras = [p for p in paras if len(p) >= 30]
        if not paras:
            continue
        buf = ""
        chunks: list[str] = []
        for p in paras:
            if not buf:
                buf = p
            elif len(buf) + len(p) + 1 <= 420:
                buf += "\n" + p
            else:
                chunks.append(buf)
                buf = p
        if buf:
            chunks.append(buf)
        for text in chunks:
            out.append({
                "at": base.isoformat(timespec="seconds"),
                "content": text,
                "summary": _first_sentence(text),
                "emotion": "静",
                "event": f"读《{path.stem}》",
                "status": "detail",
                "unresolved": 0,
                "origin": "reading",
            })
            idx += 1

    return out


# 先试「日期 + 时间 + 名字: 内容」，失败再试「名字: 内容」。
# 一次写成一个可选整体会因为回溯把「2026-11-01 21」当成人名，所以拆成两条规则。
CHAT_WITH_DATE = re.compile(
    r"^\s*\[?(?P<date>\d{4}[-/]\d{1,2}[-/]\d{1,2})"
    r"(?:[ T]+(?P<time>\d{1,2}:\d{2}(?::\d{2})?))?\]?\s+"
    r"(?P<who>[^:：]{1,20})\s*[:：]\s*(?P<text>.+)$"
)
CHAT_PLAIN = re.compile(r"^\s*(?P<who>[^:：]{1,20})\s*[:：]\s*(?P<text>.+)$")


def chat_items(path: Path) -> list[dict]:
    """把「名字: 内容」格式的聊天记录拆成记忆。

    支持行首带日期/时间。认不出格式的行会被跳过，不会瞎猜。
    """
    out: list[dict] = []
    skipped = 0
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if not line.strip():
            continue
        m = CHAT_WITH_DATE.match(line)
        if m:
            who = m.group("who").strip()
            text = m.group("text").strip()
            d = m.group("date").replace("/", "-")
            t = m.group("time") or "00:00"
            if len(t) == 5:
                t += ":00"
            at = f"{d}T{t}+08:00"
        else:
            m = CHAT_PLAIN.match(line)
            if not m:
                skipped += 1
                continue
            who = m.group("who").strip()
            text = m.group("text").strip()
            at = _now_iso()
        if not text:
            continue
        out.append({
            "at": at,
            "content": f"{who}：{text}",
            "summary": _first_sentence(text),
            "emotion": "",
            "event": "聊天记录",
            "status": "detail",
            "unresolved": 0,
            "origin": "chat",
        })
    if skipped:
        print(f"  （跳过 {skipped} 行认不出的）")
    return out


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="往记忆图书馆里灌记忆")
    ap.add_argument("--seed", action="store_true", help="导入 seed/experience.json 示例经历")
    ap.add_argument("--file", type=Path, help="导入同格式 JSON")
    ap.add_argument("--novel", type=Path, help="把一本小说/长文拆成记忆")
    ap.add_argument("--chat", type=Path, help="把聊天记录拆成记忆")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--user-id", default="", help="记忆归属，默认空（通用）")
    ap.add_argument("--dry-run", action="store_true", help="只看会写什么，不落库")
    args = ap.parse_args()

    if not any([args.seed, args.file, args.novel, args.chat]):
        ap.print_help()
        return 2

    mems: list[dict] = []
    dias: list[dict] = []

    if args.seed:
        if not SEED_FILE.exists():
            print(f"找不到 {SEED_FILE}")
            return 1
        m, d = load_json_items(SEED_FILE)
        mems += m
        dias += d
        print(f"示例经历：{len(m)} 条记忆，{len(d)} 篇日记")

    if args.file:
        m, d = load_json_items(args.file)
        mems += m
        dias += d
        print(f"{args.file.name}：{len(m)} 条记忆，{len(d)} 篇日记")

    if args.novel:
        m = novel_items(args.novel)
        mems += m
        print(f"{args.novel.name}：切成 {len(m)} 条记忆")

    if args.chat:
        m = chat_items(args.chat)
        mems += m
        print(f"{args.chat.name}：解析出 {len(m)} 条记忆")

    if not mems and not dias:
        print("没有可导入的内容")
        return 1

    if args.dry_run:
        print("\n--- 前 5 条预览 ---")
        for it in mems[:5]:
            print(f"[{str(it.get('at'))[:16]}] {str(it.get('event') or '')} | "
                  f"{str(it.get('summary') or '')[:50]}")
        print(f"\n合计：记忆 {len(mems)} 条，日记 {len(dias)} 篇（dry-run，未写入）")
        return 0

    con = _connect(args.db)
    written_m, written_d = 0, 0
    skipped_m, skipped_d = 0, 0
    try:
        with con:
            for it in mems:
                status = str(it.get("status") or "detail")
                if status not in VALID_STATUS:
                    status = "detail"
                ok = _insert_book(
                    con,
                    user_id=args.user_id,
                    content=str(it.get("content") or ""),
                    summary=str(it.get("summary") or _first_sentence(it.get("content") or "")),
                    emotion=str(it.get("emotion") or ""),
                    at=str(it.get("at") or _now_iso()),
                    event=str(it.get("event") or "往事"),
                    status=status,
                    unresolved=bool(it.get("unresolved")),
                    origin=str(it.get("origin") or "seed"),
                    decay_rate=float(it.get("decay_rate") or 1.0),
                )
                if ok:
                    written_m += 1
                else:
                    skipped_m += 1
            for it in dias:
                ok = _insert_diary(
                    con,
                    user_id=args.user_id,
                    content=str(it.get("content") or ""),
                    at=str(it.get("at") or _now_iso()),
                    origin=str(it.get("origin") or "seed"),
                )
                if ok:
                    written_d += 1
                else:
                    skipped_d += 1
    finally:
        con.close()

    print(f"\n写入完成：记忆 {written_m} 条、日记 {written_d} 篇 → {args.db}")
    if skipped_m or skipped_d:
        print(f"已存在跳过：记忆 {skipped_m} 条、日记 {skipped_d} 篇")
    print("建议接着跑：python tools/backfill_embeddings.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
