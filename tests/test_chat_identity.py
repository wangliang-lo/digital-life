# -*- coding: utf-8 -*-
"""IM 里的"这是谁"和"别刷屏"。

IM所有会话共用她一份脑（同一个 user_id），所以：
- 认人只能靠说话人的名字（contact_profiles 里的 name）
- 同一个人反复发同一句 / 一分钟刷四条，不用每条都回

运行：python -m unittest tests.test_chat_identity -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "chat_bridge"))

import dl_api  # noqa: E402


class RepeatGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        dl_api._REPEAT_STATE.clear()

    def test_same_line_twice_is_skipped(self) -> None:
        first = dl_api.repeat_reason("熟人D", "在吗在吗", now=1000.0)
        second = dl_api.repeat_reason("熟人D", "在吗在吗", now=1005.0)
        self.assertIsNone(first)
        self.assertIsNotNone(second, "同一句话第二遍不该再回")
        self.assertIn("已经回过", second or "")

    def test_questions_still_answered_when_flooding(self) -> None:
        for i in range(3):
            dl_api.repeat_reason("熟人D", f"随便说一句{i}", now=2000.0 + i)
        allowed = dl_api.repeat_reason("熟人D", "你到底在干嘛？", now=2003.0)
        self.assertIsNone(allowed, "问句还是要回")

    def test_flooding_without_question_is_skipped(self) -> None:
        for i in range(3):
            dl_api.repeat_reason("熟人D", f"哈哈哈哈{i}", now=3000.0 + i)
        skipped = dl_api.repeat_reason("熟人D", "啦啦啦", now=3003.0)
        self.assertIsNotNone(skipped, "一分钟刷四条，不是问句就不接")

    def test_different_people_are_independent(self) -> None:
        dl_api.repeat_reason("熟人D", "哈哈哈", now=4000.0)
        other = dl_api.repeat_reason("破忒头", "哈哈哈", now=4001.0)
        self.assertIsNone(other, "别人说的话不共用刷屏计数")


class ContactLookupTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # 2026-11-17：公开仓库不带 data/，没有联系人数据就跳过
        from pathlib import Path as _P
        _bridge = _P(__file__).resolve().parents[1] / "chat_bridge"
        if not (_bridge / "data" / "aliases.json").exists():
            import unittest as _u
            raise _u.SkipTest("没有 contacts/aliases 数据，跳过联系人用例")
    def test_known_name_maps_to_a_contact_card(self) -> None:
        cid = dl_api.contact_id_for_sender("熟人D")
        self.assertTrue(cid.startswith("conv:"), f"熟人D没对应到联系人卡片：{cid!r}")

    def test_unknown_name_returns_empty(self) -> None:
        self.assertEqual(dl_api.contact_id_for_sender("根本不认识的路人"), "")

    def test_owner_names_include_father(self) -> None:
        names = dl_api.owner_sender_names()
        self.assertIn("爸爸", names)


if __name__ == "__main__":
    unittest.main()
