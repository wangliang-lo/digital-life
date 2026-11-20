#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""抖音浏览器看护：她要刷抖音时 9227 必须活着。

2026-11-04 停电后 9227 没起来，她连着两次说"路没通"。
dl_api 里已经改成按需拉起，但那个改动要重启才生效；在那之前先由这个小看护顶上。

    python tools/douyin_watchdog.py --interval 60
"""
from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STATE = ROOT / "data" / "douyin_state.json"
SCRIPT = ROOT / "tools" / "start_douyin_browser.mjs"


def alive(timeout: float = 1.2) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 9227), timeout=timeout):
            return True
    except OSError:
        return False


def enabled() -> bool:
    try:
        return bool(json.loads(STATE.read_text(encoding="utf-8")).get("enabled"))
    except Exception:  # noqa: BLE001
        return False


def launch() -> None:
    node = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"
    subprocess.Popen([node, str(SCRIPT)], cwd=str(ROOT),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--once", action="store_true", help="只检查一次就退出（给计划任务用）")
    parser.add_argument("--force", action="store_true", help="忽略省电模式，强制看护预启动")
    args = parser.parse_args(argv)

    # 2026-11-08 爸爸："抖音让她在选择打开时再打开，平常关掉就行别浪费电。"
    # 这个看护以前每 5 分钟就把专用浏览器拉起来（哪怕没人用），是 24 小时开着的根因。
    # 现在默认"按需启动"：插件在真的要用抖音时自己会拉起浏览器（core.mjs 的 endpoint()），
    # 所以预启动没必要。想恢复老的常驻行为，删掉这个标志文件就行。
    power_off_flag = ROOT / "data" / "douyin_power_off.flag"
    if power_off_flag.exists() and not args.force:
        print("省电模式：浏览器按需启动，看护跳过预启动。", flush=True)
        return 0

    def check_once() -> None:
        if enabled() and not alive():
            print("9227 不在，拉起浏览器……", flush=True)
            launch()
            deadline = time.time() + 45
            while time.time() < deadline and not alive():
                time.sleep(1.5)
            print("就绪" if alive() else "拉起超时", flush=True)

    if args.once:
        check_once()
        return 0

    print(f"抖音浏览器看护启动（每 {args.interval}s 检查一次）", flush=True)
    while True:
        try:
            if enabled() and not alive():
                print("9227 不在，拉起浏览器……", flush=True)
                launch()
                deadline = time.time() + 45
                while time.time() < deadline and not alive():
                    time.sleep(1.5)
                print("就绪" if alive() else "拉起超时", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"看护出错：{type(exc).__name__}: {exc}", flush=True)
        time.sleep(max(10, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
