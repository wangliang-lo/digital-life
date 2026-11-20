#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""抖音专用浏览器的电源管理：平时关着，要用才开。

2026-11-08 爸爸："抖音让她在选择打开时再打开，平常关掉就行别浪费电。"

背景（很重要）：
- 专用浏览器（Edge，CDP 9227）以前 24 小时开着，19 个进程。
- 插件里的 `endpoint()` 会**自动拉起**浏览器：只要有东西去连 9227，
  发现没开就会立刻启动。所以"关掉"必须同时保证**没有私信服务在轮询**
  （私信服务每 15 秒连一次，关了浏览器会立刻被拉回来）。

用法：
    douyin_power.py status            看一眼：开没开、闲多久
    douyin_power.py close             关掉（先停浏览器进程树）
    douyin_power.py open              打开（起专用浏览器，等 CDP 就绪）
    douyin_power.py supervise         空闲超过 N 分钟就关（给定时任务用）
    douyin_power.py idle-minutes      只输出空闲分钟数

退出码：0 正常；supervise 关了浏览器时输出 `closed`。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PORT = 9227
CDP = f"http://127.0.0.1:{PORT}"
IDLE_MINUTES = 10          # 连续这么多分钟没人用就关
PLUGIN = Path.home() / ".dsh" / "profiles" / "desktop" / "node_modules" / "dsh-douyin-browser-local"
# 2026-11-08：专用浏览器的 profile / control 由插件 runtime-paths.mjs 决定，
# 是 ~/.dsh/douyin-browser/{cli-profile,control}，**不是**插件目录下的 control。
# 一开始我按插件目录写，起出来的浏览器 profile 叫 "douyin"，插件会认为 9227 不属于它。
RUNTIME = Path.home() / ".dsh" / "douyin-browser"
PROFILE_DIR = RUNTIME / "cli-profile"
CONTROL = RUNTIME / "control"
STATE = PLUGIN / "state"
WATCH_LOCK = STATE / "watch-loop.lock"
CHAT_EVENTS = STATE / "chat-events.jsonl"


def cdp_alive() -> bool:
    try:
        with urllib.request.urlopen(CDP + "/json/version", timeout=3) as resp:
            return bool(json.loads(resp.read().decode("utf-8")).get("Browser"))
    except (urllib.error.URLError, OSError, ValueError):
        return False


def listener_pid() -> int:
    """谁在监听 9227。"""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper().startswith("TCP") and f":{PORT}" in parts[1] \
                and parts[3].upper() == "LISTENING":
            try:
                return int(parts[4])
            except ValueError:
                continue
    return 0


def last_activity() -> float:
    """最近一次有人用抖音的时间（秒）。默认取状态目录里最新被写过的文件。"""
    newest = 0.0
    for path in list(STATE.glob("*.json")) + list(STATE.glob("*.jsonl")) + list(CONTROL.glob("*.json")):
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


def watching() -> bool:
    """她正在刷视频：有运行锁就绝不动它。"""
    if not WATCH_LOCK.exists():
        return False
    try:
        lock = json.loads(WATCH_LOCK.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True          # 读不出来也当在跑，宁可不关
    pid = int(lock.get("pid") or 0)
    if pid <= 0:
        return True
    try:
        import psutil  # noqa: PLC0415

        return psutil.pid_exists(pid)
    except Exception:  # noqa: BLE001
        return True


def idle_minutes() -> float:
    return max(0.0, (time.time() - last_activity()) / 60.0)


def close_browser() -> dict:
    pid = listener_pid()
    if not pid:
        return {"ok": True, "closed": False, "message": "浏览器本来就没开"}
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    for _ in range(10):
        time.sleep(0.5)
        if not cdp_alive():
            return {"ok": True, "closed": True, "pid": pid}
    return {"ok": False, "error": "杀了进程但 9227 还活着"}


def open_browser() -> dict:
    if cdp_alive():
        return {"ok": True, "already": True}
    host = PLUGIN / "browser-host.mjs"
    node = Path(r"C:\Program Files\nodejs\node.exe")
    if not host.exists():
        return {"ok": False, "error": f"找不到 {host}"}
    try:
        subprocess.Popen([str(node), str(host), str(PROFILE_DIR), str(CONTROL), str(PORT)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    for _ in range(60):
        time.sleep(1)
        if cdp_alive():
            return {"ok": True, "started": True}
    return {"ok": False, "error": "启动后 60 秒内 CDP 还没就绪"}


def main(argv: list[str]) -> int:
    action = (argv[0] if argv else "status").strip().lower()
    alive = cdp_alive()
    if action == "status":
        print(json.dumps({"browser": alive, "pid": listener_pid() if alive else 0,
                          "idle_minutes": round(idle_minutes(), 1), "watching": watching()},
                         ensure_ascii=False))
        return 0
    if action == "idle-minutes":
        print(round(idle_minutes(), 1))
        return 0
    if action == "open":
        print(json.dumps(open_browser(), ensure_ascii=False))
        return 0
    if action == "close":
        if watching():
            print(json.dumps({"ok": False, "error": "她正在刷，先不动"}, ensure_ascii=False))
            return 1
        print(json.dumps(close_browser(), ensure_ascii=False))
        return 0
    if action == "supervise":
        if not alive:
            print("closed" if False else "already-closed")
            return 0
        if watching():
            print("watching")
            return 0
        idle = idle_minutes()
        if idle < IDLE_MINUTES:
            print(f"busy {idle:.1f}m")
            return 0
        result = close_browser()
        print(json.dumps({**result, "idle_minutes": round(idle, 1)}, ensure_ascii=False))
        return 0
    print("用法: status | idle-minutes | open | close | supervise", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
