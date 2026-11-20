# -*- coding: utf-8 -*-
"""memory_maintenance 测试：全部用临时库副本，不碰真库。"""
import json
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent / "chat_bridge" / "tools"
sys.path.insert(0, str(TOOLS))
import memory_maintenance as mm  # noqa: E402

REAL_DB = Path(__file__).resolve().parent.parent / "chat_bridge" / "data" / "xiaoyu.db"
NOW = datetime(2026, 11, 16, 12, 0, 0)

SCHEMA = """
CREATE TABLE books (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL DEFAULT 'default',
 content TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
 folder_date TEXT NOT NULL DEFAULT '2026-11-01', folder_user TEXT NOT NULL DEFAULT 'u',
 folder_event TEXT NOT NULL DEFAULT 'e', mv REAL NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'detail', created_at TEXT NOT NULL DEFAULT '2026-11-01T00:00:00',
 last_accessed TEXT NOT NULL DEFAULT '2026-11-01T00:00:00');
CREATE UNIQUE INDEX idx_books_dedup ON books(user_id, folder_date, folder_event, summary) WHERE status != 'ghost';
"""


def _dist(db):
    c = sqlite3.connect(db)
    try:
        return dict(c.execute("SELECT status, COUNT(*) FROM books GROUP BY status").fetchall())
    finally:
        c.close()


def _status(db, rid):
    c = sqlite3.connect(db)
    try:
        return c.execute("SELECT status FROM books WHERE id=?", (rid,)).fetchone()[0]
    finally:
        c.close()


@pytest.fixture
def db(tmp_path):
    """造一个小库：1 空壳、2/3 重复(mv 不同)、4 过期、5 正常。"""
    p = tmp_path / "t.db"
    c = sqlite3.connect(p)
    c.executescript(SCHEMA)
    old = (NOW - timedelta(days=200)).isoformat()
    rows = [
        # id, summary, content, mv, status, last_accessed, folder_event
        (1, "[表情]（看不到具体图案）", "", 5, "detail", NOW.isoformat(), "a"),
        (2, "今天一起去公园散步看到了很多花", "我们聊了散步", 10, "detail", NOW.isoformat(), "b"),
        (3, "今天一起去公园散步，看到了很多花！", "我们聊了散步", 30, "detail", NOW.isoformat(), "c"),
        (4, "很久以前聊过的一件小事情", "旧事", 5, "summary", old, "d"),
        (5, "她说明天要去面试，有点紧张", "面试", 50, "detail", NOW.isoformat(), "e"),
    ]
    for r in rows:
        c.execute("INSERT INTO books(id,summary,content,mv,status,last_accessed,folder_event) VALUES(?,?,?,?,?,?,?)", r)
    c.commit()
    c.close()
    return p


# ---- 空壳判定 ----
def test_hollow_detected():
    assert mm.is_hollow("[表情]（看不到具体图案）", "")


def test_normal_sentence_not_hollow():
    assert not mm.is_hollow("今天和她聊了很久关于旅行的计划", "详细内容")
    # 短但没有提示词，也不判空壳
    assert not mm.is_hollow("好的", "")


# ---- 完全重复 ----
def test_duplicate_keeps_highest_mv(db):
    books = mm.load_books(sqlite3.connect(db))
    plan = {p["id"]: p for p in mm.plan_actions(books, NOW)}
    assert plan[2]["category"] == mm.CAT_DUP and plan[2]["new_status"] == "suppressed"
    assert 3 not in plan  # mv 高的保留
    assert plan[1]["category"] == mm.CAT_HOLLOW
    assert plan[4]["new_status"] == "ghost"
    assert 5 not in plan


# ---- dry-run ----
def test_dry_run_does_not_write(db):
    before = _dist(db)
    res = mm.run(db, apply=False, now=NOW)
    assert _dist(db) == before
    assert res["planned"] == 3
    assert res["manifest"] is None
    assert res["report"].exists()


# ---- apply ----
def test_apply_changes_status_and_writes_manifest(db):
    res = mm.run(db, apply=True, now=NOW)
    assert _status(db, 1) == "suppressed"
    assert _status(db, 2) == "suppressed"
    assert _status(db, 3) == "detail"
    assert _status(db, 4) == "ghost"
    assert _status(db, 5) == "detail"
    assert sum(_dist(db).values()) == 5  # 不删行
    assert res["manifest"].exists()
    rows = [json.loads(l) for l in res["manifest"].read_text(encoding="utf-8").splitlines()]
    assert {r["id"] for r in rows} == {1, 2, 4}
    assert all({"id", "reason", "result"} <= set(r) for r in rows)
    assert "总条数：5" in res["report"].read_text(encoding="utf-8")


# ---- 唯一索引冲突 ----
def test_unique_conflict_falls_back_to_ghost(db):
    # 用触发器模拟 idx_books_dedup 冲突：改成 suppressed 时抛 IntegrityError
    c = sqlite3.connect(db)
    c.execute("""CREATE TRIGGER boom BEFORE UPDATE OF status ON books
                 WHEN NEW.status='suppressed'
                 BEGIN SELECT RAISE(ABORT, 'UNIQUE constraint failed: simulated'); END""")
    c.commit()
    c.close()
    res = mm.run(db, apply=True, now=NOW)  # 不能抛异常
    assert _status(db, 1) == "ghost"
    assert _status(db, 2) == "ghost"
    assert any("冲突回退" in r for r in res["results"].values())


# ---- 安全：真库不被测试触碰（只读核对，不写）----
@pytest.mark.skipif(not REAL_DB.exists(), reason="无真库")
def test_works_on_copy_of_real_db(tmp_path):
    cp = tmp_path / "copy.db"
    shutil.copy2(REAL_DB, cp)
    before = _dist(cp)
    mm.run(cp, apply=False)
    assert _dist(cp) == before
    mm.run(cp, apply=True)
    assert sum(_dist(cp).values()) == sum(before.values())
