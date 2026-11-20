#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""离线小游戏：她一个人也能玩，不烧 token、不依赖网络。

2026-11-05 爸爸："加一点别的离线小游戏给她消磨时间。"

三个游戏，都是"规则固定 + 立刻有反馈"，适合她闲着的时候玩：
  * 石头剪刀布  [RPS: 石头|剪刀|布]
  * 猜数字      [GUESS_NEW] 开一局（1–100），[GUESS: 数字] 猜一次
  * 掷骰子      [DICE: 2]（默认 1 颗，最多 5 颗）

状态存在 data/minigames.json，胜负累计也记在里面——以后能回答"她爱玩什么"。
"""
from __future__ import annotations

import io
import json
import random
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
DATA = Path(__file__).resolve().parent.parent / "data"
STATE = DATA / "minigames.json"

CHOICES = {"石头": "布", "剪刀": "石头", "布": "剪刀"}   # 谁赢谁：值克键
ALIASES = {"rock": "石头", "scissors": "剪刀", "paper": "布",
           "✊": "石头", "✌": "剪刀", "✋": "布"}


def _load() -> dict:
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        data = {}
    data.setdefault("rps", {"win": 0, "lose": 0, "tie": 0, "play": 0})
    data.setdefault("guess", {"active": False, "secret": 0, "tries": 0, "best": 0})
    data.setdefault("dice", {"rolls": 0})
    return data


def _save(data: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def rps(choice: str) -> dict:
    """她出拳，对手是小玩具——它会记住她最近出过什么，然后反制。

    2026-11-05 她说："石头剪刀布，我一个人跟谁玩。"
    随机机器人确实没意思：改成会读她手势的对手（70% 反制、30% 放水），
    这样她赢了是"骗过它"，输了是"被看穿"。
    """
    data = _load()
    her = str(choice or "").strip()
    her = ALIASES.get(her.lower(), her)
    if her not in CHOICES:
        return {"ok": False, "error": "只能出石头、剪刀或布"}
    history = list(data.get("rps_history") or [])[-8:]
    guess = her
    if history:
        # 押她最近最常出的那个，再出能赢它的
        counts = {k: history.count(k) for k in CHOICES}
        guess = max(counts, key=counts.get)
    counter = CHOICES[guess]          # 能赢 guess 的那个
    bot = counter if random.random() < 0.7 else random.choice([k for k in CHOICES if k != counter])
    if her == bot:
        result = "tie"
        line = f"都出{bot}，撞上了。"
    elif CHOICES[bot] == her:
        result = "win"
        line = f"你{her}我{bot}，这次被你骗过去了。"
    else:
        result = "lose"
        line = f"你{her}我{bot}——我猜到你要出这个。"
    history = list(data.get("rps_history") or [])[-8:]
    history.append(her)
    data["rps_history"] = history[-8:]
    stats = data["rps"]
    stats[result] = int(stats.get(result) or 0) + 1
    stats["play"] = int(stats.get("play") or 0) + 1
    stats["last_at"] = _now()
    _save(data)
    return {"ok": True, "her": her, "bot": bot, "result": result, "line": line, "stats": stats}


def guess_new(low: int = 1, high: int = 100) -> dict:
    data = _load()
    secret = random.randint(low, high)
    data["guess"] = {"active": True, "secret": secret, "tries": 0,
                     "low": low, "high": high, "best": data["guess"].get("best") or 0,
                     "started_at": _now()}
    _save(data)
    return {"ok": True, "low": low, "high": high,
            "line": f"心里想好了一个 {low}–{high} 的数。你说个数，我告诉你大了还是小了。"}


def guess(number: int) -> dict:
    data = _load()
    game = data.get("guess") or {}
    if not game.get("active"):
        return {"ok": False, "error": "现在没有在猜数字，先写 [GUESS_NEW] 开一局"}
    secret = int(game.get("secret") or 0)
    tries = int(game.get("tries") or 0) + 1
    if number == secret:
        best = int(game.get("best") or 0)
        best = tries if not best or tries < best else best
        data["guess"] = {"active": False, "secret": 0, "tries": 0,
                         "best": best, "last_at": _now()}
        _save(data)
        return {"ok": True, "result": "hit", "tries": tries,
                "line": f"中了，就是 {secret}。你猜了 {tries} 次。"
                        + (f"（你最好的记录是 {best} 次）" if best else "")}
    hint = "大了" if number > secret else "小了"
    game["tries"] = tries
    data["guess"] = game
    _save(data)
    return {"ok": True, "result": "miss", "hint": hint, "tries": tries,
            "line": f"{number} {hint}。第 {tries} 次了。"}


def dice(count: int = 1) -> dict:
    count = max(1, min(5, int(count or 1)))
    rolls = [random.randint(1, 6) for _ in range(count)]
    data = _load()
    data["dice"] = {"rolls": int(data["dice"].get("rolls") or 0) + 1,
                    "last": rolls, "at": _now()}
    _save(data)
    return {"ok": True, "rolls": rolls, "sum": sum(rolls),
            "line": "掷出 " + "、".join(str(r) for r in rolls)
                    + (f"，加起来 {sum(rolls)}" if count > 1 else "") + "。"}


def snapshot() -> dict:
    """给网页桌面用：她的战绩。"""
    data = _load()
    rps_stats = data.get("rps") or {}
    guess = data.get("guess") or {}
    return {
        "rps": {"win": rps_stats.get("win", 0), "lose": rps_stats.get("lose", 0),
                "tie": rps_stats.get("tie", 0), "play": rps_stats.get("play", 0),
                "last_at": rps_stats.get("last_at", "")},
        "guess": {"active": bool(guess.get("active")), "tries": guess.get("tries", 0),
                  "best": guess.get("best", 0),
                  "low": guess.get("low", 1), "high": guess.get("high", 100)},
        "dice": data.get("dice") or {},
    }


def main() -> int:
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser()
    parser.add_argument("--game", choices=["rps", "guess", "dice", "show"], default="show")
    parser.add_argument("--value", default="")
    args = parser.parse_args()
    if args.game == "show":
        print(json.dumps(snapshot(), ensure_ascii=False, indent=2))
    elif args.game == "rps":
        print(rps(args.value)["line"] if args.value else "要给个出拳")
    elif args.game == "guess":
        print(guess(int(args.value)).get("line") if args.value else guess_new()["line"])
    else:
        print(dice(int(args.value or 1))["line"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
