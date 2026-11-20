"""小鱼的注意力：同一时刻只有一件在心上（2026-11-11）。

爸爸的定调：
* 她被叫的时候**一定知道**有人在叫；
* 继续画还是放下画笔去回，由她自己判断；
* 画到一半被打断，回来还能接上（注意力残留）。

这个模块只记账，不做决定；决定留给主意识，但会告诉它"你刚被打断过"。
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

DATA = Path(__file__).resolve().parent / "data"
STATE = DATA / "attention.json"

# focus 只给"手上有活"的事：画画、写字。读书是 normal。
FOCUS_CAPABLE = {"draw", "write"}
FOCUS_MIN_SECONDS = 90


def _now() -> float:
    return time.time()


def _load() -> dict[str, Any]:
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:  # noqa: BLE001
        pass
    return {"target": "", "kind": "", "mode": "idle", "since": _now(), "calls": [], "residue": ""}


def _save(data: dict[str, Any]) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def focus(kind: str, target: str) -> dict[str, Any]:
    """进入专注（画画/写字）。"""
    data = _load()
    data.update({
        "target": str(target or kind),
        "kind": str(kind),
        "mode": "focus" if kind in FOCUS_CAPABLE else "normal",
        "since": _now(),
        "residue": "",
    })
    _save(data)
    return data


def idle(reason: str = "") -> None:
    """放下手上的事。"""
    data = _load()
    data.update({"target": "", "kind": "", "mode": "idle", "since": _now()})
    if reason:
        data["residue"] = str(reason)[:120]
    _save(data)


def incoming(caller: str, text: str, urgent: bool = False) -> dict[str, Any]:
    """有人叫她。她一定收到这个事件——回不回、什么时候回，是她的事。"""
    data = _load()
    calls = list(data.get("calls") or [])
    calls.append({
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "from": str(caller)[:40],
        "text": str(text)[:80],
        "urgent": bool(urgent),
    })
    data["calls"] = calls[-8:]
    _save(data)
    return snapshot()


def pending_calls() -> list[dict[str, Any]]:
    return list(_load().get("calls") or [])


def clear_calls() -> None:
    data = _load()
    data["calls"] = []
    _save(data)


def interrupted(note: str) -> None:
    """她被打断了：记一笔，留着回来时想。"""
    data = _load()
    data["mode"] = "idle"
    data["residue"] = str(note)[:120]
    data["interrupted_at"] = _now()
    data["since"] = _now()
    _save(data)


def snapshot() -> dict[str, Any]:
    data = _load()
    mode = str(data.get("mode") or "idle")
    minutes = max(0.0, (_now() - float(data.get("since") or _now())) / 60.0)
    return {
        "mode": mode,
        "target": str(data.get("target") or ""),
        "kind": str(data.get("kind") or ""),
        "minutes": round(minutes, 1),
        "calls": list(data.get("calls") or []),
        "residue": str(data.get("residue") or ""),
        "can_be_interrupted": mode != "focus" or minutes < FOCUS_MIN_SECONDS,
    }


def describe(skip_activity: bool = False) -> str:
    """给她的一句话：你手上正干着什么、刚才有没有人叫。

    skip_activity=True 时不再重复"你在做什么"——那半句已经由生活循环说了。
    """
    snap = snapshot()
    bits = []
    if not skip_activity:
        if snap["mode"] == "focus":
            bits.append(f"你在{snap['target']}，已经专注 {snap['minutes']:.0f} 分钟")
        elif snap["mode"] == "normal":
            bits.append(f"你在{snap['target']}")
        else:
            bits.append("你手上没在做什么")

    calls = snap["calls"]
    if calls:
        who = "、".join(dict.fromkeys(c["from"] for c in calls))[:20]
        bits.append(f"刚才 {who} 叫过你（共 {len(calls)} 次），你还没回——可以继续做手上的事，也可以放下画笔去回")
    if snap["residue"]:
        bits.append(f"上一次被打断时你在：{snap['residue']}")
    if not bits:
        return ""
    return "；".join(bits) + "。"
