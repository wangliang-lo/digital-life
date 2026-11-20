# -*- coding: utf-8 -*-
"""回复队列：单线程串行处理，保护数字生命 API 和IM发送通道。

除了文字回复，这里还负责"发一张表情包"：
    1. 模型在回复里写了 [表情:开心]  -> 发对应的表情（并从正文里删掉标签）
    2. 没写标签 -> 按返回的情绪（joy/hurt/impulse）加权，有概率附一张
    3. 用户明确要「发个表情」 -> 一定发
"""

from __future__ import annotations

import queue
import random
import re
import threading
import time

from .adapters.base import SendResult, IMAdapter
from .client import ChatReply, DigitalLifeClient, DigitalLifeError
from .config import Config, StickerConfig
from .logs import get_logger
from .pipeline import MessagePipeline, Route, split_message
from .stats import Metrics
from .stickers.store import StickerStore

log = get_logger("worker")

STICKER_TAG = re.compile(r"[\[【](?:表情|贴纸|sticker|emoji)[:：]\s*([^\]】]{1,20})[\]】]", re.IGNORECASE)
_ASK_STICKER = ("发个表情", "发张表情", "表情包", "发个图", "来张图", "贴纸")


def mood_tag(emotion: dict[str, float]) -> str:
    """把数字生命的情绪三维映射成表情标签。"""
    joy = float(emotion.get("joy", 50.0))
    hurt = float(emotion.get("hurt", 20.0))
    impulse = float(emotion.get("impulse", 30.0))
    if hurt >= 65:
        return "委屈"
    if impulse >= 75:
        return "生气"
    if joy >= 65:
        return "开心"
    if joy < 35:
        return "失落"
    return "日常"


