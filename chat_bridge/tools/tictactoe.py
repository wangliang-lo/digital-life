#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""井字棋：她自己可以下，也可以拉着爸爸下，或者跟机器人下。

2026-11-04 爸爸："加一个逗小宠物（与本地大模型聊天）和玩井字棋
（可以主动邀请与用户玩，也可以与机器人玩）的按键。"

状态存在 data/tictactoe.json：
    {"board": ["","",...9], "turn": "her"|"other", "opponent": "bot"|"father",
     "result": ""|"her"|"other"|"draw", "started_at": ..., "updated_at": ...}

她下棋的方式是在回复里写：
    [TICTAC_NEW: bot]        # 跟机器人开一局
    [TICTAC_NEW: 爸爸]        # 邀请爸爸
    [TICTAC: 5]              # 落在 5 号格（1-9，左上开始）
"""
from __future__ import annotations

import json
import random
import sys
from datetime import datetime
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.append(str(Path(__file__).resolve().parent))
from state_io import write_json  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "tictactoe.json"

WIN_LINES = (
    (0, 1, 2), (3, 4, 5), (6, 7, 8),
    (0, 3, 6), (1, 4, 7), (2, 5, 8),
    (0, 4, 8), (2, 4, 6),
)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load() -> dict:
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("board"), list) and len(data["board"]) == 9:
            return data
    except Exception:  # noqa: BLE001
        pass
    return {"board": [""] * 9, "turn": "her", "opponent": "bot", "result": "", "updated_at": _now()}


def save(state: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        state["updated_at"] = _now()
        write_json(STATE, state)
    except Exception:  # noqa: BLE001
        pass


def winner(board: list[str]) -> str:
    for a, b, c in WIN_LINES:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    if all(board):
        return "draw"
    return ""


def new_game(opponent: str = "bot") -> dict:
    raw = str(opponent).strip()
    if raw in ("爸爸", "father", "用户"):
        kind = "father"
    elif raw in ("玩具", "小玩具", "toy", "模型", "本地模型"):
        kind = "toy"
    else:
        kind = "bot"
    state = {
        "board": [""] * 9,
        "turn": "her",
        "opponent": kind,
        "result": "",
        "started_at": _now(),
        "updated_at": _now(),
    }
    save(state)
    return state


def _bot_pick(board: list[str]) -> int:
    """很朴素的极小极大：能赢就赢，能挡就挡，否则中间/角。"""
    def best_move(side: str) -> tuple[int | None, int]:
        result = winner(board)
        if result == side:
            return None, 1
        if result == "other":
            return None, -1
        if result == "draw":
            return None, 0
        best_score = -2 if side == "other" else 2
        best_index = None
        for index in range(9):
            if board[index]:
                continue
            board[index] = side
            _, score = best_move("her" if side == "other" else "other")
            board[index] = ""
            if side == "other":
                if score > best_score:
                    best_score, best_index = score, index
            else:
                if score < best_score:
                    best_score, best_index = score, index
        return best_index, best_score

    index, _ = best_move("other")
    if index is None:
        free = [i for i, cell in enumerate(board) if not cell]
        index = random.choice(free) if free else None
    return index


def _toy_pick(board: list[str]) -> int | None:
    """让本地小模型（qwen）落一子：真去问它，问不出就退回随机。

    2026-11-04 爸爸："她原来不能和本地大模型玩井字棋嘛。"
    机器人是极小极大，下不过它；小玩具是本地那个模型，会下错、会瞎走，反倒有意思。
    """
    free = [index + 1 for index, cell in enumerate(board) if not cell]
    if not free:
        return None
    rows = []
    for index, cell in enumerate(board, start=1):
        rows.append(f"{index}={'你' if cell == 'her' else ('对方' if cell == 'other' else '空')}")
    prompt = (
        "我们在下井字棋，你是『小玩具』，执对方（X），对手执 O。\n"
        "棋盘（1-9 从上左开始）：" + "，".join(rows) + "\n"
        f"只能从这些空格里选一个：{free}。\n"
        "只回一个数字，不要解释。"
    )
    try:
        import json as _json  # noqa: PLC0415
        import urllib.request as _request  # noqa: PLC0415

        payload = _json.dumps({
            "model": "qwen2.5:14b",
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"temperature": 0.9, "num_predict": 8},
        }, ensure_ascii=False).encode("utf-8")
        req = _request.Request("http://127.0.0.1:11434/api/chat", data=payload,
                               headers={"Content-Type": "application/json; charset=utf-8"})
        with _request.urlopen(req, timeout=90) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        text = str((data.get("message") or {}).get("content") or "")
        import re as _re  # noqa: PLC0415

        for token in _re.findall(r"[1-9]", text):
            pick = int(token)
            if pick in free:
                return pick - 1
    except Exception:  # noqa: BLE001
        pass
    return random.choice([i - 1 for i in free])


def apply_move(position: int, side: str) -> dict:
    state = load()
    board = list(state.get("board") or [""] * 9)
    if state.get("result"):
        return state
    if not (1 <= position <= 9):
        return state
    index = position - 1
    if board[index]:
        return state
    board[index] = side
    result = winner(board)
    state["board"] = board
    state["result"] = result or ""
    if result:
        state["turn"] = "her"
    else:
        state["turn"] = "her" if side == "other" else "other"
    save(state)

    # 她落子后，对手自动回一手：机器人（极小极大）或小玩具（本地模型）
    if not state["result"] and state["turn"] == "other":
        pick = None
        if state["opponent"] == "bot":
            pick = _bot_pick(list(state["board"]))
        elif state["opponent"] == "toy":
            pick = _toy_pick(list(state["board"]))
        if pick is not None:
            state["board"][pick] = "other"
            result = winner(state["board"])
            state["result"] = result or ""
            state["turn"] = "her"
            save(state)
    return state


def describe(state: dict) -> str:
    """给她看的一行文字（她不需要看格子图，只要知道哪几格空了）。"""
    board = state.get("board") or [""] * 9
    marks = {"her": "你", "other": "对方"}
    cells = []
    for index, cell in enumerate(board, start=1):
        cells.append(f"{index}:{marks.get(cell, '·') if cell else '空'}")
    if state.get("result") == "her":
        tail = "你赢了"
    elif state.get("result") == "other":
        tail = "对方赢了"
    elif state.get("result") == "draw":
        tail = "平局"
    else:
        tail = "轮到你" if state.get("turn") == "her" else "等对方"
    return "；".join(cells) + f"（{tail}）"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    action = sys.argv[1] if len(sys.argv) > 1 else "show"
    if action == "new":
        opponent = sys.argv[2] if len(sys.argv) > 2 else "bot"
        print(json.dumps(new_game(opponent), ensure_ascii=False))
    elif action == "move" and len(sys.argv) > 2:
        print(json.dumps(apply_move(int(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else "her"),
                         ensure_ascii=False))
    else:
        state = load()
        print(json.dumps({"state": state, "text": describe(state)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
