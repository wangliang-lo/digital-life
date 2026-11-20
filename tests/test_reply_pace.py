# -*- coding: utf-8 -*-
"""她回话的节奏：IM上要像真人，不能一秒蹦一句。

规则（human_reply_delay_ms）：
- 有 1.5-3.2 秒的"看到→想一下"
- 按字数估打字时间（约 7 字/秒，最多 6 秒）
- 6% 的概率走神多停 2-4 秒
- 总上限 15 秒

运行：python -m unittest tests.test_reply_pace -v
"""
from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "chat_bridge"))

from dl_api import human_reply_delay_ms  # noqa: E402


class ReplyPaceTest(unittest.TestCase):
    def test_short_reply_takes_at_least_think_time(self) -> None:
        delay = human_reply_delay_ms("嗯嗯", rng=random.Random(7))
        self.assertGreaterEqual(delay, 1500, "再短的回复也要有一点'想一下'的时间")
        self.assertLessEqual(delay, 15000)

    def test_longer_reply_waits_longer(self) -> None:
        short = human_reply_delay_ms("嗯嗯", rng=random.Random(11))
        long = human_reply_delay_ms("今天读完了小王子第十四章，那个点灯的人每一分钟点一次灯", rng=random.Random(11))
        self.assertGreater(long, short, "字多就该打得久一点")

    def test_delay_is_capped(self) -> None:
        delay = human_reply_delay_ms("字" * 400, rng=random.Random(3))
        self.assertLessEqual(delay, 15000, "再长也别让人以为她掉线")

    def test_whitespace_does_not_count(self) -> None:
        a = human_reply_delay_ms("嗯嗯", rng=random.Random(5))
        b = human_reply_delay_ms("嗯 嗯\n", rng=random.Random(5))
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
