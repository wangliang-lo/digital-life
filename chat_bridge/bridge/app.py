# -*- coding: utf-8 -*-
"""装配层：把适配器、消息管道、回复队列、媒体处理、定时器、控制口组装成一台机器。"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import __version__
from .adapters import create_adapter
from .adapters.base import IncomingMessage, IMAdapter
from .client import DigitalLifeClient
from .config import Config, ConfigError
from .control import ControlServer
from .logs import get_logger
from .media import ImageDescriber, MediaRouter, VoiceTranscriber
from .pipeline import MessagePipeline, Route
from .scheduler import ProactiveScheduler
from .stats import Metrics
from .stickers import StickerCommandHandler, StickerStore
from .worker import ReplyWorker

log = get_logger("app")


class BridgeApp:
    def __init__(self, cfg: Config, adapter: IMAdapter | None = None) -> None:
        self.cfg = cfg
        self.metrics = Metrics()
        self.stop_event = threading.Event()
        self.client = DigitalLifeClient(cfg.api)
        self.adapter: IMAdapter | None = adapter
        self.pipeline: MessagePipeline | None = None
        self.worker: ReplyWorker | None = None
        self.scheduler: ProactiveScheduler | None = None
        self.control: ControlServer | None = None

        self.sticker_store: StickerStore | None = None
        self.commands: StickerCommandHandler | None = None
        self.media: MediaRouter | None = None
        self._media_pool: ThreadPoolExecutor | None = None
        self._last_media: dict[str, IncomingMessage] = {}

        self.paused = False
        self.started_at: float = 0.0
        self._stopped = False
        self._stop_lock = threading.Lock()

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> None:
        self._setup_stickers()
        self._setup_media()

        if self.adapter is None:
            self.adapter = create_adapter(self.cfg)
        log.info("启动适配器：%s", self.adapter.name)
        self.adapter.start(self._on_incoming)

        self.pipeline = MessagePipeline(self.cfg.chat, self.adapter.self_wxid, self.metrics)
        if not self.adapter.self_wxid:
            log.warning("没有拿到自己的 wxid，防自环主要依赖适配器上报的 is_self 标记")

        self.worker = ReplyWorker(
            self.cfg,
            self.adapter,
            self.client,
            self.pipeline,
            self.metrics,
            self.stop_event,
            stickers=self.sticker_store,
        )
        self.scheduler = ProactiveScheduler(
            self.cfg.proactive, self.adapter, self.client, self.pipeline, self.metrics, self.stop_event
        )
        self.worker.start()
        self.scheduler.start()

        if self.cfg.control.enabled:
            self.control = ControlServer(self.cfg.control, self)
            try:
                self.control.start()
            except OSError as exc:
                log.error("控制接口启动失败（端口 %d 被占用？）：%s", self.cfg.control.port, exc)
                self.control = None

        self.started_at = time.time()
        log.info(
            "中间件就绪：账号 %s，唤醒词 %s，API %s",
            self.adapter.self_wxid or "未知",
            "/".join(self.cfg.chat.wake_words) or "（无）",
            self.cfg.api.base_url,
        )
        if self.sticker_store:
            log.info("表情包：%d 个（%s）", self.sticker_store.count(), self.sticker_store.root)

    def _setup_stickers(self) -> None:
        if not self.cfg.stickers.enabled:
            return
        root = self.cfg.resolve_dir(self.cfg.stickers.dir)
        self.sticker_store = StickerStore(root, max_kb=self.cfg.stickers.max_kb)
        self.sticker_store.load()
        self.commands = StickerCommandHandler(
            self.sticker_store,
            owner_wxids=self.cfg.chat.owner_wxids,
            enabled=self.cfg.stickers.commands_enabled,
        )
        if not self.cfg.chat.owner_wxids:
            log.warning("没有配置 chat.owner_wxids：/表情 指令不可用（自动收集仍然工作）")

    def _setup_media(self) -> None:
        if not self.cfg.media.enabled:
            return
        self.media = MediaRouter(
            self.cfg.media,
            VoiceTranscriber(self.cfg.asr),
            ImageDescriber(self.cfg.vision),
            self.sticker_store,
            self.metrics,
        )
        self._media_pool = ThreadPoolExecutor(
            max_workers=self.cfg.media.workers, thread_name_prefix="media"
        )
        log.info(
            "多模态：语音=%s（%s），图片=%s（%s）",
            "开" if self.cfg.media.voice_enabled and self.cfg.asr.provider != "none" else "关",
            self.cfg.asr.provider,
            "开" if self.cfg.media.image_enabled and self.cfg.vision.provider != "none" else "关",
            self.cfg.vision.provider,
        )

    def stop(self) -> None:
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
        self.stop_event.set()
        log.info("正在停止……")
        if self.control:
            self.control.stop()
        if self._media_pool:
            self._media_pool.shutdown(wait=False, cancel_futures=True)
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=5.0)
        if self.scheduler and self.scheduler.is_alive():
            self.scheduler.join(timeout=5.0)
        if self.adapter:
            try:
                self.adapter.stop()
            except Exception:  # noqa: BLE001
                log.exception("关闭适配器时出错")
        log.info("已停止")

    def request_stop(self) -> None:
        """给控制口 /shutdown 用：在别的线程里关。"""
        self.stop_event.set()

    # ------------------------------------------------------------------ 消息入口

    def _on_incoming(self, msg: IncomingMessage) -> None:
        """适配器回调，必须极快返回；重活全部丢给队列/线程池。"""
        if self.stop_event.is_set() or self.pipeline is None or self.worker is None:
            return
        if self.paused:
            self.metrics.inc("ignored_paused")
            return

        if msg.has_media:
            self._remember_media(msg)

        route = self.pipeline.evaluate(msg)
        if route is None:
            return

        self.metrics.set_last(
            "last_route",
            {
                "at": time.time(),
                "kind": route.kind,
                "sender": route.sender_name or route.sender_wxid,
                "text": route.text[:120],
                "trigger": route.trigger,
                "media": route.media_kind,
            },
        )

        if self.commands and route.text and self.commands.is_command(route.text):
            self._handle_command(route)
            return

        if route.media_kind and self.media and self._media_pool:
            self.metrics.inc("media_queued")
            self._media_pool.submit(self._enrich_and_dispatch, route)
            return

        self.worker.submit(route)

    def _remember_media(self, msg: IncomingMessage) -> None:
        """记下每个会话最后一条媒体，给 /表情 存 用。"""
        self._last_media[msg.conversation_id] = msg
        if len(self._last_media) > 200:
            for key in list(self._last_media)[:50]:
                self._last_media.pop(key, None)

    def _enrich_and_dispatch(self, route: Route) -> None:
        assert self.media is not None and self.worker is not None
        try:
            route.text = self.media.enrich(
                route.text, route.media_kind, route.media_path, route.media_url
            )
        except Exception:  # noqa: BLE001
            log.exception("媒体增强失败")
        if not route.text:
            route.text = self._placeholder_for(route.media_kind)
        self.metrics.inc("media_processed")
        self.worker.submit(route)

    def _placeholder_for(self, kind: str) -> str:
        if kind == "voice":
            return self.cfg.media.placeholder_voice
        if kind == "image":
            return self.cfg.media.placeholder_image
        return self.cfg.media.placeholder_emoji

    # ------------------------------------------------------------------ 表情指令

    def _handle_command(self, route: Route) -> None:
        assert self.commands is not None and self.sticker_store is not None
        last_media = self._last_media.get(route.conversation_id)
        save = self.media.save_media if self.media else (lambda msg, tags: None)
        result = self.commands.handle(route.text, route.sender_wxid, last_media, save)
        if result is None:
            return
        self.metrics.inc("sticker_commands")

        if result.sticker is not None:
            path = self.sticker_store.resolve(result.sticker)
            if path.exists():
                if path.suffix.lower() == ".gif":
                    sent = self.adapter.send_gif(route.target_wxid, str(path))
                else:
                    sent = self.adapter.send_image(route.target_wxid, str(path))
                if sent.ok:
                    self.sticker_store.mark_used(result.sticker.id)
                else:
                    log.warning("指令发表情失败：%s", sent.detail)

        if result.text:
            self._send_direct(route, result.text)

    def _send_direct(self, route: Route, text: str) -> None:
        assert self.adapter is not None
        if route.kind == "group" and route.at_wxid:
            result = self.adapter.send_room_at(route.target_wxid, text, route.at_wxid)
        else:
            result = self.adapter.send_text(route.target_wxid, text)
        if result.ok and self.pipeline:
            self.pipeline.note_sent(route.conversation_id, text)

    # ------------------------------------------------------------------ 控制口动作

    def send_manual(self, target: str, text: str) -> dict:
        if self.adapter is None:
            return {"ok": False, "error": "中间件尚未启动"}
        target = (target or "").strip()
        text = (text or "").strip()
        if not target:
            return {"ok": False, "error": "target 不能为空（wxid / 群名 / 文件传输助手）"}
        if not text:
            return {"ok": False, "error": "text 不能为空"}

        resolved = self.adapter.resolve_target(target)
        if not resolved:
            return {"ok": False, "error": f"找不到发送目标：{target}"}
        wxid, kind = resolved
        result = self.adapter.send_text(wxid, text)
        if result.ok and self.pipeline:
            self.pipeline.note_sent(wxid, text)
        return {
            "ok": result.ok,
            "target": target,
            "wxid": wxid,
            "kind": kind,
            "error": None if result.ok else result.detail,
        }

    def send_sticker(self, tag: str = "", target: str = "") -> dict:
        """控制口/CLI 用：直接发一张表情。"""
        if self.adapter is None or self.sticker_store is None:
            return {"ok": False, "error": "中间件尚未启动或表情包未启用"}
        sticker = self.sticker_store.pick([tag]) if tag else self.sticker_store.pick_random()
        if sticker is None:
            return {"ok": False, "error": "表情包里没有匹配的表情"}
        target = (target or "").strip()
        if target:
            resolved = self.adapter.resolve_target(target)
            if not resolved:
                return {"ok": False, "error": f"找不到发送目标：{target}"}
            wxid = resolved[0]
        else:
            return {"ok": False, "error": "target 不能为空"}

        path = self.sticker_store.resolve(sticker)
        result = (
            self.adapter.send_gif(wxid, str(path))
            if path.suffix.lower() == ".gif"
            else self.adapter.send_image(wxid, str(path))
        )
        if result.ok:
            self.sticker_store.mark_used(sticker.id)
        return {
            "ok": result.ok,
            "sticker": sticker.id,
            "tags": sticker.tags,
            "target": target,
            "error": None if result.ok else result.detail,
        }

    def trigger_proactive(self) -> dict:
        if not self.scheduler:
            return {"ok": False, "sent": False, "error": "定时器尚未启动"}
        text = self.scheduler.fire(manual=True)
        return {
            "ok": True,
            "sent": bool(text),
            "message": text,
            "error": None if text else (self.scheduler.last_error or "数字生命这会儿没有主动消息"),
        }

    def set_paused(self, paused: bool) -> dict:
        self.paused = bool(paused)
        if self.scheduler:
            self.scheduler.paused = self.paused
        log.info("已%s消息处理", "暂停" if self.paused else "恢复")
        return {"ok": True, "paused": self.paused}

    def reload_config(self) -> dict:
        if not self.cfg.path:
            return {"ok": False, "error": "启动时没有指定配置文件，无法热重载"}
        try:
            fresh = Config.load(self.cfg.path)
        except ConfigError as exc:
            return {"ok": False, "error": str(exc)}

        # 只热更新运行期可以安全替换的部分；适配器/端口需要重启
        self.cfg.api = fresh.api
        self.client.cfg = fresh.api
        self.cfg.chat = fresh.chat
        self.cfg.proactive = fresh.proactive
        self.cfg.stickers = fresh.stickers
        self.cfg.media = fresh.media
        if self.pipeline:
            self.pipeline.cfg = fresh.chat
        if self.scheduler:
            self.scheduler.cfg = fresh.proactive
        if self.worker:
            self.worker.cfg.api = fresh.api
            self.worker.sticker_cfg = fresh.stickers
        if self.media:
            self.media.cfg = fresh.media
        if self.sticker_store:
            self.sticker_store.reload()
        log.info("配置已热重载：%s", self.cfg.path)
        return {
            "ok": True,
            "reloaded": ["api", "chat(过滤规则)", "proactive", "stickers", "media"],
            "restart_required": ["chat.adapter", "control.port", "logging", "asr.provider", "vision.provider"],
        }

    # ------------------------------------------------------------------ 观测

    def status(self) -> dict:
        now = time.time()
        scheduler_info: dict = {"enabled": False}
        if self.scheduler:
            scheduler_info = {
                "enabled": self.cfg.proactive.enabled,
                "paused": self.scheduler.paused,
                "interval_minutes": self.cfg.proactive.interval_minutes,
                "target": self.cfg.proactive.target,
                "last_fire_at": _iso(self.scheduler.last_fire_at),
                "last_message": self.scheduler.last_message[:200],
                "last_error": self.scheduler.last_error,
            }
        worker_info: dict = {"pending": 0, "max": self.cfg.chat.max_queue}
        if self.worker:
            worker_info["pending"] = self.worker.pending

        sticker_info = {"enabled": bool(self.sticker_store)}
        if self.sticker_store:
            sticker_info.update(
                {
                    "count": self.sticker_store.count(),
                    "tags": self.sticker_store.tag_counts(),
                    "dir": str(self.sticker_store.root),
                }
            )

        return {
            "ok": True,
            "service": "wxbridge",
            "version": __version__,
            "started_at": _iso(self.started_at),
            "uptime_seconds": round(now - self.started_at, 1) if self.started_at else 0,
            "paused": self.paused,
            "adapter": {
                "name": self.adapter.name if self.adapter else None,
                "self_wxid": self.adapter.self_wxid if self.adapter else "",
                "alive": self.adapter.is_alive() if self.adapter else False,
            },
            "api": {"base_url": self.cfg.api.base_url, "chat": self.cfg.api.chat_url},
            "media": {
                "enabled": bool(self.media),
                "asr": self.cfg.asr.provider,
                "asr_model": self.cfg.asr.local_model,
                "vision": self.cfg.vision.provider,
                "vision_model": self.cfg.vision.model,
            },
            "stickers": sticker_info,
            "queue": worker_info,
            "scheduler": scheduler_info,
            "metrics": self.metrics.snapshot(),
        }


def _iso(timestamp: float) -> str | None:
    if not timestamp:
        return None
    return datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")
