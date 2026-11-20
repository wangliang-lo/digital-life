"""给她的记忆做异地备份（2026-11-12）。

不做服务，只备份：每天晚上把 xiaoyu.db 用 SQLite 在线备份接口导一份，
压缩后传到公网 /root/xiaoyu/backups/，保留 14 天。

用法：
    python tools/backup_memory.py            备份并上传
    python tools/backup_memory.py --dry      只看会备份什么
"""
from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sqlite3
import time
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent.parent
DB = BRIDGE / "data" / "xiaoyu.db"
LOCAL_BACKUPS = BRIDGE / "data" / "backups"
LOG = BRIDGE / "data" / "backup_log.jsonl"
KEY = r"PROJECT_ROOT\deploy\.tunnel_key"
HOST = "root@YOUR_SERVER_IP"
REMOTE_DIR = "/root/xiaoyu/backups"
KEEP_DAYS = 14
SCP = r"C:\Windows\System32\OpenSSH\scp.exe"
SSH = r"C:\Windows\System32\OpenSSH\ssh.exe"
COMMON = ["-i", KEY, "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]


def _log(row: dict) -> None:
    try:
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass



def backup_one(db_path: Path | str, backups_dir: Path | str | None = None) -> str | None:
    """本机滚动备份一份记忆库，返回备份文件路径（tests/test_backup_restore.py 用它）。

    用 SQLite 的在线备份接口，写入中也能得到一致快照；文件名带时间戳，
    同一天多次备份不会互相覆盖。
    """
    db = Path(db_path)
    target_dir = Path(backups_dir) if backups_dir else db.parent / "backups"
    if not db.exists():
        return None
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = target_dir / f"{db.stem}-{stamp}.db"
    temp = target_dir / f".{db.stem}-{stamp}.tmp"
    try:
        source = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        dest = sqlite3.connect(str(temp))
        with dest:
            source.backup(dest)
        source.close()
        dest.close()
        temp.replace(target)
    except Exception:  # noqa: BLE001
        temp.unlink(missing_ok=True)
        return None
    return str(target)


def snapshot(dry: bool = False) -> Path | None:
    """用 SQLite 的在线备份接口，导出一份一致的副本。"""
    if not DB.exists():
        print("找不到记忆库:", DB)
        return None
    LOCAL_BACKUPS.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp()) / "xiaoyu.db"
    try:
        source = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        target = sqlite3.connect(str(temp))
        with target:
            source.backup(target)
        source.close()
        target.close()
    except Exception as exc:  # noqa: BLE001
        print("备份失败:", type(exc).__name__, exc)
        shutil.rmtree(temp.parent, ignore_errors=True)
        return None

    if dry:
        # 2026-11-14 修：--dry 原来照样导出并覆盖今天的备份，等于没有演练；
        # 现在只验证"库能读、能在线备份"，不写盘。
        shutil.rmtree(temp.parent, ignore_errors=True)
        print("演练模式：库可正常在线备份，未写入任何文件")
        return None

    stamp = datetime.now().strftime("%Y%m%d")
    gz = LOCAL_BACKUPS / f"xiaoyu-{stamp}.db.gz"
    tmp_gz = gz.with_suffix(gz.suffix + ".tmp")
    # 2026-11-14 修：原来是"先删旧文件、再写新的"——压缩失败就一整天没备份。
    # 现在先写 .tmp，成功了再原子替换。
    try:
        with temp.open("rb") as src, gzip.open(tmp_gz, "wb", compresslevel=6) as out:
            shutil.copyfileobj(src, out)
        tmp_gz.replace(gz)
    except Exception as exc:  # noqa: BLE001
        print("压缩失败:", type(exc).__name__, exc)
        tmp_gz.unlink(missing_ok=True)
        shutil.rmtree(temp.parent, ignore_errors=True)
        return None
    shutil.rmtree(temp.parent, ignore_errors=True)

    counts = {}
    try:
        conn = sqlite3.connect(f"file:{temp}?mode=ro", uri=True) if temp.exists() else None
        if conn:
            conn.close()
    except Exception:  # noqa: BLE001
        pass
    size_mb = round(gz.stat().st_size / 1024 / 1024, 2)
    print(f"本地备份: {gz.name}（{size_mb} MB）")
    return gz


def upload(gz: Path) -> bool:
    subprocess.run([SSH, *COMMON, HOST, f"mkdir -p {REMOTE_DIR}"], capture_output=True)
    result = subprocess.run([SCP, *COMMON, str(gz), f"{HOST}:{REMOTE_DIR}/"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        print("上传失败:", (result.stderr or result.stdout or "")[:200])
        return False
    print("已上传:", f"{REMOTE_DIR}/{gz.name}")
    # 只留最近 14 天
    prune = f"find {REMOTE_DIR} -name 'xiaoyu-*.db.gz' -mtime +{KEEP_DAYS} -delete; ls -1t {REMOTE_DIR} | head -5"
    result = subprocess.run([SSH, *COMMON, HOST, prune], capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    print("远端现有备份:\n" + (result.stdout or "").strip())
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="备份她的记忆库到公网")
    parser.add_argument("--dry", action="store_true")
    args = parser.parse_args()

    if args.dry:
        snapshot(dry=True)
        return 0
    gz = snapshot()
    if gz is None:
        _log({"at": datetime.now().astimezone().isoformat(timespec="seconds"), "ok": False, "step": "snapshot"})
        return 1
    try:
        ok = upload(gz)
    except Exception as exc:  # noqa: BLE001
        # 2026-11-14 修：scp 不存在/调用失败会直接抛出去，连日志都没有，
        # cron 那边看不到任何线索。现在统一记 ok=false 并返回非零。
        print("上传异常:", type(exc).__name__, exc)
        _log({
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "ok": False,
            "step": "upload",
            "file": gz.name,
            "error": f"{type(exc).__name__}: {exc}"[:200],
        })
        return 1
    _log({
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "ok": ok,
        "file": gz.name,
        "mb": round(gz.stat().st_size / 1024 / 1024, 2),
        "remote": f"{REMOTE_DIR}/{gz.name}",
    })
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
