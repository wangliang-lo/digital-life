# -*- coding: utf-8 -*-
"""网页端三条通道（FATHER / IM / TIKTOK）共用同一个脑子。

IM / TIKTOK 登录后，输入用"名字: 内容"的格式；说话人从前缀里取，
渠道自动标成IM/抖音。这样三个入口是一份记忆，只是场合不同。

运行：python -m unittest tests.test_surfaces -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "chat_bridge"))

import web_ui  # noqa: E402


class SurfaceKeysTest(unittest.TestCase):
    def test_chat_and_tiktok_exist(self) -> None:
        for key in ("IM", "TIKTOK"):
            self.assertIn(key, web_ui.AUTH_KEYS)
            self.assertEqual(web_ui.AUTH_KEYS[key]["api_user_id"], web_ui.MAIN_USER_ID)

    def test_aliases(self) -> None:
        self.assertEqual(web_ui.AUTH_ALIASES.get("DOUYIN"), "TIKTOK")
        self.assertEqual(web_ui.AUTH_ALIASES.get("WX"), "IM")

    def test_channel_mapping(self) -> None:
        self.assertEqual(web_ui.surface_channel("chat"), "chat_private")
        self.assertEqual(web_ui.surface_channel("douyin"), "douyin")
        self.assertEqual(web_ui.surface_channel(""), "")


class PrefixParseTest(unittest.TestCase):
    def test_sender_prefix(self) -> None:
        self.assertEqual(
            web_ui.parse_surface_input("爸爸: 你先睡吧"), ("爸爸", "你先睡吧")
        )

    def test_chinese_colon(self) -> None:
        self.assertEqual(
            web_ui.parse_surface_input("熟人D：在吗"), ("熟人D", "在吗")
        )

    def test_no_prefix_uses_default(self) -> None:
        self.assertEqual(
            web_ui.parse_surface_input("我先睡了", "爸爸"), ("爸爸", "我先睡了")
        )

    def test_url_is_not_a_prefix(self) -> None:
        text = "http://example.com 你看这个"
        sender, body = web_ui.parse_surface_input(text, "爸爸")
        self.assertEqual(sender, "爸爸")
        self.assertEqual(body, text)

    def test_multiline_keeps_body(self) -> None:
        sender, body = web_ui.parse_surface_input("爸爸: 第一行\n第二行")
        self.assertEqual(sender, "爸爸")
        self.assertEqual(body, "第一行\n第二行")


if __name__ == "__main__":
    unittest.main()
