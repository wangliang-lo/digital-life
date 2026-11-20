"""小鱼的空间：她在哪、那儿能做什么、走过去要多久（2026-11-11）。

爸爸的定调：
* 不是演示，不要 60 秒——一个小房间，几步就到（3~8 秒）；
* 跟人说话在**电脑桌**，不是在门口；
* 她在一个房间，能力跟着房间走：在画架就只能画，在电脑桌才能说话、读书、查东西。

这个模块只管"位置与许可"，不调模型、不碰记忆。
"""
from __future__ import annotations

import json
import random
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

DATA = Path(__file__).resolve().parent / "data"
STATE = DATA / "space_state.json"

# 房间与能力。cap 是这一区允许做的事；不在表里的能力就是"这儿没有"。
ROOMS: dict[str, dict[str, Any]] = {
    "desk": {
        "name": "电脑桌",
        "cap": {"talk", "read", "search", "write", "channel", "game"},
        "display": "坐在电脑桌前",
    },
    "easel": {
        "name": "画架",
        "cap": {"draw", "look_at_art"},
        "display": "站在画架前",
    },
    "garden": {
        "name": "院子",
        "cap": {"garden", "rest_outside"},
        "display": "蹲在院子里",
    },
    "bed": {
        "name": "床",
        "cap": {"sleep", "dream", "recall"},
        "display": "躺在床上",
    },
    "hall": {
        "name": "走廊",
        "cap": set(),          # 走路时什么都干不了，只能应付一句短话
        "display": "正走过去",
    },
}

# 小房间：走过去几秒。对称，无向。
EDGES: dict[tuple[str, str], float] = {
    ("desk", "easel"): 4.0,
    ("desk", "garden"): 7.0,
    ("desk", "bed"): 6.0,
    ("easel", "garden"): 6.0,
    ("easel", "bed"): 8.0,
    ("garden", "bed"): 7.0,
}
DEFAULT_TRAVEL = 9.0


def _load() -> dict[str, Any]:
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("room") in ROOMS:
            return data
    except Exception:  # noqa: BLE001
        pass
    return {"room": "desk", "since": datetime.now().astimezone().isoformat(timespec="seconds")}


def _save(data: dict[str, Any]) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def travel_seconds(a: str, b: str) -> float:
    if a == b:
        return 0.0
    return EDGES.get((a, b)) or EDGES.get((b, a)) or DEFAULT_TRAVEL


def _mtime_age_minutes(path: Path) -> float:
    try:
        return (time.time() - path.stat().st_mtime) / 60.0
    except OSError:
        return 9999.0


def _last_read_minutes() -> float:
    """最近一次读书是几分钟前（读 book_chapters 里最新的 read_at）。"""
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % str(DATA / "xiaoyu.db"), uri=True)
        row = conn.execute("select max(read_at) from book_chapters where read_at != ''").fetchone()
        conn.close()
        if row and row[0]:
            stamp = datetime.fromisoformat(str(row[0]))
            if stamp.tzinfo is None:
                stamp = stamp.astimezone()
            return max(0.0, (datetime.now().astimezone() - stamp).total_seconds() / 60.0)
    except Exception:  # noqa: BLE001
        pass
    return 9999.0


def _asleep() -> bool:
    data = _load_state_file()
    return bool(data.get("asleep"))


