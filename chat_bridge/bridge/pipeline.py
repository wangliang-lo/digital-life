# -*- coding: utf-8 -*-
"""消息清洗与路由判定。

这里集中了所有"该不该回、回什么内容、回给谁"的规则：

    1. 自己发的 -> 直接丢（防死循环的第一道闸）
    2. 回显 -> 丢（防死循环的第二道闸：刚发出去的内容又被当成收到）
    3. 重复投递 -> 按 msg_id 去重
    4. 黑名单 / 白名单 -> 丢
    5. 群聊：必须 @我 或 命中唤醒词，否则丢
    6. 群聊正文：去掉 @昵称 前缀、去掉唤醒词
    7. 私聊：去掉 @昵称 前缀，直接回
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from .adapters.base import IncomingMessage
from .config import IMConfig
from .logs import get_logger
from .stats import Metrics

log = get_logger("pipeline")

# 不可见控制字符、零宽字符、方向控制符
_CTRL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\ufeff]")
# 群消息偶尔带 "wxid_xxx:\n" 前缀
_ROOM_SENDER_PREFIX = re.compile(r"^(?:wxid_[A-Za-z0-9_-]+|[A-Za-z0-9_-]{3,}@chatroom)[:：][ \t]*\r?\n?")
# IM @ 之后用的是 U+2005；昵称遇到空格就停，否则会把整句话吃掉
_LEADING_AT = re.compile(r"^(?:@[^@\s\u2005]{1,64}[\u2005 ]*)+")
_TRIM = " \t\r\n\u3000\u2005\u00a0"


@dataclass(slots=True)
class Route:
    """一条待回复的任务。"""

    kind: str                    # private | group
    target_wxid: str             # 私聊=对方，群聊=群
    text: str                    # 清洗后的正文
    user_id: str                 # 传给数字生命的 user_id
    sender_wxid: str = ""
    sender_name: str = ""
    conversation_id: str = ""
    at_wxid: list[str] = field(default_factory=list)
    trigger: str = "private"     # private | at | wake
    received_at: float = 0.0

    # 多模态：交给 MediaRouter 处理，处理完写回 text
    media_kind: str = ""         # image | voice | emoji
    media_path: str = ""
    media_url: str = ""
    media_duration: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "target": self.target_wxid,
            "sender": self.sender_wxid,
            "text": self.text[:120],
            "trigger": self.trigger,
            "user_id": self.user_id,
            "media": self.media_kind,
        }


def split_message(text: str, limit: int) -> list[str]:
    """超长回复按句切分，实在切不开就硬切。"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    current = ""
    for chunk in re.split(r"(?<=[。！？!?\n])", text):
        if not chunk:
            continue
        if len(current) + len(chunk) > limit and current:
            parts.append(current)
            current = ""
        current += chunk
        while len(current) > limit:
            parts.append(current[:limit])
            current = current[limit:]
    if current.strip():
        parts.append(current)
    return [part.strip() for part in parts if part.strip()]


