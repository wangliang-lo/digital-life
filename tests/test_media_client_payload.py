# -*- coding: utf-8 -*-
"""覆盖事故 1、2：客户端必须接受并转发跨机器媒体 base64 字段。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "chat_bridge").exists():
    ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "chat_bridge"))

from bridge.client import DigitalLifeClient  # noqa: E402
from bridge.config import ApiConfig  # noqa: E402


class ClientMediaPayloadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = DigitalLifeClient(ApiConfig(base_url="http://unused", retries=0))
        self.post = mock.Mock(return_value={"reply": "ok"})
        self.client._post = self.post

    def test_chat_accepts_and_sends_base64_media_fields(self) -> None:
        self.client.chat(
            "看看这些媒体",
            image_base64="aW1hZ2UtYnl0ZXM=",
            audio_base64="YXVkaW8tYnl0ZXM=",
            image_suffix=".png",
            audio_suffix="wav",
        )

        self.post.assert_called_once()
        url, payload = self.post.call_args.args
        self.assertEqual(url, "http://unused/chat")
        self.assertEqual(
            payload,
            {
                "message": "看看这些媒体",
                "image_base64": "aW1hZ2UtYnl0ZXM=",
                "audio_base64": "YXVkaW8tYnl0ZXM=",
                "image_suffix": ".png",
                "audio_suffix": "wav",
            },
        )

    def test_chat_omits_base64_media_fields_when_not_provided(self) -> None:
        self.client.chat("只有文字")

        self.post.assert_called_once()
        _, payload = self.post.call_args.args
        self.assertEqual(payload, {"message": "只有文字"})
        for field in ("image_base64", "audio_base64", "image_suffix", "audio_suffix"):
            with self.subTest(field=field):
                self.assertNotIn(field, payload)


if __name__ == "__main__":
    unittest.main()
