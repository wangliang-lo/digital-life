#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把抖音助手学到的东西，反向写回数字生命的「图书馆」。

抖音助手把看过的作品、记下的事写在
`~/.dsh/douyin-browser/memory/shared.sqlite` 的 `memory_entries` 里。
这个脚本把 source != 'digital-life' 的条目（也就是它自己看的、记的）写进
`data/xiaoyu.db` 的 `books`，让「她刷抖音看到的东西」也变成她的记忆。

    python tools/sync_from_douyin.py                 # 同步一次
    python tools/sync_from_douyin.py --watch 300     # 每 5 分钟增量
    python tools/sync_from_douyin.py --user douyin   # 写到指定 user_id 名下

写进去的记忆用较低的 mv（约 20-40），交给 digital_life 自己的三阶遗忘去衰减：
不重要的会先变成摘要、再变幽灵、最后消失。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))

from digital_life import LibraryMemory, short_text  # noqa: E402

DEFAULT_DIGITAL_DB = ROOT / "data" / "xiaoyu.db"
DEFAULT_SHARED_DB = Path.home() / ".dsh" / "douyin-browser" / "memory" / "shared.sqlite"
STATE_FILE = ROOT / "data" / ".douyin_reverse_sync.json"


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"entries": {}}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def read_entries(shared_db: Path, limit: int) -> list[dict]:
    con = sqlite3.connect(f"file:{shared_db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT id, kind, content, source, title, author, url, metadata_json, updated_at
            FROM memory_entries
            WHERE source != 'digital-life' AND kind IN ('video','note')
            ORDER BY updated_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()


def parse_meta(raw: str) -> dict:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def build_text(entry: dict) -> tuple[str, str, str, float]:
    """返回 (content, summary, emotion_tag, mv)。"""
    title = str(entry.get("title") or "").strip()
    author = str(entry.get("author") or "").strip()
    body = str(entry.get("content") or "").strip()
    if title and author:
        # 标题常以「@作者·」开头，去掉避免重复
        title = title.lstrip()
        for prefix in (f"@{author}·", f"@{author}：", f"@{author}:", f"@{author}"):
            if title.startswith(prefix):
                title = title[len(prefix):].strip()
                break
    meta = parse_meta(str(entry.get("metadata_json") or ""))
    signal = str(meta.get("signal") or "").lower()
    playback = meta.get("playback") if isinstance(meta.get("playback"), dict) else {}
    duration = float(playback.get("duration") or 0)
    position = float(playback.get("time") or 0)
    ratio = (position / duration) if duration > 0 else 0.0

    if title:
        content = f"我刷到 @{author or '抖音用户'} 的视频：{title}"
        if entry.get("url"):
            content += f"\n{entry['url']}"
        if body and body not in title:
            content += f"\n印象：{body[:120]}"
        summary = f"刷到 @{author or '抖音用户'}：{short_text(title, 30)}"
    else:
        content = f"我在抖音记下的事：{body[:300]}"
        summary = f"抖音记忆：{short_text(body, 30)}"

    emotion = "平静"
    mv = 20.0
    if signal == "like":
        emotion, mv = "温暖", 30.0
    elif signal == "dislike":
        emotion, mv = "失落", 16.0
    if ratio >= 0.6:
        mv += 6.0
    mv = min(45.0, mv)
    return content, summary, emotion, mv


def _entry_version(updated_at: object) -> int:
    """把 updated_at 变成单调递增的版本号（秒级时间戳），旧记录没有时间就退回 1。"""
    raw = str(updated_at or "").strip()
    if not raw:
        return 1
    try:
        return max(1, int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()))
    except (ValueError, TypeError, OSError):
        return 1



def sync(digital_db: Path, shared_db: Path, user_id: str, limit: int) -> int:
    entries = read_entries(shared_db, limit)
    if not entries:
        print("抖音助手那边没有可同步的条目")
        return 0

    state = load_state()
    seen: dict = state.setdefault("entries", {})
    lib = LibraryMemory(digital_db, user_id=user_id)
    added = 0
    try:
        existing = {str(row["summary"]) for row in lib.list_books()}
        for entry in entries:
            key = f"{entry['id']}:{entry['updated_at']}"
            if key in seen:
                continue
            content, summary, emotion, mv = build_text(entry)
            if len(summary) < 4:
                seen[key] = 0
                continue
            if summary in existing:
                seen[key] = 0
                continue
            book = lib.add_book(
                content=content,
                summary=summary,
                emotion_tag=emotion,
                folder_date=datetime.now().date().isoformat(),
                folder_user=str(entry.get("author") or "抖音"),
                folder_event="日常",
                mv=mv,
                status="detail",
                decay_rate=1.0,
                # 溯源：同一条抖音记录改写后（summary 不变、updated_at 变新）用更大的
                # version 覆盖旧正文，避免过时的说法一直被她当成事实。
                origin="douyin_sync",
                source_id=str(entry.get("id") or ""),
                version=_entry_version(entry.get("updated_at")),
            )
            seen[key] = int(book["id"])
            existing.add(summary)
            added += 1
            print(f"+ [{mv:.0f}mv] {summary}")
        state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        save_state(state)
    finally:
        lib.close()
    print(f"反向同步完成：新增 {added} 条（user_id={user_id}）")
    return added


def main() -> int:
    parser = argparse.ArgumentParser(description="抖音助手 → 数字生命 反向记忆同步")
    parser.add_argument("--digital-db", default=str(DEFAULT_DIGITAL_DB))
    parser.add_argument("--shared-db", default=str(DEFAULT_SHARED_DB))
    parser.add_argument("--user", default="douyin-assistant", help="写在哪个 user_id 名下")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--watch", type=int, default=0)
    args = parser.parse_args()

    digital_db = Path(args.digital_db)
    shared_db = Path(args.shared_db)
    if not shared_db.exists():
        print(f"找不到抖音助手的共享记忆库：{shared_db}")
        return 2
    if not digital_db.exists():
        print(f"找不到数字生命数据库：{digital_db}")
        return 2

    if args.watch <= 0:
        sync(digital_db, shared_db, args.user, args.limit)
        return 0

    print(f"每 {args.watch}s 反向同步一次，Ctrl+C 退出")
    while True:
        try:
            sync(digital_db, shared_db, args.user, args.limit)
        except Exception as exc:  # noqa: BLE001
            print("同步失败：", exc)
        time.sleep(max(5, args.watch))


if __name__ == "__main__":
    raise SystemExit(main())
