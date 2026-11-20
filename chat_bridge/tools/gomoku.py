#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""五子棋（9×9）：她可以自己跟机器人/小玩具下，也可以邀请正在说话的人下。

2026-11-15 爸爸："可以让她去下五子棋了（9*9棋盘就好）"。

状态存在 data/gomoku.json：
    {"board": ["", "", ...81], "turn": "her"|"other", "opponent": "bot"|"toy"|"<人名>",
     "result": ""|"her"|"other"|"draw", "started_at": ..., "updated_at": ...}

她下棋的方式是在回复里写：
    [GOMOKU_NEW: 小玩具]      # 开一局
    [GOMOKU: 5,5]             # 落在第 5 行第 5 列（1-9，左上开始）
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.append(str(Path(__file__).resolve().parent))
from state_io import write_json  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "gomoku.json"
SIZE = 9
NEED = 5


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load() -> dict:
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("board"), list) and len(data["board"]) == SIZE * SIZE:
            return data
    except Exception:  # noqa: BLE001
        pass
    return {"board": [""] * (SIZE * SIZE), "turn": "her", "opponent": "bot",
            "result": "", "updated_at": _now()}


def save(state: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        state["updated_at"] = _now()
        write_json(STATE, state)
    except Exception:  # noqa: BLE001
        pass


def _kind(opponent: str) -> str:
    raw = str(opponent or "").strip()
    if raw in ("爸爸", "father", "用户", "主人"):
        return "father"
    if raw in ("玩具", "小玩具", "toy", "模型", "本地模型", "机器人", "bot"):
        return "toy"
    return raw or "bot"


def new_game(opponent: str = "bot") -> dict:
    state = {
        "board": [""] * (SIZE * SIZE),
        "turn": "her",
        "opponent": _kind(opponent),
        "result": "",
        "started_at": _now(),
        "updated_at": _now(),
    }
    save(state)
    return state


def idx(row: int, col: int) -> int:
    return row * SIZE + col


def _line_positions(r: int, c: int, dr: int, dc: int) -> list[tuple[int, int]]:
    out = []
    rr, cc = r + dr, c + dc
    while 0 <= rr < SIZE and 0 <= cc < SIZE:
        out.append((rr, cc))
        rr += dr
        cc += dc
    return out


def winner(board: list[str]) -> str:
    """五连即胜；不判和棋（棋盘满了才判）。"""
    for r in range(SIZE):
        for c in range(SIZE):
            stone = board[idx(r, c)]
            if not stone:
                continue
            for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1)):
                run = 1
                for rr, cc in _line_positions(r, c, dr, dc):
                    if board[idx(rr, cc)] != stone:
                        break
                    run += 1
                    if run >= NEED:
                        return stone
    if all(board):
        return "draw"
    return ""


def apply_move(board: list[str], row: int, col: int, side: str) -> bool:
    if not (0 <= row < SIZE and 0 <= col < SIZE):
        return False
    pos = idx(row, col)
    if board[pos]:
        return False
    board[pos] = side
    return True


def _score_cell(board: list[str], r: int, c: int, side: str) -> float:
    """粗打分：往四个方向上数自己/对方的连子。"""
    score = 0.0
    for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1)):
        for sign in (1, -1):
            run = 0
            for rr, cc in _line_positions(r, c, dr * sign, dc * sign):
                if board[idx(rr, cc)] == side:
                    run += 1
                else:
                    break
            score += run * run
    # 中心偏好
    score += (4 - abs(r - 4) - abs(c - 4)) * 0.15
    return score


