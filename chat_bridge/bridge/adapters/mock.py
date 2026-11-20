# -*- coding: utf-8 -*-
"""Mock 适配器：不碰IM，用来跑通链路和写测试。

    mock = MockAdapter()
    mock.inject(IncomingMessage(msg_id="1", from_wxid="wxid_a", content="你好"))
"""

from __future__ import annotations

import threading

from ..config import Config
from ..logs import get_logger
from .base import FILEHELPER_WXID, IncomingMessage, SendResult

log = get_logger("adapter.mock")


class MockAdapter:
    name = "mock"

    def __init__(self, cfg: Config | None = None, self_wxid: str = "wxid_self") -> None:
        self.cfg = cfg
        self._self_wxid = self_wxid
        self.handler = None
        self.sent: list[dict] = []
        self.fail_send = False
        self.alive = True
        self._lock = threading.Lock()
        self.rooms = {
            "测试群": "123456@chatroom",
            "数字生命测试群": "654321@chatroom",
        }
        self.contacts = {
            "张三": "wxid_zhangsan",
            "李四": "wxid_lisi",
        }

    # ------------------------------------------------------------------ 生命周期

    def start(self, on_message) -> None:  # noqa: ANN001
        self.handler = on_message
        log.info("Mock 适配器已启动（不会真的发IM）")

    def stop(self) -> None:
        self.alive = False

    @property
    def self_wxid(self) -> str:
        return self._self_wxid

    def is_alive(self) -> bool:
        return self.alive

    # ------------------------------------------------------------------ 测试注入

    def inject(self, msg: IncomingMessage) -> None:
        if self.handler is None:
            raise RuntimeError("MockAdapter 还没 start()")
        self.handler(msg)

    def make_message(
        self,
        content: str,
        *,
        from_wxid: str = "wxid_zhangsan",
        from_name: str = "张三",
        room_wxid: str = "",
        at: bool = False,
        msg_id: str | None = None,
        is_self: bool = False,
        media_kind: str = "",
        media_path: str = "",
        media_url: str = "",
    ) -> IncomingMessage:
        return IncomingMessage(
            msg_id=msg_id or f"m{len(self.sent) + int(__import__('time').time() * 1000)}",
            from_wxid=from_wxid,
            content=content,
            from_name=from_name,
            room_wxid=room_wxid,
            at_wxid=[self._self_wxid] if at else [],
            is_self=is_self,
            media_kind=media_kind,
            media_path=media_path,
            media_url=media_url,
        )

    # ------------------------------------------------------------------ 发送

    def send_text(self, to_wxid: str, text: str) -> SendResult:
        if self.fail_send:
            return SendResult(False, "mock: 强制失败")
        with self._lock:
            self.sent.append({"kind": "text", "to": to_wxid, "text": text, "at": []})
        log.info("[mock] -> %s: %s", to_wxid, text[:60].replace("\n", " "))
        return SendResult(True, "mock")

    def send_room_at(self, room_wxid: str, text: str, at_wxid: list[str]) -> SendResult:
        if self.fail_send:
            return SendResult(False, "mock: 强制失败")
        with self._lock:
            self.sent.append({"kind": "room_at", "to": room_wxid, "text": text, "at": list(at_wxid)})
        log.info("[mock] -> %s @%s: %s", room_wxid, at_wxid, text[:60].replace("\n", " "))
        return SendResult(True, "mock")

    def send_image(self, to_wxid: str, file_path: str) -> SendResult:
        if self.fail_send:
            return SendResult(False, "mock: 强制失败")
        with self._lock:
            self.sent.append({"kind": "image", "to": to_wxid, "text": "", "at": [], "file": file_path})
        log.info("[mock] -> %s [image] %s", to_wxid, file_path)
        return SendResult(True, "mock")

    def send_gif(self, to_wxid: str, file_path: str) -> SendResult:
        if self.fail_send:
            return SendResult(False, "mock: 强制失败")
        with self._lock:
            self.sent.append({"kind": "gif", "to": to_wxid, "text": "", "at": [], "file": file_path})
        log.info("[mock] -> %s [gif] %s", to_wxid, file_path)
        return SendResult(True, "mock")

    def resolve_target(self, target: str) -> tuple[str, str] | None:
        target = (target or "").strip()
        if target in ("文件传输助手", "filehelper"):
            return (FILEHELPER_WXID, "private")
        if target in self.rooms:
            return (self.rooms[target], "group")
        if target in self.contacts:
            return (self.contacts[target], "private")
        if target.endswith("@chatroom") or target.startswith("wxid_"):
            return (target, "group" if target.endswith("@chatroom") else "private")
        return None

    # ------------------------------------------------------------------ 观测

    def drain(self) -> list[dict]:
        with self._lock:
            items = list(self.sent)
            self.sent.clear()
        return items
