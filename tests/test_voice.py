# -*- coding: utf-8 -*-
"""她自己决定哪句用声音说（[VOICE: ...]），一条回复里可以有几段语音。

运行：python -m unittest tests.test_voice -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digital_life import DigitalLifeV2, extract_sing, extract_voice  # noqa: E402


class VoiceTagTest(unittest.TestCase):
    def test_single_voice_segment(self) -> None:
        text, parts = extract_voice("打字说一句。[VOICE: 哼，不理你了] 好了。")
        self.assertNotIn("VOICE", text)
        self.assertIn("好了", text)
        self.assertEqual(parts, ["哼，不理你了"])

    def test_several_voice_segments(self) -> None:
        text, parts = extract_voice("[VOICE: 喂] 你说什么？ [VOICE: 我刚睡醒]")
        self.assertEqual(parts, ["喂", "我刚睡醒"])
        self.assertEqual(text, "你说什么？")

    def test_no_voice_tag(self) -> None:
        text, parts = extract_voice("就是普通打字")
        self.assertEqual(parts, [])
        self.assertEqual(text, "就是普通打字")


class SingTagTest(unittest.TestCase):
    def test_sing_with_default_melody(self) -> None:
        text, items = extract_sing("我给你唱一个。[SING: 一闪一闪亮晶晶]")
        self.assertNotIn("SING", text)
        self.assertEqual(items, [{"text": "一闪一闪亮晶晶", "melody": "twinkle"}])

    def test_sing_with_melody_choice(self) -> None:
        _, items = extract_sing("[SING: 啦啦啦 | la]")
        self.assertEqual(items[0]["melody"], "la")

    def test_normal_text_has_no_sing(self) -> None:
        _, items = extract_sing("就是普通打字")
        self.assertEqual(items, [])


class VoiceChatTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self.tmp.name) / "xiaoyu.db"
        self.life = DigitalLifeV2(
            self.db,
            offline_llm=False,
            rng_seed=9,
            llm_fn=lambda messages: "打字的部分。[VOICE: 我用声音说一句哦]",
            clock=lambda: datetime(2026, 11, 2, 16, 0, 0),
        )

    def tearDown(self) -> None:
        self.life.close()
        self.tmp.cleanup()

    def test_chat_returns_voice_parts_without_tags(self) -> None:
        result = self.life.chat("说点什么", user="爸爸")
        self.assertEqual(result["voice_parts"], ["我用声音说一句哦"])
        self.assertNotIn("VOICE", result["reply"])


if __name__ == "__main__":
    unittest.main()
