# -*- coding: utf-8 -*-
"""tools/backup_memory.py 的单元测试。

约束（见 TASK.md）：不联网、不 scp、不碰真实 data/xiaoyu.db。
所有路径都用 tmp_path，所有外部命令都用假的 subprocess.run 挡掉。

运行：
    cd C:\\aihuman && python -m pytest tests/test_backup_memory.py -q
"""
from __future__ import annotations

import gzip
import re
import shutil
import sqlite3
import sys
import types
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "chat_bridge"))

import tools.backup_memory as bm  # noqa: E402


# ---------------------------------------------------------------- fixtures

def make_db(path: Path) -> Path:
    """造一个小库：两张表、几行数据。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute(
            "CREATE TABLE notes (id INTEGER PRIMARY KEY, person_id INTEGER, body TEXT)"
        )
        conn.executemany(
            "INSERT INTO people (id, name) VALUES (?, ?)",
            [(1, "阿雨"), (2, "小雨")],
        )
        conn.executemany(
            "INSERT INTO notes (id, person_id, body) VALUES (?, ?, ?)",
            [(1, 1, "今天下雨了"), (2, 1, "带了伞"), (3, 2, "想吃火锅")],
        )
    conn.close()
    return path


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """把模块里所有真实路径常量指到 tmp_path。"""
    db = make_db(tmp_path / "xiaoyu.db")
    backups = tmp_path / "data" / "backups"
    log = tmp_path / "data" / "backup_log.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bm, "DB", db)
    monkeypatch.setattr(bm, "LOCAL_BACKUPS", backups)
    monkeypatch.setattr(bm, "LOG", log)
    return types.SimpleNamespace(db=db, backups=backups, log=log, root=tmp_path)


def read_gz(gz: Path, dest: Path) -> sqlite3.Connection:
    """解压 .gz 到 dest 并打开，返回连接。"""
    with gzip.open(gz, "rb") as src, dest.open("wb") as out:
        shutil.copyfileobj(src, out)
    return sqlite3.connect(dest)


def fake_subprocess(calls, scp_rc=0, ssh_rc=0, stdout="xiaoyu-20261114.db.gz\n",
                    error=None):
    """造一个假的 subprocess 模块，记录每次调用。

    error 不为 None 时，第一次调用就抛异常（模拟 scp.exe 不存在）。
    """

    def run(argv, *args, **kwargs):
        calls.append([str(a) for a in argv])
        if error is not None:
            raise error
        rc = scp_rc if "scp" in str(argv[0]).lower() else ssh_rc
        return types.SimpleNamespace(returncode=rc, stdout=stdout, stderr="boom")

    return types.SimpleNamespace(run=run)


# ------------------------------------------------------- 1. 导出与内容校验

def test_snapshot_exports_readable_db_with_tables_and_rows(sandbox):
    gz = bm.snapshot()

    assert gz is not None
    assert gz.exists()
    assert gz.parent == sandbox.backups

    conn = read_gz(gz, sandbox.root / "restored.db")
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"people", "notes"} <= tables

        assert conn.execute("SELECT count(*) FROM people").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 3
        assert conn.execute(
            "SELECT name FROM people WHERE id = ?", (1,)
        ).fetchone()[0] == "阿雨"
        assert conn.execute(
            "SELECT body FROM notes WHERE id = ?", (3,)
        ).fetchone()[0] == "想吃火锅"
    finally:
        conn.close()


def test_snapshot_does_not_touch_real_database(sandbox):
    """导出后真实库的内容不变，且 tmp 里只多出备份文件。"""
    before = sqlite3.connect(sandbox.db)
    before_count = before.execute("SELECT count(*) FROM notes").fetchone()[0]
    before.close()

    bm.snapshot()

    after = sqlite3.connect(sandbox.db)
    try:
        assert after.execute("SELECT count(*) FROM notes").fetchone()[0] == before_count
    finally:
        after.close()


def test_snapshot_returns_none_when_db_missing(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(bm, "DB", sandbox.root / "nope.db")

    assert bm.snapshot() is None
    assert "找不到记忆库" in capsys.readouterr().out
    assert not sandbox.backups.exists() or not list(sandbox.backups.iterdir())


def test_snapshot_keeps_db_when_source_is_unreadable(sandbox, monkeypatch, capsys):
    """源文件不是合法 sqlite 时返回 None，而不是抛异常。"""
    broken = sandbox.root / "broken.db"
    broken.write_bytes(b"not a database at all")
    monkeypatch.setattr(bm, "DB", broken)

    assert bm.snapshot() is None
    assert "备份失败" in capsys.readouterr().out


# ------------------------------------------------------------- 2. 文件命名

def test_backup_filename_is_xiaoyu_yyyymmdd_db_gz(sandbox):
    gz = bm.snapshot()

    assert re.fullmatch(r"xiaoyu-\d{8}\.db\.gz", gz.name), gz.name
    assert gz.name == f"xiaoyu-{datetime.now().strftime('%Y%m%d')}.db.gz"


def test_same_day_snapshot_overwrites_instead_of_stacking(sandbox):
    first = bm.snapshot()
    second = bm.snapshot()

    assert first == second
    assert [p.name for p in sandbox.backups.iterdir()] == [second.name]


# -------------------------------------------- 3. 上传失败不影响本地备份

def test_upload_scp_nonzero_returns_false_and_keeps_local(sandbox, monkeypatch):
    calls = []
    monkeypatch.setattr(bm, "subprocess", fake_subprocess(calls, scp_rc=1))
    gz = sandbox.backups / "xiaoyu-20261114.db.gz"
    gz.parent.mkdir(parents=True, exist_ok=True)
    gz.write_bytes(gzip.compress(b"payload"))

    assert bm.upload(gz) is False
    assert gz.exists()
    # scp 失败时不应再去远端删东西
    assert not any("find " in cmd[-1] for cmd in calls)


def test_upload_subprocess_raises_and_keeps_local(sandbox, monkeypatch):
    """scp.exe 不存在 / 起不来时异常会冒出去，但本地备份不能被删。"""
    calls = []
    monkeypatch.setattr(
        bm, "subprocess",
        fake_subprocess(calls, error=FileNotFoundError("scp.exe missing")),
    )
    gz = sandbox.backups / "xiaoyu-20261114.db.gz"
    gz.parent.mkdir(parents=True, exist_ok=True)
    gz.write_bytes(gzip.compress(b"payload"))

    with pytest.raises(FileNotFoundError):
        bm.upload(gz)
    assert gz.exists()


def test_main_upload_failure_returns_1_and_keeps_local(sandbox, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["backup_memory.py"])
    monkeypatch.setattr(bm, "upload", lambda gz: False)

    assert bm.main() == 1

    files = list(sandbox.backups.glob("xiaoyu-*.db.gz"))
    assert len(files) == 1
    assert files[0].stat().st_size > 0


# --------------------------------------------------------- 4. 远端清理命令

def test_prune_command_deletes_only_files_older_than_keep_days(sandbox, monkeypatch):
    calls = []
    monkeypatch.setattr(bm, "subprocess", fake_subprocess(calls))
    gz = sandbox.backups / "xiaoyu-20261114.db.gz"
    gz.parent.mkdir(parents=True, exist_ok=True)
    gz.write_bytes(gzip.compress(b"payload"))

    assert bm.upload(gz) is True

    prune = [cmd[-1] for cmd in calls if "find " in cmd[-1]]
    assert len(prune) == 1, calls
    cmd = prune[0]

    assert bm.KEEP_DAYS == 14
    assert f"-mtime +{bm.KEEP_DAYS}" in cmd          # 只匹配 14 天以前的
    assert f"-mtime -{bm.KEEP_DAYS}" not in cmd      # 不会删 14 天内的
    assert "-delete" in cmd
    assert f"{bm.REMOTE_DIR}" in cmd
    assert "'xiaoyu-*.db.gz'" in cmd                 # 只删自己的备份，不碰别的文件
    assert "rm -rf" not in cmd                       # 不整目录删


def test_prune_never_mentions_the_file_just_uploaded(sandbox, monkeypatch):
    calls = []
    monkeypatch.setattr(bm, "subprocess", fake_subprocess(calls))
    gz = sandbox.backups / "xiaoyu-20261114.db.gz"
    gz.parent.mkdir(parents=True, exist_ok=True)
    gz.write_bytes(gzip.compress(b"payload"))

    bm.upload(gz)

    # 上传用 scp，清理用 ssh，两者参数不能混
    scp_calls = [c for c in calls if c[0].lower().endswith("scp.exe")]
    ssh_calls = [c for c in calls if c[0].lower().endswith("ssh.exe")]
    assert len(scp_calls) == 1
    assert len(ssh_calls) == 2
    assert str(gz) in scp_calls[0]
    assert all("-delete" not in " ".join(c) for c in scp_calls)


def test_upload_ignores_ssh_prune_failure(sandbox, monkeypatch):
    """已知行为：清理那一步 ssh 失败，upload 仍然报成功。"""
    calls = []
    monkeypatch.setattr(bm, "subprocess", fake_subprocess(calls, ssh_rc=255))
    gz = sandbox.backups / "xiaoyu-20261114.db.gz"
    gz.parent.mkdir(parents=True, exist_ok=True)
    gz.write_bytes(gzip.compress(b"payload"))

    assert bm.upload(gz) is True


# ---------------------------------------------------------- 5. main 退出码

def test_main_success_returns_0(sandbox, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["backup_memory.py"])
    monkeypatch.setattr(bm, "upload", lambda gz: True)

    assert bm.main() == 0

    rows = sandbox.log.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    import json

    row = json.loads(rows[0])
    assert row["ok"] is True
    assert row["file"].startswith("xiaoyu-")


def test_main_export_failure_returns_nonzero(sandbox, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["backup_memory.py"])
    monkeypatch.setattr(bm, "DB", sandbox.root / "missing.db")
    called = []
    monkeypatch.setattr(bm, "upload", lambda gz: called.append(gz) or True)

    assert bm.main() != 0
    assert called == []          # 没有备份就不该上传

    import json

    row = json.loads(sandbox.log.read_text(encoding="utf-8").splitlines()[0])
    assert row["ok"] is False
    assert row["step"] == "snapshot"


def test_main_dry_run_returns_0_without_uploading(sandbox, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["backup_memory.py", "--dry"])
    called = []
    monkeypatch.setattr(bm, "upload", lambda gz: called.append(gz) or True)

    assert bm.main() == 0
    assert called == []
    # 2026-11-14：dry 现在完全不落盘（只验证库能在备份），所以不该生成文件
    assert list(sandbox.backups.glob("xiaoyu-*.db.gz")) == []
