# -*- coding: utf-8 -*-
"""覆盖事故 3：网页表情包列表接口必须返回可选择的贴纸项目。"""
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


class StickersApiTest(unittest.TestCase):
    def test_get_api_stickers_returns_selection_items(self) -> None:
        stickers = [
            SimpleNamespace(id="happy", tags=["开心", "笑脸"], note="开心时使用"),
            SimpleNamespace(id="sorry", tags=["道歉"], note=""),
        ]
        app = SimpleNamespace(
            stickers=SimpleNamespace(list=mock.Mock(return_value=stickers))
        )
        handler_type = web_ui._make_handler(app)
        handler = object.__new__(handler_type)
        handler.path = "/api/stickers"
        handler._session = mock.Mock(return_value={"user_id": "tester"})
        handler._json = mock.Mock()

        handler.do_GET()

        handler._json.assert_called_once()
        status, payload = handler._json.call_args.args
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertIsInstance(payload["items"], list)
        self.assertEqual(len(payload["items"]), len(stickers))
        for item, source in zip(payload["items"], stickers, strict=True):
            with self.subTest(sticker_id=source.id):
                self.assertTrue({"id", "tags", "note", "url"}.issubset(item))
                self.assertEqual(item["id"], source.id)
                self.assertIsInstance(item["tags"], list)
                self.assertEqual(item["url"], f"/api/sticker?id={source.id}")


if __name__ == "__main__":
    unittest.main()
