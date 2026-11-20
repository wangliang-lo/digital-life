# -*- coding: utf-8 -*-
"""本地控制 API：DSH / 脚本 / 手搓 curl 都能指挥这个中间件。

只绑 127.0.0.1；如果配置了 token，则要求请求头：
    X-Bridge-Token: <token>
或
    Authorization: Bearer <token>

接口：
    GET  /health                 存活检查
    GET  /status                 运行状态（队列、计数、最近消息）
    POST /send                   {"target": "文件传输助手", "text": "..."}
    POST /sticker                {"tag": "开心", "target": "文件传输助手"}
    POST /proactive              立刻触发一次主动聊天
    POST /pause                  {"paused": true|false}
    POST /reload                 重新读取配置文件
    POST /shutdown               优雅退出
"""

from __future__ import annotations

import hmac
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

from . import __version__
from .config import ControlConfig
from .logs import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from .app import BridgeApp

log = get_logger("control")

_MAX_BODY = 1024 * 1024


class _Handler(BaseHTTPRequestHandler):
    server_version = f"WxBridge/{__version__}"
    protocol_version = "HTTP/1.1"

    app: "BridgeApp"
    token: str = ""

    # ------------------------------------------------------------ HTTP 方法

    def handle_one_request(self) -> None:
        # 客户端提前断开时不要往 stderr 喷堆栈
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path == "/health":
            self._json(200, {"ok": True, "version": __version__, "service": "wxbridge"})
        elif path == "/status":
            self._json(200, self.app.status())
        else:
            self._json(404, {"ok": False, "error": f"未知接口 {path}"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        body = self._read_json()
        if body is None:
            return

        try:
            if path == "/send":
                target = str(body.get("target") or "").strip()
                text = str(body.get("text") or "")
                result = self.app.send_manual(target, text)
                self._json(200 if result.get("ok") else 400, result)
            elif path == "/sticker":
                tag = str(body.get("tag") or "").strip()
                target = str(body.get("target") or self.app.cfg.proactive.target).strip()
                result = self.app.send_sticker(tag, target)
                self._json(200 if result.get("ok") else 400, result)
            elif path == "/proactive":
                result = self.app.trigger_proactive()
                self._json(200 if result.get("ok") else 500, result)
            elif path == "/pause":
                paused = bool(body.get("paused", True))
                self._json(200, self.app.set_paused(paused))
            elif path == "/reload":
                self._json(200, self.app.reload_config())
            elif path == "/shutdown":
                self._json(200, {"ok": True, "message": "正在退出"})
                threading.Thread(target=self.app.request_stop, name="shutdown", daemon=True).start()
            else:
                self._json(404, {"ok": False, "error": f"未知接口 {path}"})
        except Exception as exc:  # noqa: BLE001 - 控制口绝不能把异常打回堆栈
            log.exception("处理 %s 时出错", path)
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    # ------------------------------------------------------------ 工具

    def _authorized(self) -> bool:
        if not self.token:
            return True
        header = self.headers.get("X-Bridge-Token", "")
        if not header:
            auth = self.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                header = auth[7:].strip()
        if hmac.compare_digest(header, self.token):
            return True
        self._json(401, {"ok": False, "error": "token 不正确"})
        return False

    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > _MAX_BODY:
            self._json(413, {"ok": False, "error": "请求体过大"})
            return None
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"ok": False, "error": "请求体必须是 UTF-8 JSON"})
            return None
        if not isinstance(data, dict):
            self._json(400, {"ok": False, "error": "请求体必须是 JSON 对象"})
            return None
        return data

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        log.debug("%s - %s", self.address_string(), fmt % args)


class ControlServer:
    def __init__(self, cfg: ControlConfig, app: "BridgeApp") -> None:
        self.cfg = cfg
        self.app = app
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.cfg.host in ("localhost", "::1") else self.cfg.host
        return f"http://{host}:{self.cfg.port}"

    def start(self) -> None:
        handler = type(
            "BridgeControlHandler",
            (_Handler,),
            {"app": self.app, "token": self.cfg.token},
        )
        server = ThreadingHTTPServer((self.cfg.host, self.cfg.port), handler)
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name="control-server", daemon=True)
        self._thread.start()
        log.info("控制接口已启动：%s", self.url)

    def stop(self) -> None:
        server = self._server
        if server is None:
            return
        server.shutdown()
        server.server_close()
        self._server = None
        log.info("控制接口已停止")
