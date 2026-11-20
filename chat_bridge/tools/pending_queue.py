# -*- coding: utf-8 -*-
"""待回消息队列：只追加、不改写历史，进程被杀 / 多线程并发都不丢消息。

文件（都在 data/ 下，旁边各一个）：
- pending_messages.jsonl          主队列，只 append（prune 例外：先归档再原子替换）
- pending_messages.acked.jsonl    已处理 id，只 append
- pending_messages.archive.jsonl  prune 移走的旧记录，只 append

旧格式兼容：旧记录没有 id，读取时按 at+sender+text 的哈希生成稳定 id（前缀 "L"）；
旧记录里的 replied_at 仍然算"已处理"。旧文件不会被改写或删除。
"""
from __future__ import annotations

import hashlib
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))  # 直接 import 时找得到 state_io
from state_io import _FileLock, write_jsonl_append, write_text_atomic  # noqa: E402

PENDING_FILE = Path(__file__).resolve().parent.parent / "data" / "pending_messages.jsonl"
DEDUP_SECONDS = 60.0


def acked_path(path: Path) -> Path:
    return path.with_name(path.stem + ".acked.jsonl")


def archive_path(path: Path) -> Path:
    return path.with_name(path.stem + ".archive.jsonl")


def _p(path) -> Path:
    return Path(path) if path else PENDING_FILE


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []


def _legacy_id(rec: dict) -> str:
    raw = f"{rec.get('at', '')}|{rec.get('sender', '')}|{rec.get('text', '')}"
    return "L" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _parse(line: str) -> dict | None:
    """有效记录 → dict（保证有 id）；空行 / 坏行 / 没有 text → None。"""
    line = line.strip()
    if not line:
        return None
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    if not isinstance(rec, dict) or not str(rec.get("text") or "").strip():
        return None
    if not rec.get("id"):
        rec["id"] = _legacy_id(rec)
    return rec


def _ts(rec: dict) -> float | None:
    if isinstance(rec.get("ts"), (int, float)):
        return float(rec["ts"])
    try:
        at = datetime.fromisoformat(str(rec.get("at") or ""))
        if at.tzinfo is None:
            at = at.astimezone()
        return at.timestamp()
    except ValueError:
        return None


def _acked_ids(path: Path) -> set[str]:
    ids: set[str] = set()
    for line in _read_lines(acked_path(path)):
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("id"):
            ids.add(str(item["id"]))
    return ids


def push(sender: str, text: str, reason: str = "", *, path=None) -> dict:
    """追加一条。60 秒内同 sender+text 重复 → 不再写，返回已有那条。"""
    path = _p(path)
    now = datetime.now().astimezone()
    sender = str(sender or "").strip()
    body = str(text or "").strip()[:2000]
    with _FileLock(path):  # 去重检查 + 追加 同一把锁，线程/进程间都互斥
        for line in reversed(_read_lines(path)[-200:]):
            old = _parse(line)
            if not old or old.get("sender") != sender or old.get("text") != body:
                continue
            ts = _ts(old)
            if ts is not None and 0 <= now.timestamp() - ts < DEDUP_SECONDS:
                return old
        record = {
            "id": uuid.uuid4().hex,
            "at": now.isoformat(timespec="seconds"),
            "ts": now.timestamp(),
            "sender": sender,
            "text": body,
            "reason": str(reason or "")[:80],
        }
        write_jsonl_append(path, record)
    return record


def list_pending(limit: int = 50, *, path=None) -> list[dict]:
    """最近 limit 条未处理的有效记录，新的在前。"""
    path = _p(path)
    acked = _acked_ids(path)
    out: list[dict] = []
    for line in reversed(_read_lines(path)):
        rec = _parse(line)
        if rec is None or rec["id"] in acked or rec.get("replied_at"):
            continue
        out.append(rec)
        if len(out) >= limit:
            break
    return out


def ack(record_id: str, *, path=None) -> None:
    """标记已处理：只往 acked 文件追加，主文件不动。"""
    path = _p(path)
    write_jsonl_append(
        acked_path(path),
        {"id": str(record_id), "at": datetime.now().astimezone().isoformat(timespec="seconds")},
    )


def pending_count(*, path=None) -> int:
    return len(list_pending(limit=10**9, path=path))


def prune(keep: int = 200, *, path=None) -> int:
    """主文件只留最后 keep 行，更早的先追加进 archive，再原子替换主文件。返回归档条数。

    顺序保证不丢：archive 写成功之后才替换主文件；中途崩溃最多是 archive 里多一份。
    坏行也归档（包成 {"_corrupt": 原文}），不静默吞掉。
    """
    path = _p(path)
    keep = max(0, int(keep))
    with _FileLock(path):
        lines = [ln for ln in _read_lines(path) if ln.strip()]
        if len(lines) <= keep:
            return 0
        old, recent = lines[: len(lines) - keep], lines[len(lines) - keep:]
        for line in old:
            rec = _parse(line)
            if rec is None:
                rec = {"_corrupt": line}
            write_jsonl_append(archive_path(path), rec)
        write_text_atomic(path, "".join(ln + "\n" for ln in recent))
    return len(old)
