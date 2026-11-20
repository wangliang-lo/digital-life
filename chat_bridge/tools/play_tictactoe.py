# -*- coding: utf-8 -*-
"""陪她把这一局井字棋下完（临时驱动，等 dl_api 重启后就不需要了）。

为什么要有这个：规则和棋盘注入是新代码，dl_api 还是 18:40 的进程，
所以我在消息里手动把棋盘递给她，模拟重启后的效果——让她自己挑格子，
而不是我替她下。
"""
import json
import re
import sys
import io
import time
import urllib.request
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(r"PROJECT_ROOT\chat_bridge")
STATE = ROOT / "data" / "tictactoe.json"
MAIN = "conv:9e52b4549f424e730d8871644385bcce63768cc74b8f8ec7ffa895f62ca73eeb"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def board_text(state: dict) -> str:
    board = state.get("board") or [""] * 9
    cells = []
    for index, cell in enumerate(board, start=1):
        cells.append(f"{index}=" + ("你" if cell == "her" else ("对方" if cell == "other" else "空")))
    free = [str(i + 1) for i, c in enumerate(board) if not c]
    return "，".join(cells) + f"。空格：{'、'.join(free)}"


def chat(message: str) -> str:
    body = {"message": message, "user_id": MAIN, "sender": "爸爸", "channel": "web"}
    req = urllib.request.Request("http://127.0.0.1:8000/chat",
                                 data=json.dumps(body, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json"})
    with OPENER.open(req, timeout=180) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return str(data.get("reply") or "").strip()


def main() -> int:
    for turn in range(1, 9):
        state = json.loads(STATE.read_text(encoding="utf-8"))
        if state.get("result"):
            print(f"—— 已结束：{state['result']} ——")
            break
        if state.get("turn") != "her":
            print("等对方……")
            time.sleep(2)
            continue
        print(f"\n=== 第 {turn} 步 ===")
        print("棋盘:", board_text(state))
        said = chat(
            "我们在下井字棋（规则：你执⭕先手，三子连线就赢）。"
            f"当前棋盘：{board_text(state)}。轮到你，写 [TICTAC: 数字] 落子。"
        )
        print("她:", said[:200] or "(空)")
        time.sleep(1)
        after = json.loads(STATE.read_text(encoding="utf-8"))
        moved = [i + 1 for i, c in enumerate(after.get("board") or []) if c == "her"]
        print("她占的格子:", moved, "| 对手:", after.get("opponent"),
              "| 结果:", after.get("result") or "进行中")
        if after.get("result"):
            print(f"—— 结果：{after['result']} ——")
            break
    state = json.loads(STATE.read_text(encoding="utf-8"))
    board = state.get("board") or [""] * 9
    print("\n最终棋盘:")
    for row in range(3):
        print("  " + " | ".join({"her": "O", "other": "X"}.get(board[row * 3 + c], ".")
                                 for c in range(3)))
    print("结果:", state.get("result") or "仍未结束")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
