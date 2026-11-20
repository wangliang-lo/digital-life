#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""V1.0 测试期体检：一条命令看完她现在的运行状态和最近的异常。

    python tools/monitor.py            # 看一遍
    python tools/monitor.py --errors   # 只看异常
    python tools/monitor.py --json

检查项：
  1. 服务：dl_api / web_ui / 协议桥 / IM hook / ASR 是否在监听
  2. 日志：最近 300 行里有没有 Traceback / ERROR / "写入失败"
  3. 状态新鲜度：她多久没说话、指标多久没落、心跳文件是否正常
  4. 数据完整性：SQLite quick_check、待回队列里有没有探针残留
  5. 今日活动量：对话轮数、频道留言、走神、复盘探针

退出码：0 正常；1 有需要人看一眼的项。
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent          # chat_bridge
DATA = ROOT / "data"
LOGS = ROOT / "logs"
DB = DATA / "xiaoyu.db"

PORTS = {8000: "数字生命 dl_api", 8003: "协议桥", 8899: "网页端", 30001: "IM hook",
         11229: "IM onebot", 8005: "本地 ASR", 11434: "Ollama", 9227: "抖音浏览器"}
ERROR_MARKS = ("Traceback", "ERROR", "写入失败", "未捕获", "Unhandled")
# 客户端提前断开是常态，不是 bug（2026-11-05 体检时被这条噪音骗过一次）
NOISE_MARKS = ("ConnectionResetError", "ConnectionAbortedError", "BrokenPipeError",
               "ConnectionReset", "WinError 10054")


def _listening() -> set[int]:
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             encoding="utf-8", errors="ignore").stdout
    except Exception:  # noqa: BLE001
        return set()
    ports = set()
    for line in out.splitlines():
        if "LISTENING" not in line:
            continue
        for port in PORTS:
            if f":{port} " in line:
                ports.add(port)
    return ports


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _minutes_since(stamp: str) -> float:
    try:
        at = datetime.fromisoformat(str(stamp))
        if at.tzinfo is None:
            at = at.astimezone()
        return (datetime.now().astimezone() - at).total_seconds() / 60.0
    except Exception:  # noqa: BLE001
        return -1.0


def scan_logs() -> list[str]:
    hits: list[str] = []
    if not LOGS.exists():
        return hits
    for path in sorted(LOGS.glob("*.log")):
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()[-300:]
        except OSError:
            continue
        # 按"Traceback 块"看整段，块里只要出现已知噪音就整段跳过
        block: list[str] = []

        def flush(block_lines: list[str]) -> None:
            if not block_lines:
                return
            joined = "\n".join(block_lines)
            if any(mark in joined for mark in NOISE_MARKS):
                return
            first = block_lines[0].strip()[:150]
            hits.append(f"{path.name}: {first}")

        for line in lines:
            if "Traceback" in line:
                flush(block)
                block = [line]
            elif block:
                block.append(line)
            elif any(mark in line for mark in ERROR_MARKS) and not any(
                    mark in line for mark in NOISE_MARKS):
                hits.append(f"{path.name}: {line.strip()[:150]}")
        flush(block)
    return hits[-12:]


def web_js_check() -> tuple[bool, str]:
    """网页端的 JS 能不能过语法检查（页面打不开/连不上，最常见就是这里崩）。"""
    index = ROOT / "web" / "index.html"
    if not index.exists():
        return False, "web/index.html 不见了"
    try:
        html = index.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return False, f"读不了：{exc}"
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    if not scripts:
        return True, "没有内联脚本"
    tmp = Path(r"PROJECT_ROOT\_tmp")
    tmp.mkdir(parents=True, exist_ok=True)
    target = tmp / "monitor_index_check.js"
    target.write_text("\n;\n".join(scripts), encoding="utf-8")
    try:
        proc = subprocess.run(["node", "--check", str(target)], capture_output=True,
                              text=True, encoding="utf-8", errors="ignore", timeout=30)
    except Exception as exc:  # noqa: BLE001
        return True, f"跳过（node 不可用：{type(exc).__name__}）"
    if proc.returncode == 0:
        return True, "语法正常"
    detail = (proc.stderr or "").strip().splitlines()[-1:] or ["未知错误"]
    return False, detail[0][:160]


def db_check() -> str:
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            result = con.execute("PRAGMA quick_check").fetchone()
        finally:
            con.close()
        return str((result or ["?"])[0])
    except Exception as exc:  # noqa: BLE001
        return f"打不开：{exc}"


