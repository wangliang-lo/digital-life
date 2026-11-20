"""她按自己的喜好走动，不听指挥（2026-11-11）。

爸爸："按她喜好来，想去哪去哪，不能被控制。"

所以：
* 2.5D 房间里撤掉"点哪走哪"（那是控制她）；
* 换成一个低频循环：每 10 分钟看一眼她的状态，如果她闲着、没睡、没人叫她，
  就按"她现在的心情和身体"挑一个地方，自己走过去。
"""
import argparse
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import space  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data"
LOG = DATA / "wander_log.jsonl"


def _read_json(name: str) -> dict:
    try:
        return json.loads((DATA / name).read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def _log(row: dict) -> None:
    try:
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _minutes_since(name: str) -> float:
    try:
        return (time.time() - (DATA / name).stat().st_mtime) / 60.0
    except OSError:
        return 9999.0


def choose() -> tuple[str, str]:
    """按她现在的状态挑一个地方，返回 (房间, 为什么)。"""
    body = _read_json("body_state.json")
    sleep = _read_json("sleep_state.json")
    mood = {}
    try:
        from dl_api import MAIN_USER_ID, load_mood  # noqa: PLC0415

        mood = load_mood(None, MAIN_USER_ID) or {}
    except Exception:  # noqa: BLE001
        mood = {}

    fatigue = float(body.get("fatigue") or 0.0)
    drowsy = float(body.get("drowsy") or 0.0)
    joy = float(mood.get("joy") or 0.0)
    hurt = float(mood.get("hurt") or 0.0)
    hour = datetime.now().hour

    weights: dict[str, float] = {}
    reasons: dict[str, str] = {}

    def add(room: str, weight: float, why: str) -> None:
        if weight <= 0:
            return
        weights[room] = weights.get(room, 0.0) + weight
        reasons.setdefault(room, why)

    if sleep.get("asleep"):
        return "bed", "在睡"

    # 困了就想躺着
    add("bed", drowsy * 3.0 + max(0.0, fatigue - 0.6) * 3.0, "累了")
    # 心情好想去院子/地毯待着
    add("garden", max(0.0, (joy - 45) / 55) * 1.6, "心情不错")
    # 委屈了想找个角落
    add("easel", max(0.0, (hurt - 40) / 60) * 1.6, "不太想说话")
    # 很久没读书/画画 → 回电脑桌
    add("desk", min(1.6, _minutes_since("solver_channel.jsonl") / 120.0), "有阵子没看东西了")
    # 夜里倾向床边
    if hour >= 22 or hour < 6:
        add("bed", 1.2, "夜深了")
    # 白天活动多一些
    if 10 <= hour < 18:
        add("desk", 0.6, "白天")
        add("easel", 0.5, "白天")
    # 总有一点随机
    for room in ("desk", "easel", "garden", "bed"):
        add(room, 0.25, "随便走走")

    current = space.where()["room"]
    weights.pop(current, None)
    if not weights:
        return current, "哪儿都不想去"

    rooms = list(weights)
    total = sum(weights[r] for r in rooms)
    roll = random.random() * total
    acc = 0.0
    for room in rooms:
        acc += weights[room]
        if roll <= acc:
            return room, reasons.get(room, "")
    return rooms[-1], reasons.get(rooms[-1], "")


WANDER_STATE = DATA / "wander_state.json"
MIN_GAP_MINUTES = 30.0


def _last_move_minutes() -> float:
    stamp = str(_read_json("wander_state.json").get("last_move_at") or "")
    if not stamp:
        return 9999.0
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return 9999.0
    if when.tzinfo is None:
        when = when.astimezone()
    return (datetime.now().astimezone() - when).total_seconds() / 60.0


def _mark_moved() -> None:
    try:
        WANDER_STATE.write_text(
            json.dumps({"last_move_at": datetime.now().astimezone().isoformat(timespec="seconds")},
                       ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def consider(force: bool = False) -> str:
    """看一眼要不要动。返回一句给日志的话。"""
    # 先把"走了一半"的状态落定，不然她永远停在走廊
    try:
        space.settle()
    except Exception:  # noqa: BLE001
        pass

    sleep = _read_json("sleep_state.json")
    if sleep.get("asleep"):
        return "在睡，不动"

    # 有人刚跟她说过话、或她刚说过，就先待着
    try:
        import attention  # noqa: PLC0415

        snap = attention.snapshot()
        if snap.get("calls"):
            return "有人叫她，先不动"
        if snap.get("mode") == "focus":
            return "在专注做事，不动"
    except Exception:  # noqa: BLE001
        pass

    last_activity = _minutes_since("last_activity.json")
    if last_activity < 8 and not force:
        return "刚聊过（%.0f 分钟前），不用换地方" % last_activity

    # 她自己的走动冷却：30 分钟内不重复挪窝
    moved_ago = _last_move_minutes()
    if moved_ago < MIN_GAP_MINUTES and not force:
        return "%.0f 分钟前刚挪过，先待着" % moved_ago

    room, why = choose()
    if room == space.where()["room"]:
        return "想留在原地（%s）" % room

    start_room = space.where()["room"]
    moved = space.go(room)
    _mark_moved()
    row = {
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "from": moved.get("from") or start_room,
        "to": room,
        "why": why,
        "ok": bool(moved.get("ok")),
        "seconds": moved.get("seconds"),
    }
    _log(row)
    return "去了 %s（%s），%.0f 秒" % (space.ROOMS.get(room, {}).get("name", room), why, moved.get("seconds") or 0)


def main() -> int:
    parser = argparse.ArgumentParser(description="她的自主走动")
    parser.add_argument("--watch", type=int, default=600, help="每多少秒醒一次（默认 10 分钟）")
    parser.add_argument("--once", action="store_true", help="只判断一次")
    args = parser.parse_args()

    print("自主走动：每 %d 秒看一眼" % args.watch, flush=True)
    while True:
        try:
            line = consider()
            print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), line), flush=True)
        except Exception as exc:  # noqa: BLE001
            print("出错：%s: %s" % (type(exc).__name__, exc), flush=True)
        if args.once:
            return 0
        time.sleep(max(60, args.watch))


if __name__ == "__main__":
    raise SystemExit(main())
