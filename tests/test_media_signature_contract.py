# -*- coding: utf-8 -*-
"""覆盖事故 1：WebApp 与 DigitalLifeClient 的媒体 kwargs 必须保持兼容。"""
from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "chat_bridge").exists():
    ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "chat_bridge"))

from bridge.client import DigitalLifeClient  # noqa: E402
import web_ui  # noqa: E402


class MediaSignatureContractTest(unittest.TestCase):
    def test_both_sides_accept_the_same_base64_media_kwargs(self) -> None:
        methods = {
            "DigitalLifeClient.chat": DigitalLifeClient.chat,
            "WebApp.chat": web_ui.WebApp.chat,
        }
        for method_name, method in methods.items():
            parameters = inspect.signature(method).parameters
            for field in ("image_base64", "audio_base64", "image_suffix", "audio_suffix"):
                with self.subTest(method=method_name, field=field):
                    self.assertIn(
                        field,
                        parameters,
                        f"{method_name} 不接受关键字参数 {field}",
                    )
                    self.assertNotEqual(
                        parameters[field].kind,
                        inspect.Parameter.POSITIONAL_ONLY,
                        f"{method_name} 的 {field} 不能按关键字传递",
                    )


if __name__ == "__main__":
    unittest.main()
