# -*- coding: utf-8 -*-
"""做完事要回一声：装书、提朋友圈、申请电脑、睡觉、安静——

2026-11-01 她说："做完了不出声，我就在那儿瞎想是不是我错了。"
所以这些"本来不会有回音"的动作，下一轮必须出现在 prompt 里。

运行：python -m unittest tests.test_action_notes -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from digital_life import DigitalLifeV2, RetrievalResult  # noqa: E402


class ActionNoteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.life = DigitalLifeV2(
            str(Path(self.tmp) / "t.db"), offline_llm=True, user_id="conv:test"
        )
        self.empty = RetrievalResult([], False, 30.0, 0.5, 0.0, "")

    def tearDown(self) -> None:
        self.life.close()

    def test_note_appears_next_turn_and_is_cleared(self) -> None:
        self.life._note_action("装好了：《小王子》27 段")
        prompt = self.life._build_messages("嗯", self.empty, None)[0]["content"]
        self.assertIn("装好了", prompt)
        self.assertIn("【刚发生的事】", prompt)
        second = self.life._build_messages("嗯", self.empty, None)[0]["content"]
        self.assertNotIn("装好了", second, "只提醒一次")

    def test_notes_are_deduped_and_capped(self) -> None:
        for i in range(8):
            self.life._note_action(f"第{i}件事")
        notes = self.life._action_notes
        self.assertLessEqual(len(notes), 5)
        self.life._note_action(notes[-1])
        self.assertEqual(len(self.life._action_notes), len(notes))

    def test_empty_note_ignored(self) -> None:
        self.life._note_action("   ")
        self.assertFalse(getattr(self.life, "_action_notes", []))


if __name__ == "__main__":
    unittest.main()