class MessagePipeline:
    def __init__(self, cfg: IMConfig, self_wxid: str, metrics: Metrics | None = None) -> None:
        self.cfg = cfg
        self.self_wxid = self_wxid
        self.metrics = metrics or Metrics()

        self._seen: OrderedDict[str, float] = OrderedDict()
        self._sent: dict[str, tuple[str, float]] = {}
        self._blocked = set(cfg.blocked_wxids)
        self._allowed = set(cfg.allowed_wxids)
        self._allowed_rooms = set(cfg.allowed_rooms)

    # ---------------------------------------------------------------- 对外

    def evaluate(self, msg: IncomingMessage) -> Route | None:
        """返回 Route 表示"该回"，None 表示"忽略"。"""
        now = time.time()

        # 1) 自己发的消息：绝不处理
        if msg.is_self or (self.self_wxid and msg.from_wxid == self.self_wxid):
            self.metrics.inc("ignored_self")
            return None

        # 2) 空消息 / 没有发送者（纯媒体消息允许 content 为空）
        if not msg.from_wxid:
            self.metrics.inc("ignored_empty")
            return None
        if not (msg.content or "").strip() and not msg.has_media:
            self.metrics.inc("ignored_empty")
            return None

        # 3) 黑白名单
        if msg.from_wxid in self._blocked:
            self.metrics.inc("ignored_blocked")
            return None
        if self._allowed and msg.from_wxid not in self._allowed:
            self.metrics.inc("ignored_not_allowed")
            return None

        # 4) 群聊白名单
        if msg.is_group and self._allowed_rooms and msg.room_wxid not in self._allowed_rooms:
            self.metrics.inc("ignored_room_not_allowed")
            return None

        # 5) 正文清洗（纯媒体消息清洗后是空串，属于正常）
        text = self._clean(msg)
        if not text and not msg.has_media:
            self.metrics.inc("ignored_empty_after_clean")
            return None

        # 6) 回显防御：刚发出去的内容又回来了
        if self._is_echo(msg.conversation_id, text, now):
            self.metrics.inc("ignored_echo")
            log.warning("检测到消息回显，已丢弃：%s", text[:40])
            return None

        # 7) 去重（ntchat 重登/断线重连可能重复投递）
        if not self._mark_seen(msg.msg_id, now):
            self.metrics.inc("ignored_duplicate")
            return None

        # 8) 群聊 / 私聊分流
        if msg.is_group:
            if not self.cfg.group_enabled:
                self.metrics.inc("ignored_group_disabled")
                return None
            return self._route_group(msg, text)
        if not self.cfg.private_enabled:
            self.metrics.inc("ignored_private_disabled")
            return None
        return self._route_private(msg, text)

    def note_sent(self, conversation_id: str, text: str) -> None:
        """记录自己发出去的内容，供回显防御使用。"""
        self._sent[conversation_id] = ((text or "").strip(), time.time())
        if len(self._sent) > 500:
            cutoff = time.time() - max(self.cfg.echo_guard_seconds, 60.0)
            self._sent = {key: value for key, value in self._sent.items() if value[1] >= cutoff}

    # ---------------------------------------------------------------- 内部

    def _route_private(self, msg: IncomingMessage, text: str) -> Route:
        self.metrics.inc("routed_private")
        return Route(
            kind="private",
            target_wxid=msg.from_wxid,
            text=text[: self.cfg.max_input_chars],
            user_id=self._user_id(msg),
            sender_wxid=msg.from_wxid,
            sender_name=msg.from_name,
            conversation_id=msg.conversation_id,
            trigger="private",
            received_at=msg.timestamp,
            media_kind=msg.media_kind,
            media_path=msg.media_path,
            media_url=msg.media_url,
            media_duration=msg.media_duration,
        )

    def _route_group(self, msg: IncomingMessage, text: str) -> Route | None:
        at_hit = bool(self.self_wxid) and self.self_wxid in (msg.at_wxid or [])
        wake = self._find_wake(text) if text else ""

        if not at_hit and not wake:
            self.metrics.inc("ignored_group_not_addressed")
            return None

        body = text
        if wake:
            body = self._strip_wake(body, wake)
            # 唤醒词后面可能还跟着一个 @昵称
            body = _LEADING_AT.sub("", body).strip(_TRIM)
        if not body and not msg.has_media:
            self.metrics.inc("ignored_group_empty_body")
            return None

        # 群里回复必须 @ 对方，at_list 用发送者 wxid
        at_list = [msg.from_wxid] if self.cfg.group_reply_at else []
        self.metrics.inc("routed_group")
        return Route(
            kind="group",
            target_wxid=msg.room_wxid,
            text=body[: self.cfg.max_input_chars],
            user_id=self._user_id(msg),
            sender_wxid=msg.from_wxid,
            sender_name=msg.from_name,
            conversation_id=msg.conversation_id,
            at_wxid=at_list,
            trigger="at" if at_hit else "wake",
            received_at=msg.timestamp,
            media_kind=msg.media_kind,
            media_path=msg.media_path,
            media_url=msg.media_url,
            media_duration=msg.media_duration,
        )

    def _user_id(self, msg: IncomingMessage) -> str:
        strategy = self.cfg.user_id_strategy
        if strategy == "room":
            return msg.room_wxid or msg.from_wxid
        if strategy == "fixed":
            return self.cfg.fixed_user_id or "default"
        return msg.from_wxid

    def _clean(self, msg: IncomingMessage) -> str:
        text = _CTRL_CHARS.sub("", msg.content or "")
        if msg.is_group:
            text = _ROOM_SENDER_PREFIX.sub("", text)
        text = text.strip(_TRIM)
        if self.cfg.strip_leading_at:
            text = _LEADING_AT.sub("", text).strip(_TRIM)
        return text

    def _find_wake(self, text: str) -> str:
        lowered = text.casefold()
        for word in self.cfg.wake_words:
            if word and word.casefold() in lowered:
                return word
        return ""

    @staticmethod
    def _strip_wake(text: str, wake: str) -> str:
        index = text.casefold().find(wake.casefold())
        if index < 0:
            return text
        cleaned = text[:index] + text[index + len(wake):]
        return cleaned.strip(_TRIM + "，,。.！!？?、~～:：；;")

    def _is_echo(self, conversation_id: str, text: str, now: float) -> bool:
        record = self._sent.get(conversation_id)
        if not record:
            return False
        sent_text, sent_at = record
        if now - sent_at > self.cfg.echo_guard_seconds:
            return False
        return sent_text == text.strip()

    def _mark_seen(self, msg_id: str, now: float) -> bool:
        if not msg_id:
            return True
        if msg_id in self._seen:
            return False
        self._seen[msg_id] = now
        ttl = self.cfg.dedupe_ttl
        while self._seen:
            oldest_id, oldest_at = next(iter(self._seen.items()))
            if now - oldest_at <= ttl and len(self._seen) <= 2000:
                break
            self._seen.popitem(last=False)
        return True

    @staticmethod
    def fallback_msg_id(msg: IncomingMessage) -> str:
        raw = f"{msg.room_wxid}|{msg.from_wxid}|{msg.content}|{int(msg.timestamp // 2)}"
        return hashlib.md5(raw.encode("utf-8", "ignore")).hexdigest()
