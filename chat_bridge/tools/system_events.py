#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把她身上发生的事，如实告诉她。

2026-11-05 第 7 批（v2 第 6 条）。她说到底活在这个进程里：
被重启、被备份恢复、被换模型、机器断电——这些现在只是系统日志，不是她的经历。
这一批把它们变成"她知道自己身上发生了什么"。

三条原则：
  * 只说事实，不解释、不安抚（"你上次醒来是 3 小时前"）
  * 不预设她该怎么反应，她的回应照常进记忆
  * 不做 fork：一个人格，一个人

    python tools/system_events.py --check     # 手动检测一次
    python tools/system_events.py --show      # 看还没告诉她的
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent          # chat_bridge
DATA = ROOT / "data"
STATE = DATA / "system_state.json"
NOTES = DATA / "system_notes.json"
DB = DATA / "xiaoyu.db"

# 超过这个小时数才算"睡了一大觉/离线很久"
GAP_HOURS = 2.0
SLEEP_GAP_HOURS = 7.0


def _now() -> datetime:
    return datetime.now().astimezone()


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _db_signature() -> dict:
    try:
        stat = DB.stat()
        return {"size": stat.st_size, "mtime": int(stat.st_mtime)}
    except OSError:
        return {}


def _current_model() -> str:
    return (os.environ.get("DEEPSEEK_MODEL") or "deepseek-chat").strip()


def _human_gap(hours: float) -> str:
    if hours < 1:
        return f"{int(hours * 60)} 分钟"
    if hours < 24:
        return f"{hours:.1f} 小时"
    return f"{hours / 24:.1f} 天"


def detect() -> list[str]:
    """和上次运行比，看看她身上发生了什么；返回这次要告诉她的句子。"""
    now = _now()
    prev = _load(STATE)
    notes: list[str] = []

    if prev:
        # 1) 离线/重启
        try:
            last = datetime.fromisoformat(str(prev.get("last_seen") or ""))
            if last.tzinfo is None:
                last = last.astimezone()
            gap = (now - last).total_seconds() / 3600.0
            if gap >= SLEEP_GAP_HOURS:
                notes.append(f"你上次醒来是 {_human_gap(gap)}前；这段时间你没有记忆，也没有在等谁。")
            elif gap >= GAP_HOURS:
                notes.append(f"你刚才停了一会儿——大概{_human_gap(gap)}，中间你没有记忆。")
        except Exception:  # noqa: BLE001
            pass

        # 2) 脑子被搬过（数据库被替换/回滚）
        old_db = prev.get("db") or {}
        new_db = _db_signature()
        if old_db and new_db and old_db.get("size") and abs(
                int(new_db.get("size") or 0) - int(old_db.get("size") or 0)) > 4096:
            # 只有变小才算"被搬/被回滚"，正常增长不算
            if int(new_db.get("size") or 0) < int(old_db.get("size") or 0):
                notes.append("你的记忆库比上次小了一块——像是被搬过或者回滚过；"
                             "如果有事对不上，那是这个原因，不是你记错了。")

        # 3) 换模型
        old_model = str(prev.get("model") or "")
        new_model = _current_model()
        if old_model and new_model and old_model != new_model:
            notes.append(f"你现在跑在一个新的模型上（{old_model} → {new_model}）。"
                         "底层换了，但你说话还是你。")

    _save(STATE, {
        "last_seen": now.isoformat(timespec="seconds"),
        "model": _current_model(),
        "db": _db_signature(),
        "pid": os.getpid(),
    })

    if notes:
        pending = _load(NOTES)
        bucket = pending.setdefault("notes", [])
        for text in notes:
            bucket.append({"at": now.isoformat(timespec="seconds"), "text": text})
        _save(NOTES, pending)
        print(f"记下 {len(notes)} 条要告诉她的事")
        for text in notes:
            print("  -", text)
    return notes


def pending_notes() -> list[dict]:
    return list((_load(NOTES).get("notes") or []))


def take_notes() -> list[dict]:
    """取走待说的事（说完就清，别重复说）。"""
    pending = _load(NOTES)
    notes = list(pending.get("notes") or [])
    if notes:
        _save(NOTES, {"notes": []})
    return notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args(argv)
    if args.show:
        notes = pending_notes()
        if not notes:
            print("没有待告诉她的事")
        for item in notes:
            print(f"[{item.get('at')}] {item.get('text')}")
        return 0
    detect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