def today_stats() -> dict:
    today = datetime.now().date().isoformat()
    stats: dict = {}
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            stats["turns"] = con.execute(
                "SELECT COUNT(*) FROM traces WHERE substr(created_at,1,10)=?", (today,)).fetchone()[0]
            row = con.execute(
                "SELECT MAX(created_at) FROM traces").fetchone()
            stats["last_talk"] = str((row or [""])[0] or "")
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        pass
    channel = [r for r in _jsonl(DATA / "solver_channel.jsonl") if str(r.get("at") or "").startswith(today)]
    stats["channel"] = sum(1 for r in channel if r.get("type") == "message")
    stats["silent"] = sum(1 for r in channel if r.get("type") == "silent")
    stats["wander"] = len([r for r in _jsonl(DATA / "wander_log.jsonl") if str(r.get("at") or "").startswith(today)])
    stats["probe"] = len([r for r in _jsonl(DATA / "introspection_log.jsonl") if str(r.get("at") or "").startswith(today)])
    return stats


def pending_waiting(path: Path | None = None) -> list[dict]:
    """待回队列里还没处理的记录（认 acked 文件和旧的 replied_at）。"""
    sys.path.append(str(Path(__file__).resolve().parent))
    import pending_queue  # noqa: PLC0415

    return pending_queue.list_pending(10**9, path=path or DATA / "pending_messages.jsonl")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--errors", action="store_true", help="只看异常")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    problems: list[str] = []
    listening = _listening()
    errors = scan_logs()
    stats = today_stats()
    check = db_check()

    aside = {
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "ports": {str(p): (p in listening) for p in PORTS},
        "errors": errors,
        "db": check,
        "stats": stats,
    }
    if args.json:
        print(json.dumps(aside, ensure_ascii=False, indent=2))
        return 0

    if args.errors:
        if not errors:
            print("最近日志里没有异常")
            return 0
        for line in errors:
            print(line)
        return 1

    print(f"小鱼 V1.0 体检 · {datetime.now():%m-%d %H:%M}\n")
    print("服务：")
    for port, name in PORTS.items():
        ok = port in listening
        mark = "✅" if ok else ("⚪" if port == 9227 else "❌")
        print(f"  {mark} {name:<14} {port}")
        if not ok and port in (8000, 8899, 8003):
            problems.append(f"{name}({port}) 没在监听")

    js_ok, js_detail = web_js_check()
    print(f"\n网页端 JS：{'✅' if js_ok else '❌'} {js_detail}")
    if not js_ok:
        problems.append(f"网页端 JS 语法错误：{js_detail}")

    print(f"\n数据库：{'✅' if check == 'ok' else '❌'} {check}")
    if check != "ok":
        problems.append(f"数据库 quick_check = {check}")

    last_talk = str(stats.get("last_talk") or "")
    if last_talk:
        idle = _minutes_since(last_talk)
        print(f"最近一轮对话：{last_talk[5:16]}（{idle:.0f} 分钟前）")
        if idle > 180:
            problems.append(f"她已经 {idle/60:.1f} 小时没有对话")
    print(f"今天：对话 {stats.get('turns', 0)} 轮 · 频道 {stats.get('channel', 0)} 条 · "
          f"沉默 {stats.get('silent', 0)} · 走神 {stats.get('wander', 0)} · 探针 {stats.get('probe', 0)}")

    # 指标新鲜度
    metrics = _jsonl(DATA / "metrics_hourly.jsonl")
    if metrics:
        age = _minutes_since(str(metrics[-1].get("at") or ""))
        flag = "✅" if 0 <= age <= 25 else "⚠️"
        print(f"指标最新一条：{flag} {age:.0f} 分钟前")
        if age > 30:
            problems.append(f"metrics_hourly 已经 {age:.0f} 分钟没更新（时钟可能停了）")
    else:
        print("指标：⚠️ 还没有数据（等第一次 10 分钟走表）")

    # 待回队列里的探针残留
    pending = pending_waiting()
    probes = [r for r in pending if str(r.get("text") or "") in ("阿哦是谁", "在吗")]
    if probes:
        problems.append(f"待回队列里有 {len(probes)} 条探针残留")
    print(f"待回队列：{len(pending)} 条" + ("（含探针残留 ⚠️）" if probes else ""))

    # 睡眠状态合理性
    try:
        sleep = json.loads((DATA / "sleep_state.json").read_text(encoding="utf-8"))
        updated = str(sleep.get("updated_at") or "")
        age = _minutes_since(updated) if updated else -1
        flag = "✅" if 0 <= age <= 60 * 24 else "⚠️"
        print(f"睡眠状态：{flag} asleep={sleep.get('asleep')}（{age/60:.1f} 小时前更新）")
        if age > 60 * 24:
            problems.append("sleep_state 超过一天没更新")
    except Exception:  # noqa: BLE001
        problems.append("sleep_state 读不出来")

    print("\n最近异常（最多 12 条）：")
    if errors:
        for line in errors:
            print("  " + line)
        problems.append(f"日志里有 {len(errors)} 条异常")
    else:
        print("  ✅ 没有")

    print()
    if problems:
        print("需要看一眼：")
        for item in problems:
            print("  ·", item)
        return 1
    print("总体：正常")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
