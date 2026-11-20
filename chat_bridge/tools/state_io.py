"""状态文件的原子读写（JSON / JSONL）。

- write_json：临时文件 + os.replace，进程被杀也不会留下半截文件；失败抛异常。
- write_jsonl_append：追加一行并 fsync，父目录自动建。
- read_json：读不到/坏 JSON 返回 default，不抛。
- update_json：读 dict → 合并 patch → 原子写回，带文件级锁（进程内线程锁 + 跨进程锁文件）。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}


def _thread_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(os.path.abspath(path))
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.RLock()
        return lock


class _FileLock:
    """线程锁 + 旁路锁文件（O_EXCL 创建），跨进程互斥；陈旧锁超时后抢占。"""

    def __init__(self, path: Path, timeout: float = 10.0, stale: float = 30.0) -> None:
        self.path = Path(path)
        self.lockfile = self.path.with_name(self.path.name + ".lock")
        self.timeout = timeout
        self.stale = stale
        self._tlock = _thread_lock(self.path)
        self._fd: int | None = None

    def __enter__(self) -> "_FileLock":
        self._tlock.acquire()
        try:
            self.lockfile.parent.mkdir(parents=True, exist_ok=True)
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    self._fd = os.open(str(self.lockfile), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    return self
                except FileExistsError:
                    try:
                        if time.time() - self.lockfile.stat().st_mtime > self.stale:
                            self.lockfile.unlink(missing_ok=True)
                            continue
                    except OSError:
                        pass
                    if time.monotonic() > deadline:
                        # 拿不到跨进程锁也不能卡死：退化为只有线程锁
                        self._fd = None
                        return self
                    time.sleep(0.01)
        except BaseException:
            self._tlock.release()
            raise

    def __exit__(self, *exc: Any) -> None:
        try:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
                try:
                    self.lockfile.unlink(missing_ok=True)
                except OSError:
                    pass
        finally:
            self._tlock.release()


def write_json(path: str | os.PathLike, data: Any, *, indent: int | None = 2) -> None:
    """原子写 JSON。失败抛异常（调用方决定记不记日志），旧文件保持原样。"""
    write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=indent) + "\n")


def write_text_atomic(path: str | os.PathLike, text: str) -> None:
    """原子写文本：临时文件 + fsync + os.replace。失败抛异常，旧文件保持原样。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        # Windows 上目标被读者短暂占用会 PermissionError，重试几次
        for attempt in range(20):
            try:
                os.replace(tmp_name, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.02)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_jsonl_append(path: str | os.PathLike, record: Any) -> None:
    """追加一行 JSON，flush + fsync；父目录不存在自动建。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with _thread_lock(path):
        with open(path, "a", encoding="utf-8", newline="") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())


def read_json(path: str | os.PathLike, default: Any = None) -> Any:
    """读 JSON；文件不存在 / 坏 JSON / 读失败 → 返回 default，不抛。"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def update_json(path: str | os.PathLike, patch: dict[str, Any]) -> dict[str, Any]:
    """读出 dict → 合并 patch → 原子写回（文件级锁，避免并发丢更新）。返回合并后的 dict。"""
    path = Path(path)
    with _FileLock(path):
        current = read_json(path, {})
        if not isinstance(current, dict):
            current = {}
        current.update(patch)
        write_json(path, current)
        return current
