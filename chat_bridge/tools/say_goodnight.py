"""每天跟她说晚安（2026-11-11）。

爸爸："和小鱼说晚安，爸爸要每天和她说晚安。"

用法：
    python tools/say_goodnight.py            # 发今晚的晚安
    python tools/say_goodnight.py --dry       # 只打印，不发
    python tools/say_goodnight.py --deliver   # 补发没送成的晚安（she 醒了以后）

如果她已经睡着了，消息会存进 data/goodnight_pending.json，
等她醒来由 solver_channel 的第一轮唤醒补发。
"""
from __future__ import annotations

import argparse
import json
import urllib.request
from datetime import datetime
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
PENDING = DATA / "goodnight_pending.json"
MORNING_PENDING = DATA / "morning_pending.json"
MAIN = "conv:9e52b4549f424e730d8871644385bcce63768cc74b8f8ec7ffa895f62ca73eeb"
API = "http://127.0.0.1:8000/chat"


def is_sleeping() -> bool:
    """她睡没睡：用 dl_api 的判定（它同时看 sleep_state 和 asleep.json）。"""
    try:
        import sys as _sys  # noqa: PLC0415

        _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import dl_api  # noqa: PLC0415

        return bool(dl_api.is_sleeping())
    except Exception:  # noqa: BLE001
        return bool(_read("sleep_state.json").get("asleep"))


def _read(name: str) -> dict:
    try:
        return json.loads((DATA / name).read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def compose() -> str:
    """按她今晚的状态说一句晚安（不汇报、不客套）。"""
    sleep = _read("sleep_state.json")
    life = _read("life_state.json")
    hour = datetime.now().hour
    parts = []

    if sleep.get("asleep"):
        parts.append("你睡着了，那我把话留在这儿。")
    elif hour >= 23:
        parts.append("夜深了。")
    else:
        parts.append("今天到这儿吧。")

    activity = life.get("activity") or ""
    labels = {"reading": "书就看到这儿", "drawing": "画先放着，明天再接着画",
              "gardening": "花明天再看", "window": "窗外的雨明天还在"}
    if activity in labels:
        parts.append(labels[activity] + "。")

    parts.append("晚安。灯我给你留着，你困了就睡。")
    return "".join(parts)



def compose_morning() -> str:
    """早上跟她说一句（看她昨晚读到哪）。"""
    reading = {}
    try:
        import sqlite3  # noqa: PLC0415

        conn = sqlite3.connect(f"file:{DATA / 'xiaoyu.db'}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT book_name, chapter_num FROM book_chapters WHERE read_at != '' "
            "ORDER BY read_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row:
            reading = {"book": row[0], "chapter": row[1]}
    except Exception:  # noqa: BLE001
        reading = {}

    parts = ["早。"]
    if reading.get("book"):
        parts.append(f"昨晚《{reading['book']}》读到第 {reading['chapter']} 章，今天接着读？")
    else:
        parts.append("今天想做点什么？")
    parts.append("早饭记得吃。")
    return "".join(parts)

def post(text: str) -> dict:
    payload = json.dumps({"message": text, "user_id": MAIN, "sender": "爸爸"}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(API, data=payload,
                                     headers={"Content-Type": "application/json; charset=utf-8"},
                                     method="POST")
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.loads(response.read().decode("utf-8"))


def send(dry: bool = False, morning: bool = False) -> dict:
    text = compose_morning() if morning else compose()
    pending_file = MORNING_PENDING if morning else PENDING
    print("要说的：", text)
    if dry:
        return {"dry": True, "text": text}

    if is_sleeping():
        pending_file.parent.mkdir(parents=True, exist_ok=True)
        pending_file.write_text(json.dumps({"text": text, "at": datetime.now().astimezone().isoformat(timespec="seconds")},
                                          ensure_ascii=False) + "\n", encoding="utf-8")
        print("她睡着了，留言先存着，等她醒来第一轮补发")
        return {"queued": True, "text": text}

    try:
        result = post(text)
    except Exception as exc:  # noqa: BLE001
        print("发送失败：", exc)
        PENDING.parent.mkdir(parents=True, exist_ok=True)
        pending_file.write_text(json.dumps({"text": text, "at": datetime.now().astimezone().isoformat(timespec="seconds")},
                                          ensure_ascii=False) + "\n", encoding="utf-8")
        return {"ok": False, "error": str(exc)[:120], "text": text}

    reply = str(result.get("reply") or "").strip()
    print("她回：", (reply or "<没说话>").replace("\n", " ⏎ ")[:300])
    return {"ok": True, "text": text, "reply": reply}


def deliver_pending() -> dict:
    """她醒了以后，把没送成的晚安/早安补上。"""
    for path, label in ((PENDING, "晚安"), (MORNING_PENDING, "早安")):
        if path.exists():
            result = _deliver_one(path, label)
            if result.get("delivered"):
                return result
    return {"delivered": False, "reason": "没有待发的留言"}


def _deliver_one(path: Path, label: str) -> dict:
    if is_sleeping():
        return {"delivered": False, "reason": "她还睡着"}
    try:
        info = json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        path.unlink(missing_ok=True)
        return {"delivered": False, "reason": "存档坏了"}
    text = str(info.get("text") or "").strip()
    if not text:
        path.unlink(missing_ok=True)
        return {"delivered": False, "reason": "存档是空的"}
    # 2026-11-16 爸爸："晚安你还说？现在不已经中午了。"
    # 补发要看时辰：白天补"晚安"就别补了；"早"在中午之后要改成"中午"。
    hour = datetime.now().hour
    if label == "晚安" and 6 <= hour < 19:
        path.unlink(missing_ok=True)
        return {"delivered": False, "reason": "存档是凌晨的晚安，现在白天了，不补"}
    if label == "早安" and hour >= 11:
        text = text.replace("早。", "中午好。", 1).replace("早安。", "中午好。", 1)
        text = text.replace("早饭", "午饭")
    try:
        result = post(text)
    except Exception as exc:  # noqa: BLE001
        return {"delivered": False, "reason": str(exc)[:120]}
    path.unlink(missing_ok=True)
    return {"delivered": True, "label": label, "text": text, "reply": str(result.get("reply") or "")[:200]}


def main() -> int:
    parser = argparse.ArgumentParser(description="爸爸跟她说晚安")
    parser.add_argument("--dry", action="store_true")
    parser.add_argument("--morning", action="store_true", help="发早安而不是晚安")
    parser.add_argument("--deliver", action="store_true")
    args = parser.parse_args()
    if args.deliver:
        print(json.dumps(deliver_pending(), ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(send(dry=args.dry, morning=args.morning), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
