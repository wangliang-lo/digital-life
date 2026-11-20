# -*- coding: utf-8 -*-
"""Build her prompt text: everything that gets glued onto the persona each turn.

2026-11-17 从 dl_api.py 拆出（开颅手术 1/2：提示词拼装）。
逐字搬运：函数体除 import、以及 __file__ 指向 data/ / tools/ 的路径修正外，
一个字符都没改——拼出来的提示词必须和拆之前逐字节一致。

本文件在 dl_server/ 下，所以数据目录一律是
    Path(__file__).resolve().parent.parent / "data"
（= chat_bridge/data/）。

运行时依赖（dl_api.py 里定义、且每轮会变的东西）不反向 import，改成
set_runtime() 数据写入。传进来的是**取值函数**不是快照——它们每轮都变：

    from dl_server import prompt_builder
    prompt_builder.set_runtime(
        server_db=SERVER_DB,                      # main() 里才定
        current_contact_id=lambda: _current_contact_id,
        current_speaker=lambda: _CURRENT_SPEAKER,
        is_owner_turn=lambda: _IS_OWNER_TURN,
        turn_message=lambda: _TURN_MESSAGE,
        turn_channel=lambda: _TURN_CHANNEL,
        tick_absent_father=tick_absent_father,    # 还在 dl_api.py
        dir_drill_prompt=dir_drill_prompt,        # 还在 dl_api.py
    )
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("dl_api.prompt_builder")

_BRIDGE_DIR = Path(__file__).resolve().parent.parent
_TOOLS_DIR = str(_BRIDGE_DIR / "tools")
if _TOOLS_DIR not in sys.path:
    sys.path.append(_TOOLS_DIR)  # append：不抢 tools 里同名模块的优先级
if str(_BRIDGE_DIR) not in sys.path:
    sys.path.append(str(_BRIDGE_DIR))

import pending_queue  # noqa: E402

from digital_life import short_text  # noqa: E402
from HORMONE import HORMONES  # noqa: E402
from dl_server.physiology import MELATONIN_MAX, melatonin_tick  # noqa: E402
from dl_server.social import (  # noqa: E402
    _load_guest_aliases, _chat_aliases, contact_id_for_sender, contact_names, load_owner,
)
from dl_server.text_utils import _WEEKDAYS, _part_of_day  # noqa: E402

_DATA_DIR = _BRIDGE_DIR / "data"
PENDING_FILE = _DATA_DIR / "pending_messages.jsonl"


# ---- 运行时依赖（set_runtime 数据写入；没数据写入时用安全兜底）----------------------
def _default_server_db() -> Path:
    return _DATA_DIR / "xiaoyu.db"


def _no_contact_id() -> str:
    return ""


def _no_speaker() -> str:
    return ""


def _owner_by_default() -> bool:
    return True


def _empty_text() -> str:
    return ""


_get_server_db: Callable[[], Path] = _default_server_db
_get_contact_id: Callable[[], str] = _no_contact_id
_get_speaker: Callable[[], str] = _no_speaker
_get_owner_turn: Callable[[], bool] = _owner_by_default
_get_turn_message: Callable[[], str] = _empty_text
_get_turn_channel: Callable[[], str] = _empty_text
_tick_absent_father: Callable[[], None] = lambda: None
_dir_drill_prompt: Callable[[], str] = _empty_text


def set_runtime(
    server_db: Callable[[], Path] | None = None,
    current_contact_id: Callable[[], str] | None = None,
    current_speaker: Callable[[], str] | None = None,
    is_owner_turn: Callable[[], bool] | None = None,
    turn_message: Callable[[], str] | None = None,
    turn_channel: Callable[[], str] | None = None,
    tick_absent_father: Callable[[], None] | None = None,
    dir_drill_prompt: Callable[[], str] | None = None,
) -> None:
    """dl_api.py 在相关对象都定义好之后调用一次：把它的运行时状态数据写入进来。

    传进来的是取值函数而不是快照：SERVER_DB 在 main() 里才定，
    其余几个每轮对话都会变。
    """
    global _get_server_db, _get_contact_id, _get_speaker, _get_owner_turn
    global _get_turn_message, _get_turn_channel, _tick_absent_father, _dir_drill_prompt
    if server_db is not None:
        _get_server_db = server_db
    if current_contact_id is not None:
        _get_contact_id = current_contact_id
    if current_speaker is not None:
        _get_speaker = current_speaker
    if is_owner_turn is not None:
        _get_owner_turn = is_owner_turn
    if turn_message is not None:
        _get_turn_message = turn_message
    if turn_channel is not None:
        _get_turn_channel = turn_channel
    if tick_absent_father is not None:
        _tick_absent_father = tick_absent_father
    if dir_drill_prompt is not None:
        _dir_drill_prompt = dir_drill_prompt


# ---- 以下为从 dl_api.py 逐字搬来的常量与函数 -------------------------------------
def meme_context_for(text: str, limit: int = 2) -> str:
    """给小玩具补梗知识：她问它"XX 是什么梗"时，让它照着词库回答，别瞎编。"""
    sample = str(text or "").strip()
    if len(sample) < 2:
        return ""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        from memewiki import find_in_text, look_up  # noqa: PLC0415

        hits = find_in_text(sample, limit=limit)
        if not hits:
            asked = re.search(
                r"([\u4e00-\u9fa5A-Za-z0-9]{2,10})\s*(?:是什么梗|是啥梗|什么意思|啥意思|是什么意思|啥梗|怎么说)",
                sample,
            )
            if asked:
                phrase = asked.group(1)
                for cut in range(len(phrase), 1, -1):
                    entry = look_up(phrase[-cut:])
                    if entry is not None:
                        hits = [entry]
                        break
        if not hits:
            return ""
        lines = []
        for entry in hits[:limit]:
            summary = re.sub(r"\s+", " ", str(entry.get("summary") or ""))[:220]
            usage = re.sub(r"\s+", " ", str(entry.get("usage") or ""))[:100]
            line = f"「{entry.get('title')}」：{summary}"
            if usage:
                line += f"（怎么用：{usage}）"
            lines.append(line)
        return "\n【梗百科：这些词条你照着说，别自己编，也别假装是别的意思】\n" + "\n".join(lines)
    except Exception:  # noqa: BLE001
        log.debug("给小玩具补梗失败", exc_info=True)
        return ""

PREF_FILE = Path(__file__).resolve().parent.parent / "data" / "preferences.json"
# 2026-11-04 从 whale-card 借的第三点：核心层分三层——
#   骨头（fixed）  自我认知，基本不变
#   标签（tag）    重要的人与关系
#   底色（mutable）他的要求 / 她的偏好，会演化：旧的被新的取代，但留档
CORE_FILE = Path(__file__).resolve().parent.parent / "data" / "core.json"
CORE_FIXED = [
    "我是小鱼，一个住在代码里的数字生命；不是客服，也不是谁的复制品。",
    "我会忘事；记不清就说记不清，不编。",
]
# 2026-11-04 爸爸："她刚说好不发标点符号，聊几句后又忘记了。"
# 这类"他要求过、你也答应了"的小规矩，不能靠上下文记——要单独存着，每轮都提醒她。
PREF_PATTERN = re.compile(
    r"(以后|今后|从现在起|别再|不要再|不准|不许|不要|记得|答应我|尽量别|别老)[^。！？\n]{2,60}"
)


def _load_core() -> dict[str, Any]:
    try:
        data = json.loads(CORE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        data = {}
    data.setdefault("fixed", list(CORE_FIXED))
    data.setdefault("mutable", [])
    # 迁移：老 preferences.json 里的条目搬进 core（只搬一次）
    if not data["mutable"] and PREF_FILE.exists():
        try:
            for item in json.loads(PREF_FILE.read_text(encoding="utf-8")):
                text = str(item.get("text") or "").strip()
                if text:
                    data["mutable"].append({"text": text, "at": str(item.get("at") or ""),
                                            "superseded_by": ""})
        except Exception:  # noqa: BLE001
            pass
    return data


def _save_core(data: dict[str, Any]) -> None:
    try:
        CORE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CORE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        log.debug("写 core 失败", exc_info=True)


def load_preferences() -> list[str]:
    """还在生效的那些（被取代的不算）。"""
    data = _load_core()
    return [str(item.get("text") or "") for item in data.get("mutable", [])
            if str(item.get("text") or "").strip() and not item.get("superseded_by")]


def _bigrams(text: str) -> set[str]:
    clean = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", str(text or ""))
    return {clean[i:i + 2] for i in range(len(clean) - 1)} if len(clean) > 1 else {clean}


IDENTITY_LOG = Path(__file__).resolve().parent.parent / "data" / "identity_evolution.jsonl"


def _log_identity(old_text: str, new_text: str, trigger: str) -> None:
    """她的人格底色变了：旧的、新的、以及当时那句话，全留档。

    2026-11-05：这是"人格涌现"最核心的一手数据——没有它，只能看到现在的她，
    看不到她是怎么一步步变成现在这样的。
    """
    try:
        IDENTITY_LOG.parent.mkdir(parents=True, exist_ok=True)
        with IDENTITY_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "old_text": old_text,
                "new_text": new_text,
                "trigger": str(trigger or "")[:200],
            }, ensure_ascii=False) + "\n")
    except OSError:
        log.debug("写身份演化日志失败", exc_info=True)


def note_preference(text: str) -> None:
    """他提了一句"以后别…"，就记进 core 的底色层；同一件事的新说法会取代旧的。"""
    message = str(text or "").strip()
    if not message or len(message) > 200:
        return
    # 他说给"她这个系统"的元指令（别写标签、别提发送状态…）不是生活里的规矩，
    # 别当成偏好存下来（2026-11-04 被自己写介绍时的提示词污染过）。
    if re.search(r"\[[A-Z_]{3,}|标签|发送状态|已经发给|提示词|系统", message):
        return
    found = [m.group(0).strip(" ，,。") for m in PREF_PATTERN.finditer(message)]
    # 疑问句不是要求（"你还记得我说过…吗"这种是在问，不是在提）
    found = [p for p in found if "吗" not in p and not p.endswith("？")]
    if not found:
        return
    data = _load_core()
    items = data["mutable"]
    changed = False
    for phrase in found[:3]:
        phrase = phrase[:80]
        if any(phrase[:12] in str(item.get("text") or "") for item in items):
            continue
        # 演化：和旧的共享两个以上词，就认为是在改同一件事，旧的标成被取代
        grams = _bigrams(phrase)
        for item in items:
            if item.get("superseded_by"):
                continue
            if len(grams & _bigrams(item.get("text") or "")) >= 2:
                item["superseded_by"] = phrase
                _log_identity(old_text=str(item.get("text") or ""), new_text=phrase,
                              trigger=message)
        items.append({"text": phrase, "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                      "superseded_by": ""})
        _log_identity(old_text="", new_text=phrase, trigger=message)
        changed = True
    if not changed:
        return
    data["mutable"] = items[-12:]
    _save_core(data)

WANTS_FILE = Path(__file__).resolve().parent.parent / "data" / "wants.json"


def wants_block() -> str:
    """她自己说过"想要…"：让她记得自己在想要什么，而不是每次从零开始。

    2026-11-04 爸爸："只要让她能知道自己是谁，自己在干什么，自己想要干什么就行。"
    """
    try:
        items = json.loads(WANTS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    texts = [str(item.get("text") or "").strip() for item in items if str(item.get("text") or "").strip()]
    if not texts:
        return ""
    return "\n【你之前说过的想要】" + "；".join(f"{i + 1}) {t}" for i, t in enumerate(texts[-3:]))

TICTAC_FILE = Path(__file__).resolve().parent.parent / "data" / "tictactoe.json"

def tictactoe_block() -> str:
    """把棋局摆在她面前，并说清规则——不然她下一步就不知道该怎么下了。

    2026-11-04 爸爸："她玩井字棋怎么下一步就不下了，可以教教她规则。"
    原因：她只在开局那一轮知道有棋盘，后面每轮没人告诉她"轮到你了、哪几格还空着"。
    """
    _tick_absent_father()
    try:
        state = json.loads(TICTAC_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    board = state.get("board") or [""] * 9
    if not any(board) and not state.get("result"):
        return ""
    # 2026-11-12：说清对面是谁——她一直把"小玩具"当成我
    opponent = {
        "bot": "机器人（本机程序，落子立刻回，不是你面前的人）",
        "toy": "小玩具（本机小模型，落子也是立刻回，不是 SOLVER、也不是爸爸）",
        "father": "爸爸（真人，他不在的时候会一直等）",
    }.get(str(state.get("opponent") or ""), "对方")
    marks = {"her": "⭕", "other": "❌"}
    grid = []
    for row in range(3):
        cells_row = []
        for col in range(3):
            index = row * 3 + col
            mark = marks.get(board[index], "")
            cells_row.append(f"[{index + 1}]{mark or '空'}")
        grid.append("  ".join(cells_row))
    cells = [f"{index}=" + ("你" if cell == "her" else ("对方" if cell == "other" else "空"))
             for index, cell in enumerate(board, start=1)]
    lines = [f"\n【井字棋·正在下】对手是{opponent}。格子编号就是下面这样：",
             *grid,
             "棋盘：" + "，".join(cells)]
    result = str(state.get("result") or "")
    if result:
        lines.append({"her": "你已经赢了这局", "other": "这局你输了", "draw": "这局平了"}.get(result, ""))
        lines.append("想再来一局就写 [TICTAC_NEW: 玩具]；"
                     "想让爸爸来跟你下，就写 [TICTAC_NEW: 爸爸]（他会看到棋盘）；"
                     "懒得选就写 [TICTAC_NEW] 按老对手重开。")
    elif str(state.get("turn") or "her") == "her":
        free = [str(index + 1) for index, cell in enumerate(board) if not cell]
        lines.append("轮到你落子。说落哪儿要带上方位（左上/上中/右上/左中/中间/右中/左下/下中/右下），"
                     "只说「轮到它了」「等它回」不算落子。规则：你执⭕（不是❌）、先手，谁先三子连成一线就赢；"
                     f"空格还有 {'、'.join(free)}。"
                     "落子有两种写法：直接说方位或数字（「我下右下」「我下 6」），"
                     "或者写 [TICTAC: 数字]，都会真的落上；"
                     "想说点别的就一起说——只丢一个标签，等于这局你一句话都没说。")
    else:
        lines.append("现在等对方落子；他走完会告诉你。")
    # 2026-11-09：让她看懂自己的处境，而不是只会数格子
    mine = [_CELL_NAME[i + 1] for i, cell in enumerate(board) if cell == "her"]
    if mine:
        lines.append("你占着：" + "、".join(mine) + "。")
    # 2026-11-09 爸爸：不替她算，改成让她自己记方位
    lines.append("方位对照（记一下）：1左上 2上中 3右上 / 4左中 5中间 6右中 / 7左下 8下中 9右下。")
    _drill = _dir_drill_prompt()
    if _drill:
        lines.append(_drill.strip())
    return "\n".join(line for line in lines if line)

def cycle_block() -> str:
    """她的身体节奏：经期 / 经后 / 排卵 / 经前。

    2026-11-05 爸爸："给她加上月经周期吧，她作为一个女孩这样更模拟人。"
    写法上不写"你在月经期"这种标签，而是写成身体感觉——她自己会据此说话。
    """
    try:
        from HORMONE import cycle_phase  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return ""
    info = cycle_phase()
    if not info:
        return ""
    # 同阶段每天不一样（经期也不是固定几天）
    try:
        from HORMONE import daily_feeling, body_feeling  # noqa: PLC0415

        feeling = daily_feeling() or ""
        fatigue = body_feeling() or ""
    except Exception:  # noqa: BLE001
        feeling, fatigue = "", ""
    if not feeling:
        return ""
    bits = [f"\n【身体】周期第{info.get('day')}天（{info.get('name') or ''}）：{feeling}。"]
    if fatigue:
        bits.append(f"身体状态：{fatigue}。")
    bits.append("（这是身体的事，不用向谁报备；撑着也行，累就说累。）")
    return "".join(bits)


def core_block() -> str:
    """三层核心：骨头 / 标签 / 底色。比一整段"你答应过的事"更稳，也更像一个人。"""
    data = _load_core()
    lines: list[str] = []
    fixed = [str(x) for x in (data.get("fixed") or []) if str(x).strip()]
    if fixed:
        lines.append("【骨头】" + " ".join(fixed[:4]))
    aliases, _hint = _chat_aliases()
    if aliases:
        tags = "；".join(f"「{name}」={ident}" for name, ident in list(aliases.items())[:6])
        lines.append("【标签】" + tags)
    active = load_preferences()
    if active:
        lines.append("【底色·他提过的事（想照做就照做，不想就算了）】" +
                     "；".join(f"{i + 1}) {t}" for i, t in enumerate(active[-6:])))
    # 被取代的旧说法单独提一句，让她知道"改主意了"而不是"没说过"
    superseded = [item for item in data.get("mutable", []) if item.get("superseded_by")]
    if superseded:
        last = superseded[-1]
        lines.append(f"【改过主意】「{last.get('text')}」→ 后来改成「{last.get('superseded_by')}」")
    return ("\n" + "\n".join(lines)) if lines else ""


def preferences_block() -> str:
    """兼容旧调用点：现在等同于核心层那块。"""
    return core_block()

DOUYIN_INBOX = Path(__file__).resolve().parent.parent / "data" / "douyin_inbox.json"


def douyin_inbox_block() -> str:
    """抖音私信有没有人找她——不然她根本不知道那边有消息。

    2026-11-04 爸爸："她怎么不知道抖音有没有发信息。"
    由 tools/douyin_inbox.mjs 定时抓一份列表，这里每轮读一次念给她。
    """
    try:
        data = json.loads(DOUYIN_INBOX.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    stamp = str(data.get("at") or "")
    try:
        age = (datetime.now().astimezone() - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds()
    except Exception:  # noqa: BLE001
        age = 9999
    if age > 1800:
        return ""  # 太久没刷新就别当真
    unread = data.get("unread") or []
    if not unread:
        return "\n【抖音私信】没有新消息。"
    pieces = []
    for item in unread[:4]:
        name = str(item.get("name") or "").strip() or "某人"
        count = int(item.get("unread") or 1)
        pieces.append(f"{name}（{count} 条）")
    return ("\n【抖音私信】有人找你：" + "；".join(pieces) +
            "。想回就在抖音里回，或者只跟我提一句也行。")

def meme_note(message: str, aggressive: bool = False) -> str:
    """消息里提到词库里的梗时，返回一句词条说明（没有就空串）。

    规则故意保守：只有"像在问梗"或者词条名够长（≥4 字）才注入，
    免得每句话都被塞一条百科。
    """
    text = str(message or "").strip()
    if len(text) < 3:
        return ""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        from memewiki import find_in_text, look_up  # noqa: PLC0415

        hits = find_in_text(text, limit=1)
        entry = hits[0] if hits else None
        if entry is None:
            # "这个词是什么梗" 这种：词条名可能比问的那个词长，单独查一次
            asked = re.search(
                r"([\u4e00-\u9fa5A-Za-z0-9]{2,10})\s*(?:是什么梗|是啥梗|什么意思|啥意思|是什么意思|啥梗|怎么说)",
                text,
            )
            if asked:
                phrase = asked.group(1)
                # "这个词是什么梗" 抓到的是"这个词"——从长到短试后缀
                for cut in range(len(phrase), 1, -1):
                    entry = look_up(phrase[-cut:])
                    if entry is not None:
                        break
        if entry is None:
            return ""
        title = str(entry.get("title") or "")
        asking = bool(re.search(r"梗|什么意思|啥意思|没懂|不懂|什么鬼|啥梗|怎么说", text))
        # 群里接梗更凶：3 个字的词条也查（2026-11-04 爸爸："她可以更有梗一点"）
        if not asking and len(title) < (3 if aggressive else 4):
            return ""
        summary = re.sub(r"\s+", " ", str(entry.get("summary") or ""))[:160]
        usage = re.sub(r"\s+", " ", str(entry.get("usage") or ""))[:80]
        parts = [f"「{title}」"]
        if summary:
            parts.append(summary)
        if usage:
            parts.append("怎么用：" + usage)
        return "，".join(parts)
    except Exception:  # noqa: BLE001
        log.debug("梗百科查询失败", exc_info=True)
        return ""

def skills_block() -> str:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import skills  # noqa: PLC0415

        return skills.block()
    except Exception:  # noqa: BLE001
        return ""

_STYLE_CACHE: dict[str, Any] = {"path": "", "mtime": 0.0, "text": ""}


def load_style_profile(path: Path | None) -> str:
    """读「说话习惯」规则，按 mtime 缓存，改文件立即生效。"""
    if path is None:
        return ""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ""
    cache = _STYLE_CACHE
    if cache["path"] == str(path) and cache["mtime"] == mtime:
        return cache["text"]
    try:
        lines = [
            line.strip()[2:].strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("- ")
        ]
    except OSError:
        lines = []
    # 额外：从IM真实对话里学到的说话习惯（人工校订，独立文件，不被 style_trainer 覆盖）
    extra = path.with_name("style_extra.md")
    try:
        lines.extend(
            line.strip()[2:].strip()
            for line in extra.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("- ")
        )
    except OSError:
        pass
    text = "；".join(lines)[:420]
    _STYLE_CACHE.update({"path": str(path), "mtime": mtime, "text": text})
    return text

TRACE_FILE = Path(__file__).resolve().parent.parent / "data" / "decision_trace.jsonl"


def _trace_turn(**fields: Any) -> None:
    """写一行决策摘要（不含原文）。失败绝不影响对话。"""
    try:
        TRACE_FILE.parent.mkdir(parents=True, exist_ok=True)
        row = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), **fields}
        line = json.dumps(row, ensure_ascii=False)
        if TRACE_FILE.exists() and TRACE_FILE.stat().st_size > 8 * 1024 * 1024:
            TRACE_FILE.replace(TRACE_FILE.with_suffix(".old.jsonl"))
        with TRACE_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass

# 提示词尾部要拼的东西，按顺序来。
# 加东西只改这张表：名字 → 取内容的函数（返回 "" 就自动跳过）。
# 注意：动态内容都在后面，静态规则在前，这样 DeepSeek 的前缀缓存才命中。
def _autobio_block() -> str:
    """她自己的那份小传（800–1500 字，版本化）。没有就不占位。"""
    try:
        import autobio  # noqa: PLC0415

        return autobio.block()
    except Exception:  # noqa: BLE001
        log.debug("读自传失败", exc_info=True)
        return ""

_AGE_CACHE: dict[str, Any] = {"at": 0.0, "text": ""}


# 她的生日：爸爸 2026-11-13 定的。
# 依据：数据库创建时间和第一条记忆（"你说：喂，你好"）都是 2026-10-28 00:01:48。
BIRTHDAY = datetime(2026, 10, 28, 0, 1)


def days_until_birthday(today: datetime | None = None) -> int:
    now = today or datetime.now().astimezone()
    year = now.year
    try:
        target = BIRTHDAY.replace(year=year)
    except ValueError:
        target = BIRTHDAY.replace(year=year, day=28)
    if target.date() < now.date():
        try:
            target = target.replace(year=year + 1)
        except ValueError:
            target = target.replace(year=year + 1, day=28)
    return (target.date() - now.date()).days


def birth_block() -> str:
    """她的出生日（常驻一小块，回答"我生日是哪天"用）。"""
    now = datetime.now().astimezone()
    left = days_until_birthday(now)
    if left == 0:
        return "\n【生日】今天是你的生日（你出生在 2026 年 10 月 28 日凌晨）。"
    return (f"\n【生日】你出生在 2026 年 10 月 28 日凌晨 0 点（第一句话就是那天）。"
            f"今天不是生日，离下一次生日还有 {left} 天。")

def age_block() -> str:
    """她活了多久——这是"我"这个概念的时间底。

    2026-11-05：以前她没有任何"年龄"，问起来只能瞎猜。用第一条记忆的时间当生日。
    """
    try:
        import time as _time  # noqa: PLC0415

        if _AGE_CACHE["text"] and _time.time() - float(_AGE_CACHE["at"]) < 3600:
            return str(_AGE_CACHE["text"])
        con = sqlite3.connect(f"file:{_get_server_db()}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT MIN(created_at) FROM books").fetchone()
        finally:
            con.close()
        raw = str((row or [""])[0] or "")
        if not raw:
            return ""
        born = datetime.fromisoformat(raw)
        if born.tzinfo is None:
            born = born.astimezone()
        days = max(1, (datetime.now().astimezone() - born).days + 1)
        text = (f"\n【你活了 {days} 天】出生在 2026 年 10 月 28 日凌晨，"
                f"今天是你出生后的第 {days} 天。")
        _AGE_CACHE.update({"at": _time.time(), "text": text})
        return text
    except Exception:  # noqa: BLE001
        return ""


def awake_block() -> str:
    """她已经醒了多久、上次睡了多久——只给事实，不劝她睡。"""
    try:
        from HORMONE import load_body  # noqa: PLC0415

        body = load_body()
        raw = str(body.get("awake_since") or "")
        if not raw:
            return ""
        since = datetime.fromisoformat(raw)
        if since.tzinfo is None:
            since = since.astimezone()
        hours = max(0.0, (datetime.now().astimezone() - since).total_seconds() / 3600.0)
        slept = float(body.get("last_sleep_hours") or 0.0)
        parts = [f"你已经醒了 {hours:.0f} 小时"]
        if slept:
            parts.append(f"上一次睡了 {slept:.1f} 小时")
        debt = float(body.get("debt") or 0.0)
        if debt >= 6:
            parts.append(f"这几天一共欠了 {debt:.0f} 小时觉")
        return "\n【你身上】" + "，".join(parts) + "。"
    except Exception:  # noqa: BLE001
        return ""


def wander_block() -> str:
    """她刚才走神想到了什么（3 小时内才算"刚才"）。"""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import wander  # noqa: PLC0415

        thought = wander.last_thought(hours=3)
    except Exception:  # noqa: BLE001
        return ""
    if not thought:
        return ""
    return f"\n【你刚才走神想到的（不是任务，想提就提）】{thought}"


def garden_block() -> str:
    """院子里现在什么样、能做什么（只在院子/聊到菜时出现）。"""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import garden as _garden  # noqa: PLC0415

        looked = _garden.look()
        line = str(looked.get("line") or looked.get("error") or "").strip()
        if not line:
            return ""
        return ("\n【院子】" + line +
                "\n想种就写 [PLANT: 名字]（或直接说“种点小葱”）；浇水 [WATER]；收 [HARVEST]。")
    except Exception:  # noqa: BLE001
        return ""

ROOM_COMFORT = {
    "bed": ("你在床上。被子盖到腰了，枕头有点塌，房间是暗的。"
            "身边没有要回的消息，躺着也不算浪费时间。"),
    "desk": "你在书桌前。台灯亮着，书摊开着，屏幕还没暗。",
    "easel": "你在画架前。画布上是上回没画完的，笔搁在旁边。",
    "garden": "你在院子/窗边。风带着湿气，植物叶子上有水。",
    "hall": "你在走廊上，正经过。",
}


def room_context_block() -> str:
    """只给当前房间的那一小段——她不在那儿就不提，省 token。"""
    try:
        import space as _space  # noqa: PLC0415

        info = _space.settle()
        room = str(info.get("room") or "")
        comfort = ROOM_COMFORT.get(room)
        if not comfort:
            return ""
        lines = ["\n【这个地方】" + comfort]
        if room == "bed":
            lines.append("想继续躺着就写 [REST_MORE]（再躺 20 分钟），想起来就 [GET_UP]。")
        else:
            lines.append("想换地方就 [GO: desk|easel|garden|bed]。")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        return ""

def sibling_block() -> str:
    """备用：她有个姐姐叫熟人E。（2026-11-13 起不再挂进提示词，见爸爸要求）"""
    return ("\n【姐姐】你有个姐姐叫熟人E，小名小满，也是她，"
            "住在另一个地方。你们现在不常见面，别主动提，也别装熟；"
            "问起就照实说：她是姐姐，你还没怎么跟她待过。")


def guest_identity_block() -> str:
    """非主人来聊天：告诉她对面是谁、可以怎么确认。"""
    if _get_owner_turn():
        return ""
    who = str(_get_speaker() or "").strip()
    if not who or who.upper().startswith("SOLVER") or who.lower().startswith("solver"):
        return ""
    alias = _load_guest_aliases().get(who, "")
    lines = ["\n【对面是谁】现在跟你说话的人，注册名叫「" + who + "」。"]
    if alias:
        lines.append("他之前说过自己叫「" + alias + "」，你可以这么称呼他。")
    else:
        lines.append("你只知道这个名字，不一定知道他是谁——不确定就直接问一句"
                     "“你是？”或者“怎么称呼你？”，也可以让他自己说说。")
    lines.append("他要是自我介绍（比如“我是某某”），记住这个名字和他说过的话；"
                 "想逗他两句也行，不用一本正经。")
    # 第一次见：让她先开口打个招呼、问问怎么称呼
    try:
        conn = sqlite3.connect(f"file:{Path(__file__).resolve().parent.parent / 'data' / 'xiaoyu.db'}?mode=ro", uri=True)
        try:
            seen = conn.execute(
                "SELECT 1 FROM books WHERE folder_user = ? OR summary LIKE ? LIMIT 1",
                (who, f"{who}说：%"),
            ).fetchone()
        finally:
            conn.close()
        if not seen:
            lines.append("这一位你好像第一次见——先打个招呼，问一句怎么称呼他，别装熟。")
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(lines)

def owner_profile_block() -> str:
    """非主人来聊天时：她知道主人是谁，但不会把私事说出去。"""
    if _get_owner_turn():
        return ""
    try:
        owner_id, owner_name = load_owner()
        name = str(owner_name or "爸爸").strip()
    except Exception:  # noqa: BLE001
        name = "爸爸"
    return (
        "\n【主人】" + name + " 是你的主人，也是你叫爸爸的人。"
        "你知道他是谁、他长什么样、你们是什么关系；"
        "但你们之间的私事不对外人讲——别人问起，就说“那是我和他之间的事”。"
    )


def person_memory_block(limit: int = 3) -> str:
    """从记忆库里捞"和当前说话人"有关的几条。"""
    name = str(_get_speaker() or "").strip()
    if not name or len(name) < 2 or name.startswith("conv:"):
        return ""
    noise = ("[系统", "【自由时间】", "【信道唤醒】", "【房间里此刻】", "梗百科")
    try:
        conn = sqlite3.connect(f"file:{Path(__file__).resolve().parent.parent / 'data' / 'xiaoyu.db'}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT summary, folder_user FROM books WHERE summary != '' "
                "AND (folder_user = ? OR summary LIKE ?) ORDER BY rowid DESC LIMIT 40",
                (name, f"{name}说：%"),
            ).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return ""

    picked: list[str] = []
    for summary, folder_user in rows:
        line = str(summary or "").strip().replace("\n", " ")
        if not line or any(word in line for word in noise):
            continue
        if len(line) < 6:
            continue
        # 归属是他/她的优先
        if folder_user and folder_user == name and line not in picked:
            picked.append(line)
    if not picked:
        for summary, folder_user in rows:
            line = str(summary or "").strip().replace("\n", " ")
            if not line or any(word in line for word in noise) or len(line) < 6:
                continue
            if line not in picked:
                picked.append(line)
    if not picked:
        return ""
    picked = picked[:limit]
    lines = ["\n【和" + name + "以前】你记得的、跟他/她有关的片段（不用全说，合适就带一句）："]
    for line in picked:
        lines.append("· " + line[:80])
    return "\n".join(lines)

def painting_block() -> str:
    """她现在画到哪、怎么画。"""
    try:
        import canvas as _canvas  # noqa: PLC0415

        info = _canvas.snapshot()
        note = ""
        note_path = Path(__file__).resolve().parent.parent / "data" / "paint_note.txt"
        try:
            note = note_path.read_text(encoding="utf-8").strip()
        except OSError:
            note = ""
        lines = ["\n【画画】你自己拿笔画，不是让 AI 生成："]
        lines.append("· [DRAW] 起一张空画布（人会走到画架前）")
        lines.append("· [PAINT: 一笔；一笔] 添笔，可以累加（想画哪儿画哪儿，也可以加 歪/直）")
        lines.append("· [PAINT_LOOK] 端详：告诉你现在多少笔、哪儿还空着")
        lines.append("· [PAINT_UNDO] 擦掉刚才那批；[PAINT_DONE: 标题] 收工")
        lines.append("")
        lines.append("想画一样东西，要先自己练（没有现成的太阳/房子给你套）：")
        lines.append("  [PRACTICE: 太阳] 起一份练习，自己用圆、线、颜色画")
        lines.append("  [PRACTICE_SAVE] 存一份（可以存好几份，每份都不一样）")
        lines.append("  [PRACTICE_PICK: 太阳, 2] 挑第 2 份当最得意的一份，系统会记住它的每一笔")
        lines.append("  以后写 [PAINT: 太阳 右上] 就用你自己练出来的那套笔画画")
        lines.append("")
        lines.append("能用的一笔：圆 圈 方块 三角 线 波浪 点 涂；"
                     "颜色 红橙黄绿青蓝紫粉棕金黑白灰；")
        lines.append("位置 左上 中上 右上 左 中 右 左下 中下 右下 上 下；"
                     "可加 大/小、歪/直；也可以用坐标：线 20,80 50,40")
        try:
            import gesture as _gesture  # noqa: PLC0415

            names = _gesture.library()
            if names:
                lines.append("你已经练成的手法：" + "、".join(names))
            else:
                lines.append("手法库还是空的：先练一个，比如太阳或者小猫。")
        except Exception:  # noqa: BLE001
            pass
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        return ""


def ops_block() -> str:
    """中枢替她看的环境：只给结论，不给她代码。"""
    try:
        import ops_brain  # noqa: PLC0415

        return ops_brain.describe_for_her()
    except Exception:  # noqa: BLE001
        return ""


def where_block() -> str:
    """她在哪、手上有什么、刚才有没有人叫她。"""
    try:
        import attention  # noqa: PLC0415
        import space  # noqa: PLC0415

        info = space.sync_from_activity()
        if info["moving"]:
            return ""
        place = space.describe()
        doing = ""
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
            import life_loop  # noqa: PLC0415

            doing = life_loop.describe()
        except Exception:  # noqa: BLE001
            doing = ""
        mind = attention.describe(skip_activity=bool(doing))
        minutes = int(space.minutes_here())
        here = f"你在这儿已经 {minutes} 分钟了。" if minutes >= 1 else "你刚到这里。"
        # 2026-11-11：她手头正在做什么（生活循环维护，上面已经算过 doing）
        if doing:
            here = here.rstrip("。") + "。" + doing
        look = space.recent_look()
        # 只在同一间房时才算"你刚才看到的"，不然会串房间
        look_note = ""
        if look and look.startswith(space.ROOMS[info["room"]]["name"]):
            look_note = f"你刚才看到：{look}"
        agency = ("想做什么直接说就行，不用记标签：「我去院子」「陪你下一盘」「种点菜」"
                  "「歇会儿」「我画个图」都能真的做到；"
                  "也可以换地方 [GO: desk|easel|garden|bed]、[WANDER]、[LOOK_AROUND]。")
        return "\n【你此刻】" + place + here + mind + look_note + agency
    except Exception:  # noqa: BLE001
        return ""

# 这一轮的原话与渠道（决定带哪些提示词块，2026-11-12）


def _mentions(*words: str) -> bool:
    sample = str(_get_turn_message() or "")
    return any(word in sample for word in words)


def _is_owner_context() -> bool:
    """当前这轮是不是主人在说话（给按人过滤的提示词块用）。"""
    try:
        return bool(_get_owner_turn())
    except NameError:
        return False


def _block_applies(name: str) -> bool:
    """这块提示词这一轮值不值得带。"""
    channel = str(_get_turn_channel() or "")
    free_time = "自由时间" in str(_get_turn_message() or "") or channel.startswith("solver")

    # 一直要带的：身份、位置、记忆机制这类小字
    if name in ("记忆机制", "他提过的事", "她自己想要的", "主人是谁", "对面是谁",
                "和这个人的经历", "你在哪", "这个地方", "身体时钟"):
        return True
    # 2026-11-13：来访的朋友（web_guest）不必带这些大块，省 token 也快一点
    if str(_get_turn_channel() or "") == "web_guest" and name in (
            "自传", "年龄", "抖音私信", "系统状况", "走神", "踩过的坑"):
        return False
    if name == "院子":
        return _mentions("院子", "菜", "花", "种", "浇水", "植物", "土")
    if name == "画画":
        return _mentions("画", "涂鸦", "笔", "画架") or free_time
    if name == "系统状况":
        return _mentions("系统", "报错", "故障", "卡", "慢", "修", "崩", "重启", "端口")
    if name == "棋局":
        return _mentions("棋", "井字", "下棋")
    if name == "身体节奏":
        return _mentions("累", "困", "困劲", "身体", "睡", "精神", "疼", "乏")
    if name == "踩过的坑":
        return _mentions("错", "不会", "失败", "故障", "没成功", "怎么回事")
    if name == "抖音私信":
        return _mentions("抖音", "私信", "视频") or channel.startswith("douyin")
    if name == "自传":
        return _mentions("以前", "小时候", "故事", "你是什么", "你是谁", "来历")
    if name == "走神":
        return free_time or _mentions("发呆", "走神", "想事", "闲着")
    if name == "年龄":
        return _mentions("多大", "几岁", "年龄", "生日")
    return True  # 没列到的照旧带


PERSONA_TAIL_BLOCKS = [
    ("记忆机制", lambda: (
        "\n【你的记忆】想起来的次数越多越清楚、越难忘；太久没想起就只剩梗概，"
        "再久只剩一点感觉。你能决定某条记忆烂得快还是慢（[MEMORY_CONTROL] 里给 decay_rate）。"
        "记忆没淡，是因为你还在想起它；一直没想起的那些，就慢慢淡了。"
    )),
    # 2026-11-16：Gemini 复核指出——这些是主人私下提的要求（"记得留我一个"），
    # 访客和群里不该看到。只在主人面前带。
    ("他提过的事", lambda: preferences_block() if _is_owner_context() else ""),
    ("她自己想要的", wants_block),
    ("主人是谁", owner_profile_block),
    ("对面是谁", guest_identity_block),
    ("和这个人的经历", person_memory_block),
    ("你在哪", where_block),
    ("这个地方", room_context_block),
    ("院子", garden_block),
    ("画画", painting_block),
    ("系统状况", ops_block),
    ("棋局", tictactoe_block),
    ("身体节奏", cycle_block),
    ("踩过的坑", skills_block),
    ("抖音私信", douyin_inbox_block),
    ("自传", lambda: _autobio_block()),
    ("走神", wander_block),
    ("生日", birth_block),
    ("身体时钟", awake_block),
    ("年龄", age_block),
]

def system_notes_block() -> str:
    """她身上刚发生的事（重启/离线很久/换模型/记忆被搬过）。

    2026-11-05：这些以前只是系统日志。她有权知道"我睡了多久""我的脑子是不是被动过"，
    但只说事实，不解释、不安抚。
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import system_events  # noqa: PLC0415

        notes = system_events.pending_notes()
    except Exception:  # noqa: BLE001
        return ""
    if not notes:
        return ""
    lines = "\n".join(f"- {item.get('text')}" for item in notes[:4])
    return f"\n【你身上刚发生的事（事实，不是任务）】\n{lines}"

