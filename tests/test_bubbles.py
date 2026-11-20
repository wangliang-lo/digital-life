# -*- coding: utf-8 -*-
"""分气泡时不要把英文单词/型号切断（DeepSeek-V4.1-Flash 不能变成 Fla / sh）。

运行：python -m unittest tests.test_bubbles -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "chat_bridge"))

from dl_api import split_bubbles  # noqa: E402


class SplitBubblesTest(unittest.TestCase):
    def test_model_name_is_not_cut(self) -> None:
        text = "查到了，DeepSeek-V4.1-Flash 刚发布，原生视觉理解，还更快更省，平台上还有 V4-Pro 百万上下文"
        parts = split_bubbles(text, limit=30)
        joined = "".join(parts)
        self.assertIn("DeepSeek-V4.1-Flash", joined)
        for part in parts:
            self.assertFalse(part.endswith("Fla"), part)
            self.assertNotEqual(part.strip(), "sh", part)

    def test_english_splits_on_spaces(self) -> None:
        text = "Hello world this is a fairly long english sentence that needs splitting somewhere sensible"
        parts = split_bubbles(text, limit=40)
        words = set(text.split())
        for part in parts:
            last = part.split()[-1]
            self.assertIn(last, words, f"切在词中间了：{part!r}")
        self.assertEqual(" ".join(parts).split(), text.split())

    def test_chinese_still_splits_at_punctuation(self) -> None:
        parts = split_bubbles("第一句。" * 12, limit=20)
        self.assertTrue(all(part.endswith("。") for part in parts[:-1]) or all(len(p) <= 20 for p in parts))

    def test_short_text_is_one_bubble(self) -> None:
        self.assertEqual(split_bubbles("在吗", limit=120), ["在吗"])


if __name__ == "__main__":
    unittest.main()
