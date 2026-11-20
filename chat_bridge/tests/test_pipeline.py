# -*- coding: utf-8 -*-
"""消息管道单元测试（不碰IM、不碰网络）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bridge.adapters.base import IncomingMessage  # noqa: E402
from bridge.config import IMConfig  # noqa: E402
from bridge.pipeline import MessagePipeline, split_message  # noqa: E402

SELF = "wxid_self"
ROOM = "123456@chatroom"


def make(content: str, **kwargs) -> IncomingMessage:
    data = {
        "msg_id": kwargs.pop("msg_id", f"m-{content}-{kwargs.get('room_wxid', '')}"),
        "from_wxid": kwargs.pop("from_wxid", "wxid_friend"),
        "content": content,
        "from_name": kwargs.pop("from_name", "张三"),
    }
    data.update(kwargs)
    return IncomingMessage(**data)


class TestPrivate(unittest.TestCase):
    def setUp(self) -> None:
        self.pipe = MessagePipeline(IMConfig(), SELF)

    def test_private_message_routed(self) -> None:
        route = self.pipe.evaluate(make("今天好累"))
        self.assertIsNotNone(route)
        assert route is not None
        self.assertEqual(route.kind, "private")
        self.assertEqual(route.target_wxid, "wxid_friend")
        self.assertEqual(route.text, "今天好累")
        self.assertEqual(route.user_id, "wxid_friend")

    def test_self_message_ignored(self) -> None:
        msg = make("我自己发的", from_wxid=SELF, is_self=True)
        self.assertIsNone(self.pipe.evaluate(msg))
        self.assertEqual(self.pipe.metrics.snapshot()["ignored_self"], 1)

    def test_self_message_ignored_even_without_flag(self) -> None:
        # 有些适配器不上报 is_self，靠 wxid 兜底
        self.assertIsNone(self.pipe.evaluate(make("我自己发的", from_wxid=SELF)))

    def test_private_wake_word_is_not_required(self) -> None:
        self.assertIsNotNone(self.pipe.evaluate(make("在吗")))

    def test_leading_at_removed_in_private(self) -> None:
        route = self.pipe.evaluate(make("@张三 在吗"))
        assert route is not None
        self.assertEqual(route.text, "在吗")

    def test_blocked(self) -> None:
        pipe = MessagePipeline(IMConfig(blocked_wxids=("wxid_bad",)), SELF)
        self.assertIsNone(pipe.evaluate(make("hi", from_wxid="wxid_bad")))

    def test_allowed_list(self) -> None:
        pipe = MessagePipeline(IMConfig(allowed_wxids=("wxid_ok",)), SELF)
        self.assertIsNone(pipe.evaluate(make("hi", from_wxid="wxid_other")))
        self.assertIsNotNone(pipe.evaluate(make("hi", from_wxid="wxid_ok")))

    def test_truncate_long_input(self) -> None:
        pipe = MessagePipeline(IMConfig(max_input_chars=10), SELF)
        route = pipe.evaluate(make("一" * 100))
        assert route is not None
        self.assertEqual(len(route.text), 10)


class TestGroup(unittest.TestCase):
    def setUp(self) -> None:
        self.pipe = MessagePipeline(IMConfig(wake_words=("她",)), SELF)

    def test_group_without_at_or_wake_ignored(self) -> None:
        msg = make("随便聊聊", room_wxid=ROOM)
        self.assertIsNone(self.pipe.evaluate(msg))

    def test_group_at_routed(self) -> None:
        msg = make("@小鱼 在吗", room_wxid=ROOM, at_wxid=[SELF])
        route = self.pipe.evaluate(msg)
        assert route is not None
        self.assertEqual(route.kind, "group")
        self.assertEqual(route.target_wxid, ROOM)
        self.assertEqual(route.text, "在吗")
        self.assertEqual(route.at_wxid, ["wxid_friend"])
        self.assertEqual(route.trigger, "at")

    def test_group_wake_word_routed(self) -> None:
        msg = make("她 今天吃什么", room_wxid=ROOM)
        route = self.pipe.evaluate(msg)
        assert route is not None
        self.assertEqual(route.text, "今天吃什么")
        self.assertEqual(route.trigger, "wake")

    def test_group_wake_word_with_punctuation(self) -> None:
        msg = make("她，晚上好", room_wxid=ROOM)
        route = self.pipe.evaluate(msg)
        assert route is not None
        self.assertEqual(route.text, "晚上好")

    def test_group_at_plus_wake(self) -> None:
        msg = make("@小鱼 她 帮我看看", room_wxid=ROOM, at_wxid=[SELF])
        route = self.pipe.evaluate(msg)
        assert route is not None
        self.assertEqual(route.text, "帮我看看")

    def test_group_room_whitelist(self) -> None:
        pipe = MessagePipeline(IMConfig(allowed_rooms=("999@chatroom",)), SELF)
        self.assertIsNone(pipe.evaluate(make("@小鱼 在吗", room_wxid=ROOM, at_wxid=[SELF])))

    def test_group_reply_without_at_when_disabled(self) -> None:
        pipe = MessagePipeline(IMConfig(group_reply_at=False), SELF)
        route = pipe.evaluate(make("她 嗨", room_wxid=ROOM))
        assert route is not None
        self.assertEqual(route.at_wxid, [])


class TestDedupeAndEcho(unittest.TestCase):
    def setUp(self) -> None:
        self.pipe = MessagePipeline(IMConfig(), SELF)

    def test_duplicate_msg_id_dropped(self) -> None:
        msg = make("你好", msg_id="dup-1")
        self.assertIsNotNone(self.pipe.evaluate(msg))
        again = make("你好", msg_id="dup-1")
        self.assertIsNone(self.pipe.evaluate(again))
        self.assertEqual(self.pipe.metrics.snapshot()["ignored_duplicate"], 1)

    def test_echo_guard(self) -> None:
        self.pipe.note_sent("wxid_friend", "刚刚回复的内容")
        msg = make("刚刚回复的内容", from_wxid="wxid_friend")
        self.assertIsNone(self.pipe.evaluate(msg))
        self.assertEqual(self.pipe.metrics.snapshot()["ignored_echo"], 1)

    def test_empty_message_ignored(self) -> None:
        self.assertIsNone(self.pipe.evaluate(make("   ")))


class TestUserStrategy(unittest.TestCase):
    def test_room_strategy(self) -> None:
        pipe = MessagePipeline(IMConfig(user_id_strategy="room"), SELF)
        route = pipe.evaluate(make("她 在吗", room_wxid=ROOM))
        assert route is not None
        self.assertEqual(route.user_id, ROOM)

    def test_fixed_strategy(self) -> None:
        pipe = MessagePipeline(IMConfig(user_id_strategy="fixed", fixed_user_id="xiaoyu"), SELF)
        route = pipe.evaluate(make("在吗"))
        assert route is not None
        self.assertEqual(route.user_id, "xiaoyu")


class TestSplit(unittest.TestCase):
    def test_short(self) -> None:
        self.assertEqual(split_message("你好", 100), ["你好"])

    def test_split_on_sentence(self) -> None:
        text = "第一句。" * 30
        parts = split_message(text, 50)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= 50 for part in parts))
        self.assertEqual("".join(parts), text)

    def test_hard_split(self) -> None:
        text = "啊" * 250
        parts = split_message(text, 100)
        self.assertTrue(all(len(part) <= 100 for part in parts))
        self.assertEqual("".join(parts), text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