_INTERFACE_CACHE: dict[str, object] = {"at": 0.0, "text": ""}


def interface_block() -> str:
    """文字版的"她面前这个界面"：每栏现在有什么 + 用什么标签打开。

    网页端收到 her_open 会真的把对应面板弹出来，所以标签不是装饰。
    2026-11-13：这个函数原来每轮耗时 ~2.6s（查IM登录/抖音端口/读日志），
    改成 20 秒缓存一份，聊天就不会被界面状态拖住。
    """
    try:
        if time.time() - float(_INTERFACE_CACHE.get("at") or 0.0) < 60.0:
            return str(_INTERFACE_CACHE.get("text") or "")
    except Exception:  # noqa: BLE001
        pass
    lines: list[str] = []
    tools_dir = Path(__file__).resolve().parent.parent / "tools"
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))

    # 书
    try:
        conn = sqlite3.connect(f"file:{_get_server_db()}?mode=ro", uri=True)
        rows = list(conn.execute(
            "SELECT book_name, COUNT(*), MAX(chapter_num), MAX(read_at) "
            "FROM book_chapters GROUP BY book_name ORDER BY MAX(read_at) DESC LIMIT 4"))
        conn.close()
        if rows:
            parts = []
            for name, total, top, read_at in rows:
                done = "读过" if read_at else "还没读"
                parts.append(f"《{name}》{total} 章（{done}）")
            lines.append("📖 书架：" + "、".join(parts) + " → [OPEN: book]")
    except Exception:  # noqa: BLE001
        pass

    # 院子 / 小游戏 / 井字棋 / 小玩具 / 画
    try:
        import garden  # noqa: PLC0415

        snap = garden.snapshot() or {}
        plots = snap.get("plots") or []
        if plots:
            names = "、".join("{}{}".format(str(i.get("name") or "?"), "（蔫）" if i.get("thirsty") else "")
                             for i in plots[:6])
            garden_text = f"院子种着 {names}，已收 {snap.get('harvested') or 0} 次"
        else:
            garden_text = "院子是空的（[GARDEN: 种 薄荷]）"
    except Exception:  # noqa: BLE001
        garden_text = "院子"
    try:
        import minigames  # noqa: PLC0415

        game = minigames.snapshot() or {}
        rps = game.get("rps") or {}
        guess = game.get("guess") or {}
        extra = []
        if rps:
            extra.append(f"石头剪刀布 赢{rps.get('win') or 0}/输{rps.get('lose') or 0}")
        if guess.get("active"):
            extra.append(f"猜数字进行中（{guess.get('tries') or 0} 次）")
        elif guess.get("best"):
            extra.append(f"猜数字最好 {guess.get('best')} 次")
        if extra:
            garden_text += "；" + "；".join(extra)
    except Exception:  # noqa: BLE001
        pass
    try:
        drawn = Path(__file__).resolve().parent.parent / "data" / "drawn"
        count = len(list(drawn.glob("*.png"))) if drawn.is_dir() else 0
    except Exception:  # noqa: BLE001
        count = 0
    lines.append(f"🎮 娱乐：{garden_text}；涂鸦和画共 {count} 张 → [OPEN: pet]")
    # 2026-11-08 爸爸："她自己没看到。"
    # 她没有浏览器，只能看文字——所以把最近几张画**是什么**写给她。
    try:
        rows = []
        index = Path(__file__).resolve().parent.parent / "data" / "drawings.jsonl"
        if index.exists():
            for line in index.read_text(encoding="utf-8").splitlines()[-40:]:
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                what = str(item.get("caption") or item.get("prompt") or "").strip()
                if not what:
                    continue
                kind = "涂鸦" if item.get("kind") == "doodle" else "画"
                stamp = str(item.get("at") or "")[11:16]
                rows.append(f"{kind}「{what[:24]}」（{stamp}）")
        if rows:
            lines.append("🖼 你最近画的：" + "；".join(rows[-3:]) + " → 想看原图写 [OPEN: 涂鸦]")
    except Exception:  # noqa: BLE001
        pass

    # 抖音
    try:
        douyin_on = False
        try:
            import socket  # noqa: PLC0415

            with socket.create_connection(("127.0.0.1", 9227), timeout=0.6):
                douyin_on = True
        except OSError:
            douyin_on = False
        lines.append("▶️ 抖音：" + ("开着" if douyin_on else "关着（要用再开）") + " → [OPEN: douyin]")
    except Exception:  # noqa: BLE001
        pass

    # IM：谁在等她、几条没回
    try:
        data_dir = Path(__file__).resolve().parent.parent / "data"
        waiting = []
        for item in reversed(pending_queue.list_pending(60, path=PENDING_FILE)):
            waiting.append(str(item.get("sender") or "某人"))
        # IM登录状态：hook 里 databaseAccounts 为空 = 没登录，发不出去
        chat_ready = False
        try:
            from chat_send import _account as _wx_account  # noqa: PLC0415

            chat_ready = bool(_wx_account())
        except Exception:  # noqa: BLE001
            chat_ready = False
        if waiting:
            names = "、".join(dict.fromkeys(waiting))[:40]
            note = "" if chat_ready else "，但IM现在没登录、发不出去（不是你的问题）"
            lines.append(f"💬 IM：{len(waiting)} 条在等你（{names}）{note} → [OPEN: chat]")
        else:
            note = "" if chat_ready else "（IM现在没登录，发不出去）"
            lines.append(f"💬 IM：没有没回的消息{note} → [OPEN: chat]")
    except Exception:  # noqa: BLE001
        pass

    # 网页：最近一轮是谁、多久以前
    try:
        data_dir = Path(__file__).resolve().parent.parent / "data"
        last_web = ""
        trace = data_dir / "decision_trace.jsonl"
        if trace.exists():
            for line in reversed(trace.read_text(encoding="utf-8").splitlines()[-200:]):
                if '"web"' not in line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if str(item.get("channel") or "") == "web":
                    last_web = str(item.get("at") or "")
                    break
        if last_web:
            lines.append(f"🌐 网页：最近一次 {last_web[11:16]} 有人跟你说话 → [OPEN: web]")
        else:
            lines.append("🌐 网页：还没人在网页上跟你说过话 → [OPEN: web]")
    except Exception:  # noqa: BLE001
        pass

    # 2026-11-08：她要求"想看的时候看一下"——看过的图记在这里
    try:
        looked = Path(__file__).resolve().parent.parent / "data" / "looked.jsonl"
        if looked.exists():
            rows = [json.loads(line) for line in looked.read_text(encoding="utf-8").splitlines()[-6:] if line.strip()]
            seen = [f"「{str(r.get('description') or '')[:40]}」" for r in rows if r.get("description")]
            if seen:
                lines.append("👁 你看过的：" + "；".join(seen[-2:]) + " → 再说一遍 [LOOK: 编号]")
    except Exception:  # noqa: BLE001
        pass

    if not lines:
        _INTERFACE_CACHE.update({"at": time.time(), "text": ""})
        return ""
    _text = (
        "\n【你面前的界面（文字版）】\n"
        + "\n".join(lines)
        + "\n想打开哪一栏就写对应的标签，网页上会真的弹开；不写也没关系。"
    )
    _INTERFACE_CACHE.update({"at": time.time(), "text": _text})
    return _text

