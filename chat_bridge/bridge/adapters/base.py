# -*- coding: utf-8 -*-
"""IM适配器接口。

所有适配器只做三件事：把平台消息规范化成 IncomingMessage、把文本发出去、
把"名字"解析成 wxid。业务逻辑（唤醒词、去重、调 API）一律不放在适配器里。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Protocol, runtime_checkable

FILEHELPER_WXID = "filehelper"


class AdapterError(RuntimeError):
    """适配器启动/发送失败。"""


class IMVersionError(AdapterError):
    """IM版本与适配器不匹配。"""


@dataclass(slots=True)
class IncomingMessage:
    """规范化后的IM消息（文本 / 图片 / 语音 / 表情）。"""

    msg_id: str
    from_wxid: str
    content: str = ""
    from_name: str = ""
    room_wxid: str = ""
    room_name: str = ""
    at_wxid: list[str] = field(default_factory=list)
    at_name: list[str] = field(default_factory=list)
    is_self: bool = False
    timestamp: float = 0.0

    # 多模态：media_kind 为空表示纯文本
    media_kind: str = ""            # image | voice | emoji
    media_path: str = ""            # 本地文件路径（image / voice）
    media_url: str = ""             # 表情 CDN 地址（emoji）
    media_duration: float = 0.0     # 语音秒数（拿得到就填）

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = time.time()

    @property
    def is_group(self) -> bool:
        return bool(self.room_wxid)

    @property
    def has_media(self) -> bool:
        return bool(self.media_kind)

    @property
    def conversation_id(self) -> str:
        """私聊=对方 wxid，群聊=群 wxid。"""
        return self.room_wxid or self.from_wxid


@dataclass(slots=True)
class SendResult:
    ok: bool
    detail: str = ""


Handler = Callable[[IncomingMessage], None]


@runtime_checkable
class IMAdapter(Protocol):
    name: str

    def start(self, on_message: Handler) -> None:
        ...

    def stop(self) -> None:
        ...

    @property
    def self_wxid(self) -> str:
        ...

    def is_alive(self) -> bool:
        ...

    def send_text(self, to_wxid: str, text: str) -> SendResult:
        ...

    def send_room_at(self, room_wxid: str, text: str, at_wxid: list[str]) -> SendResult:
        ...

    def send_image(self, to_wxid: str, file_path: str) -> SendResult:
        ...

    def send_gif(self, to_wxid: str, file_path: str) -> SendResult:
        ...

    def resolve_target(self, target: str) -> tuple[str, str] | None:
        """把 "文件传输助手" / "测试群" / wxid 解析成 (wxid, "private"|"group")。"""
        ...
