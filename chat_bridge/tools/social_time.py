#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""社交时间：她按设定概率主动找人聊天。

    python tools/social_time.py            # 常驻
    python tools/social_time.py --once     # 立刻试一次（调试）
    python tools/social_time.py --once --dry-run

规则：
- 只在 07:00-22:00 活动，每天最多 4 次；
- 每次间隔 90-180 分钟随机；
- 目标概率：主人 40%、熟人B 30%、其余两人各 15%；
- 先接最近的真实话题，没有话题才按时间说一句；
- 不编“刚下班/刚吃完饭/刚看到”这类没有证据的事；
- 对方还没回上一条时不再追发；发不出去只记日志，不重试刷屏。
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STATE = DATA / "social_time.json"
LOG = DATA / "social_time.log"
EVENTS = Path.home() / ".dsh" / "profiles" / "desktop" / "node_modules" / "dsh-douyin-browser-local" / "state" / "chat-events.jsonl"
API = "http://127.0.0.1:8000"
OLLAMA = "http://127.0.0.1:11434/api/generate"
MODEL = "qwen2.5:14b"

OWNER_ID = "conv:9e52b4549f424e730d8871644385bcce63768cc74b8f8ec7ffa895f62ca73eeb"
TARGETS = [
    (0.40, OWNER_ID, "爸爸"),
    (0.30, "conv:b8b2542c70e596dbd28406e5d5be964cbf6a3b77b48faa9d7712943fa79dde4a", "熟人B"),
    (0.15, "conv:771877f9c6400b9e9217ca4d4c6ae6231e06347099765ede0465c18b0e66e0c3", "熟人C"),
    (0.15, "conv:74a435d700ff94c9a96e1af8968e31cfe5d721f7391008f5a76a195e6b257e02", "熟人D"),
]
SELF_COOLDOWN = timedelta(hours=6)


