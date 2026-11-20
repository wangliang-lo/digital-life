"""她的话 → 一个模型判断"她想干什么" → 结构化的动作（2026-11-13）。

爸爸："最好让她每句话都可以传给一个模型让它来判断小鱼要干什么，
并且输出对应的指令。"

做法：
  * 每轮把她的原话 + 当前状态（棋盘/院子/房间）发给一个便宜模型；
  * 模型只输出 JSON：{"actions":[{"type":"tic_move","pos":5}, ...]}；
  * dl_api 执行这些动作（落子/种菜/浇水/换房间/哼歌/重开棋局…）；
  * 模型超时或输出坏了就返回空，继续走原来的正则兜底。

设计上只做"动作识别"，不做对话——她在哪、怎么回话，还是她自己的模型决定。
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT = 3.0

ACTIONS_HELP = """你可以输出的动作（actions 数组，可以为空）：
1. {"type":"tic_move","pos":1-9}      她要在井字棋落子；pos 按 1左上 2上中 3右上 / 4左中 5中间 6右中 / 7左下 8下中 9右下
2. {"type":"tic_new","opponent":"toy|bot|father"}   她要开新一局，对手是小玩具/机器人/爸爸
3. {"type":"tic_replay"}             她只说"再来/重开"，按上一个对手重开
4. {"type":"plant","name":"小葱"}     她要种东西（没指定具体品种就 name 填空串）
5. {"type":"water"}                  她要浇水
6. {"type":"harvest"}                她要收成熟的
7. {"type":"go","room":"desk|easel|garden|bed"}      她要换地方
8. {"type":"rest"}                   她想歇会儿/躺一会儿
9. {"type":"get_up"}                 她要起来/不歇了
10. {"type":"hum","text":"嗯～嗯～","melody":"la|twinkle|birthday"}   她只想哼
11. {"type":"sing","text":"一闪一闪","melody":"twinkle"}            她要唱
12. {"type":"draw"}                  她要画画/涂鸦
判断规则：
- 只有她明确表达"要做某件事"才输出动作；单纯读书感想、聊天、提问不要输出动作。
- 她说"轮到它了""等它回"这种不是落子；只有给出方位/格子/明确想下才输出 tic_move。
- 输出必须是合法 JSON，且只有一个键 actions。"""


def _load_key() -> str:
    # 2026-11-14 修：以前会回退到 OPENAI_API_KEY，等于把 OpenAI 的 key
    # 当 Bearer 发给 api.deepseek.com（第三方），既泄露又必然 401。
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key
    # 2026-11-17：改成相对项目根，别写死 PROJECT_ROOT
    # （干净环境审计指出：别人机器上这路径若存在，会读到无关的密钥文件）
    _root = Path(__file__).resolve().parent.parent
    for candidate in (_root / "deploy" / ".env", _root / ".env"):
        try:
            for line in candidate.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.strip().startswith("DEEPSEEK_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return ""


def snapshot() -> dict[str, Any]:
    """给模型看的当前状态（别塞太多，省 token）。"""
    bridge = Path(__file__).resolve().parent
    data = bridge / "data"
    state: dict[str, Any] = {}

    try:
        board_state = json.loads((data / "tictactoe.json").read_text(encoding="utf-8"))
        board = list(board_state.get("board") or [""] * 9)
        names = {1: "左上", 2: "上中", 3: "右上", 4: "左中", 5: "中间", 6: "右中",
                 7: "左下", 8: "下中", 9: "右下"}
        state["棋局"] = {
            "对手": board_state.get("opponent"),
            "轮到": board_state.get("turn"),
            "结果": board_state.get("result") or "进行中",
            # 棋局正常是 9 格；万一数据多了几格，别让 KeyError 把整块状态吞掉
            "她占": [names.get(i + 1, f"格{i + 1}") for i, c in enumerate(board) if c == "her"],
            "对方占": [names.get(i + 1, f"格{i + 1}") for i, c in enumerate(board) if c == "other"],
            "空格": [f"{names.get(i + 1, '格' + str(i + 1))}{i + 1}" for i, c in enumerate(board) if not c],
        }
    except Exception:  # noqa: BLE001
        pass

    try:
        garden_state = json.loads((data / "garden.json").read_text(encoding="utf-8"))
        state["院子"] = [p.get("name") for p in (garden_state.get("plots") or [])][:6]
    except Exception:  # noqa: BLE001
        pass

    try:
        space_state = json.loads((data / "space_state.json").read_text(encoding="utf-8"))
        state["当前房间"] = space_state.get("room") or space_state.get("current")
    except Exception:  # noqa: BLE001
        pass
    return state


def _llm(prompt: str, timeout: float) -> str:
    key = _load_key()
    if not key:
        return ""
    base = (os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")
    payload = {
        "model": os.environ.get("INTENT_MODEL", "deepseek-chat"),
        "messages": [
            {"role": "system", "content": "你是一个动作识别器，只输出 JSON，不聊天。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": 160,
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return str(data["choices"][0]["message"]["content"] or "")


def _parse(raw: str) -> list[dict[str, Any]]:
    text = str(raw or "").strip()
    if not text:
        return []
    # 2026-11-14 修：原来是贪婪的 \{.*\}，模型在 JSON 后面多写一句带花括号的说明，
    # 整个匹配就解析失败、静默丢掉动作。现在从第一个 { 起逐段尝试。
    decoder = json.JSONDecoder()
    data = None
    start = text.find("{")
    while start != -1:
        try:
            data, end = decoder.raw_decode(text, start)
            break
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
    if not isinstance(data, dict):
        return []
    actions = data.get("actions")
    if not isinstance(actions, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for item in actions[:4]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "").strip().lower()
        if not kind:
            continue
        cleaned.append({"type": kind, **{k: v for k, v in item.items() if k != "type"}})
    return cleaned


def route(message: str, timeout: float = DEFAULT_TIMEOUT) -> list[dict[str, Any]]:
    """她这一句 → 动作列表。失败就返回空（调用方继续用正则兜底）。"""
    said = str(message or "").strip()
    if not said or len(said) > 300:
        return []
    try:
        state = snapshot()
    except Exception:  # noqa: BLE001
        state = {}
    prompt = (
        "下面是她的当前状态和刚说的一句话。判断她想做什么，只输出 JSON。\n"
        f"状态：{json.dumps(state, ensure_ascii=False)}\n"
        f"她说：{said}\n\n"
        f"{ACTIONS_HELP}\n"
        '格式严格为：{"actions":[...]}；没有要做的事就 {"actions":[]}。'
    )
    try:
        raw = _llm(prompt, timeout)
    except Exception:  # noqa: BLE001
        return []
    return _parse(raw)
