# -*- coding: utf-8 -*-
"""记忆生命周期端到端测试（不 mock 数据库，用真 SQLite 临时库）。

覆盖 ChatGPT 建议里最核心的几条：
1. 重启连续性：写入 → 关库 → 重开 → 还能检索到，时间戳不漂
2. 三阶遗忘：detail → summary → ghost → 物理删除
3. 多用户隔离：A 的私密记忆，B 检索不到
4. 回忆强化：想起来的记忆会变牢（mv 上升）
5. 去重：同一句话不会存成两条

运行：
    cd C:\\aihuman && python -m unittest tests.test_memory_lifecycle -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import DECAY_IDLE_DAYS, GHOST_TTL_DAYS  # noqa: E402
from memory import LibraryMemory  # noqa: E402

OWNER = "conv:test-owner"
GUEST = "conv:test-guest"


class MemoryLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = str(Path(self.tmp.name) / "test.db")
        self.lib = LibraryMemory(db_path=self.db, user_id=OWNER)
        self.now = datetime(2026, 11, 2, 12, 0, 0)

    def tearDown(self) -> None:
        try:
            self.lib.close()
        except Exception:  # noqa: BLE001
            pass
        self.tmp.cleanup()

    # ---------- 1. 重启连续性 ----------
    def test_restart_keeps_memory_and_timestamp(self) -> None:
        book = self.lib.add_book(
            content="用户说：我很喜欢下雨天，尤其是半夜的雨。",
            summary="喜欢半夜下雨",
            emotion_tag="平静",
            folder_date="2026-11-02",
            folder_user="爸爸",
            folder_event="日常",
            mv=70.0,
            status="detail",
            now=self.now,
        )
        book_id = book["id"] if isinstance(book, dict) else book
        created = self.lib.conn.execute(
            "SELECT created_at FROM books WHERE id = ?", (book_id,)
        ).fetchone()["created_at"]
        self.lib.close()

        # 重新打开同一个库
        self.lib = LibraryMemory(db_path=self.db, user_id=OWNER)
        rows = self.lib.list_books()
        self.assertTrue(any(int(r["id"]) == int(book_id) for r in rows), "重启后记忆丢了")

        after = self.lib.conn.execute(
            "SELECT created_at FROM books WHERE id = ?", (book_id,)
        ).fetchone()["created_at"]
        self.assertEqual(created, after, "重启后 created_at 漂移了")

        result = self.lib.retrieve("下雨天", impulse=0.0, roll=0.0)
        self.assertTrue(result.hits, "重启后检索不到这条记忆")

    # ---------- 2. 三阶遗忘 ----------
    def test_three_stage_forgetting(self) -> None:
        book = self.lib.add_book(
            content="用户说：昨天在便利店遇到一只很凶的猫。",
            summary="便利店遇到凶猫",
            emotion_tag="日常",
            folder_date="2026-10-01",
            folder_user="爸爸",
            folder_event="日常",
            mv=25.0,
            status="detail",
            now=self.now - timedelta(days=40),
        )
        book_id = book["id"] if isinstance(book, dict) else book

        def status_of() -> str:
            row = self.lib.conn.execute(
                "SELECT status FROM books WHERE id = ?", (book_id,)
            ).fetchone()
            return str(row["status"]) if row else "deleted"

        # 把 last_accessed 拨到很久以前，模拟一直没被想起
        self.lib.conn.execute(
            "UPDATE books SET last_accessed = ? WHERE id = ?",
            (str(self.now - timedelta(days=60)), book_id),
        )
        self.lib.conn.commit()

        seen = [status_of()]
        clock = self.now
        for _ in range(14):
            clock = clock + timedelta(days=1)   # 每晚推进一天，否则 last_accessed 永远不旧
            self.lib.apply_night_hygiene(clock)
            seen.append(status_of())

        self.assertIn("detail", seen, "初始应该是 detail")
        self.assertIn("summary", seen, "应降级到 summary")
        self.assertTrue({"ghost", "deleted"} & set(seen), f"应降级到 ghost，实际经历：{seen}")

        # 幽灵到期后应被物理删除
        self.lib.purge_ghosts(clock + timedelta(days=GHOST_TTL_DAYS + 2))
        self.assertEqual(status_of(), "deleted", "幽灵超过保留期应被删除")

    # ---------- 3. 多用户隔离 ----------
    def test_multi_user_isolation(self) -> None:
        self.lib.add_book(
            content="用户说：我银行卡密码是我生日。",
            summary="他说了银行卡密码是他生日",
            emotion_tag="亲密",
            folder_date="2026-11-02",
            folder_user="爸爸",
            folder_event="亲密",
            mv=95.0,
            status="detail",
            now=self.now,
        )
        guest = LibraryMemory(db_path=self.db, user_id=GUEST)
        try:
            result = guest.retrieve("银行卡密码 生日", impulse=0.0, roll=0.0)
            self.assertEqual(result.hits, [], "别的用户不该检索到主人的私密记忆")
        finally:
            guest.close()

    # ---------- 4. 回忆强化 ----------
    def test_recall_reinforces_memory(self) -> None:
        book = self.lib.add_book(
            content="用户说：我们约好周末去看海。",
            summary="约好周末去看海",
            emotion_tag="开心",
            folder_date="2026-11-01",
            folder_user="爸爸",
            folder_event="亲密",
            mv=40.0,
            status="detail",
            now=self.now - timedelta(days=1),
        )
        book_id = book["id"] if isinstance(book, dict) else book
        before = float(
            self.lib.conn.execute("SELECT mv FROM books WHERE id = ?", (book_id,)).fetchone()["mv"]
        )
        self.lib.retrieve("看海", impulse=0.0, roll=0.0)
        after = float(
            self.lib.conn.execute("SELECT mv FROM books WHERE id = ?", (book_id,)).fetchone()["mv"]
        )
        self.assertGreater(after, before, "想起来一次，记忆应该更牢")

    # ---------- 5. 去重 ----------
    def test_duplicate_summary_is_not_stored_twice(self) -> None:
        for _ in range(3):
            self.lib.add_book(
                content="用户说：今天喝了咖啡。",
                summary="今天喝了咖啡",
                emotion_tag="日常",
                folder_date="2026-11-02",
                folder_user="爸爸",
                folder_event="日常",
                mv=30.0,
                status="detail",
                now=self.now,
            )
        rows = [r for r in self.lib.list_books() if "咖啡" in str(r.get("summary", ""))]
        self.assertEqual(len(rows), 1, f"同一句话被存成了 {len(rows)} 条")

    # ---------- 6. 来源标记（外部同步的记忆不外传） ----------
    def test_origin_is_recorded(self) -> None:
        cols = [r[1] for r in self.lib.conn.execute("PRAGMA table_info(books)").fetchall()]
        if "origin" not in cols:
            self.skipTest("当前 schema 没有 origin 字段（历史版本）")
        self.lib.add_book(
            content="来源：抖音同步",
            summary="外部同步进来的一条",
            emotion_tag="日常",
            folder_date="2026-11-02",
            folder_user="抖音",
            folder_event="日常",
            mv=20.0,
            status="detail",
            origin="douyin",
            now=self.now,
        )
        row = self.lib.conn.execute(
            "SELECT origin FROM books WHERE summary = ?", ("外部同步进来的一条",)
        ).fetchone()
        self.assertEqual(str(row["origin"]), "douyin")

    # ---------- 7. 版本覆盖：外部记录改了，旧说法不能继续被当成事实 ----------
    def test_newer_version_replaces_stale_content(self) -> None:
        def write(version: int, content: str) -> None:
            self.lib.add_book(
                content=content,
                summary="小糖的外号",
                emotion_tag="日常",
                folder_date="2026-11-02",
                folder_user="抖音",
                folder_event="日常",
                mv=30.0,
                status="detail",
                origin="douyin_sync",
                source_id="entry-1",
                version=version,
                now=self.now,
            )

        write(100, "外号：胡三磕")
        write(200, "外号：小糖")
        row = self.lib.conn.execute(
            "SELECT content, version FROM books WHERE summary = ?", ("小糖的外号",)
        ).fetchone()
        self.assertEqual(str(row["content"]), "外号：小糖", "新版本应覆盖旧正文")
        self.assertEqual(int(row["version"]), 200)

        # 版本更旧的内容不许回滚
        write(150, "外号：胡三磕")
        row = self.lib.conn.execute(
            "SELECT content, version FROM books WHERE summary = ?", ("小糖的外号",)
        ).fetchone()
        self.assertEqual(str(row["content"]), "外号：小糖", "旧版本不该覆盖新版本")
        self.assertEqual(int(row["version"]), 200)

    def test_chat_repeat_keeps_original_content(self) -> None:
        """同一句话重复说，正文不该被后一次覆盖（保住最初的细节）。"""
        for text in ("用户说：我怕黑。", "用户说：我怕黑，今晚别关灯。"):
            self.lib.add_book(
                content=text,
                summary="怕黑",
                emotion_tag="日常",
                folder_date="2026-11-02",
                folder_user="爸爸",
                folder_event="日常",
                mv=30.0,
                status="detail",
                now=self.now,
            )
        row = self.lib.conn.execute(
            "SELECT content, version FROM books WHERE summary = ?", ("怕黑",)
        ).fetchone()
        self.assertEqual(str(row["content"]), "用户说：我怕黑。")
        self.assertEqual(int(row["version"]), 1)

    # ---------- 8. 兜底补位的"最近的事"必须标出来，免得被当成答案 ----------
    def test_fallback_recents_are_labeled(self) -> None:
        for i in range(4):
            self.lib.add_book(
                content=f"用户说：今天第{i}件小事。",
                summary=f"今天第{i}件小事",
                emotion_tag="日常",
                folder_date="2026-11-02",
                folder_user="爸爸",
                folder_event="日常",
                mv=40.0,
                status="detail",
                now=self.now,
            )
        result = self.lib.retrieve("你还记得我上次跟你说下雨天的事吗？", impulse=0.0, deep=True, roll=0.0)
        self.assertTrue(result.hits, "回忆问询至少要翻出点东西")
        fallback = [hit for hit in result.hits if hit.fallback]
        self.assertTrue(fallback, "没有直接命中时，补位的最近的事应该被标成 fallback")

        text = result.to_prompt_text()
        self.assertIn("[最近的事·未必相关]", text)
        self.assertIn("别把最近的事当成答案", text)
        self.assertTrue(fallback[0].to_dict()["fallback"])
    # ---------- 9. 问句脚手架不该把"旧问句记忆"顶成答案 ----------
    def test_question_scaffold_is_not_a_keyword(self) -> None:
        from memory import strip_question_scaffold

        self.assertEqual(strip_question_scaffold("你还记得我上次跟你说下雨天的事吗"), "下雨天的事吗")

        # 两本"她以前被问过什么"的书：只含问句脚手架，没有任何下雨的内容
        for i, summary in enumerate(("爸爸说：你还记得我们之前说过的抖音调试的事吗",
                                     "你说：你还记得我昨天说加班吗")):
            self.lib.add_book(
                content=f"用户说：{summary}",
                summary=summary,
                emotion_tag="日常",
                folder_date="2026-11-02",
                folder_user="爸爸",
                folder_event="日常",
                mv=60.0,
                status="detail",
                now=self.now,
            )

        result = self.lib.retrieve("你还记得我上次跟你说下雨天的事吗？", impulse=0.0, deep=True, roll=0.0)
        real = [hit for hit in result.hits if not hit.fallback]
        self.assertEqual(real, [], f"没有下雨的记忆，就不该把旧问句当命中：{[h.summary for h in real]}")

    # ---------- 10. 只靠"最近"和"记忆值高"不够格当命中 ----------
    def test_recency_alone_is_not_a_hit(self) -> None:
        self.lib.add_book(
            content="用户说：今天喝了咖啡。",
            summary="今天喝了咖啡",
            emotion_tag="日常",
            folder_date="2026-11-02",
            folder_user="爸爸",
            folder_event="日常",
            mv=100.0,
            status="detail",
            now=self.now,
        )
        result = self.lib.retrieve("量子力学", impulse=0.0, deep=True, roll=0.0)
        self.assertEqual(result.hits, [], "不相关的问题不该拿最近的书凑答案")

        # 真正相关时照样能想起来
        result = self.lib.retrieve("你还记得我喜欢喝什么吗", impulse=0.0, deep=True, roll=0.0)
        self.assertTrue(
            any("咖啡" in hit.summary for hit in result.hits),
            "问了咖啡，就该想起咖啡那条",
        )


if __name__ == "__main__":
    unittest.main()