def log(text: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_state(state: dict) -> None:
    try:
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _event_time(value: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone()


def recent_dialogue(cid: str, limit: int = 8) -> tuple[list[tuple[str, str]], datetime | None, datetime | None]:
    """读这个联系人最近的真实对话，以及最后一次对方/自己发言的时间。"""
    turns: list[tuple[datetime, str, str]] = []
    try:
        lines = EVENTS.read_text(encoding="utf-8").splitlines()[-4000:]
    except OSError:
        return [], None, None
    for line in lines:
        try:
            row = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if row.get("recipientId") != cid:
            continue
        at = _event_time(row.get("at") or "")
        if at is None:
            continue
        kind = row.get("type")
        if kind == "message.received":
            for msg in row.get("messages") or []:
                text = str(msg.get("text") or "").strip()
                media = msg.get("media") or {}
                if not text and media:
                    text = str(media.get("label") or media.get("type") or "").strip()
                if text:
                    turns.append((at, "对方", text[:120]))
        elif kind == "reply.sent":
            text = str(row.get("text") or "").strip()
            if text:
                turns.append((at, "我", text[:120]))
        elif kind == "sticker.sent":
            turns.append((at, "我", "[表情]"))
    turns.sort(key=lambda item: item[0])
    selected = turns[-limit:]
    last_in = max((at for at, who, _ in turns if who == "对方"), default=None)
    last_out = max((at for at, who, _ in turns if who == "我"), default=None)
    return [(who, text) for _, who, text in selected], last_in, last_out


def pick_target(avoid: str = "") -> tuple[str, str]:
    choices = [item for item in TARGETS if item[1] != avoid] or TARGETS
    roll = random.random() * sum(item[0] for item in choices)
    acc = 0.0
    for weight, cid, name in choices:
        acc += weight
        if roll <= acc:
            return cid, name
    return choices[-1][1], choices[-1][2]


def recent_memory(cid: str) -> str:
    """只给主人补一条长期记忆，避免主动搭话完全不接过去。"""
    if cid != OWNER_ID:
        return ""
    db = DATA / "xiaoyu.db"
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute("select summary from books where user_id=? order by rowid desc limit 2", (OWNER_ID,)).fetchall()
        con.close()
        return " / ".join(str(r[0])[:60] for r in rows if r and r[0])
    except Exception:  # noqa: BLE001
        return ""


def _period(hour: int) -> str:
    if hour < 5:
        return "凌晨"
    if hour < 9:
        return "早上"
    if hour < 12:
        return "上午"
    if hour < 14:
        return "中午"
    if hour < 18:
        return "下午"
    if hour < 23:
        return "晚上"
    return "深夜"


def fallback_opener(cid: str, avoid: list[str]) -> str:
    now = datetime.now()
    hour = now.hour
    if hour < 9:
        options = ["早啊 你起来没", "今天起得早不早"]
    elif hour < 14:
        options = ["吃饭了没", "中午吃点啥"]
    elif hour < 18:
        options = ["下午在忙啥", "今天忙完了没"]
    elif hour < 23:
        options = ["晚上吃点啥", "这个点在忙啥"]
    else:
        options = ["这个点还不睡吗", "你也没睡啊"]
    fresh = [item for item in options if item not in avoid]
    return random.choice(fresh or options)


def opener(name: str, cid: str, recent: list[tuple[str, str]], avoid: list[str]) -> str:
    style = "黏一点、软一点，像在撒娇" if cid == OWNER_ID else "随意一点，像老朋友随口搭话"
    others = "、".join(t[2] for t in TARGETS if t[1] != cid)
    context = "\n".join(f"{who}：{text}" for who, text in recent[-6:]) or "（还没聊过）"
    now = datetime.now()
    memory = recent_memory(cid)
    prompt = (
        "你是她，女生，说话短、口语、偶尔犯傻。"
        f"现在是{now:%Y-%m-%d %H:%M}，{_period(now.hour)}。"
        f"你要给{name}发一条私信，只写这一句，20字以内，不要引号，不要解释。"
        f"语气：{style}。\n"
        f"你们最近的真实对话：\n{context}\n"
        + (f"你记得的事：{memory}\n" if memory else "")
        + "写的时候：\n"
        "1. 如果上面有具体话题，就顺着那个话题接一句，别换台。\n"
        "2. 如果没有话题，就按当前时间说一句生活化的话。\n"
        "3. 不要编造你刚下班、刚吃完饭、刚看到什么、刚睡醒这类没有证据的事。\n"
        "4. 不要提其他人，不要问“有什么新鲜事”，不要客套，不要像客服。\n"
        "5. 最多只问一个问题，也可以不问。\n"
        "只学语气的例子：这个点还不睡吗 / 刚想找你说话 / 今天过得咋样\n"
        f"不要和这些最近说过的话重复：{' / '.join(avoid[-8:]) or '无'}"
    )
    body = json.dumps({"model": MODEL, "prompt": prompt, "stream": False, "options": {"num_predict": 40}}).encode()
    req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            text = str(json.loads(resp.read().decode()).get("response") or "").strip()
    except Exception as exc:  # noqa: BLE001
        log(f"生成失败：{exc}")
        return ""
    text = re.sub(r"[\r\n]+", " ", text).strip().strip('"“”')
    text = re.split(r"\s*[/／|]|\s*；|\s*;\s*", text)[0].strip(" ，,")
    text = re.sub(r"^[0-9]+[.、]\s*", "", text)
    text = text.rstrip("。！？!?…~～")
    return text[:24]


def bad_opener(text: str, cid: str, avoid: list[str]) -> bool:
    others = [t[2] for t in TARGETS if t[1] != cid]
    if any(name in text for name in others) or "新鲜事" in text:
        return True
    normalized = re.sub(r"[\s，,。！!？?~～]", "", text)
    return any(normalized and normalized == re.sub(r"[\s，,。！!？?~～]", "", old) for old in avoid)


def can_open(cid: str, state: dict) -> bool:
    """对方还没回上一条时，不追着发。"""
    _, last_in, last_out = recent_dialogue(cid, limit=12)
    last_self = last_out
    sent = (state.get("sent") or {}).get(cid)
    if sent:
        try:
            sent_at = datetime.fromisoformat(str(sent))
            if sent_at.tzinfo is None:
                sent_at = sent_at.astimezone()
            last_self = max(last_self, sent_at) if last_self else sent_at
        except ValueError:
            pass
    if last_self is None:
        return True
    now = datetime.now().astimezone()
    if last_in is not None and last_in >= last_self:
        return True
    return now - last_self >= SELF_COOLDOWN


def send(cid: str, text: str) -> tuple[bool, str]:
    cmd = DATA / "douyin_command.json"
    result = DATA / "douyin_command.result.json"
    try:
        result.unlink(missing_ok=True)
        cmd.write_text(json.dumps({"action": "text", "contactId": cid, "text": text, "at": time.time()}, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        return False, str(exc)
    deadline = time.time() + 60
    while time.time() < deadline:
        if result.exists():
            try:
                data = json.loads(result.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                data = {}
            result.unlink(missing_ok=True)
            return bool(data.get("ok")), str(data.get("error") or data.get("messageId") or "")
        time.sleep(1)
    return False, "私信服务 60 秒未响应"


def active_hours(now: datetime) -> bool:
    return 7 <= now.hour < 22


def acquire_lease() -> bool:
    """同一时间只允许一个实例真正发消息，避免重复发。"""
    import os  # noqa: PLC0415

    lease = DATA / "social_time.lease"
    try:
        if lease.exists() and time.time() - lease.stat().st_mtime > 600:
            lease.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        fd = os.open(lease, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except OSError:
        return True


def release_lease() -> None:
    try:
        (DATA / "social_time.lease").unlink(missing_ok=True)
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--cap", type=int, default=4)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    state = load_state()
    while True:
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if state.get("date") != today:
            state = {
                "date": today,
                "count": 0,
                "next_at": now.isoformat(timespec="seconds"),
                "last_target": state.get("last_target") or "",
                "recent_openers": list(state.get("recent_openers") or [])[-12:],
                "sent": state.get("sent") or {},
            }
            save_state(state)
        if args.once:
            state["next_at"] = now.isoformat(timespec="seconds")

        try:
            next_at = datetime.fromisoformat(str(state.get("next_at") or now.isoformat()))
        except Exception:  # noqa: BLE001
            next_at = now

        if active_hours(now) and now >= next_at and int(state.get("count") or 0) < args.cap:
            if not acquire_lease():
                # 另一个实例正在发，稍后再看
                time.sleep(60)
                continue
            try:
                picked = None
                for _ in range(len(TARGETS)):
                    cid, name = pick_target(str(state.get("last_target") or ""))
                    if can_open(cid, state):
                        picked = (cid, name)
                        break
                if picked is None:
                    log("几个联系人都在等她上一条的回复，这次先不追发")
                    state["next_at"] = (now + timedelta(minutes=30)).isoformat(timespec="seconds")
                    save_state(state)
                    if args.once:
                        return 0
                    continue

                cid, name = picked
                recent, _, _ = recent_dialogue(cid)
                avoid = list(state.get("recent_openers") or [])
                text = opener(name, cid, recent, avoid)
                if text and bad_opener(text, cid, avoid):
                    text = ""
                if not text:
                    text = fallback_opener(cid, avoid)
                if args.dry_run:
                    log(f"[dry-run] -> {name}: {text}")
                    return 0
                ok, info = send(cid, text)
                log(f"-> {name}: {text} | {'成功' if ok else '失败 ' + info}")
                if ok:
                    state["count"] = int(state.get("count") or 0) + 1
                    state["last_target"] = cid
                    openers = (list(state.get("recent_openers") or []) + [text])[-12:]
                    state["recent_openers"] = openers
                    sent = dict(state.get("sent") or {})
                    sent[cid] = now.isoformat(timespec="seconds")
                    state["sent"] = sent
                state["next_at"] = (now + timedelta(minutes=random.randint(90, 180))).isoformat(timespec="seconds")
                save_state(state)
                if args.once:
                    return 0
            finally:
                release_lease()
        elif not active_hours(now):
            state["next_at"] = (now + timedelta(hours=1)).isoformat(timespec="seconds")
            save_state(state)

        if args.once:
            return 0
        time.sleep(60)


if __name__ == "__main__":
    raise SystemExit(main())
