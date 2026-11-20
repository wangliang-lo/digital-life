#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""小鱼的完整测试入口（内建 71 个 + 记忆生命周期/备份恢复）。

    python run_tests.py            # 全部
    python run_tests.py -v         # 详细

为什么要有这个文件（而不是直接 python -m unittest）：
在 DSH 之类的沙箱里运行时，`tempfile.mkdtemp()` 用 0o700 建出来的目录
之后会拒绝写入，SQLite 于是报 "unable to open database file"，
`tests/test_memory_lifecycle.py`、`tests/test_backup_restore.py` 会 10 个全红。
那是环境的权限问题，不是她的代码坏了。这里先探测一次，必要时把临时目录
改成 0o777 再跑；在普通命令行里探测通过，行为跟 `python -m unittest` 完全一样。
"""
from __future__ import annotations

import os
import random
import string
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def _temp_dirs_usable() -> bool:
    """当前环境里 mkdtemp 建出来的目录还能不能写文件。"""
    probe_dir = None
    try:
        probe_dir = tempfile.mkdtemp()
        probe = os.path.join(probe_dir, "probe.txt")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        return True
    except OSError:
        return False
    finally:
        if probe_dir:
            try:
                for name in os.listdir(probe_dir) if os.path.isdir(probe_dir) else []:
                    os.unlink(os.path.join(probe_dir, name))
            except OSError:
                pass
            try:
                os.rmdir(probe_dir)
            except OSError:
                pass


def _patch_mkdtemp() -> None:
    """沙箱里：改用 0o777 建临时目录，建完能用。"""

    def mkdtemp(suffix: str | None = None, prefix: str | None = None, dir: str | None = None) -> str:
        suffix = suffix or ""
        prefix = prefix or tempfile.gettempprefix()
        base = dir or tempfile.gettempdir()
        for _ in range(100):
            name = prefix + "".join(random.choices(string.ascii_letters + string.digits, k=8)) + suffix
            path = os.path.join(base, name)
            try:
                os.mkdir(path, 0o777)
            except FileExistsError:
                continue
            return path
        raise FileExistsError("无法创建临时目录")

    tempfile.mkdtemp = mkdtemp


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    verbose = "-v" in argv or "--verbose" in argv
    args = [a for a in argv if a not in ("-v", "--verbose")]

    if _temp_dirs_usable():
        print("临时目录：正常")
    else:
        _patch_mkdtemp()
        print("临时目录：沙箱限制了 mkdtemp 的权限，已改用可写目录继续")

    names = args or [
        "digital_life",
        "tests.test_memory_lifecycle",
        "tests.test_backup_restore",
        "tests.test_owner_requests",
        "tests.test_chat_memory",
        "tests.test_reply_pace",
    "tests.test_media_client_payload",
    "tests.test_media_dl_api_base64",
    "tests.test_media_signature_contract",
    "tests.test_media_web_ui_stickers",
    "tests.test_prompt_integrity",
    "tests.test_web_ui_routes",
    "tests.test_red_lines",
    "tests.test_doodle_shapes",
    "tests.test_hormone_relief",
    "tests.test_intent_router",
    "tests.test_backup_memory",
        "tests.test_reply_choice",
        "tests.test_voice",
        "tests.test_chat_identity",
        "tests.test_surfaces",
        "tests.test_bubbles",
        "tests.test_action_notes",
        "tests.test_reply_shape",
    ]
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite(loader.loadTestsFromName(name) for name in names)
    result = unittest.TextTestRunner(verbosity=2 if verbose else 1).run(suite)
    failed = len(result.failures) + len(result.errors)
    print(
        f"\n结果：{result.testsRun} 个测试，失败 {failed}，跳过 {len(result.skipped)}"
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
