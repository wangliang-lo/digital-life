#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按真实互动给每个联系人算好感度（0-100），存进 xiaoyu.db 的 affection 表。

    python tools/affection.py            # 常驻，每 10 分钟结算一次
    python tools/affection.py --once

规则：
- 收到消息 +0.5/条（愿意花时间陪你）
- 她回复 +0.8/次（愿意理你）
- 暖话（想你/谢谢/喜欢/乖/辛苦）+1.5
- 冷话（烦/滚/闭嘴/笨/恶心）-2.0
- 24 小时没互动，往 50 回落 0.5
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "xiaoyu.db"
STATE = ROOT / "data" / "affection_state.json"
EVENTS = Path.home() / ".dsh" / "profiles" / "desktop" / "node_modules" / "dsh-douyin-browser-local" / "state" / "chat-events.jsonl"

WARM = ("想你", "谢谢", "喜欢你", "乖", "辛苦", "抱抱", "晚安", "宝贝")
COLD = ("烦", "滚", "闭嘴", "笨", "恶心", "别烦")


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB)
    con.execute(
        "create table if not exists affection (user_id text primary key, value real not null, updated_at text, last_event_at text)"
    )
    con.commit()
    return con


def get_value(con: sqlite3.Connection, uid: str) -> tuple[float, str]:
    row = con.execute("select value, last_event_at from affection where user_id=?", (uid,)).fetchone()
    if not row:
        con.execute("insert or replace into affection(user_id, value, updated_at, last_event_at) values(?,?,?,?)",
                    (uid, 50.0, datetime.now().isoformat(timespec="seconds"), None))
        con.commit()
        return 50.0, ""
    return float(row[0]), str(row[1] or "")


def bump(con: sqlite3.Connection, uid: str, delta: float, at: str) -> None:
    value, _ = get_value(con, uid)
    value = max(5.0, min(100.0, value + delta))
    con.execute("update affection set value=?, updated_at=?, last_event_at=? where user_id=?",
                (round(value, 2), datetime.now().isoformat(timespec="seconds"), at, uid))
    con.commit()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=600)
    args = parser.parse_args()

    while True:
        try:
            lines = EVENTS.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        try:
            state = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            state = {"offset": 0}
        offset = int(state.get("offset") or 0)
        if offset > len(lines):
            offset = 0
        con = connect()
        for line in lines[offset:]:
            try:
                row = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            uid = row.get("recipientId")
            if not uid or not str(uid).startswith("conv:"):
                continue
            at = str(row.get("at") or "")
            if row.get("type") == "message.received":
                for msg in row.get("messages") or []:
                    text = str(msg.get("text") or "")
                    bump(con, uid, 0.1, at)
                    if any(word in text for word in WARM):
                        bump(con, uid, 1.0, at)
                    if any(word in text for word in COLD):
                        bump(con, uid, -3.0, at)
            elif row.get("type") == "reply.sent":
                bump(con, uid, 0.15, at)
        # 长期没互动就慢慢回落
        now = datetime.now()
        for uid, value, last in con.execute("select user_id, value, last_event_at from affection").fetchall():
            if not last:
                continue
            try:
                ts = datetime.fromisoformat(last.replace("Z", ""))
            except Exception:  # noqa: BLE001
                continue
            if (now - ts).total_seconds() > 86400 and value > 50:
                bump(con, uid, -0.5, last)
        con.close()
        STATE.write_text(json.dumps({"offset": len(lines)}, ensure_ascii=False), encoding="utf-8")
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
