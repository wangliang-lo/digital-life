"""中枢：替她看环境、接住她的修复请求（2026-11-11）。

爸爸定的边界：
* 中枢**只能诊断和提案**，改代码必须经爸爸同意；
* 她自己发现问题时，用 [FIX_REQUEST: …] 提出来，请求排进队列等他批。

接口：
    diagnose()                 跑一轮环境自检，返回问题清单
    propose(...)               把一条请求写进队列（她自己提的，或中枢自己发现的）
    list_requests(status)      看队列
    approve(rid) / reject(rid) 爸爸拍板
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

DATA = Path(__file__).resolve().parent / "data"
REQUESTS = DATA / "ops_requests.jsonl"
STATE = DATA / "ops_state.json"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def _port_open(port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _processes() -> list[str]:
    """当前跑着的、和她有关的命令行。"""
    try:
        import psutil  # noqa: PLC0415

        lines = []
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmd = " ".join(proc.info.get("cmdline") or [])
            except Exception:  # noqa: BLE001
                continue
            if any(k in cmd for k in ("dl_api.py", "web_ui.py", "solver_channel.py",
                                      "pending_replay.py", "tts_server.py",
                                      "life_loop.py", "life_supervisor.ps1")):
                lines.append(cmd)
        return lines
    except Exception:  # noqa: BLE001
        return []


def _tail_errors(path: Path, limit: int = 400) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore").splitlines()[-limit:]
    except OSError:
        return []
    bad = []
    for line in text:
        if "Traceback" in line or "ERROR" in line or "Exception" in line:
            # 已知的无害噪声
            if any(skip in line for skip in ("ConnectionResetError", "WinError 10054", "IncompleteRead")):
                continue
            bad.append(line.strip()[:160])
    return bad


def diagnose() -> dict[str, Any]:
    """跑一轮自检。每一条都能落到"哪个文件/哪个进程/多少秒"上。"""
    findings: list[dict[str, Any]] = []

    def add(key: str, severity: str, title: str, detail: str, fix: str) -> None:
        findings.append({
            "key": key,
            "severity": severity,
            "title": title,
            "detail": detail,
            "fix": fix,
        })

    # 1. 端口
    for port, name in ((8000, "她的大脑"), (8899, "网页端"), (8003, "协议桥")):
        if not _port_open(port):
            add(f"port_{port}", "high", f"{name}（{port}）没在听",
                f"127.0.0.1:{port} 连不上，所有走这个口的操作都会失败。",
                "重启小鱼.cmd")

    # 2. 后台循环
    procs = _processes()
    joined = "\n".join(procs)
    for needle, name in (("solver_channel.py", "信道循环"), ("pending_replay.py", "补发循环")):

        if needle not in joined:
            add(f"proc_{needle}", "medium", f"{name}没在跑",
                f"没找到 {needle} 的进程，她不会被叫醒。", "重启小鱼.cmd")

    # 2b. 生活循环：进程 + 心跳（2026-11-11 爸爸："监测保证循环没挂"）
    if "life_loop.py" not in joined and "life_supervisor.ps1" not in joined:
        add("proc_life_loop", "high", "生活循环没在跑",
            "life_loop.py / life_supervisor.ps1 都不在进程里，她不会自己换活动、换房间。",
            "重启小鱼.cmd")
    else:
        heartbeat = Path(__file__).resolve().parent / "data" / "life_heartbeat.json"
        age = None
        try:
            import json as _json  # noqa: PLC0415
            from datetime import datetime as _dt  # noqa: PLC0415

            info = _json.loads(heartbeat.read_text(encoding="utf-8")) or {}
            stamp = str(info.get("at") or "")
            if stamp:
                when = _dt.fromisoformat(stamp)
                if when.tzinfo is None:
                    when = when.astimezone()
                age = (_dt.now().astimezone() - when).total_seconds() / 60.0
        except Exception:  # noqa: BLE001
            age = None
        if age is None:
            add("life_heartbeat_missing", "medium", "生活循环没有心跳文件",
                "data/life_heartbeat.json 还没生成，可能刚重启还没跳第一下。",
                "等一分钟再看；一直在就重启")
        elif age > 3:
            add("life_heartbeat_stale", "high", "生活循环心跳停了",
                f"心跳已经 {age:.0f} 分钟没更新（阈值 3 分钟），循环可能卡住或假死。",
                "重启小鱼.cmd")

    # 3. 唤醒间隔和启动脚本对不上（今天真的踩过：脚本里改了，跑着的是旧的）
    watch = next((line for line in procs if "solver_channel.py" in line and "--watch" in line), "")
    if watch:
        current = ""
        parts = watch.split()
        if "--watch" in parts:
            index = parts.index("--watch")
            current = parts[index + 1] if index + 1 < len(parts) else ""
        script = (Path(__file__).resolve().parent / "start_all.ps1")
        expected = ""
        try:
            text = script.read_text(encoding="utf-8")
            marker = "solver_channel.py', '--watch', '"
            if marker in text:
                expected = text.split(marker)[1].split("'")[0]
        except OSError:
            expected = ""
        if current and expected and current != expected:
            add("watch_interval", "medium",
                f"信道循环还是旧间隔（{current} 秒，启动脚本里是 {expected} 秒）",
                "跑着的进程是重启前的，改过的参数没生效，token 会多花一倍。",
                "重启小鱼.cmd")

    # 4. 日志里的报错
    logs = Path(__file__).resolve().parent / "logs"
    for name in ("dl_api.out.log", "web_ui.out.log"):
        errors = _tail_errors(logs / name)
        if errors:
            add(f"log_{name}", "low", f"{name} 里有 {len(errors)} 条报错",
                errors[-1], "看日志，必要时重启")

    # 5. 磁盘
    try:
        import shutil as _shutil  # noqa: PLC0415

        free_gb = _shutil.disk_usage("C:/").free / 1024 ** 3
        if free_gb < 5:
            add("disk", "high", f"C 盘只剩 {free_gb:.1f} GB",
                "训练/日志/模型都会写盘，空间不足会写出半截文件。", "清理 _backups 与旧日志")
    except Exception:  # noqa: BLE001
        pass

    # 6. IM登录态（2026-11-11：用真的那条接口，别再静默跳过）
    try:
        from chat_send import _account as _wx_account  # noqa: PLC0415

        if not _wx_account():
            add("chat_login", "medium", "IM没登录",
                "所有IM方向的话都发不出去，她会以为发成功了。",
                "在IM 助手窗口重新扫码")
    except Exception as exc:  # noqa: BLE001
        add("chat_check", "low", "IM登录状态查不了",
            f"{type(exc).__name__}: {exc}", "检查 protocol bridge 是否在跑")

    # 7. 睡眠账本是不是卡住了
    sleep = _read_json(DATA / "sleep_state.json", {}) or {}
    updated = str(sleep.get("updated_at") or "")
    if updated:
        try:
            age_h = (datetime.now().astimezone() - datetime.fromisoformat(updated)).total_seconds() / 3600
            if age_h > 12:
                add("sleep_stale", "low", f"睡眠账本 {age_h:.0f} 小时没更新",
                    f"最后更新 {updated}，可能她的作息没被记录。", "查看 sleep_state.json")
        except ValueError:
            pass

    # 8. 待批准的请求
    pending = [row for row in _all_requests() if row.get("status") == "pending"]
    if pending:
        add("pending_requests", "info", f"有 {len(pending)} 条修复请求等你批",
            pending[-1].get("title", ""), "看 /ops")

    high = sum(1 for f in findings if f["severity"] == "high")
    return {
        "ok": True,
        "at": _now(),
        "checked": ["端口", "后台循环", "唤醒间隔", "日志报错", "磁盘", "IM登录", "睡眠账本", "待批请求"],
        "findings": findings,
        "high": high,
        "medium": sum(1 for f in findings if f["severity"] == "medium"),
        "healthy": high == 0,
    }


def _all_requests() -> list[dict[str, Any]]:
    if not REQUESTS.exists():
        return []
    rows = []
    for line in REQUESTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _append(row: dict[str, Any]) -> None:
    REQUESTS.parent.mkdir(parents=True, exist_ok=True)
    with REQUESTS.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _rewrite(rows: list[dict[str, Any]]) -> None:
    REQUESTS.parent.mkdir(parents=True, exist_ok=True)
    with REQUESTS.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def propose(title: str, detail: str = "", source: str = "her", kind: str = "fix",
            evidence: str = "") -> dict[str, Any]:
    """提一条修复请求。同一个人重复提同一件事，只留一条。"""
    title = str(title or "").strip()[:120]
    if not title:
        return {"ok": False, "error": "标题不能为空"}
    rows = _all_requests()
    for row in rows:
        if row.get("status") == "pending" and row.get("title") == title:
            return {"ok": True, "duplicate": True, "id": row.get("id")}
    row = {
        "id": uuid.uuid4().hex[:8],
        "at": _now(),
        "source": source,
        "kind": kind,
        "title": title,
        "detail": str(detail or "")[:600],
        "evidence": str(evidence or "")[:600],
        "status": "pending",
        "decided_at": "",
        "decided_by": "",
        "note": "",
    }
    _append(row)
    return {"ok": True, "id": row["id"], "row": row}


def list_requests(status: str = "") -> list[dict[str, Any]]:
    rows = _all_requests()
    if status:
        rows = [r for r in rows if r.get("status") == status]
    return rows[-50:]


def decide(rid: str, approve: bool, by: str = "爸爸", note: str = "") -> dict[str, Any]:
    """爸爸拍板。同意 = 允许我改；不同意 = 归档。"""
    rows = _all_requests()
    hit = None
    for row in rows:
        if row.get("id") == rid:
            row["status"] = "approved" if approve else "rejected"
            row["decided_at"] = _now()
            row["decided_by"] = str(by)
            row["note"] = str(note or "")[:300]
            hit = row
            break
    if hit is None:
        return {"ok": False, "error": "没有这条请求"}
    _rewrite(rows)
    return {"ok": True, "row": hit}


def pending_count() -> int:
    return sum(1 for row in _all_requests() if row.get("status") == "pending")


def describe_for_her(limit: int = 3) -> str:
    """给她的那句：现在环境有什么问题、要不要跟她爸爸说。"""
    try:
        report = diagnose()
    except Exception:  # noqa: BLE001
        return ""
    findings = [f for f in report["findings"] if f["severity"] in ("high", "medium")]
    if not findings:
        return "\n【系统状况】一切正常，没什么要说的。"
    lines = "；".join(f"{f['title']}" for f in findings[:limit])
    return (
        "\n【系统状况】" + lines
        + "。你要是发现了问题，可以写 [FIX_REQUEST: 具体什么问题]，"
          "我看到了会拿给他批——你不能自己改代码。"
    )
