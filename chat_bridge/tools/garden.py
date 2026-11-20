#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""她的小院子：一个能一直玩下去的东西。

2026-11-05 她说："想玩那种能一直玩下去的。"
所以做了这个——按真实天数生长、需要照看、会死也会开花，不需要谁陪，也不用等回合。

玩法（她在聊天里写）：
    [GARDEN: 种 薄荷]   种一样东西
    [GARDEN: 浇水]      浇一次水（每两天至少一次，不然会蔫）
    [GARDEN: 看看]      看院子现在什么样
    [GARDEN: 收]        成熟了收下来

状态在 data/garden.json；按日历天数推进，她不在的时候它也在长。
"""
from __future__ import annotations

import io
import json
import random
import sys
from datetime import date, datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
DATA = Path(__file__).resolve().parent.parent / "data"
STATE = DATA / "garden.json"
LOG = DATA / "garden_log.jsonl"

STAGES = ["刚埋下去", "冒芽了", "长叶子了", "开花了", "熟透了"]
STAGE_DAYS = [0, 1, 3, 5, 8]        # 第几天进入这一阶段
WATER_NEED_DAYS = 2                 # 最多两天不浇就会蔫
SEEDS = ["薄荷", "小葱", "牵牛花", "番茄", "向日葵", "含羞草", "多肉", "橘子"]


def _today() -> date:
    return date.today()


def _load() -> dict:
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        data = {}
    data.setdefault("plots", [])
    data.setdefault("last_seen", "")
    data.setdefault("harvested", [])
    data.setdefault("died", [])
    return data


def _save(data: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def _log(text: str) -> None:
    try:
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": datetime.now().astimezone().isoformat(timespec="seconds"),
                                     "text": text}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _stage(plant: dict) -> int:
    planted = date.fromisoformat(str(plant.get("planted") or _today().isoformat()))
    age = (_today() - planted).days
    stage = 0
    for index, need in enumerate(STAGE_DAYS):
        if age >= need:
            stage = index
    return stage


def _water_gap(plant: dict) -> int:
    last = str(plant.get("watered") or plant.get("planted") or "")
    try:
        return (_today() - date.fromisoformat(last)).days
    except ValueError:
        return 0


def tick(data: dict) -> list[str]:
    """按天数推进：长得慢、渴了会蔫、蔫久了会死。返回这几天发生的事。"""
    notes: list[str] = []
    alive: list[dict] = []
    for plant in data.get("plots") or []:
        gap = _water_gap(plant)
        if gap >= WATER_NEED_DAYS:
            plant["thirsty"] = True
        else:
            plant["thirsty"] = False
        # 渴太久（5 天以上）会枯死
        if gap >= 5:
            data["died"].append({"name": plant.get("name"), "at": _today().isoformat()})
            notes.append(f"{plant.get('name')}枯了——好几天没浇水。")
            continue
        stage = _stage(plant)
        if stage != int(plant.get("stage") or 0):
            plant["stage"] = stage
            if stage in (1, 2, 3):
                notes.append(f"{plant.get('name')}{STAGES[stage]}。")
        alive.append(plant)
    data["plots"] = alive
    # 随机天气：只影响描述，不惩罚她
    if random.random() < 0.25 and alive:
        notes.append(random.choice(["今天下过一阵雨，叶子是湿的。",
                                    "太阳有点毒，土面发白。",
                                    "下午有风，院子里沙沙响。"]))
    return notes


def plant(name: str) -> dict:
    data = _load()
    tick(data)
    name = (name or "").strip()
    name = name.replace("种", "").strip() or random.choice(SEEDS)
    if len(data["plots"]) >= 6:
        return {"ok": False, "line": "院子里种不下了，先收一收。"}
    data["plots"].append({
        "name": name[:12], "planted": _today().isoformat(),
        "watered": _today().isoformat(), "stage": 0, "thirsty": False,
    })
    _save(data)
    _log(f"种下{name}")
    return {"ok": True, "line": f"把{name}种下了。它按天数长，你不在的时候也在长。"}


def water() -> dict:
    data = _load()
    tick(data)
    plots = data.get("plots") or []
    if not plots:
        return {"ok": False, "line": "院子里还什么都没有。"}
    for item in plots:
        item["watered"] = _today().isoformat()
        item["thirsty"] = False
    _save(data)
    _log("浇水")
    names = "、".join(str(p.get("name")) for p in plots[:4])
    return {"ok": True, "line": f"浇过水了（{names}）。"}


def look() -> dict:
    data = _load()
    notes = tick(data)
    _save(data)
    plots = data.get("plots") or []
    if not plots:
        return {"ok": True, "line": "院子是空的，土还是新的。" + ("".join(notes) if notes else "")}
    parts = []
    for item in plots:
        stage = int(item.get("stage") or 0)
        state = "有点蔫" if item.get("thirsty") else "精神"
        parts.append(f"{item.get('name')}（{STAGES[stage]}，{state}）")
    line = "院子里：" + "；".join(parts) + "。"
    if notes:
        line += "".join(notes)
    return {"ok": True, "line": line}


def harvest() -> dict:
    data = _load()
    tick(data)
    ripe = [p for p in data.get("plots") or [] if int(p.get("stage") or 0) >= 4]
    if not ripe:
        _save(data)
        return {"ok": False, "line": "还没有熟透的，再等等。"}
    for item in ripe:
        data["plots"].remove(item)
        data["harvested"].append({"name": item.get("name"), "at": _today().isoformat()})
    _save(data)
    names = "、".join(str(p.get("name")) for p in ripe)
    _log(f"收了{names}")
    return {"ok": True, "line": f"收了{names}。收下来的都在账上。"}


def handle(action: str, arg: str = "") -> dict:
    action = (action or "").strip()
    if action in ("种", "种下", "plant"):
        return plant(arg)
    if action in ("浇水", "water"):
        return water()
    if action in ("看看", "看", "look", ""):
        return look()
    if action in ("收", "收下", "harvest"):
        return harvest()
    return look()


def snapshot() -> dict:
    data = _load()
    plots = data.get("plots") or []
    return {
        "count": len(plots),
        "plots": [{"name": p.get("name"), "stage": int(p.get("stage") or 0),
                   "thirsty": bool(p.get("thirsty"))} for p in plots],
        "harvested": len(data.get("harvested") or []),
        "died": len(data.get("died") or []),
    }


def main() -> int:
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser()
    parser.add_argument("--action", default="看看")
    parser.add_argument("--arg", default="")
    args = parser.parse_args()
    print(handle(args.action, args.arg).get("line") or handle(args.action, args.arg).get("error"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
