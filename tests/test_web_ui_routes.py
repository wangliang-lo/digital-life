# -*- coding: utf-8 -*-
"""web_ui 路由冒烟测试：健康检查、未登录访问、画稿、语音不可用。

手法照抄 test_media_web_ui_stickers.py：用 _make_handler 造 handler，
object.__new__ 跳过真正的网络初始化，把 _session / _json 换成 mock 抓响应。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "chat_bridge").exists():
    ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "chat_bridge"))

import web_ui  # noqa: E402


def _make_handler(path: str, session=None, app=None):
    """造一个不绑定 socket 的 handler，并把会话和 JSON 响应换成 mock。"""
    app = app if app is not None else SimpleNamespace()
    handler_type = web_ui._make_handler(app)
    handler = object.__new__(handler_type)
    handler.path = path
    handler._session = mock.Mock(return_value=session)
    handler._json = mock.Mock()
    handler.headers = {}
    handler.rfile = mock.Mock()
    handler.send_response = mock.Mock()
    handler.send_header = mock.Mock()
    handler.end_headers = mock.Mock()
    handler.wfile = mock.Mock()
    return handler


def _headers(handler) -> dict[str, str]:
    """把 send_header 的调用收成字典，方便断言 Location 之类的头。"""
    return {call.args[0]: call.args[1] for call in handler.send_header.call_args_list}


class HealthRouteTest(unittest.TestCase):
    def test_get_api_health_returns_ok(self) -> None:
        app = SimpleNamespace(health=mock.Mock(return_value={"ok": True, "asleep": False}))
        handler = _make_handler("/api/health", app=app)

        handler.do_GET()

        handler._json.assert_called_once()
        status, payload = handler._json.call_args.args
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])


class DrawnRouteTest(unittest.TestCase):
    def test_get_api_drawn_missing_file_returns_404(self) -> None:
        handler = _make_handler("/api/drawn?name=no_such_drawing_2026.png",
                                session={"api_user_id": "tester"})

        handler.do_GET()

        handler._json.assert_called_once()
        status, payload = handler._json.call_args.args
        self.assertEqual(status, 404)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["error"])

    def test_get_api_drawn_rejects_illegal_name(self) -> None:
        # 中文名过不了 _serve_drawn 的白名单正则，走的是 400 而不是 404
        handler = _make_handler("/api/drawn?name=不存在的文件.png",
                                session={"api_user_id": "tester"})

        handler.do_GET()

        status, payload = handler._json.call_args.args
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])
        self.assertIn("名字", payload["error"])


class LoginGateTest(unittest.TestCase):
    def test_protected_page_redirects_to_login(self) -> None:
        handler = _make_handler("/", session=None)

        handler.do_GET()

        handler._json.assert_not_called()
        handler.send_response.assert_called_once_with(302)
        self.assertEqual(_headers(handler).get("Location"), "/login")

    def test_chat_page_requires_login(self) -> None:
        # 2026-11-16 爸爸："登录成功了才能看到房间。"
        # /chat 也要先登录，未登录直接 302 到 /login。
        handler = _make_handler("/chat", session=None)

        handler.do_GET()

        handler._json.assert_not_called()
        handler.send_response.assert_called_once_with(302)
        locations = [c.args[1] for c in handler.send_header.call_args_list if c.args[0] == "Location"]
        self.assertEqual(locations, ["/login"])

    def test_post_api_chat_without_session_returns_401(self) -> None:
        handler = _make_handler("/api/chat", session=None)

        handler.do_POST()

        handler._json.assert_called_once()
        status, payload = handler._json.call_args.args
        self.assertEqual(status, 401)
        self.assertFalse(payload["ok"])
        handler.rfile.read.assert_not_called()  # 没登录就不该读请求体


class VoiceUnavailableTest(unittest.TestCase):
    def test_get_api_voice_returns_503_when_tts_down(self) -> None:
        handler = _make_handler("/api/voice?text=吃了吗", session={"api_user_id": "tester"})

        # 指向必然拒绝连接的本地端口，模拟 TTS 没起；退出时自动还原
        with mock.patch.object(web_ui, "TTS_URL", "http://127.0.0.1:1"):
            handler.do_GET()

        handler._json.assert_called_once()
        status, payload = handler._json.call_args.args
        self.assertEqual(status, 503)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["error"])
        self.assertIn("声音", payload["error"])


if __name__ == "__main__":
    unittest.main()