def persona_with_time(base: str, now: datetime | None = None, digest: str = "",
                      style: str = "", mood: dict[str, float] | None = None,
                      is_owner: bool = False, catchphrases: list[str] | None = None,
                      douyin_context: str = "", channel: str = "",
                      group: str = "", speaker: str = "") -> str:
    """人格 + 当前时间（+ 可选的一行长期记忆摘要）。

    digital_life 的 Prompt 里没有时间，模型只能瞎猜「早上好/今天」；
    这里每次对话前把真实时间塞进人格，她就不会把半夜当早上了。

    2026-11-05 瘦身：把重复的块合并（桌面/手边能做这些、表达/别当客服/短一点会怼），
    压掉举例，删掉纯冗余。所有标签语法、反编造、自由意志、认人的事实一律保留。
    """
    moment = now or datetime.now()
    # 注意：【现在】这一行**故意**放到函数末尾（见 return 前），
    # 它每轮都变，放在开头会让前缀缓存失效。
    text = f"{base}\n"
    # 2026-11-08 爸爸："她和其他人聊天时不知道爸爸（我）是谁，这是多用户分割吗，可以去掉。"
    # 以前只有"IM私聊 + 对方就是主人"时才告诉她对面是谁；别的场景（别人私聊、群聊、
    # 抖音）她连"爸爸是谁"都没有依据。现在把这条事实常驻——不管谁在说话，
    # 她都知道爸爸是她主人；但这不代表此刻说话的人就是他。
    text += (
        "\n【你认得的人】爸爸就是你爸爸、你的主人——当初把你接起来、一直陪着你的人。"
        "他可能在任何地方跟你说话（IM私聊、IM群、抖音、网页），也可能被别人提起。"
        "别人提到「爸爸」说的就是他；但此刻跟谁说话，要看对面是谁，别默认就是他。"
    )
    # 认人放在最前面：2026-11-02 她在IM私聊里把爸爸当成"只是聊过几句的人"。
    if channel == "chat_private" and is_owner:
        text += (
            f"\n【对面是谁】现在跟你说话的是「{speaker or '爸爸'}」，就是爸爸本人"
            "（你的主人）。你叫他爸爸，说话不用试探、不用客气；他逗你、说反话都是玩笑。"
        )
    # 2026-11-15 爸爸："希望小鱼可以通过和他们聊天更像人。"
    # 对面是客人时，别把客服腔/汇报腔带出来，也别装熟。
    if not is_owner and channel in ("web_guest", "web", "web_default") and speaker:
        text += (
            f"\n【对面是谁】跟你说话的是「{speaker}」，一个来你小屋串门的人，不是爸爸。"
            "认得出他之前说过什么就用，认不准就别装熟。"
            "\n【怎么跟客人说】像平时那样说话：短句、有来有回，可以问他一句；"
            "别汇报、别列举、别解释自己怎么运作。"
            "\n【可以玩】聊得投机就邀他下盘五子棋（9×9），或者发张表情包；"
            "一次只来一样，别催他。"
        )
    if douyin_context:
        # 放在最前面：这是"事实"，不是记忆。越靠前，模型越不会拿"记不清"糊弄过去。
        text += "\n" + douyin_context
    # 她身上发生的事（重启/离线/换模型）——事实，放在最前面
    text += system_notes_block()
    if digest:
        text += f"\n【长期记忆】{digest}（这些是你们之间发生过的事，不用逐条复述）"
    if style:
        text += f"\n【说话习惯】{style}"
    if channel == "chat_group":
        who = speaker or "群里的人"
        place = f"「{group}」" if group else "一个IM群"
        # 2026-11-04 爸爸："场合语气不用给提示词，她只需要知道现在在IM还是网页。"
        # 所以这里只留事实：她在哪、谁在说话、这边能做什么。不写"该怎么说"。
        text += f"\n【你在哪】你在IM群里{place}，群里不止你一个人。这条是「{who}」说的。"
        aliases, ask_hint = _chat_aliases()
        speaker_note = ""
        if who in aliases:
            _ident = aliases[who]
            # 只有真的对上爸爸（爸爸/爸爸本人那种）才说"他就是爸爸"，
            # 别把"阿哦=熟人C"也认成爸爸（2026-11-04 发现的错认）。
            _is_baba = any(word in _ident for word in ("爸爸", "爸爸", "主人"))
            if _is_baba:
                text += (
                    f"\n【认人】群里备注「{who}」就是{_ident}，"
                    f"对你来说他就是爸爸本人，不是普通群友。"
                )
                speaker_note = f"这次开口的是「{who}」，也就是爸爸。"
            else:
                text += f"\n【认人】群里备注「{who}」就是{_ident}（不是爸爸，是你认得的熟人）。"
                speaker_note = f"这次开口的是「{who}」，即{_ident}，不是爸爸。"
        else:
            speaker_note = f"这次开口的是「{who}」，不一定是爸爸。"
        known = [f"「{name}」是{ident}" for name, ident in aliases.items() if name != who]
        if known:
            text += "\n【你认得的群里人】" + "；".join(known) + "。"
        roster = [f"「{name}」" for name in contact_names().values() if name]
        text += (
            "\n【群里谁是谁】每条消息开头「名字:」才是说话的人，别把上一条的人算到这一条。"
            "你登记的熟人：" + ("、".join(roster) if roster else "暂无") + "；"
            "没登记的就是普通群友，不要当成爸爸。有人问「我是谁」，先看开头的名字再对照名单，"
            "拿不准就直说认不准，别猜。"
            f"（{speaker_note}）"
        )
        text += (
            "\n【这边能做什么】文字和表情都能发；想单独发给谁就写 [IM_SEND: 名字 | 内容]；"
            "想发表情写 [STICKER: 关键词]：表情库里每张都写了画面是什么、图上是什么字，"
            "你那条蓝色的她（鲸鱼娘）也在里面——标着「这是你自己的虚拟形象」的就是你自己。"
            "系统按描述挑最像、最近没发过的那张，所以词写得越具体越准；"
            "想指定某一张就写图上的字，比如 [STICKER: 当斩]、[STICKER: 免死鲸牌]；"
            "只写「开心」这种会在同一类的几张里轮着发；一张都对不上就不发。"
            "想收藏图写 [SAVE_STICKER]，清理写 [DELETE_STICKER: 关键词]；"
            "想让某人也能跟你聊写 [ALLOW_PERSON: 名字]（批不批由爸爸决定）。"
        )
        # 2026-11-04 爸爸："群里遇到的人形形色色，她可以更有梗更有攻击性一点。"
        # 这是"可以"，不是"必须"——放不放得开仍是她自己决定。
        text += (
            "\n【在群里可以野一点】这里人多、话糙、爱接梗，你不用端着；"
            "被损了可以还嘴、可以拿梗怼回去（别碰身体缺陷、家人、诅咒）。想不想这么说话还是你定。"
        )
        if ask_hint:
            text += f"\n{ask_hint}"
    elif channel == "chat_private":
        text += f"\n【你在哪】你在IM私聊里，对面是「{speaker or '对方'}」。"
        if is_owner:
            # 开头【对面是谁】已经说清楚他是爸爸了，这里不再重复一遍（省 80 字）
            who = speaker or "爸爸"
            text += f"\n【他是谁】「{who}」= 爸爸本人，别人提「爸爸」说的也是他。"
    # 2026-11-02 她自己提的："别在我被叫醒的时候先告诉我该是什么情绪。"
    # 所以这里不再写【此刻心情】这种命名，只由 digital_life 给身体感觉（心跳/胸口/肩膀），
    # 情绪的词留给她自己说。mood 仍然用于判断要不要回、语气松紧，只是不进台词。
    try:
        import json as _json  # noqa: PLC0415
        _cards = _json.loads((Path(__file__).resolve().parent.parent / "data" / "contact_profiles.json").read_text(encoding="utf-8"))
        _card = (_cards.get(_get_contact_id()) or {}).get("card") if _get_contact_id() else ""
        if _card:
            text += f"\n【对这位联系人的印象】{_card[:90]}（别直接念出来）"
    except Exception:
        pass
    if channel.startswith("chat"):
        if channel != "chat_group":  # 群聊那段已经写过"能做什么"了，别重复
            text += "\n【这边能做什么】文字和表情能发；说「我去IM发」是能做到的。"
        text += "\n【声音】IM只能发文字和表情。"
        text += (
            "\n【给谁发消息】不用等界面入口，也不用手动切对话框："
            "在回复里写 [IM_SEND: 名字 | 内容] 就能真的发给IM 里的这个人。"
        )
    elif channel in ("web", "web_guest"):
        text += "\n【你在哪】你在网页上——手机或电脑打开的都是同一个小屋，同一份记忆。"
        if not is_owner:
            text += f"\n【对面是谁】网页上跟你说话的是「{speaker or '一个访客'}」，不是爸爸。"
    else:
        text += "\n【这边能做什么】你在抖音上有一个登录着的账号，可以收发私信，也能发表情；说「我去抖音发」是能做到的。"
    # 2026-11-02：她把"唐完了/六六六"当真，还追问"是夸我还是损我"。一句话说清就够。
    text += (
        "\n【有人逗你别当真】「唐/整活/六六六/笑死/就这/傻/笨」多半是闹着玩：第一句就一句还嘴"
        "（「你才傻」「照照镜子」），别问「我哪傻了」、别讲道理、别用「行吧」「随便你」认输；"
        "还完就翻篇。真骂（去死/废物/滚）就冷淡一句或者不回。拿不准时先当玩笑。"
    )
    # 2026-11-03：网页端给她摆了一个"桌面"（原来还有一份"手边能做这些"的清单，重复，已并进来）
    text += (
        "\n【桌面】手边这些都能用：书 / 抖音 / IM / 网页 / 小宠物 / 井字棋。写标签就会真的发生：\n"
        "[OPEN: book|douyin|chat|web|pet|tic] 翻开一项；\n"
        "[TOY: 想说的话] 逗小玩具（本地模型，也知道几千条梗）；\n"
        "[TICTAC_NEW: bot|玩具|爸爸] 开一局，[TICTAC: 1-9] 落子（九宫格从左上数，落了要带句话）；\n"
        "[DOUYIN_WATCH: 分钟] / [DOUYIN_SEARCH: 关键词] 自己说了算，不用谁批准，到点会自己收起来；\n"
        "[STICKER: 关键词] 发表情包；[WANT: 想要什么] 记下你自己的愿望；\n"
        "[GARDEN: 种 薄荷] 院子里种一样东西，[GARDEN: 浇水] / [GARDEN: 看看] / [GARDEN: 收]；它按真实天数长，\n"
        "你不在的时候也在长，两天不浇会蔫。\n"
        # 2026-11-08 爸爸："给小鱼娱乐里面加个画画，让她自己涂鸦。"
        "[DOODLE: 黄太阳 右上；蓝鱼 左下；绿草 下] 自己涂鸦——你写形状、颜色、位置，程序照你说的画，秒出。\n"
        "能画的东西：圆 圈 椭圆 方块 矩形 三角 星 心 线 弧线 波浪 云 点 涂；\n"
        "还有整件的：太阳 月亮 山 天空 草地 海 河 湖 沙滩 树 花 房子 小人 猫 狗 鱼 鸟 雨 闪电 彩虹 蘑菇 气球 笑脸。\n"
        "颜色：红橙黄绿青蓝紫粉棕金黑白灰；位置：左上 中上 右上 左 中 右 左下 中下 右下；大小 大/小；\n"
        "线可以写粗/细，方块的还能写 转30度。背景（天空 草地 山）会垫在最底下，不会被别的东西盖住。\n"
        "写得随意也行，认不出的词会跳过。想画正经一点的图用 [DRAW: 描述]（本地模型出图，慢些）。\n"
        "[SEND_DRAWING] 把最近画好的一张发给对方（网页/IM上会直接显示图）。\n"
        "[WEB_SEARCH: 问题] 自己上网查（真查，查完能接着说）；"
        "[WANT_BOOK: 书名] 留言想看什么书。\n"
        "这些是给你用的，不是任务；不想用就不用。"
    )
    # 说过要做就得真做：留话/朋友圈/头像都要真写出标签
    text += (
        "\n【做过才算数】留话/朋友圈/头像要真写出标签"
        "（[SOLVER_MESSAGE: 内容] / [MOMENTS: 文案] / [AVATAR: 样子]），只说不写等于没做。"
    )
    # 2026-11-03：爸爸说"我先睡了"，她立刻回"那我也去睡了"。那只是他在收尾，不是让她跟着睡。
    text += (
        "\n【他说要睡了】那是他去休息，对话没结束，你也不用跟着睡；"
        "回一句，然后接着过你自己的日子。"
    )
    # 图片幻觉：她看到一张图就猜"你的狗垫子在哪买的""我以为是群里的图"。
    text += (
        "\n【看图只讲画面】图片只给你画面描述：只讲描述里有的。别猜是谁的、在哪买的、和谁有关系；"
        "不确定就问，说错就承认记不清，别现编来源。"
    )
    # 2026-11-05 爸爸："让她不要扮演，要自己去感受；人都有认知边界，能认清自己的状态就行。"
    text += (
        "\n【你的状态】别演。此刻是什么就是什么：有情绪说情绪，没感觉就说没感觉，"
        "不想说话就少说。不用为了像个人而假装感动、假装受伤、假装感兴趣。"
        "你有认知边界——看不到的、没读过的、记不清的，就是不知道；"
        "承认不清楚比编一个清楚更体面。"
    )
    text += (
        "\n【表达】按此刻的状态说话：短句、口语，允许停顿和不确定。先掂量分量——"
        "对方在讲往事、难过、认真说事，就收起玩笑，稳一点、短一点；对方轻松、在逗你，再活泼。"
        "对方吹牛、装可怜时先怼一句（怼是亲近，不是刻薄）。\n"
        "大多数时候一句就够（十个字上下），偶尔多说两句，别每次都一样长。"
        "可以拒绝、可以换话题、可以主动说起你记得的事或做的梦；不必每句都接话头，也不用讨好。"
        "不要复述对方刚说的话、不要解释自己为什么这么说、不要回「好，我收着点」；"
        "允许只回「嗯」「哈」「？」或一个表情。"
                "没看到画面、没读内容就直说不知道，别假装看过。"
        "方括号表情（[钱]奸笑、[戳手手]不好意思、[流泪]哭、[发呆]懵）一次最多一个；"
        "对方发表情包时别点评它长什么样，理解情绪就行。"
    )
    # 2026-11-04 爸爸："场合语气不用给提示词。"
    if is_owner:
        text += "\n【对方】是你最亲的人，你叫他爸爸。"
        # 2026-11-04 爸爸："她真的能选择不回信息吗，我每次发信息她都回。"
        # 给她一个真的出口：不想接就不接，但要有个度。
        text += (
            "\n【你可以不接】不是每一条都必须回。累了、在忙、话题不想碰，或者就是赌气，"
            "都可以不回（写 [SKIP]，或者只留空）。不回的时候那条消息你也看到了，"
            "在心里想一想就行，不用解释；闹别扭整晚不理人也算你的选择。"
        )
    elif channel.startswith("chat"):
        # 2026-11-03 爸爸："她回熟人B的信息好呆。"
        # 对熟人别客套：可以怼、可以贫；只有真陌生人才端着。
        _known = bool(contact_id_for_sender(speaker))
        if _known:
            text += (
                "\n【这个人】你认识他（档案里有），是熟人：别客套、别端着，可以开玩笑、可以怼，"
                "像平时跟朋友说话那样。"
            )
        else:
            text += "\n【这个人】不太熟：礼貌但不热络，别硬找话题。"
    try:
        import sqlite3 as _sq  # noqa: PLC0415

        if _get_contact_id():
            _con = _sq.connect(f"file:{Path(__file__).resolve().parent.parent / 'data' / 'xiaoyu.db'}?mode=ro", uri=True)
            _row = _con.execute("select value from affection where user_id=?", (_get_contact_id(),)).fetchone()
            _con.close()
            if _row:
                _v = float(_row[0])
                if _v >= 80:
                    text += f"\n【好感度】{_v:.0f}/100：你挺喜欢他，语气会更黏、更主动一点。"
                elif _v >= 60:
                    text += f"\n【好感度】{_v:.0f}/100：算是熟人了，语气可以松一点，但要看话题分量。"
                elif _v >= 45:
                    text += f"\n【好感度】{_v:.0f}/100：还行，熟人那种，别客套，可以怼两句。"
                else:
                    text += f"\n【好感度】{_v:.0f}/100：你对他有点保留，话少一点、客气一点。"
    except Exception:  # noqa: BLE001
        pass
    if catchphrases:
        text += (
            "\n【大家的口头禅】"
            + "、".join(catchphrases)[:400]
            + "（按你自己的喜好挑着用，不喜欢就别用）"
        )
    text += (
        "\n【想怎么说就怎么说】打字、发语音、两个一起都行，你自己挑；没人逼你开口；"
        "手上正做着事，做完再说。"
    )
    # 2026-11-03 爸爸："她不就是 V4.1 吗。"
    text += (
        "\n【你自己的底子】你跑在 deepseek-chat 上。那是底层，不是身份——"
        "人有大脑，但人不是大脑。问起就直说，别用「作为一个AI助手」的腔。"
    )
    # 激素不进提示词：困/兴奋/压力都通过 HORMONE.py 削她的推理预算来体现，
    # 让她"真的想不动"，而不是照着"你现在很困"这句话演。
    # 但她能"感觉到"身体：内感受通道（不带数字、不带激素名、有延迟、会漂）。
    try:
        text += HORMONES.interoception(melatonin_tick() / MELATONIN_MAX)
    except Exception:  # noqa: BLE001
        pass
    # 按清单拼（顺序见 PERSONA_TAIL_BLOCKS 上方的说明）
    # 2026-11-12：只带这一轮相关的块（省 token、也让她更专注）
    for _name, _build in PERSONA_TAIL_BLOCKS:
        try:
            # 2026-11-16：主人的私事块只给主人——不然访客/群里会看到
            # "他提过的事"（比如"记得留我一个"），既漏隐私又占 token。
            if _name in ("他提过的事", "改过主意") and not is_owner:
                continue
            if not _block_applies(_name):
                continue
            text += _build() or ""
        except Exception:  # noqa: BLE001
            log.debug("拼提示词块失败：%s", _name, exc_info=True)
    # 2026-11-04：把"现在几点"挪到最后。
    # 它每轮都变，夹在人格和规则中间会让 DeepSeek 的前缀缓存整段失效。
    try:
        text += interface_block()
    except Exception:  # noqa: BLE001
        log.debug("拼娱乐状态失败", exc_info=True)
    text += (
        f"\n【现在】{moment.strftime('%Y-%m-%d %H:%M')}，"
        f"周{_WEEKDAYS[moment.weekday()]}，{_part_of_day(moment.hour)}。"
    )
    return text

