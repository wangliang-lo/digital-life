"""中枢视角：她现在在哪、在干什么、还剩多久、手上有什么（2026-11-11）。

爸爸："你是中枢系统，应该对她正在哪个方位、在做什么了如指掌。"

这个脚本把散在各处的状态拼成一张"现场报告"：
  space       她在哪个房间、到这儿多久、是不是在走
  life_loop   手头在做什么活动、还剩几分钟
  attention   有没有人叫她、刚才被打断过什么
  reading     读到哪本书第几章、下一章是什么
  body/mood   疲劳、困、开心、委屈
  sleep       睡没睡、睡了多久
  channel     最后一条留言 / 最后一句话
"""
from __future__ import annotations

import argparse
import json
import sys
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))

import attention  # noqa: E402
import space  # noqa: E402


def _read(name: str) -> dict[str, Any]:
    try:
        return json.loads((DATA / name).read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def _minutes_since(stamp: str) -> float | None:
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.astimezone()
    return (datetime.now().astimezone() - when).total_seconds() / 60.0


def reading_state() -> dict[str, Any]:
    """读的是哪本、第几章、下一章。"""
    db = DATA / "xiaoyu.db"
    if not db.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT book_name, chapter_num, read_at FROM book_chapters WHERE read_at != '' "
            "ORDER BY read_at DESC LIMIT 200"
        ).fetchall()
        conn.close()
    except Exception:  # noqa: BLE001
        return {}
    if not rows:
        return {}
    latest_book, latest_chapter, latest_at = rows[0]
    unread = 0
    total = 0
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        total = conn.execute(
            "SELECT count(*) FROM book_chapters WHERE book_name = ?", (latest_book,)
        ).fetchone()[0]
        unread = conn.execute(
            "SELECT count(*) FROM book_chapters WHERE book_name = ? AND read_at = ''", (latest_book,)
        ).fetchone()[0]
        conn.close()
    except Exception:  # noqa: BLE001
        pass
    return {
        "book": latest_book,
        "chapter": latest_chapter,
        "read_at": latest_at,
        "minutes_ago": _minutes_since(latest_at),
        "read": total - unread,
        "total": total,
    }


def last_channel() -> str:
    path = DATA / "solver_channel.jsonl"
    if not path.exists():
        return ""
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except Exception:  # noqa: BLE001
        return ""
    for row in reversed(rows):
        if row.get("type") == "message" and row.get("from") == "xiaoyu":
            return f"{row.get('at')} {str(row.get('text') or '')[:60]}"
    return ""


def snapshot() -> dict[str, Any]:
    position = space.sync_from_activity()
    settle = space.settle()
    attention_state = attention.snapshot()
    life = _read("life_state.json")
    body = _read("body_state.json")
    sleep = _read("sleep_state.json")
    room = settle.get("room") or position.get("room")

    remaining = None
    ends_at = str(life.get("ends_at") or "")
    if ends_at:
        left = _minutes_since(ends_at)
        if left is not None:
            remaining = -left          # 正数=还剩几分钟

    return {
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "room": room,
        "room_name": settle.get("name") or position.get("name"),
        "moving": bool(position.get("moving")),
        "moving_to": position.get("to"),
        "arrive_in": position.get("arrive_in"),
        "minutes_here": round(space.minutes_here(), 1),
        "activity": life.get("activity"),
        "activity_label": life.get("label"),
        "activity_since": life.get("since"),
        "activity_remaining_min": round(remaining, 1) if remaining is not None else None,
        "attention": attention_state,
        "reading": reading_state(),
        "body": {key: body.get(key) for key in ("fatigue", "drowsy", "energy", "hunger") if key in body},
        "asleep": bool(sleep.get("asleep")),
        "sleep_hours": sleep.get("hours"),
        "last_channel": last_channel(),
    }


def render(data: dict[str, Any]) -> str:
    lines = []
    where = data.get("room_name") or data.get("room")
    if data.get("moving"):
        lines.append(f"【位置】{where}（正走去{data.get('moving_to')}，还有 {data.get('arrive_in')} 秒）")
    else:
        lines.append(f"【位置】{where}　已待 {data.get('minutes_here')} 分钟")
    if data.get("asleep"):
        lines.append("【状态】睡着")
    else:
        remaining = data.get("activity_remaining_min")
        tail = f"，还剩 {remaining} 分钟" if remaining is not None else ""
        lines.append(f"【在做】{data.get('activity_label') or data.get('activity') or '没安排'}{tail}")
    attention_state = data.get("attention") or {}
    if attention_state.get("mode") and attention_state["mode"] != "idle":
        lines.append(f"【注意力】{attention_state.get('mode')}：{attention_state.get('target')}")
    if attention_state.get("calls"):
        lines.append(f"【有人叫】{attention_state['calls']} 次")
    reading = data.get("reading") or {}
    if reading.get("book"):
        ago = reading.get("minutes_ago")
        ago_text = f"，{ago:.0f} 分钟前" if isinstance(ago, (int, float)) else ""
        lines.append(f"【读书】《{reading['book']}》第 {reading['chapter']} 章"
                     f"（{reading.get('read')}/{reading.get('total')}）{ago_text}")
    body = data.get("body") or {}
    if body:
        lines.append("【身体】" + "、".join(f"{k} {v}" for k, v in body.items() if v is not None))
    if data.get("last_channel"):
        lines.append(f"【最后留言】{data['last_channel']}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="她此刻在哪、在干什么")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    data = snapshot()
    print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else render(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
