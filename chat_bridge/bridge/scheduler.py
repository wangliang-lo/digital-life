# -*- coding: utf-8 -*-
"""主动聊天定时器。

不是简单的 sleep(interval)：
    * 每次等待都带随机抖动，避免整点行为太机械；
    * 安静时段（默认 23:30-08:00）不打扰；
    * 支持 DSH 通过控制 API 立刻触发一次（无视安静时段）；
    * 两次主动消息之间强制最小间隔，防止手滑连点。
"""

from __future__ import annotations

import random
import re
import threading
import time
from datetime import datetime

from .adapters.base import IMAdapter
from .client import DigitalLifeClient
from .config import ProactiveConfig
from .logs import get_logger
from .pipeline import MessagePipeline
from .stats import Metrics

log = get_logger("scheduler")

_TAG_PREFIX = re.compile(r"^\s*\[(?:NEWS|DREAM|DIARY|PROACTIVE)\]\s*")


class ProactiveScheduler(threading.Thread):
    def __init__(
        self,
        cfg: ProactiveConfig,
        adapter: IMAdapter,
        client: DigitalLifeClient,
        pipeline: MessagePipeline,
        metrics: Metrics,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(name="proactive-scheduler", daemon=True)
        self.cfg = cfg
        self.adapter = adapter
        self.client = client
        self.pipeline = pipeline
        self.metrics = metrics
        self.stop_event = stop_event
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self.paused = False
        self.last_fire_at: float = 0.0
        self.last_message: str = ""
        self.last_error: str = ""

    # ------------------------------------------------------------------ 对外

    def trigger(self) -> None:
        """立刻跑一轮（DSH / 手动）。"""
        self._wake.set()

    def next_delay(self) -> float:
        base = self.cfg.interval_minutes * 60.0
        if self.cfg.jitter <= 0:
            return base
        return base * (1.0 + random.uniform(-self.cfg.jitter, self.cfg.jitter))

    # ------------------------------------------------------------------ 主循环

    def run(self) -> None:
        if not self.cfg.enabled:
            log.info("主动聊天已关闭")
            return
        log.info("主动聊天已启动：每 %.0f 分钟（抖动 %.0f%%），目标 %s",
                 self.cfg.interval_minutes, self.cfg.jitter * 100, self.cfg.target)

        if self.cfg.run_on_start:
            self.fire(manual=True)

        while not self.stop_event.is_set():
            delay = self.next_delay()
            deadline = time.monotonic() + delay
            manual = False
            # 分段等待：既能响应 trigger()，也能在 1 秒内响应退出
            while not self.stop_event.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if self._wake.wait(timeout=min(remaining, 1.0)):
                    self._wake.clear()
                    manual = True
                    break
            if self.stop_event.is_set():
                break
            self.fire(manual=manual)
        log.info("主动聊天线程已退出")

    # ------------------------------------------------------------------ 触发一次

    def fire(self, manual: bool = False) -> str | None:
        if self.paused:
            log.info("主动聊天处于暂停状态，跳过")
            return None
        if not self.adapter.is_alive():
            log.warning("IM未登录，跳过主动聊天")
            return None

        now = time.time()
        gap = max(0.0, self.cfg.min_gap_minutes * 60.0)
        if gap and self.last_fire_at and now - self.last_fire_at < gap:
            remaining = int(gap - (now - self.last_fire_at))
            log.info("距上次主动消息不足 %d 分钟，%ds 后再试", int(gap // 60), remaining)
            if not manual:
                return None
            # 手动触发也尊重最小间隔，但只等最多 5 秒，免得 HTTP 请求挂太久
            time.sleep(min(remaining, 5))

        if not manual and self.cfg.in_quiet_hours(datetime.now()):
            log.info("当前处于安静时段，跳过主动聊天")
            return None

        try:
            text = self.client.proactive(user_id=self.cfg.user_id)
        except Exception as exc:  # noqa: BLE001
            self.metrics.inc("proactive_error")
            self.last_error = str(exc)
            log.error("获取主动消息失败：%s", exc)
            return None

        if not text:
            self.metrics.inc("proactive_empty")
            log.info("数字生命这会儿没有主动消息")
            return None

        text = self._clean(text)
        if not text:
            return None

        target = self.adapter.resolve_target(self.cfg.target)
        if not target:
            self.metrics.inc("proactive_target_missing")
            self.last_error = f"找不到目标：{self.cfg.target}"
            log.error("找不到主动消息目标：%s（检查群名/备注是否写对）", self.cfg.target)
            return None

        wxid, kind = target
        result = self.adapter.send_text(wxid, text)
        if not result.ok:
            self.metrics.inc("proactive_send_failed")
            self.last_error = result.detail
            log.error("主动消息发送失败：%s", result.detail)
            return None

        self.last_fire_at = time.time()
        self.last_message = text
        self.last_error = ""
        self.metrics.inc("proactive_sent")
        self.pipeline.note_sent(wxid, text)
        log.info("主动消息已发送到 %s(%s)：%s", self.cfg.target, kind, text[:80].replace("\n", " "))
        return text

    # ------------------------------------------------------------------ 内部

    def _clean(self, text: str) -> str:
        text = text.strip()
        if self.cfg.strip_tags:
            # digital_life.proactive_chat 会带 [NEWS]/[DREAM]/[DIARY] 前缀，那是内部标记
            text = _TAG_PREFIX.sub("", text).strip()
        if len(text) > self.cfg.max_chars:
            text = text[: self.cfg.max_chars].rstrip() + "…"
        return text
