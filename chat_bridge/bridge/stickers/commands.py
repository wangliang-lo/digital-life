# -*- coding: utf-8 -*-
"""IM 里的 /表情 指令。

    /表情                 看标签和数量
    /表情 发 开心          发一张「开心」
    /表情 随机             随便发一张
    /表情 存 开心,可爱     把上一条图片/表情收进仓库
    /表情 删 <id>          删掉一张

只有 chat.owner_wxids 里的人能用（防止群里有人乱删）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..adapters.base import IncomingMessage
from ..logs import get_logger
from .store import Sticker, StickerStore

log = get_logger("stickers.commands")

PREFIXES = ("/表情", "／表情", "/贴纸", "/sticker")
_HELP = (
    "表情包用法：\n"
    "/表情            看现有标签\n"
    "/表情 发 开心     发一张\n"
    "/表情 随机        随便发\n"
    "/表情 存 开心,可爱 把上一条图片收进来\n"
    "/表情 删 <id>     删掉"
)


@dataclass(slots=True)
class StickerCommandResult:
    text: str | None = None
    sticker: Sticker | None = None


class StickerCommandHandler:
    def __init__(
        self,
        store: StickerStore,
        owner_wxids: tuple[str, ...] = (),
        enabled: bool = True,
    ) -> None:
        self.store = store
        self.owners = {wxid.strip() for wxid in owner_wxids if wxid.strip()}
        self.enabled = enabled

    # ------------------------------------------------------------------ 识别

    def is_command(self, text: str) -> bool:
        if not self.enabled:
            return False
        clean = (text or "").strip()
        return any(clean.startswith(prefix) for prefix in PREFIXES)

    # ------------------------------------------------------------------ 执行

    def handle(
        self,
        text: str,
        sender_wxid: str,
        last_media: IncomingMessage | None,
        save_media: Callable[[IncomingMessage, list[str]], Sticker | None],
    ) -> StickerCommandResult | None:
        if not self.enabled:
            return None
        if self.owners and sender_wxid not in self.owners:
            log.info("非管理员尝试使用表情指令：%s", sender_wxid)
            return None
        if not self.owners:
            log.warning("没有配置 chat.owner_wxids，表情指令不可用")
            return None

        body = (text or "").strip()
        for prefix in PREFIXES:
            if body.startswith(prefix):
                body = body[len(prefix):].strip()
                break
        parts = [part for part in body.replace("\u3000", " ").split() if part]
        action = parts[0] if parts else ""

        if action in ("", "列表", "list", "标签"):
            return StickerCommandResult(text=self._summary())

        if action in ("发", "send"):
            if len(parts) < 2:
                return StickerCommandResult(text="发哪张？例如：/表情 发 开心")
            sticker = self.store.pick([parts[1]])
            if sticker is None:
                return StickerCommandResult(text=f"还没有「{parts[1]}」的表情。")
            return StickerCommandResult(sticker=sticker)

        if action in ("随机", "random"):
            sticker = self.store.pick_random()
            if sticker is None:
                return StickerCommandResult(text="表情包还是空的，先发个图让我存一张？")
            return StickerCommandResult(sticker=sticker)

        if action in ("存", "保存", "添加", "add"):
            if last_media is None:
                return StickerCommandResult(text="这条会话里还没有能保存的图片或表情。")
            tags = parts[1:] or ["未分类"]
            sticker = save_media(last_media, tags)
            if sticker is None:
                return StickerCommandResult(text="没存下来（格式不支持或太大了）。")
            return StickerCommandResult(
                text=f"存好了：{sticker.id}（{'/'.join(sticker.tags)}）"
            )

        if action in ("删", "删除", "del", "remove"):
            if len(parts) < 2:
                return StickerCommandResult(text="要删哪个？给我 id，例如：/表情 删 a1b2c3d4e5f6")
            ok = self.store.remove(parts[1])
            return StickerCommandResult(text="删掉了。" if ok else f"没找到 id：{parts[1]}")

        return StickerCommandResult(text=_HELP)

    # ------------------------------------------------------------------ 文案

    def _summary(self) -> str:
        counts = self.store.tag_counts()
        total = self.store.count()
        if total == 0:
            return "表情包还是空的。发张图或表情给我，然后 /表情 存 开心"
        tags = "、".join(f"{tag}×{count}" for tag, count in sorted(counts.items(), key=lambda item: -item[1]))
        return f"表情包共 {total} 个：{tags}\n发一张：/表情 发 {next(iter(counts), '日常')}"
