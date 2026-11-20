# -*- coding: utf-8 -*-
"""版本伪装：只测版本号换算和失败路径，不碰真实IM进程。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bridge.version_patch import (  # noqa: E402
    OFFSETS_3_6_0_18,
    VersionPatcher,
    version_to_hex,
)


class TestVersionHex(unittest.TestCase):
    def test_3_6_0_18(self) -> None:
        self.assertEqual(version_to_hex("3.6.0.18"), 0x63060012)

    def test_3_9_12_15(self) -> None:
        self.assertEqual(version_to_hex("3.9.12.15"), 0x63090C0F)

    def test_round_trip(self) -> None:
        value = version_to_hex("4.1.8.107")
        self.assertEqual(value >> 28, 0x6)
        self.assertEqual((value >> 24) & 0x0F, 4)
        self.assertEqual((value >> 16) & 0xFF, 1)
        self.assertEqual((value >> 8) & 0xFF, 8)
        self.assertEqual(value & 0xFF, 107)

    def test_invalid(self) -> None:
        for bad in ("3.6", "3.6.0", "3.6.0.18.1", "a.b.c.d", "3.6.0.999"):
            with self.assertRaises(ValueError, msg=bad):
                version_to_hex(bad)

    def test_offsets(self) -> None:
        self.assertEqual(len(OFFSETS_3_6_0_18), 6)
        self.assertTrue(all(isinstance(offset, int) for offset in OFFSETS_3_6_0_18))


class TestPatcherFailure(unittest.TestCase):
    def test_patch_without_chat(self) -> None:
        result = VersionPatcher().patch(timeout=1.0, interval=0.2)
        self.assertFalse(result.ok)
        self.assertTrue(
            "pymem" in result.detail or "IM" in result.detail,
            f"没解释清楚失败原因：{result.detail}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
