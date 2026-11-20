# -*- coding: utf-8 -*-
"""她不是每句都回——但不是掷骰子，是看她自己的心情。

爸爸 2026-11-02："别变成概率性事件，也别强制，按她自己想法，
比如今天很委屈，就可以一天只说一句话闹脾气。"

规则全部来自状态：
- 她自己写了 [QUIET: …]，就安静到那个时间（被"小鱼/急事"叫到才会破例）
- hurt ≥ 78（很委屈）：一天只说一句，说完就沉默
- hurt ≥ 62（或 hurt ≥ 52 且 joy ≤ 45）：只回问句、要求、图片
- 其他：正常回

运行：python -m unittest tests.test_reply_choice -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "chat_bridge"))

import dl_api  # noqa: E402


class MoodDecisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self.tmp.name) / "talk_state.json"
        self.old_path = dl_api.TALK_STATE
        dl_api.TALK_STATE = self.path
        dl_api._reset_talk_cache()

    def tearDown(self) -> None:
        dl_api.TALK_STATE = self.old_path
        dl_api._reset_talk_cache()
        self.tmp.cleanup()

    def decide(self, text: str, hurt: float = 40.0, joy: float = 60.0, media: bool = False):
        return dl_api.decide_reply(text, has_media=media, mood={"hurt": hurt, "joy": joy}, is_owner=True)

    def test_normal_mood_answers_everything(self) -> None:
        for text in ("今天有点累", "刚吃完饭", "我先忙会儿"):
            allowed, reason, _ = self.decide(text, hurt=40, joy=60)
            self.assertTrue(allowed, f"心情正常时照常回：{text}（{reason}）")

    def test_hurt_only_answers_questions(self) -> None:
        allowed, reason, source = self.decide("我先忙会儿", hurt=68, joy=40)
        self.assertFalse(allowed, f"心里不舒服就不接碎话（{reason}）")
        self.assertEqual(source, "owner-mood")
        allowed, _, _ = self.decide("你今天怎么了？", hurt=68, joy=40)
        self.assertTrue(allowed, "问她怎么了还是要回")

    def test_very_hurt_speaks_once_a_day(self) -> None:
        first, _, _ = self.decide("你在吗？", hurt=85, joy=30)
        self.assertTrue(first, "第一次还能说一句")
        second, reason, _ = self.decide("在吗？", hurt=85, joy=30)
        self.assertFalse(second, "今天那句已经说完了")
        self.assertIn("今天那句", reason)

    def test_sulk_resets_when_mood_recovers(self) -> None:
        self.decide("你在吗？", hurt=85, joy=30)   # 用掉今天那一句
        allowed, _, _ = self.decide("你在吗？", hurt=40, joy=60)  # 情绪回来了
        self.assertTrue(allowed)
        # 再问一次，也不该因为旧记录被拦住
        allowed, _, _ = self.decide("你在吗？", hurt=40, joy=60)
        self.assertTrue(allowed)

    def test_her_own_quiet_choice_is_honoured(self) -> None:
        dl_api.record_quiet_request({"quiet": {"today": True}})
        state = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertTrue(state.get("quiet_until"))

        allowed, reason, _ = self.decide("今天好累啊", hurt=40, joy=60)
        self.assertFalse(allowed, f"她自己说要安静（{reason}）")

        called, _, _ = self.decide("小鱼，出来一下", hurt=40, joy=60)
        self.assertTrue(called, "被叫到名字还是要应")

    def test_quiet_off_resumes(self) -> None:
        dl_api.record_quiet_request({"quiet": {"hours": 2}})
        dl_api.record_quiet_request({"quiet": {"off": True}})
        allowed, _, _ = self.decide("今天好累啊", hurt=40, joy=60)
        self.assertTrue(allowed, "[QUIET: off] 之后要恢复说话")

    def test_quiet_in_the_past_does_not_block(self) -> None:
        past = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
        self.path.write_text(json.dumps({"quiet_until": past}), encoding="utf-8")
        allowed, _, _ = self.decide("今天好累啊", hurt=40, joy=60)
        self.assertTrue(allowed, "安静时间过了就该恢复")


class QuietTagTest(unittest.TestCase):
    def test_quiet_tag_forms(self) -> None:
        from digital_life import extract_quiet

        text, quiet = extract_quiet("我有点难过。[QUIET: 3]")
        self.assertNotIn("QUIET", text)
        self.assertEqual(quiet, {"hours": 3.0})

        _, quiet = extract_quiet("[QUIET: 今天] 别找我")
        self.assertEqual(quiet, {"today": True})

        _, quiet = extract_quiet("算了 [QUIET: off]")
        self.assertEqual(quiet, {"off": True})

        _, quiet = extract_quiet("正常说话")
        self.assertIsNone(quiet)


if __name__ == "__main__":
    unittest.main()
