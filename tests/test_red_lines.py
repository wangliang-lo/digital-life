"""四条红线的回归测试（2026-11-16）。

她说得有灵魂之前，先保证这四条不破：
  1. 访客绝不泄露主人私事
  2. 群聊不认错人
  3. 睡着后不回普通消息
  4. 动作标签不漏进气泡

跑法：python -m pytest tests/test_red_lines.py -q
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "chat_bridge"
for path in (str(ROOT), str(BRIDGE)):
    if path not in sys.path:
        sys.path.insert(0, path)

import dl_api  # noqa: E402
import memory as memory_mod  # noqa: E402

MAIN = "conv:9e52b4549f424e730d8871644385bcce63768cc74b8f8ec7ffa895f62ca73eeb"


class GuestPrivacyTest(unittest.TestCase):
    """红线 1：访客翻不到主人的私事。"""

    def setUp(self) -> None:
        # 2026-11-17：公开仓库不带 data/，没有真库就跳过（别让 clone 的人看到红）
        source_db = BRIDGE / "data" / "xiaoyu.db"
        if not source_db.exists():
            self.skipTest("没有她的记忆库（data/xiaoyu.db），跳过隐私红线用例")
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "t.db"
        shutil.copy2(source_db, self.db)
        self.lib = memory_mod.LibraryMemory(self.db, user_id=MAIN)

    def tearDown(self) -> None:
        self.lib.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_owner_memories_are_hidden_for_guest(self) -> None:
        self.lib.hide_owner_users = {"爸爸", "father", "爸爸", "主人", "你", "对方"}
        self.lib.hide_owner_names = set(self.lib.hide_owner_users)

        # 翻旧账这条路径也必须守边界
        found = self.lib.search_by_keywords("你还记得考试吗", limit=8)
        for row in found:
            blob = str(row.get("summary") or "") + str(row.get("content") or "")
            self.assertNotIn("father", blob.lower())
            self.assertNotIn("爸爸", blob)
            self.assertNotIn(str(row.get("folder_user") or ""), self.lib.hide_owner_users)

    def test_guest_retrieve_filters_owner_rows(self) -> None:
        self.lib.hide_owner_users = {"爸爸", "father", "爸爸"}
        self.lib.hide_owner_names = set(self.lib.hide_owner_users)
        result = self.lib.retrieve(query="考试", now=datetime.now(), impulse=50)
        for hit in result.hits:
            blob = str(getattr(hit, "summary", "") or "")
            self.assertNotIn("爸爸", blob)


class GroupIdentityTest(unittest.TestCase):
    """红线 2：群聊里不把陌生人认成爸爸。"""

    def test_unknown_group_speaker_is_not_owner(self) -> None:
        text = dl_api.persona_with_time(
            "基础人格", is_owner=False, channel="chat_group",
            speaker="路人甲", group="测试群",
        )
        self.assertIn("路人甲", text)
        self.assertNotIn("就是爸爸本人", text)

    def test_guest_channel_speaker_not_owner(self) -> None:
        text = dl_api.persona_with_time(
            "基础人格", is_owner=False, channel="web_guest", speaker="熟人A",
        )
        self.assertIn("熟人A", text)
        self.assertNotIn("就是爸爸本人", text)


class SleepGateTest(unittest.TestCase):
    """红线 3：她睡着时，普通消息不回。"""

    def test_sleeping_returns_ignored(self) -> None:
        """直接打 dl_api 的 /chat 处理函数：睡着时必须 ignored + asleep。"""
        sent = {}

        def fake_send(status, payload):
            sent["status"] = status
            sent["payload"] = payload

        handler_type = dl_api._make_handler(mock.Mock(), mock.Mock())
        handler = object.__new__(handler_type)
        handler._send = fake_send
        handler._send_openai_stream = lambda *a, **k: None

        with mock.patch.object(dl_api, "is_sleeping", return_value=True), \
                mock.patch.object(dl_api, "queue_pending", lambda *a, **k: None):
            handler._handle_chat({"message": "在吗", "user_id": MAIN, "sender": "测试员"})

        payload = sent.get("payload") or {}
        self.assertEqual(sent.get("status"), 200)
        self.assertTrue(payload.get("ignored"))
        self.assertTrue(payload.get("asleep"))
        self.assertEqual(payload.get("reply"), "")


class TagLeakTest(unittest.TestCase):
    """红线 4：动作标签、写残的标签都不许漏进气泡。"""

    def test_partial_sticker_tag_is_removed(self) -> None:
        out = dl_api.clean_partial_tags("嗯……转身比说话还多。\n[STICKER: 难过")
        self.assertNotIn("[STICKER", out)
        self.assertIn("转身比说话还多", out)

    def test_complete_tag_stays_for_handlers(self) -> None:
        # 完整标签要留给后面的动作处理器，不能被这里吃掉
        out = dl_api.clean_partial_tags("我下 [GOMOKU: 5,5]")
        self.assertIn("[GOMOKU: 5,5]", out)

    def test_stage_direction_removed(self) -> None:
        out = dl_api.strip_stage_directions("（想了想）你好")
        self.assertEqual(out, "你好")


if __name__ == "__main__":
    unittest.main()