def bot_move(state: dict, side: str = "other") -> tuple[int, int] | None:
    """机器人替 `side` 走一步（默认替对方走）。"""
    board = state["board"]
    foe = "her" if side == "other" else "other"
    empty = [i for i, v in enumerate(board) if not v]
    if not empty:
        return None
    # 能赢先赢
    for i in empty:
        r, c = divmod(i, SIZE)
        board[i] = side
        if winner(board) == side:
            board[i] = ""
            return r, c
        board[i] = ""
    # 能挡就挡
    for i in empty:
        r, c = divmod(i, SIZE)
        board[i] = foe
        if winner(board) == foe:
            board[i] = ""
            return r, c
        board[i] = ""
    # 否则打分（靠近已有棋子）
    best, best_score = None, -1.0
    for i in empty:
        r, c = divmod(i, SIZE)
        near = any(
            board[idx(rr, cc)]
            for rr, cc in _line_positions(r, c, 1, 0)[:2] + _line_positions(r, c, -1, 0)[:2]
            + _line_positions(r, c, 0, 1)[:2] + _line_positions(r, c, 0, -1)[:2]
            + _line_positions(r, c, 1, 1)[:1] + _line_positions(r, c, -1, -1)[:1]
        )
        if not near and board.count(""):
            if board != [""] * (SIZE * SIZE):
                continue
        value = _score_cell(board, r, c, side) * 1.05 + _score_cell(board, r, c, "her" if side == "other" else "other")
        if value > best_score:
            best, best_score = (r, c), value
    return best


def describe(state: dict) -> str:
    board = state.get("board") or [""] * (SIZE * SIZE)
    rows = []
    for r in range(SIZE):
        cells = []
        for c in range(SIZE):
            v = board[idx(r, c)]
            cells.append("●" if v == "her" else ("○" if v == "other" else "·"))
        rows.append(f"{r + 1} " + " ".join(cells))
    head = "   " + " ".join(str(c + 1) for c in range(SIZE))
    turn = "你" if state.get("turn") == "her" else "对方"
    result = state.get("result") or ""
    tail = {"her": "你赢了", "other": "对方赢了", "draw": "和棋"}.get(result, f"轮到{turn}")
    return "五子棋（9×9，五连赢）\n" + head + "\n" + "\n".join(rows) + f"\n{tail}"


def her_move(state: dict, row: int, col: int) -> tuple[bool, str]:
    if state.get("result"):
        return False, "这局已经结束了，要再来一局写 [GOMOKU_NEW: 小玩具]"
    if not apply_move(state["board"], row, col, "her"):
        return False, "这一格下不了"
    result = winner(state["board"])
    if result:
        state["result"] = result
        save(state)
        return True, {"her": "五连了，你赢", "other": "对方赢了", "draw": "和棋"}.get(result, "")
    state["turn"] = "other"
    opponent = str(state.get("opponent") or "bot")
    if opponent in ("bot", "toy", "father") and opponent != "father":
        move = bot_move(state)
        if move:
            apply_move(state["board"], move[0], move[1], "other")
            result = winner(state["board"])
            if result:
                state["result"] = result
                save(state)
                return True, (f"对方下 {move[0] + 1},{move[1] + 1}；"
                              + {"her": "你赢了", "other": "对方赢了", "draw": "和棋"}.get(result, ""))
            state["turn"] = "her"
            save(state)
            return True, f"对方下 {move[0] + 1},{move[1] + 1}，轮到你"
    save(state)
    return True, "轮到对方"


def other_move(state: dict, row: int, col: int) -> tuple[bool, str]:
    """网页上的人执「对方」：他落一子，机器人替「她」回一步。"""
    if state.get("result"):
        return False, "这局已经结束了，要再来一局写 [GOMOKU_NEW: 小玩具]"
    if not apply_move(state["board"], row, col, "other"):
        return False, "这一格下不了"
    result = winner(state["board"])
    if result:
        state["result"] = result
        save(state)
        return True, {"other": "你赢了", "her": "她赢了", "draw": "和棋"}.get(result, "")
    move = bot_move(state, "her")
    if move:
        apply_move(state["board"], move[0], move[1], "her")
        result = winner(state["board"])
        if result:
            state["result"] = result
            save(state)
            return True, "她五连了"
        save(state)
        return True, f"她下 {move[0] + 1},{move[1] + 1}"
    save(state)
    return True, ""
