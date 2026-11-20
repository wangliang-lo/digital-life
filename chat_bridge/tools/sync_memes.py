#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把梗库和 cnmeme.wiki 同步一次（补新词条 + 更新变过的正文）。

2026-11-04：官方那个 Node 同步脚本在本机写库时报
`Provided value cannot be bound to SQLite parameter 1`（node:sqlite 的绑定坑），
回滚了但没同步成。这里用 Python 重写一遍，逻辑一样：

    python tools/sync_memes.py              # 补新 + 更新
    python tools/sync_memes.py --no-existing  # 只补新
    python tools/sync_memes.py --dry-run    # 只看会改什么

写库前自动备份成 data/memes.db.bak-时间戳。词源 cnmeme.wiki，MIT 词库。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "memes.db"
API = "https://cnmeme.wiki/api/v1/public/memes"
UA = "xiaoyu-memewiki-sync/1.0"

_OPENER_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_OPENER_PROXY = urllib.request.build_opener()


def _get(url: str, timeout: float = 20.0) -> dict:
    last: Exception | None = None
    for attempt in range(4):
        for opener in (_OPENER_DIRECT, _OPENER_PROXY):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with opener.open(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8", "ignore"))
            except Exception as exc:  # noqa: BLE001
                last = exc
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"请求失败：{url} ({last})")


def _split_sections(text: str) -> tuple[str, str, str]:
    """站点的 summary 里混着「来源 … 用法 …」，拆成 summary / origin / usage。

    注意：summary 保留"来源 …"那段（跟老词条的存法一致），只把"用法"切出去。
    第一版把"来源"也切走了，结果新词条的 summary 变成空，查出来没内容。
    """
    body = " ".join(str(text or "").split())
    usage = ""
    if "用法" in body:
        body, _, tail = body.partition("用法")
        usage = tail.strip(" ：:")
    origin = ""
    if body.startswith("来源"):
        origin = body[2:].strip(" ：:")
    return body.strip(), origin, usage


def _aliases(item: dict) -> str:
    values: list[str] = []
    for key in ("aliases", "keywords"):
        for value in item.get(key) or []:
            text = str(value or "").strip()
            if text and text not in values:
                values.append(text)
    return ",".join(values[:20])


def _tags(item: dict) -> str:
    return ",".join(str(t).strip() for t in (item.get("tags") or []) if str(t).strip())


def sync(no_existing: bool = False, dry_run: bool = False) -> int:
    con = sqlite3.connect(DB)
    try:
        local = {str(row[0]): (str(row[1] or ""), str(row[2] or ""))
                 for row in con.execute("select title, summary, updated_at from memes")} \
            if _has_updated_at(con) else \
            {str(row[0]): (str(row[1] or ""), "") for row in con.execute("select title, summary from memes")}
        total_before = len(local)
        print(f"本地词库 {total_before} 条", flush=True)

        added: list[tuple] = []
        changed = 0
        offset = 0
        seen = 0
        while True:
            # 站点只认 offset/limit（page 参数会被忽略，第一次试的时候就是这么死循环的）
            data = _get(f"{API}?offset={offset}&limit=20")
            items = data.get("items") or []
            if not items:
                break
            for item in items:
                title = str(item.get("title") or item.get("slug") or "").strip()
                if not title:
                    continue
                seen += 1
                summary, origin, usage = _split_sections(item.get("summary") or "")
                updated = str(item.get("updated_at") or "")
                if title in local:
                    if no_existing:
                        continue
                    old_summary, old_updated = local[title]
                    if updated and old_updated and updated <= old_updated:
                        continue
                    if summary and len(summary) > len(old_summary):
                        changed += 1
                        if not dry_run:
                            con.execute(
                                "update memes set summary=?, origin=?, usage=?, tags=?, aliases=?, slug=? "
                                "where title=?",
                                (summary[:2000], origin[:600], usage[:1200], _tags(item),
                                 _aliases(item), str(item.get("slug") or title), title),
                            )
                            _touch_updated(con, title, updated)
                    continue
                added.append((
                    title, str(item.get("slug") or title), _tags(item), usage[:1200],
                    origin[:600], "", summary[:2000], _aliases(item),
                ))
            offset += len(items)
            if not data.get("has_more"):
                break
            if offset and offset % 400 == 0:
                print(f"  已翻 {offset} 条", flush=True)
            time.sleep(0.1)

        print(f"站点共 {seen} 条：新增 {len(added)}，更新 {changed}", flush=True)
        if dry_run:
            print("（dry-run，没有写库）", flush=True)
            return 0
        if not added and not changed:
            print("没有变化", flush=True)
            return 0

        backup = DB.with_suffix(f".db.bak-{datetime.now():%Y%m%d%H%M%S}")
        shutil.copy2(DB, backup)
        print(f"已备份：{backup.name}", flush=True)
        try:
            with con:
                for row in added:
                    con.execute(
                        "insert or replace into memes (title, Slug, tags, usage, origin, other, summary, aliases) "
                        "values (?,?,?,?,?,?,?,?)",
                        row,
                    )
            _set_meta(con, "built_at", datetime.now().astimezone().isoformat(timespec="seconds"))
            _set_meta(con, "count", str(total_before + len(added)))
            con.commit()
        except Exception:
            con.rollback()
            raise
        print(f"完成：{total_before} → {_count(con)} 条", flush=True)
        return 0
    finally:
        con.close()


def _count(con: sqlite3.Connection) -> int:
    return int(con.execute("select count(*) from memes").fetchone()[0])


def _has_updated_at(con: sqlite3.Connection) -> bool:
    return any(str(row[1]) == "updated_at" for row in con.execute("pragma table_info(memes)"))


def _touch_updated(con: sqlite3.Connection, title: str, updated: str) -> None:
    if _has_updated_at(con) and updated:
        con.execute("update memes set updated_at=? where title=?", (updated, title))


def _set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    try:
        con.execute("create table if not exists meta (key text primary key, value text)")
        con.execute("insert into meta (key, value) values (?, ?) on conflict(key) do update set value=excluded.value",
                    (key, value))
    except Exception:  # noqa: BLE001
        pass


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = set(argv or sys.argv[1:])
    return sync(no_existing="--no-existing" in args, dry_run="--dry-run" in args)


if __name__ == "__main__":
    raise SystemExit(main())
