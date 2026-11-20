# -*- coding: utf-8 -*-
"""数字生命 HTTP API 客户端（标准库 urllib，无第三方依赖）。

约定：
    POST /chat       {"message": "...", "user_id": "...", "sender": "..."}
                     -> {"reply": "..."}
    POST /proactive  {"user_id": "..."}
                     -> {"reply": "..."} 或 {"reply": null}

为了兼容不同实现，reply 也接受 message / text / data.reply 这些字段名。
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .config import ApiConfig
from .logs import get_logger

log = get_logger("client")


class DigitalLifeError(RuntimeError):
    """API 明确报错（4xx、返回格式不对、reply 为空）。"""


class _TransientError(Exception):
    """可重试的临时错误（超时、连接失败、5xx）。"""


@dataclass(slots=True)
class ChatReply:
    """一次对话的结果：文本 + 情绪（情绪用来挑表情包）。"""

    text: str
    emotion: dict[str, float] = field(default_factory=dict)
    intimacy: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


def _extract_text(data: Any) -> str | None:
    """从各种可能的返回结构里挖出文本。"""
    if data is None:
        return None
    if isinstance(data, str):
        return data.strip() or None
    if not isinstance(data, dict):
        return None

    for key in ("reply", "message", "text", "content"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    inner = data.get("data")
    if isinstance(inner, dict):
        for key in ("reply", "message", "text", "content"):
            value = inner.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


class DigitalLifeClient:
    def __init__(self, cfg: ApiConfig) -> None:
        self.cfg = cfg

    # ------------------------------------------------------------------ 公开接口

    def chat(
        self,
        message: str,
        *,
        user_id: str = "",
        sender_name: str = "",
        image_path: str = "",
        audio_path: str = "",
        emoji_url: str = "",
        channel: str = "",
        group: str = "",
        image_base64: str = "",
        audio_base64: str = "",
        image_suffix: str = "",
        audio_suffix: str = "",
        internal: bool = False,
    ) -> ChatReply:
        payload = {"message": message}
        if user_id:
            payload["user_id"] = user_id
        if sender_name:
            payload["sender"] = sender_name
        # 渠道：网页端用 IM / TIKTOK 登录时，把这一轮标成对应渠道；
        # 她还是同一个脑子，只是知道这话是在IM/抖音里收到的。
        if channel:
            payload["channel"] = channel
        if group:
            payload["group"] = group
        # 媒体字段交给 dl_api 处理（语音转写 / 图片描述），中间件本身不用懂模型
        if image_path:
            payload["image_path"] = image_path
        if audio_path:
            payload["audio_path"] = audio_path
        if emoji_url:
            payload["emoji_url"] = emoji_url
        # 2026-11-13：网页和她的脑子可能不在一台机器（公网→隧道→本机），
        # 路径对方读不到，所以把上传文件内容也带一份过去。
        if image_base64:
            payload["image_base64"] = image_base64
            if image_suffix:
                payload["image_suffix"] = image_suffix
        if internal:
            payload["internal"] = 1
        if audio_base64:
            payload["audio_base64"] = audio_base64
            if audio_suffix:
                payload["audio_suffix"] = audio_suffix

        data = self._post(self.cfg.chat_url, payload)
        reply = _extract_text(data)
        if not reply and isinstance(data, dict) and data.get("ignored"):
            return ChatReply(text="", emotion={}, intimacy=0, raw=data)
        if not reply:
            raise DigitalLifeError(f"/chat 返回里没有可用的 reply：{str(data)[:200]}")

        emotion: dict[str, float] = {}
        intimacy = 0
        if isinstance(data, dict):
            raw_emotion = data.get("emotion")
            if isinstance(raw_emotion, dict):
                for key in ("joy", "hurt", "impulse"):
                    value = raw_emotion.get(key)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        emotion[key] = float(value)
            try:
                intimacy = int(data.get("intimacy") or 0)
            except (TypeError, ValueError):
                intimacy = 0
        return ChatReply(text=reply, emotion=emotion, intimacy=intimacy, raw=data if isinstance(data, dict) else {})

    def proactive(self, *, user_id: str = "") -> str | None:
        """返回 None 表示"这会儿她不想说话"，不是错误。"""
        return _extract_text(self.proactive_data(user_id=user_id))

    def proactive_data(self, *, user_id: str = "") -> dict[str, Any]:
        """原样返回 /proactive 的 JSON（网页要拿 replies / delay_ms）。"""
        payload: dict[str, Any] = {}
        if user_id:
            payload["user_id"] = user_id
        data = self._post(self.cfg.proactive_url, payload)
        return data if isinstance(data, dict) else {}

    def ping(self, timeout: float = 3.0) -> tuple[bool, str]:
        """给 doctor 用：只确认服务活着。"""
        request = urllib.request.Request(self.cfg.status_url, method="GET")
        self._auth(request)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return True, f"HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            # 404 也说明服务在跑，只是没有 /status
            if exc.code == 404:
                return True, "HTTP 404（服务在跑，但没有 /status）"
            return False, f"HTTP {exc.code}"
        except (urllib.error.URLError, OSError, socket.timeout) as exc:
            return False, f"连不上：{exc}"

    # ------------------------------------------------------------------ 内部

    def _auth(self, request: urllib.request.Request) -> None:
        if self.cfg.token:
            request.add_header("Authorization", f"Bearer {self.cfg.token}")

    def _post(self, url: str, payload: dict[str, Any]) -> Any:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        attempts = max(1, self.cfg.retries + 1)
        last_error: Exception | None = None

        for attempt in range(attempts):
            request = urllib.request.Request(url, data=body, method="POST")
            request.add_header("Content-Type", "application/json; charset=utf-8")
            request.add_header("Accept", "application/json")
            self._auth(request)
            try:
                return self._send(request)
            except _TransientError as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    backoff = 0.8 * (2 ** attempt)
                    log.warning("API 临时失败（第 %d/%d 次）：%s，%.1fs 后重试", attempt + 1, attempts, exc, backoff)
                    time.sleep(backoff)
            except DigitalLifeError:
                raise

        raise DigitalLifeError(f"API 请求失败（已重试 {attempts} 次）：{last_error}")

    def _send(self, request: urllib.request.Request) -> Any:
        try:
            with urllib.request.urlopen(request, timeout=self.cfg.timeout) as response:
                raw = response.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")[:300]
            message = f"HTTP {exc.code} {request.full_url}：{detail}"
            if exc.code >= 500 or exc.code == 429:
                raise _TransientError(message) from exc
            raise DigitalLifeError(message) from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
            reason = getattr(exc, "reason", exc)
            raise _TransientError(f"{request.full_url} 连接失败：{reason}") from exc
        except OSError as exc:
            raise _TransientError(f"{request.full_url} 连接失败：{exc}") from exc

        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DigitalLifeError(f"API 返回不是 JSON：{raw[:200]}") from exc
