#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""抖音开关：她自己决定什么时候开抖音、什么时候关掉。

为什么需要它：抖音插件常驻时即使不刷也在空轮询（花算力、偶尔还烧 token）。
不刷的时候应当整个停掉，而不是挂着一个没人用的浏览器。

    python tools/douyin_control.py --status
    python tools/douyin_control.py --off     # 关：浏览器 + 同步轮询
    python tools/douyin_control.py --on      # 开：浏览器 + 同步轮询

睡眠时会自动调用 --off；醒来不会自动 --on（要她自己说想刷）。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "douyin_state.json"
PIDS = ROOT / "logs" / "pids"
EDGE_PORT = 9227


def write_state(enabled: bool, reason: str = "") -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    from datetime import datetime

    STATE.write_text(
        json.dumps(
            {"enabled": bool(enabled), "reason": reason, "at": datetime.now().astimezone().isoformat(timespec="seconds")},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def read_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"enabled": False, "reason": "未初始化"}


def find_browser_pids() -> list[int]:
    try:
        import psutil
    except ImportError:
        return []
    pids = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmd = " ".join(proc.info.get("cmdline") or [])
        except Exception:  # noqa: BLE001
            continue
        if "--remote-debugging-port=%d" % EDGE_PORT in cmd or f"localhost:{EDGE_PORT}" in cmd:
            pids.append(proc.info["pid"])
    return pids


def sync_pid() -> int | None:
    """先看 pid 文件，再看实际进程（pid 文件经常缺失）。"""
    f = PIDS / "sync_from_douyin.pid"
    try:
        pid = int(f.read_text(encoding="utf-8").strip())
        import psutil

        if psutil.pid_exists(pid):
            return pid
    except Exception:  # noqa: BLE001
        pass
    try:
        import psutil
    except ImportError:
        return None
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = " ".join(proc.info.get("cmdline") or [])
        except Exception:  # noqa: BLE001
            continue
        if "sync_from_douyin.py" in cmd:
            return proc.info["pid"]
    return None


def status() -> dict:
    pids = find_browser_pids()
    return {
        "enabled": bool(read_state().get("enabled")),
        "browser_processes": len(pids),
        "sync_watch": bool(sync_pid()),
        "reason": read_state().get("reason", ""),
    }


def turn_off(reason: str = "manual") -> dict:
    killed = 0
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        for pid in find_browser_pids():
            try:
                psutil.Process(pid).kill()
                killed += 1
            except Exception:  # noqa: BLE001
                pass
        pid = sync_pid()
        if pid:
            try:
                psutil.Process(pid).kill()
            except Exception:  # noqa: BLE001
                pass
    try:
        (PIDS / "sync_from_douyin.pid").unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass
    write_state(False, reason)
    return {"ok": True, "action": "off", "killed": killed, "status": status()}


def turn_on(reason: str = "manual") -> dict:
    """只拉起同步轮询；浏览器由 DSH 的抖音插件自己按需启动。"""
    if sync_pid() is None:
        try:
            subprocess.Popen(
                [sys.executable, str(ROOT / "tools" / "sync_from_douyin.py"), "--watch", "300"],
                cwd=str(ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:  # noqa: BLE001
            pass
    write_state(True, reason)
    return {"ok": True, "action": "on", "status": status()}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--on", action="store_true")
    group.add_argument("--off", action="store_true")
    group.add_argument("--status", action="store_true")
    parser.add_argument("--reason", default="manual")
    args = parser.parse_args()

    if args.status:
        result = status()
    elif args.off:
        result = turn_off(args.reason)
    else:
        result = turn_on(args.reason)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
