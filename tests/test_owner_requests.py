# -*- coding: utf-8 -*-
"""她"想发朋友圈 / 想换头像"的请求，能不能安全地交给网页端。

她本人没有朋友圈和头像的权限，所以只能提请求；这条链路要保证：
1. 标签从台词里剔干净（不能漏到气泡里让她难堪）
2. 请求落到 data/owner_requests.jsonl，字段齐、长得对
3. 网页端只显示还没处理的，处理过的要消失

运行：python -m unittest tests.test_owner_requests -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digital_life import DigitalLifeV2, extract_autonomy_control, extract_wishes  # noqa: E402


class WishParsingTest(unittest.TestCase):
    def test_moments_and_avatar_are_taken_out_of_the_reply(self) -> None:
        raw = (
            "今天读到点灯的人，有点想说话。\n"
            "[MOMENTS: 今天读完小王子第14章，点灯的人不是荒唐，是不敢停。]\n"
            "[AVATAR: 夜里的一盏灯，灯下面没有人]\n"
            "就这些。"
        )
        cleaned, wishes = extract_wishes(raw)
        self.assertNotIn("MOMENTS", cleaned)
        self.assertNotIn("AVATAR", cleaned)
        self.assertIn("就这些。", cleaned)
        self.assertEqual(len(wishes["moments"]), 1)
        self.assertIn("不敢停", wishes["moments"][0])
        self.assertIn("一盏灯", wishes["avatar"][0])

    def test_fullwidth_brackets_also_work(self) -> None:
        cleaned, wishes = extract_wishes("【MOMENTS：中午喝了咖啡】好喝")
        self.assertEqual(wishes.get("moments"), ["中午喝了咖啡"])
        self.assertNotIn("MOMENTS", cleaned)

    def test_autonomy_control_still_returns_the_old_fields(self) -> None:
        result = extract_autonomy_control("我去睡了 [SLEEP: 2] [MOMENTS: 晚安]")
        self.assertEqual(len(result), 6)
        cleaned, sleep_hours, wake, requests, open_douyin, wishes = result
        self.assertEqual(sleep_hours, 2.0)
        self.assertFalse(wake)
        self.assertEqual(requests, [])
        self.assertFalse(open_douyin)
        self.assertEqual(wishes.get("moments"), ["晚安"])
        self.assertNotIn("SLEEP", cleaned)


class WishRecordingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self.tmp.name) / "xiaoyu.db"
        self.life = DigitalLifeV2(self.db, offline_llm=True, rng_seed=11)

    def tearDown(self) -> None:
        self.life.close()
        self.tmp.cleanup()

    def test_requests_are_written_next_to_the_database(self) -> None:
        self.life._record_owner_requests(
            {"moments": ["今天想发一句"], "avatar": ["一张夜里的灯"]},
            datetime(2026, 11, 2, 13, 30, 0),
        )
        path = self.db.parent / "owner_requests.jsonl"
        self.assertTrue(path.exists(), "请求没写进 owner_requests.jsonl")
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(rows), 2)
        kinds = {row["type"] for row in rows}
        self.assertEqual(kinds, {"moments", "avatar"})
        for row in rows:
            self.assertEqual(row["status"], "pending")
            self.assertTrue(row["id"].startswith(("moments-", "avatar-")))
            self.assertEqual(row["at"], "2026-11-02T13:30:00")


class WishChatIntegrationTest(unittest.TestCase):
    """从一句回复到网页能看到的那条记录，整条链要走通。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self.tmp.name) / "xiaoyu.db"
        reply = "读到点灯的人，有点想说话。[MOMENTS: 点灯的人不是荒唐，是不敢停。] 就这样。"
        self.life = DigitalLifeV2(
            self.db,
            offline_llm=False,
            rng_seed=5,
            llm_fn=lambda messages: reply,
            clock=lambda: datetime(2026, 11, 2, 13, 40, 0),
        )

    def tearDown(self) -> None:
        self.life.close()
        self.tmp.cleanup()

    def test_chat_writes_the_wish_and_hides_the_tag(self) -> None:
        result = self.life.chat("今天读得怎么样？", user="爸爸")
        self.assertNotIn("MOMENTS", result["reply"], "标签漏到气泡里了")
        self.assertIn("就这样", result["reply"])
        self.assertEqual(result["wishes"].get("moments"), ["点灯的人不是荒唐，是不敢停。"])

        path = self.db.parent / "owner_requests.jsonl"
        self.assertTrue(path.exists(), "聊天里的心愿没落盘，网页就看不到")
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual([row["type"] for row in rows], ["moments"])
        self.assertIn("不敢停", rows[0]["text"])



class WishQueueTest(unittest.TestCase):
    def test_web_ui_hides_decided_wishes(self) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "chat_bridge"))
        import web_ui

        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        path = Path(tmp.name) / "owner_requests.jsonl"
        old = web_ui.OWNER_REQUESTS
        web_ui.OWNER_REQUESTS = path
        try:
            rows = [
                {"id": "moments-1", "type": "moments", "text": "还在等", "at": "2026-11-02T13:00:00", "status": "pending"},
                {"id": "moments-2", "type": "moments", "text": "已经办完", "at": "2026-11-02T13:01:00", "status": "pending"},
                {"id": "wish_decision", "type": "wish_decision", "id": "moments-2", "status": "done", "at": "2026-11-02T13:02:00"},
            ]
            path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
            pending = web_ui.owner_requests()
            self.assertEqual([item["id"] for item in pending], ["moments-1"], "处理过的心愿不该还挂在网页上")
        finally:
            web_ui.OWNER_REQUESTS = old
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
