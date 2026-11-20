# -*- coding: utf-8 -*-
"""覆盖事故 2：她的脑子必须把网页传来的 base64 落成原始媒体文件。"""
from __future__ import annotations

import base64
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "chat_bridge").exists():
    ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "chat_bridge"))

import dl_api  # noqa: E402


class _StopAfterEnrich(Exception):
    pass


class Base64MediaHandlerTest(unittest.TestCase):
    def test_base64_media_reaches_enricher_as_exact_nonempty_files(self) -> None:
        image_bytes = b"\x89PNG\r\n\x1a\n\x00image-regression"
        audio_bytes = b"RIFF\x00\x01audio-regression"
        captured: dict[str, str] = {}

        def stop_after_enrich(message: str, **media: str) -> None:
            captured.update(media)
            raise _StopAfterEnrich

        with tempfile.TemporaryDirectory() as temp_dir:
            handler_type = dl_api._make_handler(
                executor=object(),
                enricher=SimpleNamespace(enrich=stop_after_enrich),
                media_dir=Path(temp_dir) / "media",
            )
            handler = object.__new__(handler_type)
            handler._send = mock.Mock()

            with mock.patch.object(dl_api, "note_preference"), self.assertRaises(_StopAfterEnrich):
                handler._handle_chat(
                    {
                        "message": "",
                        "image_base64": base64.b64encode(image_bytes).decode("ascii"),
                        "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
                        "image_suffix": ".PNG",
                        "audio_suffix": "wav",
                    }
                )

            image_path = Path(captured["image_path"])
            audio_path = Path(captured["audio_path"])
            self.addCleanup(shutil.rmtree, image_path.parent, ignore_errors=True)
            self.addCleanup(shutil.rmtree, audio_path.parent, ignore_errors=True)

            for path, expected in ((image_path, image_bytes), (audio_path, audio_bytes)):
                with self.subTest(path=path):
                    self.assertTrue(path.is_file())
                    self.assertGreater(path.stat().st_size, 0)
                    self.assertEqual(path.read_bytes(), expected)

            self.assertEqual(captured["emoji_url"], "")
            handler._send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
