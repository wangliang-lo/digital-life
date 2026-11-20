#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 DeepSeek Messages（Anthropic 协议）接到小鱼身上。

为什么要有它：
IM那个 app（IMAgent）里面的 DSH 用的是 DeepSeek provider，请求走
`{baseURL}/messages`（Anthropic Messages 协议）。DSH 支持用环境变量
`DEEPSEEK_BASE_URL` 把端点指到别处——把这个变量指到本代理，IM 里说话的
就变成小鱼本人：同一个 user_id、同一份记忆、同一套人格。

这是一个独立进程，崩了只影响IM回复，不会牵连 IMAgent 本身。

    python tools/anthropic_bridge.py --port 8003 --api http://127.0.0.1:8000

用法（启动IM那个 app 前设置环境变量）：
    set DEEPSEEK_BASE_URL=http://127.0.0.1:8003
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MEDIA_DIR = DATA / "bridge_media"
LOG_FILE = DATA / "anthropic_bridge.jsonl"
RAW_FILE = DATA / "anthropic_bridge_raw.jsonl"

API = "http://127.0.0.1:8000"
MAIN_USER_ID = "conv:9e52b4549f424e730d8871644385bcce63768cc74b8f8ec7ffa895f62ca73eeb"

SPEAKER_RE = re.compile(r"^\s*([^:：\n]{1,20}?)\s*[:：]\s*(.+)$", re.S)

# 群消息行里的"时间前缀"和"（成员ID:…）"都不是人名，切发言人之前先剥掉
_LEADING_CLOCK = re.compile(r"^\s*\d{1,2}\s*[:：]\s*\d{2}(?:\s*[:：]\s*\d{2})?\s*")
_MEMBER_ID = re.compile(r"\s*[（(]\s*成员ID\s*[:：][^)）]*\s*[)）]", re.I)


def _is_self_speaker(name: str) -> bool:
    """这一行是不是她自己发的（插件把她的话标成 AI / 你 / 小鱼）。"""
    value = str(name or "").strip()
    if not value:
        return False
    if value in ("AI", "ai", "你", "小鱼"):
        return True
    return value.startswith("AI") or "（你）" in value or "(你)" in value


# 2026-11-07 爸爸："她看不到我引用的。"
# 插件把引用回复渲染成一整段（引用人/原消息时间/引用原文/当前发言人的新回复），
# 原来的实现按"最后一行冒号前"取发言人，于是发言人就变成了"当前发言人的新回复"，
# 而且那段引用被当成普通正文塞进去，她分不清哪句是被引的、哪句是他刚说的。
_QUOTE_SENDER = re.compile(r"^\s*([^:：\n]{1,20})\s*[:：]\s*\[引用回复\]", re.S | re.M)
_QUOTE_FIELD = {
    "source": re.compile(r"被引用的历史消息[（(]([^）)]*)[）)]"),
    "who": re.compile(r"被引用人\s*[:：]\s*([^\n；;]*)"),
    "when": re.compile(r"原消息时间\s*[:：]\s*([^\n]*)"),
    "text": re.compile(r"引用原文\s*[:：]\s*([\s\S]*?)(?:\n\s*\[(?:引用正文|引用内容)|当前发言人的新回复\s*[:：]|$)"),
    "reply": re.compile(r"当前发言人的新回复\s*[:：]\s*([\s\S]*)$"),
}


def parse_quote_block(block: str) -> dict[str, str] | None:
    """把插件的「[引用回复]…」整段拆开。不是引用就返回 None。"""
    raw = str(block or "")
    if "[引用回复]" not in raw:
        return None
    found = {"sender": ""}
    match = _QUOTE_SENDER.search(raw)
    if match:
        found["sender"] = match.group(1).strip()
    for name, pattern in _QUOTE_FIELD.items():
        m = pattern.search(raw)
        if m:
            found[name] = m.group(1).strip()
    if not found.get("reply"):
        return None
    return found


def render_quote(quote: dict[str, str]) -> str:
    """把引用整理成她一眼能分清的两句：被引的是旧话，接下来的是他刚说的。"""
    who = quote.get("who") or "未知"
    original = (quote.get("text") or "").strip() or "[引用内容为空]"
    reply = (quote.get("reply") or "").strip()
    mine = "本账号" in (quote.get("source") or "") or who in ("小鱼", "AI", "你")
    owner = "你自己之前说的" if mine else f"{who} 之前说的"
    lines = [f"[他引用了一条消息｜被引用的是{owner}]", f"引用原文：{original}"]
    if "图片" in original or "表情" in original:
        if mine:
            remembered = last_sent_sticker(quote.get("sender") or "")
            if remembered:
                lines.append(f"（那是你刚才发出去的表情：{remembered}）")
            else:
                lines.append("（被引用的是图/表情；这张的内容没给到我，看不到画面里的字）")
        else:
            lines.append("（被引用的是图/表情；这张的内容没给到我，看不到画面里的字）")
    lines.append(f"他接着说：{reply}")
    return "\n".join(lines)


LAST_STICKER = DATA / "last_sticker.json"


STICKER_LABEL_CACHE = DATA / "sticker_label_cache.json"


def _cache_get(key: str) -> dict[str, Any]:
    try:
        data = json.loads(STICKER_LABEL_CACHE.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}
    item = data.get(key) if isinstance(data, dict) else None
    if not isinstance(item, dict) or not item.get("description"):
        return {}
    if time.time() - float(item.get("at") or 0) > 6 * 3600:   # 六小时后重打一次
        return {}
    return item