def _load_state_file() -> dict:
    try:
        return json.loads((DATA / "sleep_state.json").read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def sync_from_activity() -> dict[str, Any]:
    """按她最近真的做了什么，把位置对齐。

    明确动作（go）两分钟内优先；否则从活动推断。
    """
    data = _load()
    manual_at = float(data.get("manual_at") or 0.0)
    if manual_at and time.time() - manual_at < 120:
        return where()

    if _asleep():
        target = "bed"
    else:
        # 2026-11-11：生活循环排在前面——她手头在做什么，就该在哪
        life_room = ""
        try:
            import json as _json  # noqa: PLC0415

            life = _json.loads((DATA / "life_state.json").read_text(encoding="utf-8")) or {}
            if life.get("activity") and life.get("room") in ROOMS:
                life_room = str(life["room"])
        except Exception:  # noqa: BLE001
            life_room = ""

        read_age = _last_read_minutes()
        draw_age = _mtime_age_minutes(DATA / "doodles.jsonl")
        activity_age = _mtime_age_minutes(DATA / "last_activity.json")

        if life_room:
            target = life_room
        elif read_age <= 10:
            target = "desk"
        elif draw_age <= 10:
            target = "easel"
        elif 630 <= int(datetime.now().strftime("%H%M")) <= 2100:
            target = "desk" if activity_age <= 30 else str(data.get("room") or "desk")
        else:
            target = str(data.get("room") or "desk")

    if target in ROOMS and data.get("room") != target:
        data["room"] = target
        data["since"] = datetime.now().astimezone().isoformat(timespec="seconds")
        data["arrive_at"] = 0.0
        _save(data)
    return where()


def minutes_here() -> float:
    info = where()
    try:
        since = datetime.fromisoformat(str(info.get("since") or ""))
        return max(0.0, (datetime.now().astimezone() - since).total_seconds() / 60.0)
    except ValueError:
        return 0.0


def where() -> dict[str, Any]:
    """她现在在哪，以及还要多久到（如果路上）。"""
    data = _load()
    room = str(data.get("room") or "desk")
    arriving = float(data.get("arrive_at") or 0.0)
    left = max(0.0, arriving - time.time()) if arriving else 0.0
    return {
        "room": room,
        "name": ROOMS[room]["name"],
        "display": ROOMS[room]["display"],
        "moving": left > 0,
        "arrive_in": round(left, 1),
        "to": str(data.get("to") or ""),
        "since": data.get("since") or "",
    }


# 她能去的房间（走廊不算目的地）
WANDER_ROOMS = ("desk", "easel", "garden", "bed")

ROOM_SCENERY = {
    "desk": "电脑屏幕亮着，桌角摊着书和一杯凉掉的茶；窗外的光是白天的颜色。",
    "easel": "鲸鱼地毯上摆着画，笔搁在一边，颜料盖子没拧紧。",
    "garden": "几盆绿植靠窗，叶子朝光长；土有点干，哪盆该浇水一眼看得出。",
    "bed": "被子掀着一角，枕头有点塌；床头灯没关。",
    "hall": "中间过道，地板凉，两边是她的房间和院子。",
}


def wander() -> dict[str, Any]:
    """随便走走：挑一个不是当前房间的地方。"""
    here = where()["room"]
    options = [room for room in WANDER_ROOMS if room != here] or list(WANDER_ROOMS)
    dest = random.choice(options)
    return go(dest)


def look_around() -> str:
    """四处看看：把她眼前的样子描述成一句话，记下来给下一轮用。"""
    info = settle()
    room = info["room"]
    hour = datetime.now().hour
    if 6 <= hour < 11:
        light = "早上，光还偏冷"
    elif 11 <= hour < 14:
        light = "正午，屋里最亮"
    elif 14 <= hour < 18:
        light = "下午，光偏暖"
    else:
        light = "天黑了，只有灯亮着"
    scenery = ROOM_SCENERY.get(room, "")
    text = f"{ROOMS[room]['name']}：{scenery}（{light}）"
    data = _load()
    data["last_look"] = text
    data["looked_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    _save(data)
    return text


def recent_look(minutes: float = 10.0) -> str:
    """最近看过的样子（10 分钟内有效）。"""
    data = _load()
    stamp = str(data.get("looked_at") or "")
    if not stamp:
        return ""
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return ""
    age = (datetime.now().astimezone() - (when if when.tzinfo else when.astimezone())).total_seconds() / 60.0
    return str(data.get("last_look") or "") if age <= minutes else ""


def can(capability: str) -> bool:
    """这个能力她现在用得了吗——先看位置，再看是不是在路上。"""
    data = _load()
    if float(data.get("arrive_at") or 0.0) > time.time():
        return capability == "speak_short"      # 路上只能短句
    room = str(data.get("room") or "desk")
    return capability in ROOMS[room]["cap"]


def go(dest: str) -> dict[str, Any]:
    """往某个房间走。返回路程与她到哪儿了。"""
    if dest not in ROOMS:
        return {"ok": False, "error": f"没有这个房间：{dest}"}
    data = _load()
    here = str(data.get("room") or "desk")
    if here == dest:
        return {"ok": True, "room": here, "seconds": 0.0, "already": True}
    seconds = travel_seconds(here, dest)
    now = time.time()
    data.update({
        "room": "hall",
        "manual_at": now,
        "to": dest,
        "from": here,
        "arrive_at": now + seconds,
        "since": datetime.now().astimezone().isoformat(timespec="seconds"),
    })
    _save(data)
    return {"ok": True, "from": here, "to": dest, "seconds": seconds}


def settle() -> dict[str, Any]:
    """到了就把状态落定：hall → 目的地。每轮开始时调一次。"""
    data = _load()
    arrive_at = float(data.get("arrive_at") or 0.0)
    if data.get("room") == "hall" and arrive_at and time.time() >= arrive_at:
        data["room"] = str(data.get("to") or "desk")
        data["arrive_at"] = 0.0
        data["since"] = datetime.now().astimezone().isoformat(timespec="seconds")
        _save(data)
    return where()


def describe() -> str:
    """给她的一句话：你在哪、这儿能干什么。"""
    info = settle()
    if info["moving"]:
        return f"你正走过去{ROOMS[info['to']]['name'] if info['to'] in ROOMS else ''}，还有 {info['arrive_in']:.0f} 秒。"
    room = info["room"]
    caps = ROOMS[room]["cap"]
    labels = {
        "talk": "跟人说话", "read": "读书", "search": "查东西", "write": "写字",
        "channel": "给信道留言", "game": "玩局小游戏", "draw": "画画",
        "look_at_art": "看自己画的东西", "garden": "照看植物", "rest_outside": "在外面待会儿",
        "sleep": "睡觉", "dream": "做梦", "recall": "翻记忆",
    }
    allowed = "、".join(labels[c] for c in sorted(caps)) or "什么都做不了，只能走过去"
    return f"你现在在{ROOMS[room]['name']}。这里能：{allowed}。"