class ReplyWorker(threading.Thread):
    def __init__(
        self,
        cfg: Config,
        adapter: IMAdapter,
        client: DigitalLifeClient,
        pipeline: MessagePipeline,
        metrics: Metrics,
        stop_event: threading.Event,
        stickers: StickerStore | None = None,
        rng: random.Random | None = None,
    ) -> None:
        super().__init__(name="reply-worker", daemon=True)
        self.cfg = cfg
        self.adapter = adapter
        self.client = client
        self.pipeline = pipeline
        self.metrics = metrics
        self.stop_event = stop_event
        self.stickers = stickers
        self.sticker_cfg: StickerConfig = cfg.stickers
        self.rng = rng or random.Random()

        self.queue: queue.Queue[Route] = queue.Queue(maxsize=cfg.chat.max_queue)
        self._last_sent: dict[str, float] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 入队

    def submit(self, route: Route) -> bool:
        try:
            self.queue.put_nowait(route)
            self.metrics.inc("queued")
            return True
        except queue.Full:
            self.metrics.inc("dropped_queue_full")
            log.error("回复队列已满（%d），丢弃来自 %s 的消息", self.cfg.chat.max_queue, route.sender_wxid)
            return False

    # ------------------------------------------------------------------ 主循环

    def run(self) -> None:
        log.info("回复线程已启动")
        while not self.stop_event.is_set():
            try:
                route = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._process(route)
            except Exception:  # noqa: BLE001 - 单条消息失败不能带崩线程
                log.exception("处理消息时出现未捕获异常")
                self.metrics.inc("worker_exception")
            finally:
                self.queue.task_done()
        log.info("回复线程已退出")

    # ------------------------------------------------------------------ 单条处理

    def _process(self, route: Route) -> None:
        if not self.adapter.is_alive():
            log.error("IM未登录，放弃回复 %s", route.conversation_id)
            self.metrics.inc("dropped_not_alive")
            return

        self._pace(route)
        started = time.time()
        reply = ""
        emotion: dict[str, float] = {}

        try:
            result: ChatReply = self.client.chat(
                route.text,
                user_id=route.user_id,
                sender_name=route.sender_name,
            )
            reply = result.text
            emotion = result.emotion
            self.metrics.inc("api_ok")
            self.metrics.set_last("last_reply", {"text": reply[:200], "at": time.time()})
        except DigitalLifeError as exc:
            self.metrics.inc("api_error")
            log.error("数字生命 API 出错：%s", exc)
            reply = self.cfg.chat.fallback_on_error
        except Exception as exc:  # noqa: BLE001
            self.metrics.inc("api_error")
            log.exception("调用数字生命 API 异常：%s", exc)
            reply = self.cfg.chat.fallback_on_error

        latency = time.time() - started
        if not reply:
            log.info("本轮没有回复（%s），耗时 %.1fs", route.conversation_id, latency)
            return

        body, explicit_tag = self._split_sticker_tag(reply.strip())
        if body:
            parts = (
                split_message(body, self.cfg.chat.max_reply_chars)
                if self.cfg.chat.split_long_reply
                else [body[: self.cfg.chat.max_reply_chars]]
            )
            log.info(
                "回复 %s（%s，%d 段，%.1fs）：%s",
                route.sender_name or route.sender_wxid,
                route.trigger,
                len(parts),
                latency,
                body[:80].replace("\n", " "),
            )
            for index, part in enumerate(parts):
                if self.stop_event.is_set():
                    return
                if not self._send(route, part):
                    break
                self.pipeline.note_sent(route.conversation_id, part)
                self._last_sent[route.conversation_id] = time.time()
                self.metrics.inc("replies_sent")
                if index + 1 < len(parts) and self.cfg.chat.send_gap > 0:
                    time.sleep(self.cfg.chat.send_gap)

        if self.cfg.chat.send_gap > 0:
            time.sleep(min(0.6, self.cfg.chat.send_gap))
        self._maybe_send_sticker(route, emotion, explicit_tag)

    def _pace(self, route: Route) -> None:
        """同一个会话两次回复之间留一点间隔，避免刷屏。"""
        interval = self.cfg.chat.min_reply_interval
        if interval <= 0:
            return
        with self._lock:
            last = self._last_sent.get(route.conversation_id, 0.0)
        wait = interval - (time.time() - last)
        if wait > 0:
            time.sleep(min(wait, 10.0))

    def _send(self, route: Route, text: str) -> bool:
        result: SendResult
        if route.kind == "group" and route.at_wxid:
            result = self.adapter.send_room_at(route.target_wxid, text, route.at_wxid)
            if not result.ok:
                # @ 失败就退回普通群消息，至少别把话丢了
                log.warning("群 @ 发送失败（%s），退化为普通消息", result.detail)
                prefix = f"@{route.sender_name} " if route.sender_name else ""
                result = self.adapter.send_text(route.target_wxid, prefix + text)
        else:
            result = self.adapter.send_text(route.target_wxid, text)

        if not result.ok:
            self.metrics.inc("send_failed")
            log.error("发送失败（%s）：%s", route.conversation_id, result.detail)
            return False
        return True

    # ------------------------------------------------------------------ 表情包

    @staticmethod
    def _split_sticker_tag(reply: str) -> tuple[str, str]:
        match = STICKER_TAG.search(reply)
        if not match:
            return reply, ""
        cleaned = (reply[: match.start()] + reply[match.end():]).strip()
        return cleaned, match.group(1).strip()

    def _maybe_send_sticker(self, route: Route, emotion: dict[str, float], explicit_tag: str) -> None:
        cfg = self.sticker_cfg
        if not cfg.enabled or self.stickers is None:
            return
        if self.stickers.count() == 0:
            return

        tag = explicit_tag
        forced = bool(tag)
        if not tag and cfg.mood_enabled:
            tag = mood_tag(emotion)
        if not tag:
            tag = "日常"

        asked = any(word in (route.text or "") for word in _ASK_STICKER)
        if not forced and not asked and self.rng.random() > cfg.chance:
            return

        sticker = self.stickers.pick([tag])
        if sticker is None:
            return
        path = self.stickers.resolve(sticker)
        if not path.exists():
            return

        if path.suffix.lower() == ".gif":
            result = self.adapter.send_gif(route.target_wxid, str(path))
        else:
            result = self.adapter.send_image(route.target_wxid, str(path))

        if result.ok:
            self.stickers.mark_used(sticker.id)
            self.metrics.inc("stickers_sent")
            log.info(
                "发表情 %s（%s）-> %s",
                sticker.id,
                "/".join(sticker.tags),
                route.target_wxid,
            )
        else:
            self.metrics.inc("sticker_send_failed")
            log.warning("表情发送失败：%s", result.detail)

    # ------------------------------------------------------------------ 观测

    @property
    def pending(self) -> int:
        return self.queue.qsize()
