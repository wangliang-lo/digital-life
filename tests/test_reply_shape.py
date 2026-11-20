# -*- coding: utf-8 -*-
"""一次只用一种表达方式：要么打字，要么发语音。

运行：python -m unittest tests.test_reply_shape -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "chat_bridge"))

from dl_api import shape_reply  # noqa: E402


class ShapeReplyTest(unittest.TestCase):
    def test_web_keeps_text_and_voice(self) -> None:
        # 2026-11-16 爸爸："要她想发语音就发，不强制，也不限制。"
        # 所以不再自动把文字清掉。
        bubbles, voices, songs = shape_reply(["在的"], ["我在呢"], [], "", roll=0.1)
        self.assertEqual(bubbles, ["在的"])
        self.assertEqual(voices, ["我在呢"])

    def test_web_keeps_both_sometimes(self) -> None:
        bubbles, voices, _ = shape_reply(["在的"], ["我在呢"], [], "", roll=0.95)
        self.assertEqual(bubbles, ["在的"])
        self.assertEqual(voices, ["我在呢"])

    def test_chat_folds_voice_to_text(self) -> None:
        bubbles, voices, songs = shape_reply(
            ["在的"], ["我在呢"], [{"text": "啦啦啦", "melody": "la"}], "chat_private", roll=0.1
        )
        self.assertEqual(voices, [])
        self.assertEqual(songs, [])
        self.assertIn("在的", bubbles)
        self.assertIn("我在呢", bubbles)
        self.assertIn("啦啦啦", bubbles)

    def test_text_only_unchanged(self) -> None:
        bubbles, voices, songs = shape_reply(["在"], [], [], "", roll=0.1)
        self.assertEqual(bubbles, ["在"])
        self.assertEqual(voices, [])
        self.assertEqual(songs, [])


if __name__ == "__main__":
    unittest.main()
