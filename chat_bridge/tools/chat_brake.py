"""给IM加一个"风控模式"刹车（2026-11-08）。

背景：账号被风控，今天她发了 200 条（10 点那小时 55 条）。
风控解除前不要再高频发，也不要再发图片/表情。

用法：
    python tools/chat_brake.py on       打开风控模式（限速、禁图）
    python tools/chat_brake.py off      关掉
    python tools/chat_brake.py status   看一眼
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

DATA = Path(r"PROJECT_ROOT\chat_bridge\data")
FLAG = DATA / "chat_risk_mode.flag"
STATE = DATA / "chat_risk_state.json"

DEFAULT = {
    "since": "",
    "min_interval_seconds": 25,     # 两条消息之间至少隔 25 秒
    "max_per_hour": 20,             # 每小时最多 20 条
    "allow_image": False,           # 风控期间不发图片/表情
    "allow_group": False,           # 也不往群里发
}


def load() -> dict:
    try:
        data = json.loads(FLAG.read_text(encoding="utf-8"))
        return {**DEFAULT, **data}
    except (OSError, ValueError):
        return {}


def gate(kind: str, to: str = "", is_group: bool = False) -> str:
    """返回空字符串表示允许发送；否则返回拒绝原因。"""
    policy = load()
    if not policy:
        return ""
    if kind == "image" and not policy.get("allow_image", False):
        return "风控模式：暂时不发图片/表情"
    if is_group and not policy.get("allow_group", False):
        return "风控模式：暂时不往群里发"
    now = time.time()
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {"last": 0, "sent": []}
    sent = [float(t) for t in (state.get("sent") or []) if now - float(t) < 3600]
    if now - float(state.get("last") or 0) < float(policy.get("min_interval_seconds") or 0):
        return "风控模式：两条消息之间至少隔 {} 秒".format(policy.get("min_interval_seconds"))
    if len(sent) >= int(policy.get("max_per_hour") or 999):
        return "风控模式：这一小时已经发了 {} 条".format(len(sent))
    return ""


def record_sent() -> None:
    now = time.time()
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {"sent": []}
    sent = [float(t) for t in (state.get("sent") or []) if now - float(t) < 3600]
    sent.append(now)
    state["sent"] = sent[-200:]
    state["last"] = now
    try:
        STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def main(argv: list[str]) -> int:
    action = (argv[0] if argv else "status").lower()
    if action == "on":
        payload = {**DEFAULT, "since": time.strftime("%Y-%m-%dT%H:%M:%S")}
        FLAG.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("风控模式已开启：", json.dumps(payload, ensure_ascii=False))
        return 0
    if action == "off":
        FLAG.unlink(missing_ok=True)
        STATE.unlink(missing_ok=True)
        print("风控模式已关闭")
        return 0
    policy = load()
    print("风控模式:", "开" if policy else "关")
    if policy:
        print(json.dumps(policy, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
