# -*- coding: utf-8 -*-
"""备份 / 恢复演练：备份成功 ≠ 能恢复。

流程（用临时库，绝不碰真库）：
1. 建一个含多用户记忆 + 日记的库
2. 用 backup_memory.backup_one 备份
3. 把主库"弄坏"（挪走 + 写垃圾字节）
4. 从备份恢复
5. 校验表结构、记录数、关键字段，并真的用它检索一次

运行：
    cd C:\\aihuman && python -m unittest tests.test_backup_restore -v
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "chat_bridge"))

from memory import LibraryMemory  # noqa: E402
from tools.backup_memory import backup_one  # noqa: E402


class BackupRestoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        base = Path(self.tmp.name)
        self.db = base / "xiaoyu.db"
        self.backups = base / "backups"
        self.now = datetime(2026, 11, 2, 12, 0, 0)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _build_data(self) -> None:
        owner = LibraryMemory(db_path=str(self.db), user_id="conv:owner")
        guest = LibraryMemory(db_path=str(self.db), user_id="conv:guest")
        try:
            owner.add_book(
                content="用户说：周日下午去看海。",
                summary="约好周日下午去看海",
                emotion_tag="开心",
                folder_date="2026-11-02",
                folder_user="爸爸",
                folder_event="亲密",
                mv=80.0,
                status="detail",
                now=self.now,
            )
            guest.add_book(
                content="用户说：我叫小王。",
                summary="他说他叫小王",
                emotion_tag="日常",
                folder_date="2026-11-02",
                folder_user="小王",
                folder_event="日常",
                mv=40.0,
                status="detail",
                now=self.now,
            )
        finally:
            owner.close()
            guest.close()

    def test_backup_then_restore(self) -> None:
        self._build_data()

        con = sqlite3.connect(self.db)
        try:
            before_tables = sorted(
                r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
            )
            before_books = con.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        finally:
            con.close()
        self.assertGreaterEqual(before_books, 2)

        # 1) 备份
        saved = backup_one(self.db, self.backups)
        self.assertIsNotNone(saved, "备份没有生成文件")
        self.assertTrue(Path(saved).exists())

        # 2) 弄坏主库：挪走 + 写垃圾
        broken = self.db.with_suffix(".broken")
        shutil.move(str(self.db), str(broken))
        self.db.write_bytes(b"not a sqlite file at all" * 100)

        # 3) 恢复
        shutil.copyfile(saved, self.db)

        # 4) 校验结构与数据
        con = sqlite3.connect(self.db)
        try:
            after_tables = sorted(
                r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
            )
            after_books = con.execute("SELECT COUNT(*) FROM books").fetchone()[0]
            users = {r[0] for r in con.execute("SELECT DISTINCT user_id FROM books")}
        finally:
            con.close()
        self.assertEqual(before_tables, after_tables, "恢复后表结构变了")
        self.assertEqual(before_books, after_books, "恢复后记录数对不上")
        self.assertIn("conv:owner", users)
        self.assertIn("conv:guest", users)

        # 5) 真的用数字生命读一次
        lib = LibraryMemory(db_path=str(self.db), user_id="conv:owner")
        try:
            result = lib.retrieve("看海", impulse=0.0, roll=0.0)
            self.assertTrue(result.hits, "恢复后的库检索不到原来的记忆")
        finally:
            lib.close()

    def test_backup_is_consistent_snapshot(self) -> None:
        """备份期间继续写入，备份文件不应损坏（SQLite 在线备份语义）。"""
        self._build_data()
        lib = LibraryMemory(db_path=str(self.db), user_id="conv:owner")
        try:
            lib.add_book(
                content="用户说：备份的时候我还在写东西。",
                summary="备份时仍在写入",
                emotion_tag="日常",
                folder_date="2026-11-02",
                folder_user="爸爸",
                folder_event="日常",
                mv=30.0,
                status="detail",
                now=self.now,
            )
            saved = backup_one(self.db, self.backups)
        finally:
            lib.close()
        self.assertIsNotNone(saved)
        con = sqlite3.connect(saved)
        try:
            count = con.execute("SELECT COUNT(*) FROM books").fetchone()[0]
            integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            con.close()
        self.assertGreaterEqual(count, 2)
        self.assertEqual(integrity, "ok")


if __name__ == "__main__":
    unittest.main()