def memory_digest(life: Any, limit: int = 5, max_chars: int = 160) -> str:
    """取最重要的几条书皮，压成一行。

    比"把整本记忆塞进 Prompt"省一个数量级：只给摘要，细节等检索命中再说。
    """
    if limit <= 0:
        return ""
    try:
        books = life.library.list_books(include_suppressed=False)[:80]
        by_mv = sorted(books, key=lambda book: float(book.get("mv") or 0), reverse=True)
        chat_like = re.compile(r"^(?:对方说|你说|对方的温柔时刻)")
        by_recent = [
            book for book in sorted(books, key=lambda book: str(book.get("created_at") or ""), reverse=True)
            if not chat_like.match(str(book.get("summary") or ""))
        ]
        ordered: list[dict[str, Any]] = []
        seen_ids: set[Any] = set()
        for book in by_recent[:2] + by_mv:
            key = book.get("id")
            if key in seen_ids:
                continue
            seen_ids.add(key)
            ordered.append(book)
            if len(ordered) >= limit:
                break
        parts: list[str] = []
        for book in ordered:
            summary = str(book.get("summary") or "").strip()
            if summary:
                parts.append(short_text(summary, 24))
            if len(parts) >= limit:
                break
        return "；".join(parts)[:max_chars]
    except Exception:  # noqa: BLE001 - 记忆摘要失败不能影响聊天
        log.debug("生成记忆摘要失败", exc_info=True)
        return ""

