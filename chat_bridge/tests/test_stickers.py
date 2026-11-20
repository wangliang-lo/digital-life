# -*- coding: utf-8 -*-
"""表情包仓库与指令测试（不碰IM）。"""

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

# 沙箱里系统临时目录不可写，统一放到工作区的 _test_tmp
_TMP_ROOT = ROOT / "_test_tmp"
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(_TMP_ROOT)


def _temp_dir() -> Path:
    """不用 TemporaryDirectory：它建的目录里再建子目录会被安全策略拒绝。"""
    path = _TMP_ROOT / f"case_{uuid.uuid4().hex[:10]}"
    path.mkdir(parents=True)
    return path

from bridge.adapters.base import IncomingMessage  # noqa: E402
from bridge.stickers import StickerCommandHandler, StickerStore  # noqa: E402

GIF = b"GIF89a" + b"\x00" * 64
GIF2 = b"GIF89a" + b"\x01" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class StickerStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = _temp_dir()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "stickers"
        self.store = StickerStore(self.root, max_kb=2048)
        self.store.load()

    def test_add_and_pick(self) -> None:
        happy = self.store.add_bytes(GIF, "", ["开心"])
        sad = self.store.add_bytes(GIF2, "", ["委屈"])
        self.assertIsNotNone(happy)
        self.assertIsNotNone(sad)
        self.assertEqual(self.store.count(), 2)

        picked = self.store.pick(["开心"])
        assert picked is not None
        self.assertEqual(picked.id, happy.id)

    def test_dedupe_merges_tags(self) -> None:
        first = self.store.add_bytes(GIF, "", ["开心"])
        again = self.store.add_bytes(GIF, "", ["可爱"])
        assert first is not None and again is not None
        self.assertEqual(first.id, again.id)
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(set(again.tags), {"开心", "可爱"})

    def test_reject_unknown_format(self) -> None:
        self.assertIsNone(self.store.add_bytes(b"not an image", ".txt"))
        self.assertEqual(self.store.count(), 0)

    def test_size_limit(self) -> None:
        small = StickerStore(self.tmp / "small", max_kb=64)
        big = b"GIF89a" + b"\x00" * (70 * 1024)
        self.assertIsNone(small.add_bytes(big, "", ["开心"]))

    def test_pick_falls_back_to_all(self) -> None:
        self.store.add_bytes(GIF, "", ["开心"])
        picked = self.store.pick(["不存在的标签"])
        self.assertIsNotNone(picked)

    def test_remove(self) -> None:
        sticker = self.store.add_bytes(GIF, "", ["开心"])
        assert sticker is not None
        self.assertTrue(self.store.remove(sticker.id))
        self.assertFalse(self.store.remove(sticker.id))
        self.assertEqual(self.store.count(), 0)
        self.assertFalse((self.root / sticker.file).exists())

    def test_persist_and_reload(self) -> None:
        self.store.add_bytes(GIF, "", ["开心"])
        again = StickerStore(self.root)
        again.load()
        self.assertEqual(again.count(), 1)
        self.assertEqual(again.tag_counts(), {"开心": 1})

    def test_mark_used(self) -> None:
        sticker = self.store.add_bytes(GIF, "", ["开心"])
        assert sticker is not None
        self.store.mark_used(sticker.id)
        self.assertEqual(self.store.get(sticker.id).uses, 1)

    def test_import_dir(self) -> None:
        src = self.tmp / "src" / "开心"
        src.mkdir(parents=True)
        (src / "a.gif").write_bytes(GIF)
        (src / "b.png").write_bytes(PNG)
        self.assertEqual(self.store.import_dir(src.parent), 2)
        self.assertEqual(self.store.count(), 2)


class StickerCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = _temp_dir()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = StickerStore(self.tmp / "stickers")
        self.store.load()
        self.handler = StickerCommandHandler(self.store, owner_wxids=("wxid_owner",))
        self.media_file = self.tmp / "x.gif"
        self.media_file.write_bytes(GIF)
        self.last_media = IncomingMessage(
            msg_id="m1", from_wxid="wxid_owner", media_kind="image", media_path=str(self.media_file)
        )

    def _save(self, msg: IncomingMessage, tags: list[str]):
        return self.store.add_file(msg.media_path, tags, source="test")

    def test_detect(self) -> None:
        self.assertTrue(self.handler.is_command("/表情"))
        self.assertTrue(self.handler.is_command("/表情 发 开心"))
        self.assertFalse(self.handler.is_command("你好"))

    def test_save_then_send(self) -> None:
        result = self.handler.handle("/表情 存 开心", "wxid_owner", self.last_media, self._save)
        assert result is not None
        self.assertIn("存好了", result.text or "")
        self.assertEqual(self.store.count(), 1)

        sent = self.handler.handle("/表情 发 开心", "wxid_owner", self.last_media, self._save)
        assert sent is not None
        self.assertIsNotNone(sent.sticker)

    def test_non_owner_ignored(self) -> None:
        self.assertIsNone(self.handler.handle("/表情 列表", "wxid_stranger", None, self._save))

    def test_no_media(self) -> None:
        result = self.handler.handle("/表情 存 开心", "wxid_owner", None, self._save)
        assert result is not None
        self.assertIn("还没有", result.text or "")

    def test_delete(self) -> None:
        sticker = self.store.add_bytes(GIF, "", ["开心"])
        assert sticker is not None
        result = self.handler.handle(f"/表情 删 {sticker.id}", "wxid_owner", None, self._save)
        assert result is not None
        self.assertIn("删掉了", result.text or "")
        self.assertEqual(self.store.count(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
