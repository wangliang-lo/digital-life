# -*- coding: utf-8 -*-
"""媒体路由测试：用桩替代 whisper / 视觉模型，不联网。"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP_ROOT = ROOT / "_test_tmp"
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(_TMP_ROOT)


def _temp_dir() -> Path:
    path = _TMP_ROOT / f"case_{uuid.uuid4().hex[:10]}"
    path.mkdir(parents=True)
    return path

from bridge.config import ASRConfig, MediaConfig, VisionConfig  # noqa: E402
from bridge.media import MediaRouter  # noqa: E402
from bridge.media.voice import MediaError  # noqa: E402
from bridge.stickers import StickerStore  # noqa: E402

GIF = b"GIF89a" + b"\x00" * 64


class StubASR:
    def __init__(self, text: str = "今天好累", fail: bool = False) -> None:
        self.text = text
        self.fail = fail
        self.enabled = True

    def transcribe(self, path: str) -> str:
        if self.fail:
            raise MediaError("boom")
        return self.text


class StubVision:
    def __init__(self, text: str = "一只橘猫趴在键盘上", fail: bool = False) -> None:
        self.text = text
        self.fail = fail
        self.enabled = True

    def describe(self, path: str) -> str:
        if self.fail:
            raise MediaError("boom")
        return self.text


class MediaRouterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = _temp_dir()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = StickerStore(self.tmp / "stickers")
        self.store.load()
        self.gif = self.tmp / "a.gif"
        self.gif.write_bytes(GIF)

    def make(self, asr=None, vision=None, media_cfg=None) -> MediaRouter:
        return MediaRouter(
            media_cfg or MediaConfig(),
            asr or StubASR(),
            vision or StubVision(),
            self.store,
        )

    def test_voice(self) -> None:
        text = self.make().enrich("", "voice", str(self.gif))
        self.assertEqual(text, "[语音] 今天好累")

    def test_voice_with_leading_text(self) -> None:
        text = self.make().enrich("在吗", "voice", str(self.gif))
        self.assertTrue(text.startswith("在吗\n[语音]"))

    def test_voice_failure_placeholder(self) -> None:
        text = self.make(asr=StubASR(fail=True)).enrich("", "voice", str(self.gif))
        self.assertEqual(text, MediaConfig().placeholder_voice)

    def test_voice_disabled(self) -> None:
        cfg = MediaConfig(voice_enabled=False)
        self.assertEqual(self.make(media_cfg=cfg).enrich("", "voice", str(self.gif)), "")

    def test_image(self) -> None:
        text = self.make().enrich("", "image", str(self.gif))
        self.assertEqual(text, "[图片] 一只橘猫趴在键盘上")

    def test_image_failure_placeholder(self) -> None:
        text = self.make(vision=StubVision(fail=True)).enrich("", "image", str(self.gif))
        self.assertEqual(text, MediaConfig().placeholder_image)

    def test_gif_is_collected(self) -> None:
        self.make().enrich("", "image", str(self.gif))
        self.assertEqual(self.store.count(), 1)

    def test_emoji_placeholder(self) -> None:
        text = self.make().enrich("", "emoji", "", "http://127.0.0.1:9/none.gif")
        self.assertEqual(text, MediaConfig().placeholder_emoji)

    def test_unknown_kind_passthrough(self) -> None:
        self.assertEqual(self.make().enrich("原文", "", ""), "原文")


class ProviderConfigTest(unittest.TestCase):
    def test_invalid_provider(self) -> None:
        from bridge.config import ConfigError

        with self.assertRaises(ConfigError):
            ASRConfig(provider="magic")
        with self.assertRaises(ConfigError):
            VisionConfig(provider="magic")


if __name__ == "__main__":
    unittest.main(verbosity=2)