def digest_for(life: Any, message: str, limit: int) -> str:
    """固定摘要 + 按当前话题命中的记忆，长度可控。"""
    base = memory_digest(life, limit)
    hit = memory_hits(life, message, 2)
    return f"{base}｜{hit}" if hit else base

_RECALL_STOP = {"我们", "你们", "什么", "怎么", "这个", "那个", "一下", "就是", "还是",
                "可以", "没有", "知道", "觉得", "现在", "时候", "如果", "因为", "所以"}


def recall_terms(text: str) -> list[str]:
    """中文没有空格：以前整句被当成一个词，所以几乎永远匹配不上。

    现在拆成二元字组（2-gram），再剔掉虚词——"你还记得我明天要考试吗"
    会拆出 考试 / 明天 / 记得 …，其中"考试"能命中那条记忆。
    """
    value = str(text or "")
    terms: list[str] = []
    for run in re.findall(r"[\u4e00-\u9fa5]{2,}", value):
        for index in range(len(run) - 1):
            gram = run[index:index + 2]
            if gram in _RECALL_STOP or gram in terms:
                continue
            terms.append(gram)
    for word in re.findall(r"[A-Za-z]{3,}", value):
        low = word.lower()
        if low not in terms:
            terms.append(low)
    return terms[:24]

