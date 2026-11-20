"""她想干什么，记成一条"意图"，让生活循环去执行（2026-11-12）。

爸爸："她想去休息，地理位置就去床上。"

她在信道/对话里说"歇会儿""发呆""不读了"，写一条意图；
life_loop 每 20 秒读一次，把活动和房间一起改过去。

意图只活 30 分钟：说过了就去做了，不会一直挂着。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

DATA = Path(__file__).resolve().parent / "data"
STATE = DATA / "intent.json"

# 顺序有讲究：先判断"想歇"，再判断"想读"，不然"不读了，歇会儿"会被当成读书
PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("rest", ("歇会儿", "歇一会", "歇一歇", "发呆", "躺一会儿", "躺一会", "躺下", "想躺",
              "不读了", "不想读", "休息", "缓一缓", "缓缓", "脑子还热", "脑子热", "累得",
              "去睡", "睡觉", "安静一会", "安静一会儿")),
    ("draw", ("画画", "画点什么", "去画", "起个稿", "涂两笔")),
    ("garden", ("院子", "弄花", "浇水", "看植物", "窗台")),
    ("read", ("去读", "接着读", "接着往下读", "翻下一本", "读下一章", "把下一章读",
              "读一章", "再看一章", "再读", "读会儿", "读会", "读书", "看书",
              "想翻", "翻一章", "把书拿起来")),
]


def note(text: str, source: str = "") -> dict[str, Any] | None:
    """从她说的话里认出一条意图。认不出就不写。"""
    sample = str(text or "").strip()
    if not sample:
        return None
    chosen = ""
    for kind, words in PATTERNS:
        if any(word in sample for word in words):
            chosen = kind
            break
    if not chosen:
        return None
    payload = {
        "kind": chosen,
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": str(source)[:40],
        "text": sample[:80],
    }
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        return None
    return payload


def recent(minutes: float = 30.0) -> dict[str, Any]:
    """读还没过期的意图；顺手把过期的清掉。"""
    try:
        payload = json.loads(STATE.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}
    stamp = str(payload.get("at") or "")
    if not stamp:
        return {}
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return {}
    if when.tzinfo is None:
        when = when.astimezone()
    age = (datetime.now().astimezone() - when).total_seconds() / 60.0
    if age > minutes:
        clear()
        return {}
    return payload


def clear() -> None:
    STATE.unlink(missing_ok=True)


def kind_text(kind: str) -> str:
    return {
        "rest": "歇会儿",
        "read": "读书",
        "draw": "画画",
        "garden": "弄植物",
        "up": "起来",
        "wander": "随便走走",
    }.get(str(kind), str(kind))
