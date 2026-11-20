# -*- coding: utf-8 -*-
"""她没回的消息，等能回了再回。

dl_api 在她安静/睡着/不在线时会把IM 里的话写进 data/pending_messages.jsonl，
这个守护进程每隔一会儿看一眼：
- 她还在安静/睡着 → 先不动
- 她回来了（安静结束、没睡） → 把最多 3 条拼成一条"刚才没顾上回"的消息发给她，
  拿到她的回复后，用 chat_send 发回IM，再把队里这几条标成已回。

用法：python tools/pending_replay.py --watch 60
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # chat_bridge
DATA = ROOT / "data"
PENDING_FILE = DATA / "pending_messages.jsonl"
TALK_STATE = DATA / "talk_state.json"
SLEEP_STATE = DATA / "sleep_state.json"
AGENT_STATE = DATA / "agent_state.json"
CHAT_URL = "http://127.0.0.1:8000/chat"
MAIN_USER_ID = "conv:9e52b4549f424e730d8871644385bcce63768cc74b8f8ec7ffa895f62ca73eeb"
MAX_PER_RUN = 3
MAX_ATTEMPTS = 3
_attempts: dict[str, int] = {}   # 发送失败计次（进程内；不再回写主文件）

sys.path.append(str(Path(__file__).resolve().parent))
import pending_queue  # noqa: E402


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def read_pending() -> list[dict]:
    """待回记录，旧的在前。超过 12 小时的直接 ack 掉（隔天再回"嗯嗯"很怪）。"""
    items: list[dict] = []
    now = datetime.now().astimezone()
    for item in reversed(pending_queue.list_pending(10**9, path=PENDING_FILE)):
        if _attempts.get(item["id"], 0) >= MAX_ATTEMPTS:
            continue
        try:
            at = datetime.fromisoformat(str(item.get("at") or ""))
            if at.tzinfo is None:
                at = at.astimezone()
            if (now - at).total_seconds() > 12 * 3600:
                pending_queue.ack(item["id"], path=PENDING_FILE)
                continue
        except ValueError:
            pass
        items.append(item)
    return items


def chat_ready() -> bool:
    """IM登录了没有。没登录就别生成回复——发了也出不去，纯烧 token。"""
    try:
        sys.path.insert(0, str(ROOT))
        from chat_send import _account  # noqa: PLC0415

        return bool(_account())
    except Exception:  # noqa: BLE001
        return False


def busy_reason() -> str:
    """她现在能不能回话。返回空串 = 可以。"""
    talk = load_json(TALK_STATE)
    until = str(talk.get("quiet_until") or "")
    if until:
        try:
            when = datetime.fromisoformat(until)
            if when.tzinfo is None:
                when = when.astimezone()
            if when > datetime.now().astimezone():
                return "安静中"
        except Exception:  # noqa: BLE001
            pass
    if not chat_ready():
        return "IM没登录，先不补回（免得白烧模型）"
    if load_json(SLEEP_STATE).get("asleep"):
        return "睡着了"
    agent = load_json(AGENT_STATE)
    if agent and not agent.get("ok", True):
        try:
            at = datetime.fromisoformat(str(agent.get("at") or ""))
            if at.tzinfo is None:
                at = at.astimezone()
            if (datetime.now().astimezone() - at).total_seconds() < 300:
                return "不在线"
        except Exception:  # noqa: BLE001
            pass
    return ""


def chat(text: str) -> str:
    import urllib.request

    # 2026-11-09：这是系统替她补回，不是新来信。标 internal，
    # 否则她安静时这段提示词会被当成新消息再排回队列（前缀越叠越多）。
    body = json.dumps({"message": text, "user_id": MAIN_USER_ID,
                       "sender": "SOLVER-REPLAY", "channel": "chat_private",
                       "internal": True},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(CHAT_URL, data=body, headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("ignored"):
        return ""
    replies = data.get("replies") or []
    if replies:
        return "\n".join(str(x) for x in replies if str(x).strip())
    return str(data.get("reply") or "").strip()


def send_chat(to: str, text: str) -> bool:
    try:
        sys.path.insert(0, str(ROOT))
        from chat_send import send_text  # noqa: PLC0415

        result = send_text(to, text)
        return bool(result.get("ok"))
    except Exception:  # noqa: BLE001
        return False


def expire_old(hours: float = 12.0) -> int:
    """超过 N 小时还没补上的，ack 掉——隔天再回"嗯嗯"很怪。"""
    now = datetime.now().astimezone()
    changed = 0
    for item in pending_queue.list_pending(10**9, path=PENDING_FILE):
        try:
            at = datetime.fromisoformat(str(item.get("at") or ""))
            if at.tzinfo is None:
                at = at.astimezone()
        except ValueError:
            continue
        if (now - at).total_seconds() > hours * 3600:
            pending_queue.ack(item["id"], path=PENDING_FILE)
            changed += 1
    return changed


def replay_once() -> bool:
    if busy_reason():
        return False
    items = read_pending()
    if not items:
        return False
    batch = items[:MAX_PER_RUN]
    who = str(batch[-1].get("sender") or "爸爸")
    lines = []
    for item in batch:
        lines.append(f"{item.get('sender') or '爸爸'}: {item.get('text')}")
    prompt = (
        "[系统：你刚才安静/没在线的时候，他给你发了下面这几条，现在你看到了。]\n"
        + "\n".join(lines)
        + "\n用你现在的心情回他一句，别解释你刚才为什么没回。"
    )
    reply = chat(prompt)
    if not reply:
        return False
    if not send_chat(who, reply):
        # 失败计次（进程内），超过 3 次就先搁着，等IM恢复/重启再说。
        for item in batch:
            _attempts[item["id"]] = _attempts.get(item["id"], 0) + 1
        sys.stderr.write(f"[pending] 发不出去，已计次（{_attempts.get(batch[0]['id'])}）\n")
        return False
    for item in batch:
        pending_queue.ack(item["id"], path=PENDING_FILE)
        _attempts.pop(item["id"], None)
    sys.stderr.write(f"[pending] 回了 {len(batch)} 条给 {who}\n")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="把没回的消息补回")
    parser.add_argument("--watch", type=float, default=60.0)
    args = parser.parse_args()
    if args.watch <= 0:
        replay_once()
        return 0
    print(f"pending replay started, watching {PENDING_FILE}", flush=True)
    while True:
        try:
            gone = expire_old()
            if gone:
                sys.stderr.write(f"[pending] 过期清理 {gone} 条\n")
            replay_once()
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[pending] error: {type(exc).__name__}: {exc}\n")
        time.sleep(max(120.0, args.watch))   # 2026-11-09：60 秒一次太密，每次都是一次完整模型调用


if __name__ == "__main__":
    raise SystemExit(main())