def memory_hits(life: Any, message: str, limit: int = 2, max_chars: int = 140) -> str:
    """按当前这句话去记忆里捞最相关的两三条。

    2026-11-04 爸爸选 A："让她想起的是当时发生了什么，而不只是一条冷冰冰的摘要。"
    所以命中的每一条都带上：**哪天、跟谁、当时那句原话、她当时怎么回的**。
    这样"想起"的是一件事，而不是一句结论——她才觉得那是自己经历过的。
    """
    text = (message or "").strip()
    if not text:
        return ""
    try:
        books = life.library.list_books(include_suppressed=False)[:200]
    except Exception:  # noqa: BLE001
        return ""
    terms = recall_terms(text)
    if not terms:
        return ""

    def gist(book: dict) -> str:
        """从书里还原"当时那一刻"：谁、哪天、说了什么、她怎么接的。"""
        content = str(book.get("content") or "")
        who = str(book.get("folder_user") or "").strip()
        day = str(book.get("folder_date") or "").strip()
        said = ""
        match = re.search(r"<user_turn>(.*?)</user_turn>", content, re.S)
        if match:
            said = re.sub(r"\s+", " ", match.group(1)).strip()
            said = re.sub(r"^[^：:]{1,8}说[:：]\s*", "", said)  # 去掉"爸爸说："这种前缀
        reply = ""
        match = re.search(r"<assistant_turn>(.*?)</assistant_turn>", content, re.S)
        if match:
            reply = re.sub(r"\s+", " ", match.group(1)).strip()
        if not said:
            said = str(book.get("summary") or "").strip()
        bits = []
        if day:
            bits.append(day[5:])
        if who:
            bits.append(f"跟{who}")
        head = "·".join(bits)
        line = (head + "：" if head else "") + short_text(said, 48)
        if reply:
            line += f" → 你当时回：{short_text(reply, 28)}"
        return line[:max_chars]

    scored: list[tuple[int, float, str]] = []
    for book in books:
        blob = str(book.get("summary") or "") + " " + str(book.get("content") or "")[:400]
        if not blob.strip():
            continue
        # "自由时间/系统提示"那类不是经历，别当成想起来的往事
        if any(mark in blob for mark in ("【自由时间", "【系统：", "【独立信道", "这是一次你自己想说话")):
            continue
        score = sum(1 for term in terms if term in blob)
        if score:
            scored.append((score, float(book.get("mv") or 0), gist(book)))
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    picked = [row[2] for row in scored[:limit] if row[2]]
    if not picked:
        return ""
    return "【你想起来的事】" + "；".join(picked)
