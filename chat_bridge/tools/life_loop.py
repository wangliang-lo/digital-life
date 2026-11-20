"""她的常驻生活循环：快时钟（2026-11-11）。

之前是"叫醒 → 做一件事 → 沉睡"（solver_channel 固定间隔）。
现在拆成两层：

  快时钟（这个文件，20 秒一跳，不调模型）
      她此刻在干什么、还剩多久、下一件事是什么、要不要换房间
  慢时钟（solver_channel，间隔由她的疲劳/困意决定）
      只有她想说话/想读书时才叫一次模型

快时钟做的事：
  * 按她的状态排一段"生活计划"（读书 → 画画 → 院子 → 歇会儿…）
  * 计划到点就换：真的走房间（space.go），换活动、换专注状态
  * 睡觉时什么都不做；有人叫她 / 在专注时不动
  * 每次变化都写 data/life_events.jsonl，房间页和她的提示词读同一份状态
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import attention  # noqa: E402
import intent  # noqa: E402
import space  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data"
STATE = DATA / "life_state.json"
EVENTS = DATA / "life_events.jsonl"
HEARTBEAT = DATA / "life_heartbeat.json"
HEARTBEAT_SECONDS = 60

# 活动 → 房间、默认时长（分钟）、是否专注
ACTIVITIES: dict[str, dict[str, Any]] = {
    "reading":   {"room": "desk",   "minutes": (25, 55), "focus": False, "label": "读书"},
    "drawing":   {"room": "easel",  "minutes": (15, 35), "focus": True,  "label": "画画"},
    "gardening": {"room": "garden", "minutes": (8, 18),  "focus": False, "label": "弄植物"},
    "window":    {"room": "garden", "minutes": (5, 12),  "focus": False, "label": "看窗外"},
    "resting":   {"room": "bed",    "minutes": (20, 45), "focus": False, "label": "歇会儿"},
    "idle":      {"room": "desk",   "minutes": (6, 15),  "focus": False, "label": "发呆"},
}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def _write_state(state: dict[str, Any]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _event(kind: str, payload: dict[str, Any]) -> None:
    try:
        with EVENTS.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": _now(), "kind": kind, **payload}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _body() -> dict[str, Any]:
    return _read_json(DATA / "body_state.json", {}) or {}


def _sleep() -> dict[str, Any]:
    return _read_json(DATA / "sleep_state.json", {}) or {}


def _mood() -> dict[str, Any]:
    try:
        from dl_api import MAIN_USER_ID, load_mood  # noqa: PLC0415

        return load_mood(None, MAIN_USER_ID) or {}
    except Exception:  # noqa: BLE001
        return {}


def _hours_now() -> int:
    return datetime.now().hour


def choose_activity(current: str = "") -> str:
    """按状态挑下一个活动（不是随便随机：累→歇，委屈→画画，开心→院子）。"""
    body = _body()
    mood = _mood()
    fatigue = float(body.get("fatigue") or 0.0)
    drowsy = float(body.get("drowsy") or 0.0)
    joy = float(mood.get("joy") or 0.0)
    hurt = float(mood.get("hurt") or 0.0)
    hour = _hours_now()

    weights: dict[str, float] = {}

    def add(name: str, weight: float) -> None:
        if weight > 0:
            weights[name] = weights.get(name, 0.0) + weight

    add("reading", 1.4)
    add("drawing", 0.9)
    add("gardening", 0.5)
    add("window", 0.5)
    add("resting", 1.0)
    add("idle", 0.4)

    if drowsy >= 0.6 or fatigue >= 0.75:
        add("resting", 3.0)
        add("reading", -1.0)
    if hurt >= 55:
        add("drawing", 1.5)
        add("window", 1.0)
    if joy >= 65:
        add("gardening", 1.0)
        add("drawing", 0.6)
    if 22 <= hour or hour < 7:
        add("resting", 2.0)
        add("reading", -0.6)
    if 9 <= hour < 18:
        add("reading", 0.6)

    # 别连着两次同一个活动
    if current in weights:
        weights[current] *= 0.35

    pool = {k: v for k, v in weights.items() if v > 0}
    if not pool:
        return "idle"
    names = list(pool)
    total = sum(pool.values())
    roll = random.random() * total
    acc = 0.0
    for name in names:
        acc += pool[name]
        if roll <= acc:
            return name
    return names[-1]


def _start_activity(name: str, state: dict[str, Any], silent: bool = False) -> dict[str, Any]:
    spec = ACTIVITIES[name]
    low, high = spec["minutes"]
    minutes = random.uniform(low, high)
    ends_at = datetime.now().astimezone() + timedelta(minutes=minutes)
    room = spec["room"]
    previous_room = space.where()["room"]
    if room != previous_room:
        space.go(room)
    if spec["focus"]:
        attention.focus(name, spec["label"])
    else:
        attention.idle()

    state.update({
        "activity": name,
        "label": spec["label"],
        "room": room,
        "since": _now(),
        "ends_at": ends_at.isoformat(timespec="seconds"),
    })
    _write_state(state)
    _event("activity_start", {
        "activity": name,
        "room": room,
        "minutes": round(minutes, 1),
        "from_room": previous_room,
    })
    if not silent:
        print(f"[{datetime.now():%H:%M:%S}] 去做：{spec['label']}（{room}，{minutes:.0f} 分钟）", flush=True)
    return state


def _recent_calls(calls: list, minutes: float = 5.0) -> bool:
    """只看最近的呼叫：老呼叫不该把生活循环冻住（2026-11-11 实际冻了 40 分钟）。"""
    now = datetime.now().astimezone()
    for call in calls:
        stamp = str((call or {}).get("at") or "")
        try:
            when = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.astimezone()
        if (now - when).total_seconds() <= minutes * 60:
            return True
    return False


def _beat(state: dict[str, Any]) -> None:
    """写心跳。中枢靠它判断循环是不是还活着。"""
    try:
        HEARTBEAT.write_text(json.dumps({
            "at": _now(),
            "pid": __import__("os").getpid(),
            "activity": state.get("activity") or "",
            "room": state.get("room") or "",
        }, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        pass


def heartbeat_age_minutes() -> float:
    """心跳多久没更新了（分钟）。没有文件就是无穷大。"""
    info = _read_json(HEARTBEAT, {}) or {}
    stamp = str(info.get("at") or "")
    if not stamp:
        return 9999.0
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return 9999.0
    if when.tzinfo is None:
        when = when.astimezone()
    return (datetime.now().astimezone() - when).total_seconds() / 60.0


def _maybe_fall_asleep(state: dict[str, Any]) -> bool:
    """很困的夜里，让她真的睡过去（不是只"歇会儿"）。"""
    sleep = _sleep()
    if sleep.get("asleep"):
        return False
    body = _body()
    drowsy = float(body.get("drowsy") or 0.0)
    fatigue = float(body.get("fatigue") or 0.0)
    hour = _hours_now()
    night = hour >= 23 or hour < 7
    if drowsy < 0.85 and fatigue < 0.9:
        return False
    if not night and fatigue < 0.95:
        return False
    # 她刚刚还在读书/说话（20 分钟内有过活动）——那就不算"困到睡着"，别判错
    try:
        activity = _read_json(DATA / "last_activity.json", {}) or {}
        stamp = str(activity.get("at") or "")
        if stamp:
            when = datetime.fromisoformat(stamp)
            if when.tzinfo is None:
                when = when.astimezone()
            idle_minutes = (datetime.now().astimezone() - when).total_seconds() / 60.0
            if idle_minutes < 20:
                return False
    except Exception:  # noqa: BLE001
        pass
    try:
        import dl_api as _dl_api  # noqa: PLC0415

        _dl_api.set_sleep_state(hours=8.0)
    except Exception as exc:  # noqa: BLE001
        print(f"自己入睡失败：{type(exc).__name__}: {exc}", flush=True)
        return False
    _event("fell_asleep", {"drowsy": round(drowsy, 2), "fatigue": round(fatigue, 2), "hour": hour})
    print(f"[{datetime.now():%H:%M:%S}] 太困了，自己去睡（困 {drowsy:.2f}）", flush=True)
    return True


INTENT_TO_ACTIVITY = {"rest": "resting", "read": "reading", "draw": "drawing", "garden": "gardening"}


def _follow_intent(state: dict[str, Any]) -> dict[str, Any] | None:
    """她说了想干什么，就照她说的换活动和房间。"""
    hint = intent.recent(minutes=30)
    if not hint:
        return None
    kind = str(hint.get("kind") or "")
    if not kind:
        return None
    if kind == "up":
        intent.clear()
        return _start_activity(choose_activity("resting"), state)
    if kind == "wander":
        intent.clear()
        return _start_activity(choose_activity(""), state)
    activity = INTENT_TO_ACTIVITY.get(kind)
    if not activity:
        return None
    current = str(state.get("activity") or "")
    if current == activity and space.where().get("room") == ACTIVITIES[activity]["room"]:
        # 已经在做了，别重复重启计时
        intent.clear()
        return None
    intent.clear()
    print(f"[{datetime.now():%H:%M:%S}] 她说了想{intent.kind_text(kind)}，照她说的来", flush=True)
    _event("follow_intent", {"kind": kind, "text": str(hint.get("text") or "")[:60]})
    return _start_activity(activity, state)


def extend_current(minutes: float) -> dict[str, Any]:
    """她说"再躺一会儿"：把当前活动的结束时间往后推。"""
    state = _read_json(STATE, {}) or {}
    ends_at = str(state.get("ends_at") or "")
    try:
        deadline = datetime.fromisoformat(ends_at)
    except ValueError:
        deadline = datetime.now().astimezone()
    deadline = max(deadline, datetime.now().astimezone()) + timedelta(minutes=max(1.0, float(minutes)))
    state["ends_at"] = deadline.isoformat(timespec="seconds")
    _write_state(state)
    _event("extend", {"minutes": float(minutes), "activity": state.get("activity")})
    return state


def tick(state: dict[str, Any]) -> dict[str, Any]:
    """快时钟一跳：只管状态与位置，不调模型。"""
    space.settle()

    if _maybe_fall_asleep(state):
        pass

    followed = _follow_intent(state)
    if followed is not None:
        state = followed

    if _sleep().get("asleep"):
        if state.get("activity") != "sleeping":
            attention.idle()
            state.update({"activity": "sleeping", "label": "睡觉", "room": "bed",
                          "since": _now(), "ends_at": ""})
            _write_state(state)
            _event("activity_start", {"activity": "sleeping", "room": "bed"})
            print(f"[{datetime.now():%H:%M:%S}] 她睡了", flush=True)
        return state

    # 醒了但状态还写着睡觉
    if state.get("activity") == "sleeping":
        _start_activity("reading", state)

    # 有人叫她 / 正在专注：计划暂停，不打断
    snap = attention.snapshot()
    if _recent_calls(snap.get("calls") or []):
        return state
    if snap.get("mode") == "focus" and state.get("activity") != "drawing":
        return state

    ends_at = state.get("ends_at") or ""
    if not ends_at:
        return _start_activity(choose_activity(str(state.get("activity") or "")), state)

    try:
        deadline = datetime.fromisoformat(ends_at)
    except ValueError:
        return _start_activity(choose_activity(), state)
    if datetime.now().astimezone() >= deadline:
        current = str(state.get("activity") or "")
        _event("activity_end", {"activity": current, "room": state.get("room")})
        return _start_activity(choose_activity(current), state)
    return state


def describe() -> str:
    """给提示词用的一句：她现在在做什么、还剩多久。"""
    state = _read_json(STATE, {}) or {}
    activity = str(state.get("activity") or "")
    if activity == "sleeping":
        return "她在睡觉。"
    if not activity:
        return ""
    spec = ACTIVITIES.get(activity, {})
    label = spec.get("label") or state.get("label") or activity
    ends_at = str(state.get("ends_at") or "")
    left = ""
    try:
        deadline = datetime.fromisoformat(ends_at)
        minutes = max(0, int((deadline - datetime.now().astimezone()).total_seconds() // 60))
        left = f"，大概还有 {minutes} 分钟"
    except ValueError:
        pass
    return f"你手头在做：{label}{left}。"


def main() -> int:
    parser = argparse.ArgumentParser(description="她的常驻生活循环（快时钟）")
    parser.add_argument("--watch", type=int, default=20, help="每多少秒一跳（默认 20）")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--reset", action="store_true", help="丢掉当前计划，重新开始")
    args = parser.parse_args()

    state = _read_json(STATE, {}) or {}
    if args.reset:
        state = {}
    print(f"生活循环启动，每 {args.watch} 秒一跳", flush=True)
    while True:
        try:
            state = tick(state)
            _beat(state)
        except Exception as exc:  # noqa: BLE001
            print(f"生活循环异常：{type(exc).__name__}: {exc}", flush=True)
        if args.once:
            return 0
        time.sleep(max(5, args.watch))


if __name__ == "__main__":
    raise SystemExit(main())
