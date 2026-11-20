# -*- coding: utf-8 -*-
"""端到端测试：假 API + Mock 适配器，验证私聊/群聊/@/唤醒词/防自环/主动聊天/控制口。"""

from __future__ import annotations

import json
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP_ROOT = ROOT / "_test_tmp"
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(_TMP_ROOT)


def _temp_dir() -> Path:
    path = _TMP_ROOT / f"case_{uuid.uuid4().hex[:10]}"
    path.mkdir(parents=True)
    return path

from bridge.app import BridgeApp  # noqa: E402
from bridge.config import Config  # noqa: E402
from bridge.adapters.mock import MockAdapter  # noqa: E402

SELF = "wxid_self"
ROOM = "123456@chatroom"
FRIEND = "wxid_zhangsan"


class _APIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        server = self.server
        server.requests.append((self.path, body))  # type: ignore[attr-defined]

        if getattr(server, "fail", False):
            self._send(500, {"error": "boom"})
            return

        if self.path == "/chat":
            text = body.get("message", "")
            reply = getattr(server, "reply_text", None) or f"回复：{text}"
            self._send(200, {"reply": reply, "emotion": getattr(server, "emotion", {})})
        elif self.path == "/proactive":
            reply = getattr(server, "proactive_reply", None)
            self._send(200, {"reply": reply})
        else:
            self._send(404, {"error": "not found"})

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/status":
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})

    def _send(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        pass


class FakeAPI:
    def __init__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _APIHandler)
        self.server.daemon_threads = True
        self.server.requests = []  # type: ignore[attr-defined]
        self.server.proactive_reply = "早上好呀，今天也要开心"  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def requests(self) -> list[tuple[str, dict]]:
        return self.server.requests  # type: ignore[attr-defined]

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_until(predicate, timeout: float = 6.0, interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class BridgeE2ETest(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeAPI()
        self.cfg = Config()
        self.cfg.api.base_url = self.api.url
        self.cfg.api.timeout = 10
        self.cfg.chat.adapter = "mock"
        self.cfg.chat.min_reply_interval = 0.0
        self.cfg.chat.send_gap = 0.0
        self.cfg.proactive.enabled = True
        self.cfg.proactive.interval_minutes = 60
        self.cfg.proactive.min_gap_minutes = 0
        self.cfg.control.enabled = False
        self.cfg.stickers.enabled = False
        self.cfg.asr.provider = "none"
        self.cfg.vision.provider = "none"
        self.adapter = MockAdapter(self.cfg, SELF)
        self.app = BridgeApp(self.cfg, adapter=self.adapter)
        self.app.start()
        self.addCleanup(self._teardown)

    def _teardown(self) -> None:
        self.app.stop()
        self.api.stop()

    # ------------------------------------------------------------------ 用例

    def test_private_chat(self) -> None:
        self.adapter.inject(self.adapter.make_message("在吗", from_wxid=FRIEND, msg_id="p1"))
        self.assertTrue(wait_until(lambda: len(self.adapter.sent) == 1))
        sent = self.adapter.sent[0]
        self.assertEqual(sent["kind"], "text")
        self.assertEqual(sent["to"], FRIEND)
        self.assertEqual(sent["text"], "回复：在吗")

        body = self.api.requests[0][1]
        self.assertEqual(body["message"], "在吗")
        self.assertEqual(body["user_id"], FRIEND)

    def test_group_at(self) -> None:
        msg = self.adapter.make_message(
            "@小鱼 在吗", room_wxid=ROOM, at=True, msg_id="g1"
        )
        self.adapter.inject(msg)
        self.assertTrue(wait_until(lambda: len(self.adapter.sent) == 1))
        sent = self.adapter.sent[0]
        self.assertEqual(sent["kind"], "room_at")
        self.assertEqual(sent["to"], ROOM)
        self.assertEqual(sent["at"], [FRIEND])
        self.assertEqual(sent["text"], "回复：在吗")

    def test_group_wake_word(self) -> None:
        self.adapter.inject(
            self.adapter.make_message("她 晚安", room_wxid=ROOM, msg_id="g2")
        )
        self.assertTrue(wait_until(lambda: len(self.adapter.sent) == 1))
        self.assertEqual(self.adapter.sent[0]["text"], "回复：晚安")
        self.assertEqual(self.adapter.sent[0]["at"], [FRIEND])

    def test_group_without_trigger_silent(self) -> None:
        self.adapter.inject(self.adapter.make_message("随便聊聊", room_wxid=ROOM, msg_id="g3"))
        time.sleep(0.6)
        self.assertEqual(self.adapter.sent, [])
        self.assertEqual(self.api.requests, [])

    def test_self_message_no_reply(self) -> None:
        self.adapter.inject(
            self.adapter.make_message("我自己说的", from_wxid=SELF, is_self=True, msg_id="s1")
        )
        time.sleep(0.6)
        self.assertEqual(self.adapter.sent, [])

    def test_echo_guard(self) -> None:
        self.adapter.inject(self.adapter.make_message("你好", from_wxid=FRIEND, msg_id="e1"))
        self.assertTrue(wait_until(lambda: len(self.adapter.sent) == 1))
        # 把刚发出去的内容当成新消息再投递一次
        self.adapter.inject(
            self.adapter.make_message("回复：你好", from_wxid=FRIEND, msg_id="e2")
        )
        time.sleep(0.6)
        self.assertEqual(len(self.adapter.sent), 1)

    def test_duplicate_delivery(self) -> None:
        msg = self.adapter.make_message("重复消息", from_wxid=FRIEND, msg_id="d1")
        self.adapter.inject(msg)
        self.adapter.inject(msg)
        self.assertTrue(wait_until(lambda: len(self.adapter.sent) == 1))
        time.sleep(0.4)
        self.assertEqual(len(self.adapter.sent), 1)

    def test_proactive(self) -> None:
        result = self.app.trigger_proactive()
        self.assertTrue(result["ok"])
        self.assertTrue(result["sent"])
        self.assertTrue(wait_until(lambda: len(self.adapter.sent) == 1))
        sent = self.adapter.sent[0]
        self.assertEqual(sent["to"], "filehelper")
        self.assertIn("早上好呀", sent["text"])

    def test_proactive_empty(self) -> None:
        self.api.server.proactive_reply = None  # type: ignore[attr-defined]
        result = self.app.trigger_proactive()
        self.assertFalse(result["sent"])
        self.assertEqual(self.adapter.sent, [])

    def test_api_failure_is_isolated(self) -> None:
        self.api.server.fail = True  # type: ignore[attr-defined]
        self.adapter.inject(self.adapter.make_message("会失败", from_wxid=FRIEND, msg_id="f1"))
        time.sleep(1.0)
        self.assertEqual(self.adapter.sent, [])
        self.assertGreaterEqual(self.app.metrics.snapshot().get("api_error", 0), 1)

    def test_status(self) -> None:
        status = self.app.status()
        self.assertTrue(status["ok"])
        self.assertEqual(status["adapter"]["name"], "mock")
        self.assertEqual(status["adapter"]["self_wxid"], SELF)
        self.assertIn("queue", status)

    def test_image_message_placeholder(self) -> None:
        png = _TMP_ROOT / "wxbridge_test_image.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        self.addCleanup(lambda: png.unlink(missing_ok=True))

        self.adapter.inject(
            self.adapter.make_message(
                "", media_kind="image", media_path=str(png), msg_id="img1"
            )
        )
        self.assertTrue(wait_until(lambda: len(self.adapter.sent) == 1))
        body = self.api.requests[0][1]
        self.assertIn("图片", body["message"])
        self.assertIn("图片", self.adapter.sent[0]["text"])


class StickerBridgeTest(unittest.TestCase):
    """表情包：按情绪自动发 + /表情 指令。"""

    def setUp(self) -> None:
        self.tmp = _temp_dir()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.api = FakeAPI()
        self.api.server.emotion = {"joy": 82, "hurt": 8, "impulse": 30}  # type: ignore[attr-defined]

        self.cfg = Config()
        self.cfg.api.base_url = self.api.url
        self.cfg.api.timeout = 10
        self.cfg.chat.adapter = "mock"
        self.cfg.chat.min_reply_interval = 0
        self.cfg.chat.send_gap = 0
        self.cfg.chat.owner_wxids = (FRIEND,)
        self.cfg.proactive.interval_minutes = 60
        self.cfg.control.enabled = False
        self.cfg.asr.provider = "none"
        self.cfg.vision.provider = "none"
        self.cfg.stickers.enabled = True
        self.cfg.stickers.dir = str(self.tmp / "stickers")
        self.cfg.stickers.chance = 1.0

        from bridge.stickers import StickerStore  # noqa: PLC0415

        seed = StickerStore(self.tmp / "stickers")
        seed.load()
        self.happy = seed.add_bytes(b"GIF89a" + b"\x00" * 64, "", ["开心"])
        self.assertIsNotNone(self.happy)

        self.adapter = MockAdapter(self.cfg, SELF)
        self.app = BridgeApp(self.cfg, adapter=self.adapter)
        self.app.start()
        self.addCleanup(self._teardown)

    def _teardown(self) -> None:
        self.app.stop()
        self.api.stop()

    def test_sticker_follows_mood(self) -> None:
        self.adapter.inject(self.adapter.make_message("你好", msg_id="st1"))
        self.assertTrue(wait_until(lambda: len(self.adapter.sent) >= 2))
        self.assertEqual(self.adapter.sent[0]["kind"], "text")
        self.assertEqual(self.adapter.sent[1]["kind"], "gif")
        self.assertEqual(self.adapter.sent[1]["to"], FRIEND)

    def test_explicit_sticker_tag(self) -> None:
        self.api.server.reply_text = "晚安[表情:开心]"  # type: ignore[attr-defined]
        self.adapter.inject(self.adapter.make_message("睡了吗", msg_id="st2"))
        self.assertTrue(wait_until(lambda: len(self.adapter.sent) >= 2))
        self.assertEqual(self.adapter.sent[0]["text"], "晚安")
        self.assertEqual(self.adapter.sent[1]["kind"], "gif")

    def test_sticker_command_flow(self) -> None:
        png = self.tmp / "cat.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)

        # 1) 先发一张图（会进 _last_media，同时触发一次按情绪的回复）
        self.adapter.inject(
            self.adapter.make_message("", media_kind="image", media_path=str(png), msg_id="cmd-img")
        )
        self.assertTrue(wait_until(lambda: any(s["kind"] == "text" for s in self.adapter.sent)))

        # 2) 存起来
        self.adapter.inject(self.adapter.make_message("/表情 存 猫猫", msg_id="cmd-save"))
        self.assertTrue(
            wait_until(lambda: any("存好了" in s["text"] for s in self.adapter.sent))
        )
        self.assertEqual(self.app.sticker_store.count(), 2)

        # 3) 发一张
        gifs_before = sum(1 for s in self.adapter.sent if s["kind"] == "gif")
        self.adapter.inject(self.adapter.make_message("/表情 发 开心", msg_id="cmd-send"))
        self.assertTrue(
            wait_until(
                lambda: sum(1 for s in self.adapter.sent if s["kind"] == "gif") > gifs_before
            )
        )


class ControlAPITest(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeAPI()
        self.cfg = Config()
        self.cfg.api.base_url = self.api.url
        self.cfg.api.timeout = 10
        self.cfg.chat.adapter = "mock"
        self.cfg.chat.min_reply_interval = 0
        self.cfg.chat.send_gap = 0
        self.cfg.proactive.interval_minutes = 60
        self.cfg.control.enabled = True
        self.cfg.control.port = free_port()
        self.cfg.stickers.enabled = False
        self.cfg.asr.provider = "none"
        self.cfg.vision.provider = "none"
        self.adapter = MockAdapter(self.cfg, SELF)
        self.app = BridgeApp(self.cfg, adapter=self.adapter)
        self.app.start()
        self.addCleanup(self._teardown)

    def _teardown(self) -> None:
        self.app.stop()
        self.api.stop()

    def _call(self, method: str, path: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.cfg.control.port}{path}", data=data, method=method
        )
        if data:
            request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_status(self) -> None:
        payload = self._call("GET", "/status")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["service"], "wxbridge")

    def test_send(self) -> None:
        payload = self._call("POST", "/send", {"target": "文件传输助手", "text": "手动消息"})
        self.assertTrue(payload["ok"])
        self.assertEqual(self.adapter.sent[0]["to"], "filehelper")
        self.assertEqual(self.adapter.sent[0]["text"], "手动消息")

    def test_pause(self) -> None:
        self._call("POST", "/pause", {"paused": True})
        self.adapter.inject(self.adapter.make_message("暂停时发的", msg_id="pp1"))
        time.sleep(0.5)
        self.assertEqual(self.adapter.sent, [])
        self._call("POST", "/pause", {"paused": False})
        self.adapter.inject(self.adapter.make_message("恢复后发的", msg_id="pp2"))
        self.assertTrue(wait_until(lambda: len(self.adapter.sent) == 1))

    def test_proactive(self) -> None:
        payload = self._call("POST", "/proactive", {})
        self.assertTrue(payload["sent"])
        self.assertTrue(wait_until(lambda: len(self.adapter.sent) == 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