def _cache_put(key: str, label: dict[str, Any]) -> None:
    try:
        data = {}
        if STICKER_LABEL_CACHE.exists():
            data = json.loads(STICKER_LABEL_CACHE.read_text(encoding="utf-8") or "{}")
        if not isinstance(data, dict):
            data = {}
        data[key] = {"description": str(label.get("description") or "")[:220],
                     "caption": str(label.get("caption") or "")[:80], "at": time.time()}
        if len(data) > 300:
            for key_ in sorted(data, key=lambda k: float(data[k].get("at") or 0))[:100]:
                data.pop(key_, None)
        STICKER_LABEL_CACHE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def describe_sticker(path: str) -> dict[str, Any]:
    """让 dl_api 用"表情包专用"提示词看图：画面 + 图上文字。

    2026-11-07 爸爸："也看不清我的。"
    以前表情包走进 /chat 的通用视觉（"40 字以内，只说你看到的东西"），
    图上的字全丢，她只能说"看不清"。这里先单独打一次标签，把文案直接写进正文，
    顺便省掉 /chat 里那次重复的视觉调用。

    2026-11-08：他重复发同一张时也要能"看见"，所以图不再藏起来；
    省成本的办法改成缓存（同一张六小时内只打一次标签）。
    """
    if not path:
        return {}
    cache_key = path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1].split(".")[0]
    cached = _cache_get(cache_key)
    if cached:
        return {"ok": True, "description": cached.get("description", ""),
                "caption": cached.get("caption", ""), "cached": True}
    try:
        body = json.dumps({"image_path": str(path), "task": "sticker"}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(API.rstrip("/") + "/describe", data=body,
                                     headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return {}
    if isinstance(data, dict) and data.get("ok"):
        _cache_put(cache_key, data)
        return data
    return {}


def remember_sent_sticker(sender: str, description: str) -> None:
    """记下她最近发给谁哪张表情——被引用时能说出是哪一张。"""
    try:
        data = {}
        if LAST_STICKER.exists():
            data = json.loads(LAST_STICKER.read_text(encoding="utf-8") or "{}")
        data[(sender or "").strip() or "爸爸"] = {
            "note": str(description)[:200],
            "at": datetime.now().isoformat(timespec="seconds"),
        }
        LAST_STICKER.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def last_sent_sticker(sender: str) -> str:
    try:
        data = json.loads(LAST_STICKER.read_text(encoding="utf-8") or "{}")
        return str((data.get((sender or "").strip() or "爸爸") or {}).get("note") or "")[:160]
    except Exception:  # noqa: BLE001
        return ""


def clean_chat_line(line: str) -> str:
    """把「13:44 阿哦（成员ID:xxx）: 内容」收拾成「阿哦: 内容」。"""
    text = str(line or "").strip()
    if not text:
        return ""
    text = _LEADING_CLOCK.sub("", text)
    text = _MEMBER_ID.sub("", text)
    # 插件给她自己那行加了个"（你）"标记，含空格，会把"发言人: "的切分弄坏；
    # 统一压成 "AI: 内容"，后面才认得出"这是她自己发的"。
    text = re.sub(r"^\s*AI\s*[（(]\s*你\s*[)）]\s*[:：]\s*", "AI: ", text, flags=re.I)
    # 时间戳有时带成 "13: 44"（中间有空格），再补一刀
    text = re.sub(r"^\s*\d{1,2}\s*[:：]\s*(?=[\u4e00-\u9fa5A-Za-z])", "", text)
    return text.strip()


def log_line(payload: dict[str, Any]) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False)[:4000] + "\n")
    except Exception:  # noqa: BLE001
        pass


