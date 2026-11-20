# -*- coding: utf-8 -*-
"""omni-bot-sdk 插件：把IM 4.x 接到本地数字生命 API。

为什么用 omni
-------------
IM 3.6.0.18 服务端已经不让登录，ntchat 那条路走不通；omni 是「数据库读消息 +
YOLO/OCR 视觉点按钮」的零 Hook 方案，理论支持IM 4.x，正好绕开版本检查。

这个插件做三件事：
    1. 收消息（私聊 / 群里 @ 或唤醒词），转给 http://127.0.0.1:8000/chat；
    2. 把 reply 用 RPA 发回去（群里 @ 对方）；
    3. 按情绪附一张表情包；定时调用 /proactive 主动发消息。

它不处理「自己的消息」（omni 的 self_msg_plugin 也会挡一道），也不碰数据库写操作。

安装
----
    cd omni_plugin
    pip install -e .          # 注册 entry point: omni_bot.plugins

配置（config.yaml）
------------------
    plugins:
      digital-life-plugin:
        enabled: true
        api_base_url: http://127.0.0.1:8000
        wake_words: [她, 小鱼]

注意
----
* omni 的 RPA 会抢占鼠标键盘，跑的时候人别去操作电脑；
* 「发送」是按显示名切换会话的，**同名联系人群会有歧义**，这是 RPA 的天然缺陷。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from omni_bot_sdk.plugins.interface import (
    MessageType,
    Plugin,
    PluginExcuteContext,
    PluginExcuteResponse,
    SendImageAction,
    SendTextMessageAction,
)

PLUGIN_NAME = "digital-life-plugin"
DEFAULT_API = "http://127.0.0.1:8000"
_PLACEHOLDERS = {
    "image": "[对方发来一张图片]",
    "voice": "[对方发来一条语音]",
    "emoji": "[对方发来一个表情]",
}


class DigitalLifeConfig(BaseModel):
    """config.yaml 里 plugins.digital-life-plugin 的字段。"""

    api_base_url: str = DEFAULT_API
    api_timeout: float = 120.0
    wake_words: list[str] = Field(default_factory=lambda: ["她", "小鱼"])
    private_enabled: bool = True
    group_enabled: bool = True
    at_reply: bool = True
    fallback_on_error: str = ""

    user_id_strategy: str = "sender"       # sender | room | fixed
    fixed_user_id: str = "default"

    stickers_enabled: bool = True
    stickers_dir: str = "stickers"
    sticker_chance: float = 0.25
    sticker_max_kb: int = 2048

    proactive_enabled: bool = True
    proactive_interval_minutes: int = 45
    proactive_quiet_hours: list[str] = Field(default_factory=lambda: ["23:30", "08:00"])
    proactive_target: str = "文件传输助手"
    proactive_user_id: str = "default"

    dedupe_ttl_seconds: int = 600


class _StickerBook:
    """只读表情包仓库，和 chat_bridge 共用同一个 stickers 目录。"""

    def __init__(self, root: str, max_kb: int = 2048) -> None:
        self.root = Path(root).expanduser()
        self.max_bytes = max_kb * 1024
        self._lock = threading.Lock()
        self._items: list[dict[str, Any]] = []
        self._loaded_at = 0.0

    def _index_path(self) -> Path:
        return self.root / "index.json"

    def load(self, force: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            if not force and self._items and time.time() - self._loaded_at < 60:
                return self._items
            items: list[dict[str, Any]] = []
            try:
                raw = json.loads(self._index_path().read_text(encoding="utf-8"))
                for item in raw.get("stickers", []):
                    path = self.root / str(item.get("file", ""))
                    if path.exists() and path.stat().st_size <= self.max_bytes:
                        items.append({**item, "_path": path})
            except (OSError, ValueError):
                items = []
            self._items = items
            self._loaded_at = time.time()
            return items

    def pick(self, tags: list[str]) -> Path | None:
        items = self.load()
        if not items:
            return None
        wanted = {tag.casefold() for tag in tags if tag}
        matched = [s for s in items if wanted & {str(t).casefold() for t in s.get("tags", [])}]
        pool = matched or items
        weights = [1.0 / (1.0 + int(s.get("uses", 0)) * 0.6) for s in pool]
        chosen = random.choices(pool, weights=weights, k=1)[0]
        path = Path(chosen["_path"])
        return path if path.exists() else None


class DigitalLifePlugin(Plugin):
    priority = 1500

    # ------------------------------------------------------------------ 元信息

    @classmethod
    def get_plugin_config_schema(cls):  # noqa: ANN206
        return DigitalLifeConfig

    def get_plugin_name(self) -> str:
        return PLUGIN_NAME

    def get_plugin_description(self) -> str:
        return "把IM消息转发给本地数字生命 API，并把回复/表情发回IM"

    def get_priority(self) -> int:
        return self.priority

    # ------------------------------------------------------------------ 生命周期

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        cfg: DigitalLifeConfig = self.get_validated_plugin_config()
        self._stickers = (
            _StickerBook(cfg.stickers_dir, cfg.sticker_max_kb) if cfg.stickers_enabled else None
        )
        self._seen: dict[str, float] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if cfg.proactive_enabled:
            self._thread = threading.Thread(target=self._proactive_loop, name="dl-proactive", daemon=True)
            self._thread.start()
            self.logger.info("数字生命插件：主动聊天已启动（每 %s 分钟）", cfg.proactive_interval_minutes)
        self.logger.info("数字生命插件已加载：API=%s", cfg.api_base_url)

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ 消息入口

    async def handle_message(self, context: PluginExcuteContext) -> None:
        message = context.get_message()
        cfg: DigitalLifeConfig = self.get_validated_plugin_config()

        if message is None or message.is_self:
            return
        if not self._dedupe(message):
            return

        kind, text = self._extract(message)
        if kind is None:
            return

        if message.is_chatroom:
            if not cfg.group_enabled:
                return
            if not message.is_at:
                wake = self._find_wake(text, cfg.wake_words)
                if not wake:
                    return
                text = self._strip_wake(text, wake)
            if cfg.at_reply and message.contact:
                # RPA 是按显示名 @ 人的
                at_name = message.contact.display_name
            else:
                at_name = None
        else:
            if not cfg.private_enabled:
                return
            at_name = None

        payload = {
            "message": text,
            "user_id": self._user_id(message, cfg),
            "sender": message.contact.display_name if message.contact else "",
        }
        image_path = self._image_path(message)
        if image_path:
            payload["image_path"] = image_path

        response = PluginExcuteResponse(plugin_name=PLUGIN_NAME, handled=True)
        try:
            data = await asyncio.to_thread(self._post, "/chat", payload, cfg.api_timeout)
        except Exception as exc:  # noqa: BLE001
            self.logger.error("调数字生命失败：%s", exc)
            if cfg.fallback_on_error:
                response.add_action(
                    SendTextMessageAction(
                        content=cfg.fallback_on_error,
                        target=message.target,
                        is_chatroom=message.is_chatroom,
                    )
                )
                context.add_response(response)
            return

        reply = str(data.get("reply") or "").strip()
        if not reply:
            return

        response.add_action(
            SendTextMessageAction(
                content=reply,
                target=message.target,
                is_chatroom=message.is_chatroom,
                at_user_name=at_name,
            )
        )

        sticker = self._pick_sticker(data.get("emotion") or {}, payload["message"], cfg)
        if sticker is not None:
            response.add_action(
                SendImageAction(
                    image_path=str(sticker),
                    target=message.target,
                    is_chatroom=message.is_chatroom,
                )
            )
        context.add_response(response)

    # ------------------------------------------------------------------ 主动聊天

    def _proactive_loop(self) -> None:
        cfg: DigitalLifeConfig = self.get_validated_plugin_config()
        interval = max(300, int(cfg.proactive_interval_minutes) * 60)
        while not self._stop.wait(interval):
            try:
                if self._in_quiet_hours(cfg.proactive_quiet_hours):
                    continue
                data = self._post("/proactive", {"user_id": cfg.proactive_user_id}, cfg.api_timeout)
                text = str(data.get("reply") or "").strip()
                if not text:
                    continue
                self.add_rpa_actions(
                    [
                        SendTextMessageAction(
                            content=text,
                            target=cfg.proactive_target,
                            is_chatroom=False,
                        )
                    ]
                )
                self.logger.info("主动消息已入队：%s", text[:60])
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("主动聊天失败：%s", exc)

    @staticmethod
    def _in_quiet_hours(hours: list[str]) -> bool:
        try:
            start, end = hours[0], hours[1]
            now = datetime.now()
            current = now.hour * 60 + now.minute
            sh, sm = (int(part) for part in start.split(":"))
            eh, em = (int(part) for part in end.split(":"))
            start_min, end_min = sh * 60 + sm, eh * 60 + em
        except (ValueError, IndexError):
            return False
        if start_min == end_min:
            return False
        if start_min < end_min:
            return start_min <= current < end_min
        return current >= start_min or current < end_min

    # ------------------------------------------------------------------ 消息解析

    def _extract(self, message: Any) -> tuple[str | None, str]:
        """返回 (kind, text)；kind=None 表示不处理这类消息。"""
        local_type = message.local_type
        if local_type in (
            MessageType.Text,
            MessageType.Text2,
            MessageType.Quote,
        ):
            return "text", str(message.parsed_content or "").strip()
        if local_type == MessageType.Audio:
            text = str(getattr(message, "audio_text", "") or "").strip()
            return "voice", f"[语音] {text}" if text else _PLACEHOLDERS["voice"]
        if local_type == MessageType.Image:
            return "image", _PLACEHOLDERS["image"]
        if local_type == MessageType.Emoji:
            desc = str(getattr(message, "description", "") or "").strip()
            return "emoji", f"[表情] {desc}" if desc else _PLACEHOLDERS["emoji"]
        return None, ""

    @staticmethod
    def _image_path(message: Any) -> str:
        if message.local_type != MessageType.Image:
            return ""
        path = getattr(message, "path", "") or ""
        return str(path) if path and Path(path).exists() else ""

    def _user_id(self, message: Any, cfg: DigitalLifeConfig) -> str:
        if cfg.user_id_strategy == "room":
            return (message.room.username if message.room else "") or "default"
        if cfg.user_id_strategy == "fixed":
            return cfg.fixed_user_id or "default"
        return (message.contact.username if message.contact else "") or "default"

    @staticmethod
    def _find_wake(text: str, words: list[str]) -> str:
        lowered = text.casefold()
        for word in words:
            if word and word.casefold() in lowered:
                return word
        return ""

    @staticmethod
    def _strip_wake(text: str, wake: str) -> str:
        index = text.casefold().find(wake.casefold())
        if index < 0:
            return text
        body = (text[:index] + text[index + len(wake):]).strip()
        return body.strip("，,。.！!？?、~～:：；; \t")

    def _dedupe(self, message: Any) -> bool:
        key = f"{getattr(message, 'server_id', '')}|{message.local_id}|{message.sort_seq}"
        digest = hashlib.sha1(key.encode("utf-8", "ignore")).hexdigest()
        now = time.time()
        ttl = self.get_validated_plugin_config().dedupe_ttl_seconds
        if any(now - stamp > ttl for stamp in self._seen.values()):
            self._seen = {k: v for k, v in self._seen.items() if now - v <= ttl}
        if digest in self._seen:
            return False
        self._seen[digest] = now
        return len(self._seen) <= 5000

    # ------------------------------------------------------------------ 表情

    def _pick_sticker(self, emotion: dict[str, Any], text: str, cfg: DigitalLifeConfig) -> Path | None:
        if not self._stickers or not cfg.stickers_enabled:
            return None
        forced = any(word in text for word in ("发个表情", "发张表情", "表情包", "发个图"))
        if not forced and random.random() > cfg.sticker_chance:
            return None
        tag = self._mood_tag(emotion)
        return self._stickers.pick([tag])

    @staticmethod
    def _mood_tag(emotion: dict[str, Any]) -> str:
        try:
            joy = float(emotion.get("joy", 50))
            hurt = float(emotion.get("hurt", 20))
            impulse = float(emotion.get("impulse", 30))
        except (TypeError, ValueError):
            return "日常"
        if hurt >= 65:
            return "委屈"
        if impulse >= 75:
            return "生气"
        if joy >= 65:
            return "开心"
        if joy < 35:
            return "失落"
        return "日常"

    # ------------------------------------------------------------------ HTTP

    def _post(self, path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        cfg: DigitalLifeConfig = self.get_validated_plugin_config()
        url = cfg.api_base_url.rstrip("/") + path
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", "application/json; charset=utf-8")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")[:200]
            raise RuntimeError(f"HTTP {exc.code} {url}: {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError(f"连不上 {url}: {exc}") from exc
