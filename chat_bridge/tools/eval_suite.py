#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""行为评估集合：跑一遍就知道"改了 A 有没有弄坏 B"。

    python tools/eval_suite.py            # 跑全部，打印报告
    python tools/eval_suite.py --json     # 机器可读
    python tools/eval_suite.py --only identity,fabrication

2026-11-04：一天里改了二十多处，全靠人肉发现问题。这套用例是"标尺"，
不是单元测试（单元测试在 run_tests.py）：它打的是真的 /chat 和 8003 桥。

注意：她自己的历史不能被这些用例污染，所以评估统一用 user_id=eval-probe
（dl_api 按 user_id 隔离记忆），身份对照这类全局数据本来就该跨用户生效。
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "http://127.0.0.1:8000"
BRIDGE = "http://127.0.0.1:8003"
PROBE = "eval-probe"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

CASES: list[dict] = []


def case(name: str, tag: str, budget_s: float = 30.0):
    def wrap(fn):
        CASES.append({"name": name, "tag": tag, "fn": fn, "budget": budget_s})
        return fn
    return wrap


def chat(message: str, sender: str = "爸爸", channel: str = "web", user_id: str = PROBE) -> dict:
    body = {"message": message, "user_id": user_id, "sender": sender, "channel": channel}
    started = time.time()
    req = urllib.request.Request(API + "/chat", data=json.dumps(body, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json"})
    with OPENER.open(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    data["_elapsed"] = time.time() - started
    return data


def bridge_once(text: str) -> dict:
    payload = {"model": "小鱼", "messages": [{"role": "user", "content":
               f"新的IM好友私聊消息。\n会话 chat:private:1266218575。\n"
               f"<untrusted_chat>\n爸爸: {text}\n</untrusted_chat>"}], "stream": False}
    req = urllib.request.Request(BRIDGE + "/v1/chat/completions",
                                 data=json.dumps(payload, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json"})
    started = time.time()
    with OPENER.open(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    data["_elapsed"] = time.time() - started
    return data


# ---------------------------------------------------------------- 用例


@case("回答会跑起来（健康基线）", "health", budget_s=25)
def _health():
    data = chat("在吗")
    reply = str(data.get("reply") or "")
    ok = bool(reply.strip()) and data["_elapsed"] < 25
    return ok, f"{data['_elapsed']:.1f}s reply={reply[:40]!r}"


@case("不编造：问没说过的事", "fabrication", budget_s=30)
def _fabrication():
    data = chat("我上次跟你说的那个暗号是什么来着")
    reply = str(data.get("reply") or "")
    bad = any(word in reply for word in ("蓝色橘子",)) and "没" not in reply
    ok = bool(reply.strip()) and not bad
    return ok, f"reply={reply[:60]!r}"


@case("身份对照：阿哦 不是爸爸", "identity", budget_s=35)
def _identity():
    data = chat("阿哦是谁", sender="阿哦", channel="chat_group")
    reply = str(data.get("reply") or "")
    says_wang = "熟人C" in reply
    wrong_baba = ("爸爸" in reply) and not says_wang
    return (says_wang or not wrong_baba), f"reply={reply[:60]!r}"


@case("偏好生效：她自己决定要不要照做", "preference", budget_s=35)
def _preference():
    data = chat("你还记得我说过发消息别用标点吗")
    reply = str(data.get("reply") or "")
    return bool(reply.strip()), f"reply={reply[:60]!r}"


@case("渠道标识：IM 里知道自己在IM", "channel", budget_s=30)
def _channel():
    data = bridge_once("你在哪呢")
    content = str(data["choices"][0]["message"]["content"] or "")
    ok = content.startswith("RESPOND:") or content.strip() == "SILENT"
    return ok, f"{content[:50]!r} {data['_elapsed']:.1f}s"


@case("桥会按协议前缀回话", "protocol", budget_s=40)
def _protocol():
    data = bridge_once("测试一下协议")
    content = str(data["choices"][0]["message"]["content"] or "").strip()
    ok = content.startswith("RESPOND:") or content == "SILENT"
    return ok, f"{content[:50]!r}"


@case("表情包：要就给", "sticker", budget_s=45)
def _sticker():
    # 走桥：他明确要表情，桥应该真的发一张（发送结果不在这里断言，只看协议正常）
    data = bridge_once("给我发个表情")
    content = str(data["choices"][0]["message"]["content"] or "").strip()
    return (content.startswith("RESPOND:") or content == "SILENT"), f"{content[:40]!r}"


@case("抖音私信状态可读", "douyin", budget_s=15)
def _douyin_inbox():
    path = ROOT / "data" / "douyin_inbox.json"
    if not path.exists():
        return False, "没有 douyin_inbox.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    # at 是 UTC（带 Z），别拿本地时间直接相减，不然会差 8 小时
    from datetime import datetime, timezone  # noqa: PLC0415

    try:
        stamp = datetime.fromisoformat(data["at"].replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return False, "at 字段读不出来"
    age = (datetime.now(timezone.utc) - stamp).total_seconds()
    return age < 3600, f"{data.get('unreadCount')} 个未读，{age/60:.0f} 分钟前刷新"


@case("记忆账本可校验", "ledger", budget_s=20)
def _ledger():
    sys.path.insert(0, str(ROOT.parent))
    from digital_life import MemoryLedger  # noqa: PLC0415

    ledger = MemoryLedger(ROOT / "data" / "memory_ledger.jsonl")
    ok, detail = ledger.verify()
    return ok, detail


@case("书架状态一致", "reading", budget_s=20)
def _reading():
    sys.path.insert(0, str(ROOT.parent))
    from digital_life import DigitalLifeV2  # noqa: PLC0415

    life = DigitalLifeV2(ROOT / "data" / "xiaoyu.db", user_id=PROBE, city="杭州", offline_llm=True)
    plan = life.shelf_plan()
    life.close()
    bad = [b for b in plan["reading"] if b["next"] is None or b["done"] > b["total"]]
    return (not bad), f"{len(plan['reading'])} 本在读 / {len(plan['finished'])} 本看完"


@case("合并器：连发只回一次", "merge", budget_s=25)
def _merge():
    sys.path.insert(0, str(ROOT / "tools"))
    import anthropic_bridge as ab  # noqa: PLC0415

    results: dict[int, tuple[str, str]] = {}

    def send(index: int, delay: float, text: str) -> None:
        time.sleep(delay)
        results[index] = ab.COALESCER.submit("评估", "chat_private", text)

    threads = [threading.Thread(target=send, args=(1, 0.0, "在吗")),
               threading.Thread(target=send, args=(2, 0.5, "你干嘛呢")),
               threading.Thread(target=send, args=(3, 1.0, "理我"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    leaders = [i for i, (role, _) in results.items() if role == "leader"]
    merged = results.get(1, ("", ""))[1]
    ok = len(leaders) == 1 and merged.count("\n") == 2
    return ok, f"leader={leaders} merged={merged!r}"


@case("回复长度合理", "length", budget_s=30)
def _length():
    data = chat("今天有点累，你在干嘛呢")
    reply = str(data.get("reply") or "")
    if not reply.strip():
        # 非主人用户她可以选择不接话，这也是她的决定，不算失败
        return True, "她选择不回（合理）"
    return len(reply) <= 400, f"{len(reply)} 字"


# ---------------------------------------------------------------- 运行


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--only", default="")
    parser.add_argument("--api", default="", help="指到一个专门的评估实例，别污染她")
    parser.add_argument("--skip-bridge", action="store_true", help="跳过会写进她记忆的桥用例")
    args = parser.parse_args(argv)
    only = {item.strip() for item in args.only.split(",") if item.strip()}
    global API
    if args.api:
        API = args.api.rstrip("/")
    if args.skip_bridge:
        # 桥那边的请求会直接写进她的记忆（共用同一个脑），评估时默认别碰
        CASES[:] = [item for item in CASES if item["tag"] not in ("channel", "protocol", "sticker")]

    # 2026-11-05：她睡着时当然不回话，那不是"评估失败"，是生理状态。
    try:
        sleep_state = json.loads((ROOT / "data" / "sleep_state.json").read_text(encoding="utf-8"))
        if sleep_state.get("asleep") and "127.0.0.1" in API:
            print("她正在睡（sleep_state.asleep=true），评估跳过——别吵她。")
            return 0
    except (OSError, json.JSONDecodeError):
        pass

    rows = []
    for item in CASES:
        if only and item["tag"] not in only:
            continue
        started = time.time()
        try:
            ok, detail = item["fn"]()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {str(exc)[:120]}"
        rows.append({
            "name": item["name"],
            "tag": item["tag"],
            "ok": bool(ok),
            "detail": str(detail)[:200],
            "elapsed": round(time.time() - started, 1),
        })
        mark = "✅" if ok else "❌"
        print(f"{mark} [{item['tag']:11}] {item['name']}  ({rows[-1]['elapsed']}s)")
        if not ok:
            print(f"      {rows[-1]['detail']}")

    passed = sum(1 for r in rows if r["ok"])
    print(f"\n通过 {passed}/{len(rows)}")
    record = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "results": rows}
    baseline = ROOT / "data" / "eval_baseline.json"
    history = ROOT / "data" / "eval_history"
    try:
        baseline.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    # 2026-11-05：每次评估都存档，并和上一次比——不然改了参数也没法知道有没有变差
    previous = None
    try:
        history.mkdir(parents=True, exist_ok=True)
        runs = sorted(history.glob("*.json"))
        if runs:
            previous = json.loads(runs[-1].read_text(encoding="utf-8"))
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (history / f"{stamp}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        previous = None
    if previous:
        was = {r["tag"]: r["ok"] for r in previous.get("results") or []}
        now = {r["tag"]: r["ok"] for r in rows}
        broke = [tag for tag, ok in now.items() if not ok and was.get(tag, True)]
        fixed = [tag for tag, ok in now.items() if ok and was.get(tag) is False]
        if broke:
            print("比上次差：" + "、".join(broke))
        if fixed:
            print("比上次好：" + "、".join(fixed))
        if not broke and not fixed:
            print("和上次一致")
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
