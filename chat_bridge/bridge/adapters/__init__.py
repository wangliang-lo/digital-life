# -*- coding: utf-8 -*-
"""适配器工厂。"""

from __future__ import annotations

from ..config import Config, ConfigError
from .base import AdapterError, IncomingMessage, SendResult, IMAdapter, IMVersionError

__all__ = [
    "AdapterError",
    "IncomingMessage",
    "SendResult",
    "IMAdapter",
    "IMVersionError",
    "create_adapter",
]


def create_adapter(cfg: Config) -> IMAdapter:
    name = cfg.chat.adapter
    if name == "ntchat":
        from .ntchat_adapter import NtchatAdapter

        return NtchatAdapter(cfg)
    if name == "mock":
        from .mock import MockAdapter

        return MockAdapter(cfg)
    raise ConfigError(f"未知适配器 {name!r}，可选：ntchat / mock")
