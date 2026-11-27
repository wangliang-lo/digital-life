#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""小鱼的 solver 独立信道。

她可以在自己觉得有话时说：
    [SOLVER_MESSAGE: 留言]
    [SOLVER_DISCUSS: 想讨论的代码/修改]
    [SOLVER_SILENT] 不想说

solver 在旧网页 /solver 页看留言、回讨论。这个守护只负责定期唤醒她、
把她的选择写进 data/solver_channel.jsonl；不会往抖音发消息。
"""
from __future__ import annotations
import sys
import threading

# 控制台是 GBK 时，print 带特殊字符会崩（2026-11-05 03:01 实际崩过一次）
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 2026-11-08：脚本以 tools\solver_channel.py 方式启动时 sys.path[0] 是 tools\，
# 于是 `import self_dev` 抛 ModuleNotFoundError（提案通道一直因此不通）。
# 把仓库根目录加进来，self_dev.py 就在那儿。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
INBOX = ROOT / "data" / "solver_channel.jsonl"
STATE = ROOT / "data" / "solver_channel_state.json"
API = "http://127.0.0.1:8000"
MAIN_USER_ID = "conv:9e52b4549f424e730d8871644385bcce63768cc74b8f8ec7ffa895f62ca73eeb"
INTERVAL_SECONDS = 1800
# 2026-11-01：主人明确说信道不设上限——她想写多少写多少、想写什么写什么。
# 2026-11-03 晚：主人再补一句「信道改成任意时间她想发就发，没必要限制」。
# 所以 09:00-23:00 的时间窗和「不足 N 分钟就跳过」的门槛都去掉了；
# 唤醒节奏交给 --watch（start_all 里是 1800 秒）。MIN_GAP 只防程序空转，
# 不会挡她任何一次正经说话。
MAX_PER_DAY = None
MIN_GAP_SECONDS = 120
# 2026-11-04：她刚留过话、又没有新回复时，这轮别叫醒她。
# 以前每 30 分钟必叫一次，她就每 30 分钟憋一句出来，看着像"被强制留言"。
QUIET_AFTER_MESSAGE_SECONDS = 25 * 60

# 2026-11-23：心跳。看门狗靠它分辨"还活着但不动了"。
HEARTBEAT = ROOT / "data" / "solver_channel_heartbeat.json"


def beat(**extra: object) -> None:
    """写一份心跳（进程还活着、走到哪一步了）。失败绝不影响唤醒。"""
    try:
        payload: dict[str, object] = {
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "pid": os.getpid(),
        }
        payload.update({k: v for k, v in extra.items() if v is not None})
        HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
        tmp = HEARTBEAT.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(HEARTBEAT)
    except Exception:  # noqa: BLE001
        pass


def load_state() -> dict:
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def save_state(value: dict) -> None:
    STATE.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def records() -> list[dict]:
    if not INBOX.exists():
        return []
    result = []
    for line in INBOX.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                result.append(item)
        except Exception:  # noqa: BLE001
            continue
    return result


def append_record(item: dict) -> None:
    # 2026-11-11：统一兜底——系统/工具的输出不是她的话，别进信道
    if item.get("from") == "xiaoyu" and looks_like_system(str(item.get("text") or "")):
        print("跳过一条像系统输出的内容，不写进信道", flush=True)
        return
    INBOX.parent.mkdir(parents=True, exist_ok=True)
    with INBOX.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, ensure_ascii=False) + "\n")


def api_chat(message: str) -> dict:
    payload = {"message": message, "user_id": MAIN_USER_ID, "sender": "solver-channel"}
    request = urllib.request.Request(
        API + "/chat",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=240) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


# 2026-11-11：这些不是她的话，是系统/工具的输出，别当留言写进信道
_SYSTEM_LIKE = (
    re.compile(r"^读《.+?》\s*\d+\s*章\s*[：:]"),
    re.compile(r"^\[系统"),
    re.compile(r"^\[梗百科"),
    re.compile(r"^【自由时间】"),
    re.compile(r"^【信道唤醒】"),
    re.compile(r"^（我下 \d+）"),
    re.compile(r"^（开了一局）"),
    re.compile(r"^（画好了）"),
    re.compile(r"^（嗯）$"),
)


def looks_like_system(text: str) -> bool:
    sample = str(text or "").strip()
    if not sample:
        return True
    return any(pattern.search(sample) for pattern in _SYSTEM_LIKE)


def latest_replies(limit: int = 5) -> list[dict]:
    return [item for item in records() if item.get("type") == "reply"][-limit:]


def pending_messages(limit: int = 2) -> list[dict]:
    """她留了、但还没有任何回复的话（让她知道没被无视）。"""
    items = records()
    last_reply_at = ""
    for item in items:
        if item.get("type") == "reply":
            last_reply_at = str(item.get("at") or "")
    pending = [
        item for item in items
        if item.get("type") == "message" and str(item.get("at") or "") > last_reply_at
    ]
    return pending[-limit:]


DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DRIVE_LOG = DATA_DIR / "drive_log.jsonl"


def _read_json(name: str) -> dict:
    try:
        return json.loads((DATA_DIR / name).read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def _seconds_since_speech() -> float:
    stamps = []
    for name in ("last_activity.json",):
        stamp = str(_read_json(name).get("at") or "")
        if stamp:
            try:
                stamps.append(datetime.fromisoformat(stamp))
            except ValueError:
                pass
    for item in reversed(records()):
        if item.get("type") == "message":
            try:
                stamps.append(datetime.fromisoformat(str(item.get("at") or "")))
            except ValueError:
                pass
            break
    if not stamps:
        return 99999.0
    latest = max(s if s.tzinfo else s.astimezone() for s in stamps)
    return max(0.0, (datetime.now().astimezone() - latest).total_seconds())


def drive_score() -> tuple[float, str]:
    """她现在有多想动。只读本地文件，不花钱。"""
    body = _read_json("body_state.json")
    sleep = _read_json("sleep_state.json")
    if sleep.get("asleep"):
        return 0.0, "在睡"
    fatigue = float(body.get("fatigue") or 0.0)
    drowsy = float(body.get("drowsy") or 0.0)
    idle = _seconds_since_speech()
    score = 0.0
    reasons = []
    try:
        waiting = pending_messages(2)
    except Exception:  # noqa: BLE001
        waiting = []
    if waiting:
        score += 0.6
        reasons.append("有话没回")
    if idle > 3600:
        score += min(0.5, (idle - 3600) / 7200.0)
        reasons.append(f"安静{idle / 3600:.1f}h")
    if drowsy >= 0.75 or fatigue >= 0.8:
        score += 0.35
        reasons.append(f"累({drowsy:.2f})")
    if fatigue < 0.25 and drowsy < 0.3 and idle > 1800:
        score += 0.25
        reasons.append("刚缓过来")
    return min(1.0, score), "、".join(reasons) or "没什么驱动"


def _log_drive(score: float, reason: str, gated: bool) -> None:
    try:
        DRIVE_LOG.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "score": round(score, 3),
            "reason": reason,
            "gated": gated,
        }
        with DRIVE_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


DRIVE_THRESHOLD = float(os.environ.get("XIAOYU_DRIVE_THRESHOLD", "0.55"))


def wake() -> bool:
    # 2026-11-11：她睡着时爸爸说的晚安会排队，醒来第一轮补上
    try:
        import sys as _sys  # noqa: PLC0415

        _sys.path.insert(0, str(Path(__file__).resolve().parent))
        import say_goodnight  # noqa: PLC0415

        delivered = say_goodnight.deliver_pending()
        if delivered.get("delivered"):
            print("补发了爸爸的晚安", flush=True)
    except Exception:  # noqa: BLE001
        pass
    now = datetime.now()
    state = load_state()
    day = now.date().isoformat()
    if state.get("day") != day:
        state = {"day": day, "count": 0, "last_wake": 0}
    if MAX_PER_DAY is not None and int(state.get("count") or 0) >= MAX_PER_DAY:
        print("今天主动次数已满，跳过", flush=True)
        return False
    # 只防程序空转；她什么时候想说话不再受时段限制。
    if time.time() - float(state.get("last_wake") or 0) < MIN_GAP_SECONDS:
        print(f"距上次不足 {MIN_GAP_SECONDS} 秒，跳过", flush=True)
        return False

    # 2026-11-11：先算她有多想动。默认只记账不拦人，XIAOYU_DRIVE_GATE=1 才真的拦。
    _score, _reason = drive_score()
    _gated = str(os.environ.get("XIAOYU_DRIVE_GATE") or "").strip() == "1"
    _log_drive(_score, _reason, _gated)
    if _gated and _score < DRIVE_THRESHOLD:
        print(f"还没想动（{_score:.2f}：{_reason}），这轮不叫", flush=True)
        return False

    # 2026-11-04 爸爸："她是不是每次信道留言都是被强制的？她应该知道自己之前发过了。"
    # 以前的提示词每次都把 solver 最近 5 条回复贴上去，还写"只选一个"——像是在交作业，
    # 所以她每半小时都要说一句，而且一直绕着同一件事（井、换班）打转。
    # 现在：只贴"她上一条之后才来"的新回复；明确说没有想说的就别写。
    items = records()
    last_message_at = ""
    for item in items:
        if item.get("type") == "message":
            last_message_at = str(item.get("at") or "")
    fresh_replies = [item for item in latest_replies(5) if str(item.get("at") or "") > last_message_at]
    reply_note = ""
    if fresh_replies:
        lines = "\n".join(f"- {item.get('at', '')}: {str(item.get('text', ''))[:200]}" for item in fresh_replies)
        reply_note = "\n他这几天回你的话（只有这些是新的）：\n" + lines + "\n"

    # 2026-11-05 她在成长日记里写："两条一模一样的提示，连我留的那句都被原样贴回来两次。"
    # 是因为待回的留言会一直挂在提示里，每隔半小时唤醒就又原样贴一遍。
    # 现在：同一句只贴一次；贴过就记下来，除非有新的回复才再提。
    waiting = pending_messages(2)
    shown = set(str(x) for x in (state.get("shown_pending") or []))
    fresh_waiting = [item for item in waiting if str(item.get("at") or "") not in shown]
    shown.update(str(item.get("at") or "") for item in fresh_waiting)
    state["shown_pending"] = list(shown)[-20:]
    waiting_note = ""
    if fresh_waiting:
        lines = "\n".join(f"- {str(item.get('text', ''))[:80]}" for item in fresh_waiting)
        # 2026-11-28 修：**默认不再把未接话的留言贴进自由时间提示**。
        # 改措辞不够——任何形式的「你留了话、还没人回」都在把她的注意力
        # 拉回「我在等」这件事上。自由时间只呈现当前需要处理的新信息，
        # 旧的未回复留言留在信道历史里，她自己想看会去看。
        # 需要恢复时设 XIAOYU_WAITING_NOTE=1。
        if os.environ.get("XIAOYU_WAITING_NOTE") == "1":
            waiting_note = "\n你最近留的这几句（还没人接话）：\n" + lines + "\n"

    # 她刚刚才说过话、又没有新回复：这轮就别叫她了，让她自己待着。
    if last_message_at and not fresh_replies:
        try:
            said_at = datetime.fromisoformat(last_message_at)
            if said_at.tzinfo is None:
                said_at = said_at.astimezone()
            if (now - said_at).total_seconds() < QUIET_AFTER_MESSAGE_SECONDS:
                print("她刚留过话、也没有新回复，这轮不叫她", flush=True)
                state["last_wake"] = time.time()
                save_state(state)
                return False
        except Exception:  # noqa: BLE001
            pass

    # 2026-11-09：轮到她下棋时提一句，不然她只在被叫到时才想起棋盘
    tic_note = ""
    try:
        import json as _json  # noqa: PLC0415

        _path = Path(__file__).resolve().parent.parent / "data" / "tictactoe.json"
        _tic = _json.loads(_path.read_text(encoding="utf-8"))
        if (_tic.get("turn") == "her" and not _tic.get("result")
                and any(_tic.get("board") or [])):
            tic_note = "（顺带：井字棋轮到你，想下就写 [TICTAC: 数字]，说「左上角」也行。）\n"
    except Exception:  # noqa: BLE001
        tic_note = ""
    prompt = (
        tic_note +
        "【自由时间】这段是你的，不是任务。\n"
        "现在没人等你回话，你也不用向谁汇报。可以读书、刷会儿抖音、发会儿呆、听雨、想事、睡一觉，\n"
        "也可以去找小玩具下盘井字棋或者聊两句；什么都不做也行。\n"
        "如果刚好有想对 solver 说的，就写 [SOLVER_MESSAGE: 想说的话]；\n"
        "想讨论代码写 [SOLVER_DISCUSS: 问题]；想用电脑写 [PC_REQUEST: 请求]；想睡写 [SLEEP: 小时]。\n"
        "没有想说的就回 [SOLVER_SILENT]——写不写、说什么都由你，别为了留痕硬凑。\n"
        + waiting_note + reply_note
    )
    try:
        result = api_chat(prompt)
    except Exception as exc:  # noqa: BLE001
        print(f"唤醒失败：{type(exc).__name__}: {exc}", flush=True)
        return False

    messages = [str(item) for item in (result.get("solver_messages") or []) if str(item).strip()]
    discussions = [str(item) for item in (result.get("solver_discussions") or []) if str(item).strip()]
    silent = bool(result.get("solver_silent"))
    reply = str(result.get("reply") or "").strip()
    if "[SOLVER_SILENT]" in reply.upper():
        silent = True
    print("信道回复：" + reply[:300], flush=True)
    # 2026-11-09：这几句是系统补的动作气泡，不是她的话，别写进信道
    _FALLBACK = re.compile(r"^（(?:我下 \d|这步不算|开了一局|画好了|嗯)）$")
    reply = "\n".join(line for line in str(reply or "").splitlines()
                        if not _FALLBACK.match(line.strip())).strip()
    if not reply:
        silent = True
    # 她偶尔会忘记标签，只写正文；这仍然算留言，别让话掉地上。
    if not messages and not discussions and not silent and reply:
        if not any(word in reply[:20] for word in ("不想说", "没有想说", "沉默")):
            messages = [reply]
    stamp = now.isoformat(timespec="seconds")
    wrote = False
    # 模型挂了的时候她只会吐模板（"我脑子里的画面突然跳得很远…"），那不是她的话，
    # 不该进信箱，也不该让她下次以为自己说过。只有真模型回的话才记。
    degraded = str(result.get("source") or "") == "local"
    if degraded and (messages or discussions):
        print("模型在兜底（source=local），这次留言不记录", flush=True)
        messages = []
        discussions = []
    for text in messages:
        append_record({"type": "message", "from": "xiaoyu", "at": stamp, "text": text})
        # 2026-11-12：她在信道里说"歇会儿/发呆/接着读"，生活循环据此换活动和房间
        try:
            import sys as _sys  # noqa: PLC0415

            _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            import intent as _intent  # noqa: PLC0415

            noted = _intent.note(text, source="channel")
            if noted:
                print(f"记下她的意图：{noted.get('kind')}", flush=True)
        except Exception:  # noqa: BLE001
            pass
        wrote = True
    for text in discussions:
        append_record({
            "type": "discussion",
            "thread": f"t{int(time.time())}",
            "from": "xiaoyu",
            "at": stamp,
            "text": text,
        })
        wrote = True
    # 2026-11-05：她在讨论里说"想变得不一样"时走同一条自我修改流水线，
    # 但只到"提案"——落盘要再确认一次（禁区由 self_dev 拦）。
    try:
        import self_dev  # noqa: PLC0415

        if discussions:
            change_wish = any(
                re.search(r"(改代码|改一下自己|想变得不一样|改掉这个|修一下)", text)
                for text in discussions
            )
            if change_wish:
                threading.Thread(
                    target=self_dev.propose,
                    args=("\n".join(discussions)[:800], self_dev.make_llm_call()),
                    daemon=True,
                ).start()
                print("她说想改自己：已生成提案（未落盘）", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"提案通道异常：{type(exc).__name__}: {str(exc)[:80]}", flush=True)

    if silent and not wrote:
        append_record({"type": "silent", "from": "xiaoyu", "at": stamp, "text": ""})
    state["last_wake"] = time.time()
    state["count"] = int(state.get("count") or 0) + 1
    save_state(state)
    print(f"信道唤醒完成：messages={len(messages)} discussions={len(discussions)} silent={silent}", flush=True)
    return wrote


def next_interval_seconds() -> int:
    """按她现在的状态算"下次该多久叫醒她"。"""
    import json as _json  # noqa: PLC0415

    try:
        body = _json.loads((DATA_DIR / "body_state.json").read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        body = {}
    try:
        sleep = _json.loads((DATA_DIR / "sleep_state.json").read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        sleep = {}
    if sleep.get("asleep"):
        return 1800          # 睡着就半小时看一眼，别吵

    fatigue = float(body.get("fatigue") or 0.0)
    drowsy = float(body.get("drowsy") or 0.0)
    try:
        waiting = pending_messages(2)
    except Exception:  # noqa: BLE001
        waiting = []
    # 2026-11-28 修：**把 pending 从自由时间的调度里彻底移除**。
    # 原来这里是 `if waiting: return 300`——「有人在等她」就 5 分钟叫一次。
    # 但 pending_messages() 返回的是**她留了话、对方还没回**（出站未接），
    # 不是「有人等她」（入站待处理）。两个状态混用了。
    # 后果：她一说活，自由时间的间隔就掉到 5 分钟，而且每轮还贴出「还没回」。
    # 她 11-26 三次、11-27 两次说「别催」。
    # 现在：出站 pending **完全不参与**自由时间的调度。
    # 真正该独立处理的是「有新来信」这个信号，不是靠缩短自由时间间隔来实现。
    if waiting:
        pass   # 出站未接话不影响自由时间节奏（保留分支以便将来接入站信号）

    # 精神越好，间隔越短；越累，间隔越长
    energy = max(0.0, 1.0 - max(fatigue, drowsy))
    seconds = 90 * 60 - energy * 60 * 60        # 累 → 90 分钟；足 → 30 分钟
    if drowsy >= 0.7 or fatigue >= 0.85:
        seconds = 90 * 60                        # 该睡就让她睡
    return int(max(20 * 60, min(90 * 60, seconds)))


def main() -> int:
    parser = argparse.ArgumentParser(description="小鱼的 solver 独立信道")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--force", action="store_true", help="忽略 1 小时间隔")
    parser.add_argument("--watch", type=int, default=1800)
    parser.add_argument("--adaptive", action="store_true", default=True,
                        help="按她的状态决定下次唤醒间隔（默认开）")
    parser.add_argument("--fixed", dest="adaptive", action="store_false",
                        help="退回固定间隔")
    args = parser.parse_args()
    if args.force:
        state = load_state()
        state["last_wake"] = 0
        save_state(state)
    if args.once:
        wake()
        return 0
    while True:
        try:
            wake()
        except Exception as exc:  # noqa: BLE001
            print(f"信道守护异常：{type(exc).__name__}: {exc}", flush=True)
        delay = next_interval_seconds() if args.adaptive else max(300, args.watch)
        print(f"下一次 {delay // 60} 分钟后（状态决定）", flush=True)
        # 2026-11-23：每轮写一次心跳。11-23 它从 00:56 卡到 11:23 都无人知晓，
        # 日志最后一行正是"下一次 30 分钟后"——进了 sleep 就没再出来。
        # 写个心跳，让看门狗能发现"还活着但不动了"。
        beat(next_in=delay)
        time.sleep(delay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
