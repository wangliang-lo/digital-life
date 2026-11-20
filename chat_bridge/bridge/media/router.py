# -*- coding: utf-8 -*-
"""媒体路由：把图片/语音/表情变成一句话，塞进聊天正文。

    route.text = "[语音] 今天好累啊"        ← ntchat 已把语音转成 mp3，这里只做 ASR
    route.text = "[图片] 一只橘猫趴在键盘上"  ← Ollama 视觉模型
    route.text = "[对方发来一个表情]"        ← 表情同时自动进表情包仓库

任何一步失败都只降级成占位符，绝不让整条消息丢掉。
"""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

from ..adapters.base import IncomingMessage
from ..config import MediaConfig
from ..logs import get_logger
from ..stats import Metrics
from ..stickers.store import Sticker, StickerStore
from .vision import ImageDescriber
from .voice import MediaError, VoiceTranscriber

log = get_logger("media.router")

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) wxbridge/1.1"
_MAX_DOWNLOAD = 4 * 1024 * 1024


class MediaRouter:
    def __init__(
        self,
        cfg: MediaConfig,
        asr: VoiceTranscriber,
        vision: ImageDescriber,
        stickers: StickerStore | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self.cfg = cfg
        self.asr = asr
        self.vision = vision
        self.stickers = stickers
        self.metrics = metrics or Metrics()

    # ------------------------------------------------------------------ 主入口

    def enrich(self, text: str, kind: str, path: str = "", url: str = "") -> str:
        note = ""
        try:
            if kind == "voice":
                note = self._voice(path)
            elif kind == "image":
                note = self._image(path)
            elif kind == "emoji":
                note = self._emoji(path, url)
        except Exception:  # noqa: BLE001 - 媒体处理失败不能影响主流程
            log.exception("处理 %s 消息失败", kind)
            note = ""

        if not note:
            return text
        return f"{text}\n{note}".strip() if text else note

    # ------------------------------------------------------------------ 语音

    def _voice(self, path: str) -> str:
        if not self.cfg.voice_enabled or not self.asr.enabled:
            return ""
        try:
            transcript = self.asr.transcribe(path)
        except MediaError as exc:
            self.metrics.inc("media_voice_failed")
            log.warning("语音转写失败：%s", exc)
            return self.cfg.placeholder_voice
        except Exception as exc:  # noqa: BLE001
            self.metrics.inc("media_voice_failed")
            log.exception("语音转写异常：%s", exc)
            return self.cfg.placeholder_voice

        self.metrics.inc("media_voice_ok")
        log.info("语音转写：%s", transcript[:80])
        return f"[语音] {transcript}"

    # ------------------------------------------------------------------ 图片

    def _image(self, path: str) -> str:
        if not path:
            return self.cfg.placeholder_image

        # GIF 顺手收进表情包（不管视觉模型开没开）
        if self.cfg.emoji_enabled and Path(path).suffix.lower() == ".gif":
            self._auto_collect_file(path, ["未分类"])

        if not self.cfg.image_enabled or not self.vision.enabled:
            return self.cfg.placeholder_image
        try:
            desc = self.vision.describe(path)
        except MediaError as exc:
            self.metrics.inc("media_image_failed")
            log.warning("图片描述失败：%s", exc)
            return self.cfg.placeholder_image
        except Exception as exc:  # noqa: BLE001
            self.metrics.inc("media_image_failed")
            log.exception("图片描述异常：%s", exc)
            return self.cfg.placeholder_image

        self.metrics.inc("media_image_ok")
        log.info("图片描述：%s", desc[:80])
        return f"[图片] {desc}"

    # ------------------------------------------------------------------ 表情

    def _emoji(self, path: str, url: str) -> str:
        if path:
            self._auto_collect_file(path, ["未分类"])
        elif url and self.cfg.emoji_enabled:
            self._auto_collect_url(url)
        return self.cfg.placeholder_emoji

    # ------------------------------------------------------------------ 收藏

    def _auto_collect_file(self, path: str, tags: list[str]) -> None:
        if not self.stickers or not self.cfg.emoji_enabled:
            return
        try:
            sticker = self.stickers.add_file(path, tags, source="auto-image")
            if sticker:
                self.metrics.inc("stickers_collected")
        except Exception:  # noqa: BLE001
            log.debug("自动收藏表情失败", exc_info=True)

    def _auto_collect_url(self, url: str) -> None:
        if not self.stickers:
            return
        data = self._download(url)
        if not data:
            return
        try:
            sticker = self.stickers.add_bytes(data, "", ["未分类"], source="auto-emoji")
            if sticker:
                self.metrics.inc("stickers_collected")
        except Exception:  # noqa: BLE001
            log.debug("自动收藏网络表情失败", exc_info=True)

    def save_media(self, msg: IncomingMessage, tags: list[str]) -> Sticker | None:
        """给 /表情 存 用：把指定消息里的图片/表情收进仓库。"""
        if not self.stickers:
            return None
        if msg.media_kind in ("image", "emoji") and msg.media_path:
            return self.stickers.add_file(msg.media_path, tags, source="command")
        if msg.media_kind == "emoji" and msg.media_url:
            data = self._download(msg.media_url)
            if data:
                return self.stickers.add_bytes(data, "", tags, source="command")
        return None

    # ------------------------------------------------------------------ 下载

    def _download(self, url: str) -> bytes | None:
        if not url.startswith(("http://", "https://")):
            return None
        request = urllib.request.Request(url, headers={"User-Agent": _UA})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                length = int(response.headers.get("Content-Length") or 0)
                if length and length > _MAX_DOWNLOAD:
                    log.info("表情太大（%d bytes），跳过", length)
                    return None
                data = response.read(_MAX_DOWNLOAD + 1)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.info("表情下载失败（IM CDN 经常需要登录态）：%s", exc)
            return None
        if len(data) > _MAX_DOWNLOAD:
            return None
        return data