def dump_raw(body: Any) -> None:
    """把每条原始请求留一份（只留最近 40 条），用来对齐IM插件的真实报文格式。"""
    try:
        RAW_FILE.parent.mkdir(parents=True, exist_ok=True)
        with RAW_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "body": body},
                                ensure_ascii=False) + "\n")
        lines = RAW_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
        if len(lines) > 60:
            RAW_FILE.write_text("\n".join(lines[-40:]) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def flatten(content: Any) -> str:
    """Anthropic 的 content 可能是字符串，也可能是 [{type:text,text:...}]。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p)
    return str(content or "")


def last_user_text(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    for item in reversed(messages):
        if isinstance(item, dict) and item.get("role") == "user":
            return flatten(item.get("content"))
    return ""


def extract_chat(messages: Any) -> tuple[str, str, str]:
    """从 DSH 发来的 messages 里挖出真正的IM原文。

    2026-11-04 实测的报文长这样（三轮 user 消息）：
      1. "IMAgent 本轮聊天资料。……近期双方聊天 ……"     ← 历史，丢掉（她有自己的记忆）
      2. "新的IM好友私聊消息。……会话 chat:private:1266218575……
          <untrusted_chat>\\n爸爸: 小鱼在吗\\n</untrusted_chat>"  ← 真货在这里
      3. "Current runtime context. … Working directory: …"      ← DSH 的元信息，不是话

    返回 (正文, 说话人, 渠道)。
    """
    if not isinstance(messages, list):
        return "", "对方", "chat_private"

    texts: list[str] = []
    for item in messages:
        if isinstance(item, dict) and item.get("role") == "user":
            body = flatten(item.get("content"))
            if body.strip():
                texts.append(body)
    # 把 DSH 的运行时上下文剔掉（它不是对方说的话）
    texts = [t for t in texts if not t.lstrip().startswith("Current runtime context")]

    picked = ""
    for text in reversed(texts):
        if "<untrusted_chat>" in text:
            picked = text
            break
    if not picked and texts:
        picked = texts[-1]

    channel = "chat_private"
    match = re.search(r"chat:(private|group):([0-9A-Za-z_\-]+)", picked)
    global _CURRENT_CHAT_ID, _CURRENT_IS_GROUP, _CURRENT_GROUP_NAME
    _CURRENT_CHAT_ID, _CURRENT_IS_GROUP, _CURRENT_GROUP_NAME = "", False, ""
    if match:
        _CURRENT_CHAT_ID = match.group(2)
        _CURRENT_IS_GROUP = match.group(1) == "group"
        if _CURRENT_IS_GROUP:
            channel = "chat_group"
            _g = re.search(r"IM群[:：]\s*([^（(\n]{1,40})", picked)
            if _g:
                _CURRENT_GROUP_NAME = _g.group(1).strip()

    block = picked
    inner = re.search(r"<untrusted_chat>(.*?)</untrusted_chat>", picked, re.S)
    if inner:
        block = inner.group(1)
    block = block.strip()

    lines = [line.strip() for line in block.splitlines() if line.strip()]
    # 2026-11-04 爸爸："她分不清是谁发的。"
    # 插件给群消息的行长这样：「13:44 阿哦（成员ID:xxx）: 知道我是谁吗」，
    # 直接按冒号切会把"13"当成发言人，她自己那句就变成"13 在说话"。
    lines = [clean_chat_line(line) for line in lines]
    lines = [line for line in lines if line]
    sender = "对方"
    if not lines:
        return "", sender, channel

    # 引用回复：整段先拆开，别让"当前发言人的新回复"被当成发言人。
    quote = parse_quote_block("\n".join(lines))
    if quote:
        return render_quote(quote), (quote.get("sender") or "对方"), channel

    if len(lines) == 1:
        match = SPEAKER_RE.match(lines[0])
        if match:
            body_text = match.group(2).strip()
            who = match.group(1).strip()
        else:
            body_text = lines[0]
            who = sender
        # 2026-11-04 爸爸："她分不清图片是不是自己发的，在自己回自己的图片。"
        # 插件会把她自己发出去的消息也塞进来（发言人写成 AI / 你）。
        # 这行如果是她自己说的，就不是"新消息"——不该让她回答自己。
        if _is_self_speaker(who):
            return "", "你", channel
        # 2026-11-04：插件对语音/特殊消息偶尔只能给出 "???" 这种占位。
        # 别让她对着问号硬猜，直接告诉她"没看到内容"，让她请对方重发。
        if re.fullmatch(r"[?\uFF1F\s:：.。,!，]+", body_text or ""):
            return "[他发的这条没解析出来（可能是语音、图片或特殊消息），你看不到内容]", who, channel
        # 2026-11-04 爸爸："她听不到语音。"
        # 语音转写失败时插件给的是占位或空串；让她知道自己"收到了但没听清"，
        # 而不是当成图片去猜。
        if re.search(r"语音|voice", body_text or "", re.I) or not (body_text or "").strip():
            return ("[他发来一条语音，但转写没成功——你听不到内容。可以说没听清，"
                    "让他打字或者重发一遍]", who, channel)
        return body_text, who, channel

    # 群里多条：保留"名字: 内容"的原文，让她自己看是谁说的
    # 末尾如果是她自己刚发的那几句，跳过——最新的"别人说的话"才算这一轮的消息
    while len(lines) > 1 and _is_self_speaker(SPEAKER_RE.match(lines[-1]).group(1) if SPEAKER_RE.match(lines[-1]) else ""):
        lines.pop()
    if not lines:
        return "", "你", channel
    match = SPEAKER_RE.match(lines[-1])
    if match:
        sender = match.group(1).strip()
    return "\n".join(lines), sender, channel


def extract_history(messages: Any, limit: int = 6) -> list[str]:
    """插件第一段 user 消息里有"近期双方聊天原文（逐行 JSON）"，把它抠出来。

    2026-11-04 爸爸："让她能看清楚之前的信息，不要编造。"
    这些就是IM 里真实的原文（含她没回过的那几条），比她自己召回的记忆可信。
    每行格式：[编号, 时间戳, "发言人", "原文", {附加信息}]
    """
    if not isinstance(messages, list):
        return []
    block = ""
    for item in messages:
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        body = flatten(item.get("content"))
        if "近期双方聊天原文" in body:
            block = body
            break
    if not block:
        return []

    lines: list[str] = []
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line.startswith("["):
            continue
        try:
            record = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(record, list) or len(record) < 4:
            continue
        speaker = str(record[2] or "").strip()
        text = str(record[3] or "").strip().replace("\r", " ")
        # 一条消息内部的换行压成空格，避免和"两条消息"混在一起
        text = re.sub(r"\s*\n\s*", " ", text)
        if not text:
            continue
        # 她自己的话标成"你（我说过的）"，别人的保留名字——
        # 2026-11-04 爸爸："她分不清是谁发的。" 光写"你"确实会让她把对面当成自己。
        name = "你（我刚才说的）" if speaker == "AI" else speaker
        if not name:
            continue
        # 2026-11-07 爸爸："她看不到我引用的。"
        # 窗口记录的第 5 段（附加信息）里带着 quote / attachments，
        # 以前整段被丢掉，历史里出现过的引用就等于不存在。
        extra = record[4] if len(record) > 4 and isinstance(record[4], dict) else {}
        quote = extra.get("quote") if isinstance(extra.get("quote"), dict) else None
        if quote:
            who = str(quote.get("speaker") or "").strip()
            quoted = str(quote.get("text") or "").strip().replace("\n", " ")
            if quoted:
                qwho = "你" if who in ("小鱼", "AI", "你") else (who or "某人")
                text = f"{text}（引用{qwho}的话：{quoted[:120]}）"
        if extra.get("attachments") or extra.get("sticker"):
            text = f"{text}（这条带图/表情）"
        lines.append(f"{name}: {text}")
    return lines[-limit:]


def build_message(history: list[str], sender: str, text: str) -> str:
    """把"最近的原文 + 这次他说的"拼成给她看的一段。

    只在她看得到原文时才有意义；没有历史就原样返回这句话。
    """
    if not history:
        return text
    head = "【IM上最近这几条是原文，照实看；没写的就是没说过，别补】"
    tail = "【他刚发的】"
    return f"{head}\n" + "\n".join(history) + f"\n{tail}\n{sender}: {text}"


STICKER_TAG = re.compile(r"[\[［【]\s*(?:STICKER|表情包|IM_STICKER)\s*(?:[:：]\s*([^\]］】\n]{1,40}))?\s*[\]］】]", re.I)
# 报文里出现这些，说明这一条本身带图/表情，才值得去捞那张图。
IMAGE_PLACEHOLDER = re.compile(
    r"\[(?:表情包|图片|照片|附件|语音)|附件为静态画面|image_url"
    # 2026-11-08：插件对"没解析出来"的特殊消息也给一句占位，里面可能其实带图
    r"|没解析出来|未提供图片内容",
    re.I,
)
# 他明确要表情的时候（"发个表情""来个表情包"），就算她没写标签也发一张
ASK_STICKER = re.compile(r"发.{0,3}表情|表情包|来个表情|斗图", re.I)


TRANSCRIPT = Path(r"PROJECT_ROOT\chat_bridge\data\bridge_transcript.jsonl")
COLD_NOTE = (
    "【你们刚聊的那段上下文断了。想把刚才那段收进图书馆，就在回复里写 [ARCHIVE]；"
    "想直接忘掉、当没聊过，写 [FORGET]；不写就当我没问。】"
)


def transcript_append(sender: str, text: str) -> None:
    """记一份滚动记录：插件把上下文清掉以后，这段还能被她自己决定怎么处理。"""
    if not text:
        return
    try:
        TRANSCRIPT.parent.mkdir(parents=True, exist_ok=True)
        with TRANSCRIPT.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"),
                                     "who": sender, "text": str(text)[:300]}, ensure_ascii=False) + "\n")
        # 别让它长成无限大的文件：超过 400 行只留最后 200 行
        try:
            lines = TRANSCRIPT.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        if len(lines) > 400:
            TRANSCRIPT.write_text("\n".join(lines[-200:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def transcript_lines(limit: int = 20) -> list[str]:
    try:
        rows = TRANSCRIPT.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    lines = []
    for row in rows:
        try:
            item = json.loads(row)
        except Exception:  # noqa: BLE001
            continue
        lines.append(f"{item.get('who') or '他'}: {item.get('text') or ''}")
    return lines


def transcript_clear() -> None:
    try:
        TRANSCRIPT.write_text("", encoding="utf-8")
    except OSError:
        pass


def remember_in_library(text: str) -> bool:
    """把一段对话写进她的图书馆（走 dl_api 的 /remember）。"""
    if not text.strip():
        return False
    payload = json.dumps({"user_id": "conv:9e52b4549f424e730d8871644385bcce63768cc74b8f8ec7ffa895f62ca73eeb",
                          "text": text[:1500]}, ensure_ascii=False).encode("utf-8")
    try:
        req = urllib.request.Request("http://127.0.0.1:8000/remember", data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=90) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


class Coalescer:
    """把连着发的几条合成一轮再回。

    2026-11-04 爸爸："真人也是看完对面的信息再统一回，一条一条回和机器人有什么区别。"
    做法：同一个人连着发消息时，等她"安静下来"（私聊 0.45 秒没有新的、最多等 2.5 秒），
    把这几条拼成一段交给她；先到的那条负责开口，后面的直接 SILENT（不重复发）。

    2026-11-07 爸爸："延迟还是好高呀。"
    原来 QUIET=0.9 / MAX_WAIT=5.0：单条消息也要白等快 1 秒，连发时能等到 5 秒，
    follower 最坏等 8 秒才回 SILENT——那几条 12 秒的尖峰就是这么来的。
    私聊收紧到 0.45/2.5（还是能把"两连发"并成一轮），群聊保持原来的节奏。
    """

    QUIET = 0.9
    MAX_WAIT = 5.0
    QUIET_PRIVATE = 0.45
    MAX_WAIT_PRIVATE = 2.5

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[tuple[str, str], dict] = {}

    def submit(self, sender: str, channel: str, text: str) -> tuple[str, str]:
        key = (sender or "", channel or "")
        private = "group" not in (channel or "")
        quiet = self.QUIET_PRIVATE if private else self.QUIET
        max_wait = self.MAX_WAIT_PRIVATE if private else self.MAX_WAIT
        now = time.time()
        with self._lock:
            item = self._items.get(key)
            if item is None:
                item = {"texts": [text], "first": now, "last": now,
                        "event": threading.Event()}
                self._items[key] = item
                leader = True
            else:
                if text not in item["texts"]:
                    item["texts"].append(text)
                item["last"] = now
                leader = False
        if not leader:
            item["event"].wait(timeout=max_wait + 2)
            return "follower", ""
        # leader：等到对方安静下来（或者最多等 max_wait）
        while time.time() - item["first"] < max_wait:
            time.sleep(0.15)
            with self._lock:
                if time.time() - item["last"] >= quiet:
                    break
        with self._lock:
            self._items.pop(key, None)
            merged = "\n".join(item["texts"])
        item["event"].set()
        return "leader", merged


COALESCER = Coalescer()


def _group_target() -> str:
    """群聊时表情要发给群（用群名，find_contact 能在会话里查到），私聊返回空。"""
    if not _CURRENT_IS_GROUP:
        return ""
    return _CURRENT_GROUP_NAME or _CURRENT_CHAT_ID


def send_sticker_to(sender: str, keyword: str, target_override: str = "") -> str:
    """从她表情库挑一张发到IM，返回一句结果说明（带上这张到底是什么）。

    2026-11-07 爸爸："延迟还是好高呀。"
    以前是在回复线程里同步发图：上传 + 等 hook 回执（最长 18 秒）全都串在文字回复前面，
    所以她一发表情包，那条文字就跟着迟到三五秒。现在挑图同步做（要拿到描述），
    上传丢到后台线程，文字先走。

    2026-11-08 爸爸："她一边在群里发言一边给我发表情包，真的不是 bug 吗。"
    是 bug：群里说话的人是「爸爸」时，这里拿 sender 当收件人，表情就私发给他了。
    群里必须发给**群**（用会话 id），私聊才发给对方。
    """
    try:
        sys.path.insert(0, str(ROOT))
        from chat_send import pick_sticker_detail, send_image  # noqa: PLC0415

        path, picked = pick_sticker_detail(keyword)
        if path is None:
            # 不替她瞎选：一个都对不上就不发，让她知道为什么。
            return f"表情没发：库里没有跟「{keyword}」对得上的（换描述里的词试试）" if keyword else "表情库空了"
        what = str(picked.get("note") or "").strip() or "一张表情"
        target = (target_override or "").strip() or sender or "爸爸"
        remember_sent_sticker(target, what)

        def worker() -> None:
            try:
                result = send_image(target, path)
                if not result.get("ok"):
                    log_line({"at": datetime.now().isoformat(timespec="seconds"),
                              "note": f"表情没发出去：{result.get('error')}", "sticker": what[:80]})
            except Exception as exc:  # noqa: BLE001
                log_line({"at": datetime.now().isoformat(timespec="seconds"),
                          "note": f"表情发送异常：{type(exc).__name__}: {exc}"})

        threading.Thread(target=worker, name="sticker-send", daemon=True).start()
        return f"已发表情：{what}"
    except Exception as exc:  # noqa: BLE001
        return f"表情发送出错：{type(exc).__name__}"


def send_sticker_if_asked(reply: str, sender: str, group_target: str = "") -> tuple[str, str]:
    """她自己写了 [STICKER: 开心] 之类的：真的从她表情库挑一张发到IM。

    2026-11-04 爸爸："还有让她能在IM发表情包。"
    插件自己的表情候选这次没下发，所以我们走自己的库（154 张，带标签）。
    返回 (去掉标签的正文, 结果说明)。群聊时 group_target 是群会话 id，表情发给群。
    """
    match = STICKER_TAG.search(reply or "")
    if not match:
        return reply, ""
    keyword = str(match.group(1) or "").strip()
    if "|" in keyword:
        keyword = keyword.split("|")[-1].strip()
    cleaned = STICKER_TAG.sub("", reply or "").strip()
    return cleaned, send_sticker_to(sender, keyword, target_override=group_target)


def _reader_image(image_id: str) -> str:
    """从IM reader 服务（127.0.0.1:5031）取图字节——表情包也能取到。

    2026-11-04：插件把图片做成"本地 reader 的 /api/v1/images/<id>"，
    它自己不把字节发给模型；我们来拿，拿完交给视觉。
    """
    cfg_path = Path(r"C:\Users\Lenovo\Desktop\aihuman\data\workspace\state\chat-io-config.json")
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    base = str(cfg.get("reader_base_url") or "").rstrip("/")
    token = str(cfg.get("reader_token") or "")
    if not base or not token:
        return ""
    try:
        req = urllib.request.Request(
            f"{base}/api/v1/images/{image_id}",
            headers={"Authorization": "Bearer " + token},
        )
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=20) as resp:
            blob = json.loads(resp.read().decode("utf-8", "ignore"))
        raw = base64.b64decode(blob.get("b64") or "")
        if not raw:
            return ""
        digest = hashlib.sha256(raw).hexdigest()
        expect = str(blob.get("sha256") or "")
        if expect and digest != expect:
            return ""  # 校验不过就别给她看错图
        mime = str(blob.get("mime") or "image/jpeg")
        suffix = ".png" if "png" in mime else (".webp" if "webp" in mime else ".jpg")
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        path = MEDIA_DIR / (image_id[:16] + suffix)
        path.write_bytes(raw)
        return str(path)
    except Exception:  # noqa: BLE001
        return ""


def find_recent_image(sender: str, within: float = 180.0) -> str:
    """插件只把表情包/图片写成一句文字占位，真正的图在本地 reader 里。

    2026-11-04 爸爸："IM她看不清图。" 以前只拿 text，从来没人去把图捞回来。
    现在按"最近一条带图、同一个人发的"找到 imageId，再从 reader 取字节。
    """
    store = Path(r"C:\Users\Lenovo\Desktop\aihuman\data\workspace\state\social-messages.json")
    media_dir = store.parent / "media"
    try:
        data = json.loads(store.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    now = time.time()
    best: tuple[float, dict] | None = None
    for items in (data.get("chats") or {}).values():
        if not isinstance(items, list):
            continue
        for item in reversed(items):
            if not isinstance(item, dict) or item.get("outgoing"):
                continue
            if not item.get("images"):
                continue
            try:
                stamp = float(item.get("timestamp") or 0)
            except (TypeError, ValueError):
                continue
            if now - stamp > within:
                continue
            # 2026-11-08 爸爸："还有其他人给她发图片她还是看不清。"
            # 带符号的昵称（Zero°、~~~）以前解析不出说话人，退化成"对方"，
            # 这里就按"对方"去匹配，永远匹配不到 → 图挂不上。
            # 占位名不参与过滤，退回"最近一张别人发来的图"。
            if (sender and sender not in ("对方", "他", "她", "你", "对方说")
                    and str(item.get("sender_name") or "") != sender):
                continue
            if best is None or stamp > best[0]:
                best = (stamp, item)
    if best is None:
        return ""
    image_id = str((best[1].get("images") or [{}])[0].get("id") or "")
    if not image_id:
        return ""
    # 2026-11-08 爸爸："她还是看不清图。"
    # 原来这里做**永久**去重：同一张图第二次直接返回空，她就只看到占位符。
    # 现在改成"永远给图"，重复的视觉成本改用下面 describe_sticker 的标签缓存来省。
    path = _reader_image(image_id)
    if path:
        try:
            (MEDIA_DIR.parent / "bridge_last_image.txt").write_text(
                json.dumps({"id": image_id, "at": time.time()}), encoding="utf-8")
        except OSError:
            pass
        return path
    # 退路：插件偶尔会把普通图片落成 state/media/<id>.json
    try:
        blob = json.loads((media_dir / f"{image_id}.json").read_text(encoding="utf-8"))
        raw = base64.b64decode(blob.get("b64") or "")
        if not raw:
            return ""
        suffix = ".png" if "png" in str(blob.get("mime") or "") else ".jpg"
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        path = MEDIA_DIR / (image_id[:16] + suffix)
        path.write_bytes(raw)
        return str(path)
    except Exception:  # noqa: BLE001
        return ""


def extract_image_url(messages: Any) -> str:
    """从 OpenAI 的 content 里抠出最后一张图（data URL 或 http）。"""
    if not isinstance(messages, list):
        return ""
    for item in reversed(messages):
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in reversed(content):
            if isinstance(part, dict) and part.get("type") in ("image_url", "image"):
                payload = part.get("image_url") or part.get("image") or {}
                url = payload.get("url") if isinstance(payload, dict) else str(payload)
                if isinstance(url, str) and url.strip():
                    return url.strip()
    return ""


def save_image(url: str) -> str:
    """把图落到本机，返回路径给 dl_api 做视觉。失败返回空。"""
    if not url:
        return ""
    raw = b""
    suffix = ".jpg"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        if url.startswith("data:image"):
            header, _, encoded = url.partition(",")
            suffix = ".png" if "png" in header.lower() else ".jpg"
            raw = base64.b64decode(encoded)
        elif url.startswith(("http://", "https://")):
            suffix = ".png" if url.lower().split("?")[0].endswith(".png") else ".jpg"
            with opener.open(url, timeout=20) as resp:
                raw = resp.read(8 * 1024 * 1024)
    except Exception:  # noqa: BLE001
        return ""
    if not raw:
        return ""
    try:
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        path = MEDIA_DIR / (uuid.uuid4().hex[:16] + suffix)
        path.write_bytes(raw)
        return str(path)
    except Exception:  # noqa: BLE001
        return ""


def call_xiaoyu(text: str, sender: str, channel: str, image_path: str = "") -> dict[str, Any]:
    body = {
        "message": text,
        "user_id": MAIN_USER_ID,
        "sender": sender,
        "channel": channel,
        # 2026-11-08：群里的表情要发给群，dl_api 那边得知道群名
        "group": _CURRENT_GROUP_NAME if _CURRENT_IS_GROUP else "",
    }
    if image_path:
        body["image_path"] = image_path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        API.rstrip("/") + "/chat", data=data,
        headers={"Content-Type": "application/json; charset=utf-8"}, method="POST",
    )
    with opener.open(req, timeout=300) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


class Handler(BaseHTTPRequestHandler):
    server_version = "XiaoyuAnthropic/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        sys.stderr.write("[bridge] " + (fmt % args) + "\n")

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_stream(self, text: str, model: str) -> None:
        """按 Anthropic Messages 的 SSE 事件推一条完整回复。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        message_id = "msg_" + datetime.now().strftime("%Y%m%d%H%M%S%f")

        def event(name: str, payload: dict[str, Any]) -> None:
            self.wfile.write(
                f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
            )
            self.wfile.flush()

        event("message_start", {"type": "message_start", "message": {
            "id": message_id, "type": "message", "role": "assistant", "model": model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 0},
        }})
        event("content_block_start", {"type": "content_block_start", "index": 0,
                                      "content_block": {"type": "text", "text": ""}})
        size = max(1, len(text) // 4) if text else 1
        for index in range(0, len(text), size):
            event("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": text[index:index + size]},
            })
        event("content_block_stop", {"type": "content_block_stop", "index": 0})
        event("message_delta", {"type": "message_delta",
                                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                "usage": {"output_tokens": max(1, len(text) // 2)}})
        event("message_stop", {"type": "message_stop"})
        self.close_connection = True

    def _send_silent(self, is_openai: bool, model: str, stream: bool) -> None:
        """这一轮她不说话（并入了别人的那条 / 自己不想回）。"""
        if is_openai:
            self._send_openai("SILENT", model, stream)
            return
        self._send_json(200, {
            "id": "msg_" + datetime.now().strftime("%Y%m%d%H%M%S%f"),
            "type": "message", "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": "SILENT"}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    def _send_openai(self, reply: str, model: str, stream: bool) -> None:
        """OpenAI chat.completions 格式（DSH 自定义模型若选这个协议就用它）。"""
        reply_id = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())
        if not stream:
            self._send_json(200, {
                "id": reply_id, "object": "chat.completion", "created": created, "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": reply},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": max(1, len(reply) // 2),
                          "total_tokens": max(2, len(reply) // 2 + 1)},
            })
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def chunk(delta: dict[str, Any], finish: str | None = None) -> None:
            frame = {
                "id": reply_id, "object": "chat.completion.chunk", "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            self.wfile.write(b"data: " + json.dumps(frame, ensure_ascii=False).encode("utf-8") + b"\n\n")
            self.wfile.flush()

        try:
            chunk({"role": "assistant", "content": ""})
            size = max(1, len(reply) // 4) if reply else 1
            for index in range(0, len(reply), size):
                chunk({"content": reply[index:index + size]})
            chunk({}, "stop")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        arrived = time.time()
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(length, 8 * 1024 * 1024)) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            self._send_json(400, {"type": "error", "error": {"type": "invalid_request_error",
                                                             "message": "bad json"}})
            return

        is_openai = path.endswith("/chat/completions")
        system = flatten(body.get("system"))
        text, sender, channel = extract_chat(body.get("messages"))
        # 她要看图：报文中带的图片（表情包/照片）落盘后交给 dl_api 的视觉
        image_path = save_image(extract_image_url(body.get("messages")))
        _probe_text = last_user_text(body.get("messages")) or ""
        # 2026-11-08：这里原来只看 last_user_text（有时是窗口/运行时上下文，不含这句占位符），
        # 于是她收到表情包时，这一步压根没进——既没取图也没说"没取到"，
        # 她拿到的还是那句"[表情包：附件为静态画面或动画采样画面]"。现在两处都看。
        if not image_path and (IMAGE_PLACEHOLDER.search(text or "") or IMAGE_PLACEHOLDER.search(_probe_text)):
            # 只在**这一条确实是图片/表情**时，才去插件媒体库把那张捞回来。
            # 2026-11-07 爸爸："延迟还是好高呀。"
            # 以前无条件捞"最近三分钟内的图"，于是发完一张表情后，接下来每条文字
            # 都会被挂上那张旧图 → 每轮白跑一次视觉（1~1.5 秒），她还会接着聊那张旧图。
            image_path = find_recent_image(sender, within=90.0)
        # 2026-11-04：插件还会"自己"触发主动发言（proactive）。那种报文里没有
        # <untrusted_chat>，只有一段"本机定时安排的一次主动发言机会……"的指令。
        # 以前这段指令被当成"他说的话"喂给她，她就照着指令回答，看着不像她。
        raw_text = last_user_text(body.get("messages"))
        proactive = ("主动发言" in raw_text and "<untrusted_chat>" not in raw_text)
        if proactive:
            text = "（这是一次你自己想说话的机会：你可以主动跟他说一句；不想说就不说。）"
            sender = "系统"
        elif not text and image_path:
            text = "[他发来一张图或表情包]"
        # 表情包：先把画面和图上文字读出来，写进正文给她；顺带省掉 /chat 里那次重复视觉。
        # 记得带上**是谁**发的——2026-11-08 爸爸："她有时候会把自己发的表情包当成对方发的。"
        _who = (sender or "").strip() or "对方"
        if image_path and ("表情包" in (text or "") or "sticker" in (raw_text or "").lower()):
            label = describe_sticker(image_path)
            if label.get("description"):
                text = f"[{_who} 发来一张表情包] " + str(label["description"])[:220]
                image_path = ""
        if not image_path and IMAGE_PLACEHOLDER.search(_probe_text or ""):
            # 2026-11-08：取不到图时**别给她一句像内容的占位符**——她会当成自己看过了。
            # 如实说没取到，避免她拿"刚才我发的那张"去补。
            text = f"[{_who} 发来一张表情包或图片，但这次我这边没取到，看不到画面；不确定就别猜。]"
        # 2026-11-04 爸爸："真人也是看完对面的信息再统一回。"
        # 连发几条时合成一轮：先到的开口，后面的安静（不重复发）。
        merge_info: dict[str, Any] = {}
        if text and not proactive and not image_path:
            _merged_at = time.time()
            role, merged = COALESCER.submit(sender, channel, text)
            if role == "follower":
                log_line({"at": datetime.now().isoformat(timespec="seconds"),
                          "protocol": "openai" if is_openai else "anthropic",
                          "note": "并入上一轮，回 SILENT", "sender": sender, "text": text[:120]})
                self._send_silent(is_openai, str(body.get("model") or "xiaoyu"), bool(body.get("stream")))
                return
            text = merged
            # 2026-11-04：日志里直接看得出"这一轮合了几条、等了多久"，
            # 不然"连发只回一条"到底有没有生效全靠猜。
            merge_info = {
                "merged": max(1, merged.count("\n") + 1),
                "waited": round(time.time() - _merged_at, 2),
            }
        # 把"最近这几条IM原文"一起带过去，省得她凭印象编
        history = [] if proactive else extract_history(body.get("messages"))
        # 2026-11-04：插件把上下文清掉以后，这一段由她自己决定"忘掉"还是"收进图书馆"。
        # 先记滚动记录，断档时再把选项递给她。
        if text and not proactive:
            transcript_append(sender, text)
        cold_note = ""
        if text and not proactive and not history and len(transcript_lines(3)) >= 3:
            cold_note = COLD_NOTE
        # 告诉她可以发表情包（插件这次没下发候选，我们走自己的表情库）
        sticker_hint = "\n（想发表情包就在回复最后写 [STICKER: 关键词]，比如开心/委屈/无语，会真发一张过去。）"
        outgoing = (build_message(history, sender, text) if text else "") + sticker_hint + cold_note
        dump_raw({"path": path, "body": body, "picked": text[:500], "sender": sender,
                  "image": bool(image_path), "history_lines": len(history), "proactive": proactive})

        if not text:
            # 没有真正的聊天原文（比如只有元信息）——当成她这次不说话
            text = ""

        started = time.time()
        if not text:
            log_line({"at": datetime.now().isoformat(timespec="seconds"),
                      "protocol": "openai" if is_openai else "anthropic",
                      "note": "没有IM原文，回 SILENT", "channel": channel})
            if is_openai:
                self._send_openai("SILENT", str(body.get("model") or "xiaoyu"), bool(body.get("stream")))
            else:
                self._send_json(200, {
                    "id": "msg_" + datetime.now().strftime("%Y%m%d%H%M%S%f"),
                    "type": "message", "role": "assistant",
                    "model": str(body.get("model") or "xiaoyu"),
                    "content": [{"type": "text", "text": "SILENT"}],
                    "stop_reason": "end_turn", "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                })
            return

        try:
            result = call_xiaoyu(outgoing, sender, channel, image_path)
            model_ms = int((time.time() - started) * 1000)
        except Exception as exc:  # noqa: BLE001
            log_line({"at": datetime.now().isoformat(timespec="seconds"), "error": f"{type(exc).__name__}: {exc}",
                      "sender": sender, "text": text[:200]})
            self._send_json(502, {"type": "error", "error": {"type": "api_error",
                                                             "message": f"xiaoyu api failed: {exc}"}})
            return

        raw_reply = str(result.get("reply") or "").strip()
        # 2026-11-08：插件的提示词会让她另起一行输出 STICKER_LABELS: […]（给表情池打标签）。
        # 她走的是 RESPOND 协议，这行不该出现在IM 里；万一她照做了，这里先剥掉，
        # 不让一串 JSON 当成正文发出去。
        if "STICKER_LABELS" in raw_reply:
            raw_reply = re.sub(r"\n?\s*STICKER_LABELS\s*[:：].*$", "", raw_reply, flags=re.S).strip()
            for item in (result.get("replies") or []):
                if isinstance(item, str) and "STICKER_LABELS" in item:
                    result["replies"] = [x for x in result["replies"]
                                         if not (isinstance(x, str) and "STICKER_LABELS" in x)]
        # 2026-11-04 爸爸："她换行自动变成两条信息就行，别一条有换行。"
        # 插件的规则是：**空行**分开才是两条消息，单个换行只是排版。
        # 所以把她原来的每条气泡用空行接起来，而不是拼成一个带换行的整段。
        bubbles = [str(item).strip() for item in (result.get("replies") or []) if str(item).strip()]
        if not bubbles:
            bubbles = [line.strip() for line in raw_reply.splitlines() if line.strip()]
        # 这个 app 的发送协议：正文以 RESPOND: 开头才真的发出去，SILENT 表示这次不说话。
        # 她安静 / 睡着 / 自己判了不回时，必须回 SILENT，否则空回复或半句话会漏到IM 里。
        if not bubbles or result.get("ignored") or result.get("asleep"):
            reply = "SILENT"
        else:
            reply = "RESPOND: " + "\n\n".join(bubbles)
        # 2026-11-04 爸爸："还有让她能在IM发表情包。"
        # /chat 会把她的 [STICKER] 吃掉、换成 result["sticker"]，所以先看那个；
        # 没被吃掉的（例如 [IM_STICKER: 名字 | 关键词]）再从正文里剥。
        sticker_note = ""
        sticker_payload = result.get("sticker") if isinstance(result.get("sticker"), dict) else None
        if sticker_payload and not result.get("ignored"):
            sticker_note = send_sticker_to(sender, str(sticker_payload.get("tag") or ""), target_override=_group_target())
        # 2026-11-04 爸爸："让她发表情包她应该可以发表情包才对。"
        # 他明确要表情时，她要是没自己写标签，就从表情库挑一张发过去。
        if not sticker_note and text and ASK_STICKER.search(text) and not result.get("ignored"):
            picked = re.search(r"(开心|难过|委屈|无语|可爱|生气|笑|哭|懵|鼓掌|白眼)", text)
            sticker_note = send_sticker_to(sender, picked.group(1) if picked else "", target_override=_group_target())
        if reply != "SILENT":
            stripped, note2 = send_sticker_if_asked(reply, sender, group_target=_group_target())
            if note2:
                sticker_note = sticker_note or note2
                reply = stripped
                if reply.strip() in ("", "RESPOND:", "RESPOND"):
                    # 她这轮只想发表情
                    reply = "SILENT"
        # 2026-11-04：这一段是留是忘，她自己说了算。
        _upper = reply.upper()
        if "[ARCHIVE]" in _upper or "[FORGET]" in _upper:
            if "[ARCHIVE]" in _upper:
                _lines = transcript_lines(30)
                _ok = remember_in_library("\n".join(_lines)) if _lines else False
                log_line({"at": datetime.now().isoformat(timespec="seconds"),
                          "note": f"她选择收进图书馆（{'成功' if _ok else '没写成'}），{len(_lines)} 条"})
            else:
                log_line({"at": datetime.now().isoformat(timespec="seconds"),
                          "note": "她选择把这一段忘掉"})
            transcript_clear()
            reply = re.sub(r"\[(?:ARCHIVE|FORGET)\]", "", reply, flags=re.I).strip()
            if reply.strip() in ("", "RESPOND:", "RESPOND"):
                reply = "SILENT"
        log_line({
            "at": datetime.now().isoformat(timespec="seconds"),
            "ms": int((time.time() - started) * 1000),
            # 2026-11-07：把"排队合并等了多久 / 模型花了多久"分开记，别再靠猜。
            "coalesce_ms": int((started - arrived) * 1000),
            "model_ms": model_ms,
            "with_image": bool(image_path),
            "protocol": "openai" if is_openai else "anthropic",
            "channel": channel,
            "sender": sender,
            "text": text[:200],
            "reply": reply[:300],
            "source": result.get("source"),
            "ignored": result.get("ignored"),
            "asleep": result.get("asleep"),
            "system_head": system[:120],
            **merge_info,
        })

        model = str(body.get("model") or "xiaoyu")
        stream = bool(body.get("stream"))
        if is_openai:
            self._send_openai(reply, model, stream)
            return

        if stream:
            self._send_stream(reply, model)
            return

        self._send_json(200, {
            "id": "msg_" + datetime.now().strftime("%Y%m%d%H%M%S%f"),
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": reply}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": max(1, len(reply) // 2)},
        })

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            self._send_json(200, {"data": [
                {"id": "xiaoyu", "object": "model"},
                {"id": "小鱼", "object": "model"},
            ]})
            return
        self._send_json(200, {"ok": True, "service": "anthropic-bridge", "api": API})


def main() -> int:
    parser = argparse.ArgumentParser(description="DeepSeek Messages 协议 → 小鱼")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8003)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    global API
    API = args.api
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(f"anthropic-bridge: http://{args.host}:{args.port} -> {API}", flush=True)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
