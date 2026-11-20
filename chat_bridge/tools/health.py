#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""小鱼整体健康检查（正常 / 降级 / 故障）。

    python tools/health.py            # 人类可读
    python tools/health.py --json     # 给监控用

设计原则（跟 ChatGPT 那份建议一致）：不同组件不能一刀切。
IM没开 ≠ 故障（账号封着），抖音没开 ≠ 故障（她睡了），
但主服务挂了、记忆库读不了，就是故障。
"""
from __future__ import annotations

import argparse
import json
import socket
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WS = Path(r"C:\Users\Lenovo\Desktop\aihuman\data\workspace")

OK, DEGRADED, FAIL = "ok", "degraded", "fail"


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def http_json(url: str, headers: dict | None = None, timeout: float = 3.0) -> dict | None:
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def check() -> list[dict]:
    items: list[dict] = []

    # 1. 她的本体
    api = http_json("http://127.0.0.1:8000/status")
    items.append({
        "name": "数字生命 dl_api",
        "status": OK if api and api.get("ok") else FAIL,
        "detail": f"users={api.get('user_count')}" if api else "8000 无响应",
    })

    # 2. 网页端
    web = port_open(8899)
    items.append({"name": "网页端 web_ui", "status": OK if web else DEGRADED, "detail": "8899" if web else "8899 未监听"})

    # 3. DSH 社交插件
    token_file = WS / "state" / "social-dsh-token.txt"
    plugin = None
    if token_file.exists():
        token = token_file.read_text(encoding="utf-8").strip()
        plugin = http_json("http://127.0.0.1:11230/health", {"authorization": "Bearer " + token})
    items.append({
        "name": "DSH 社交插件",
        "status": OK if plugin and plugin.get("ok") else DEGRADED,
        "detail": "11230" if plugin else "未启动（IM/抖音都靠它）",
    })

    # 4. 记忆库
    db = ROOT / "data" / "xiaoyu.db"
    db_status, db_detail = FAIL, "找不到数据库"
    if db.exists():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                books = con.execute("SELECT COUNT(*) FROM books").fetchone()[0]
                traces = con.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
                db_status = OK
                db_detail = f"记忆 {books} 条 / 对话 {traces} 轮"
            finally:
                con.close()
        except Exception as exc:  # noqa: BLE001
            db_detail = f"读不了：{type(exc).__name__}"
    items.append({"name": "记忆库", "status": db_status, "detail": db_detail})

    # 5. 最近一次对话
    last = http_json("http://127.0.0.1:8000/status") or {}
    trace_file = ROOT / "data" / "decision_trace.jsonl"
    trace_note = "还没有记录"
    trace_status = DEGRADED
    if trace_file.exists():
        try:
            tail = trace_file.read_text(encoding="utf-8").strip().splitlines()[-1]
            at = datetime.fromisoformat(json.loads(tail)["at"])
            age = (datetime.now().astimezone() - at).total_seconds() / 60
            trace_status = OK if age < 180 else DEGRADED
            trace_note = f"{int(age)} 分钟前有过一轮对话"
        except Exception:  # noqa: BLE001
            trace_note = "追踪文件损坏"
    items.append({"name": "最近对话", "status": trace_status, "detail": trace_note})

    # 6. 睡眠状态
    sleep = read_json(ROOT / "data" / "sleep_state.json")
    asleep = bool(sleep.get("asleep"))
    items.append({
        "name": "睡眠",
        "status": OK,
        "detail": f"在睡（到 {sleep.get('until','?')}）" if asleep else "醒着",
    })

    # 7. 激素（跟"此刻的生物钟基线"比，而不是跟固定的 1.0 比）
    hormones = read_json(ROOT / "data" / "hormones.json").get("values") or {}
    try:
        sys.path.insert(0, str(ROOT))
        import HORMONE  # noqa: PLC0415

        now = datetime.now().astimezone()
        pressure = HORMONE.sleep_pressure(now)
        off = [
            f"{k}={float(v):.2f}(基线{HORMONE.circadian_baseline(k, now):.2f})"
            for k, v in hormones.items()
            # 0.45 是她"活动能顶到的上限"（HORMONE 里的基线上下界），
            # 超过 0.50 才算真跑偏，不然每次都误报"顶格"。
            if k in HORMONE.SPEC and float(v) - HORMONE.circadian_baseline(k, now) > 0.50
        ]
        items.append({
            "name": "激素",
            "status": DEGRADED if off else OK,
            "detail": ("明显偏离生物钟：" + "、".join(off)) if off else
                      (f"跟生物钟一致；睡眠压力 {pressure:.2f}；" +
                       "；".join(f"{k}={float(v):.2f}" for k, v in list(hormones.items())[:3]))
                      if hormones else "未初始化",
        })
    except Exception as exc:  # noqa: BLE001
        items.append({"name": "激素", "status": DEGRADED,
                      "detail": "；".join(f"{k}={float(v):.2f}" for k, v in list(hormones.items())[:3])
                                or f"读不出来（{type(exc).__name__}）"})

    # 8. 抖音（关着不算故障）
    douyin = read_json(ROOT / "data" / "douyin_state.json")
    items.append({
        "name": "抖音",
        "status": OK,
        "detail": ("开着" if douyin.get("enabled") else "关着（省电，她要用会自己开）") + f" · {douyin.get('reason','')}",
    })

    # 9. IM（封号期间关着不算故障；但"端口开着、hook 没连"要能看出来）
    hook = port_open(30001)
    onebot = port_open(11229)
    dsh_plugin = port_open(11230)
    missing = [name for name, up in (("hook(30001)", hook), ("onebot(11229)", onebot),
                                     ("dshPlugin(11230)", dsh_plugin)) if not up]
    if not missing:
        wx_status, wx_detail = OK, "已连接（hook / onebot / dshPlugin 都在）"
    elif not any((hook, onebot, dsh_plugin)):
        wx_status, wx_detail = DEGRADED, "未启动（账号封禁或没开 IMAgent 时正常）"
    else:
        wx_status, wx_detail = DEGRADED, "只连了一半，缺：" + "、".join(missing)
    items.append({"name": "IM桥接", "status": wx_status, "detail": wx_detail})

    # 10. 给IM那个 agent 换脑子用的协议桥（8003，没启用时关着是正常的）
    bridge = http_json("http://127.0.0.1:8003/", timeout=2.0)
    items.append({
        "name": "协议桥 8003",
        "status": OK if bridge and bridge.get("service") == "anthropic-bridge" else DEGRADED,
        "detail": "在跑（IM换成她时用得到）" if bridge else "没跑（没启用IM=小鱼时正常）",
    })

    # 11. 看护（每 60 秒拉起一次 dl_api/web_ui；今晚就是它没拉起来才发现 BOM 问题）
    lock = ROOT / "logs" / "watchdog.lock"
    items.append({
        "name": "看护 watchdog",
        "status": OK if lock.exists() and api and api.get("ok") else DEGRADED,
        "detail": "在看着（lock 在、8000 正常）" if lock.exists() and api and api.get("ok")
                  else ("没找到 watchdog.lock，看护可能没开" if not lock.exists() else "看护在，但 8000 不正常"),
    })

    return items


ICON = {OK: "✅", DEGRADED: "⚠️ ", FAIL: "❌"}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    items = check()
    worst = FAIL if any(i["status"] == FAIL for i in items) else (
        DEGRADED if any(i["status"] == DEGRADED for i in items) else OK
    )
    if args.json:
        print(json.dumps({"overall": worst, "items": items}, ensure_ascii=False, indent=2))
    else:
        print(f"小鱼健康检查 · {datetime.now():%Y-%m-%d %H:%M}")
        for item in items:
            print(f"  {ICON[item['status']]} {item['name']:<14} {item['detail']}")
        print(f"\n总体：{ {'ok':'正常','degraded':'有降级项','fail':'有故障'}[worst] }")
    return 0 if worst != FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
