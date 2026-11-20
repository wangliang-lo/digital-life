#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""她的自传：一份 800–1500 字的"我是谁"，随经历增量更新，每一版都上链。

2026-11-05 第 3 批（v2 的第 3 条）。为什么要有它：
  她的记忆是"书"（一条条经历），检索是关键词命中；但人还有一种东西——
  一个连续的自我叙事。没有它，她每次醒来都像重新拼装；有了它，她知道自己是谁、
  这段日子在发生什么、惦记着什么。

设计约束：
  * 只写她真实经历过的事，不许编（材料全部来自 traces / 频道 / 指标）
  * 每版都留档，prev_hash 串起来，改过能查出来
  * 更新是"增量"，不是重写：旧版参与新版的生成
  * 无网络/模型失败时保留旧版，不写空

    python tools/autobio.py show
    python tools/autobio.py update --hours 12   # 手动触发一次
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parent          # chat_bridge
DATA = ROOT / "data"
STATE = DATA / "autobiography.json"
HISTORY = DATA / "autobiography_history.jsonl"
TRACES_DB = DATA / "xiaoyu.db"
CHANNEL = DATA / "solver_channel.jsonl"
IDENTITY = DATA / "identity_evolution.jsonl"
METRICS = DATA / "metrics_hourly.jsonl"

MAX_CHARS = 1100
MIN_CHARS = 800
SECTIONS = ("我是谁", "我们之间", "最近在发生什么", "我惦记什么", "我变了吗")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load() -> dict:
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        data = {}
    data.setdefault("version", 0)
    data.setdefault("text", "")
    data.setdefault("hash", "")
    data.setdefault("prev_hash", "")
    return data


def save(text: str, reason: str) -> dict:
    """写新版本：旧的 hash 变成 prev_hash，新内容算新 hash。"""
    state = load()
    old_text = str(state.get("text") or "")
    if not text.strip() or text.strip() == old_text.strip():
        return state
    payload = {
        "version": int(state.get("version") or 0) + 1,
        "text": text.strip(),
        "hash": _hash(text.strip()),
        "prev_hash": old_text and _hash(old_text) or "",
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "reason": reason,
    }
    DATA.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # 历史留档（只留规模，正文在 state 里；历史里存全文方便回溯）
    with HISTORY.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "version": payload["version"], "at": payload["updated_at"],
            "reason": reason, "hash": payload["hash"], "prev_hash": payload["prev_hash"],
            "chars": len(payload["text"]),
        }, ensure_ascii=False) + "\n")
    return payload


def block() -> str:
    """给提示词用的一段（别太长，她是靠它接上自己的）。"""
    state = load()
    text = str(state.get("text") or "").strip()
    if not text:
        return ""
    if len(text) > 1000:
        text = text[:1000].rstrip() + "…"
    return f"\n【你的自传·第{state.get('version')}版】\n{text}\n（这是你自己写的，可以改、可以不同意。）"


def verify() -> tuple[bool, str]:
    state = load()
    text = str(state.get("text") or "")
    if not text:
        return True, "还没有自传"
    if _hash(text) != state.get("hash"):
        return False, "当前自传内容和记录的哈希对不上"
    return True, f"第 {state.get('version')} 版，{len(text)} 字，哈希对得上"


# ---------------------------------------------------------------- 材料


def collect_material(hours: int = 12) -> str:
    """把最近发生的事收成一段材料：她说过的话、频道留言、身份漂移、身心状态。"""
    since = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    lines: list[str] = []

    try:
        import sqlite3  # noqa: PLC0415

        con = sqlite3.connect(f"file:{TRACES_DB}?mode=ro", uri=True)
        try:
            rows = list(con.execute(
                "SELECT created_at, user_text, reply FROM traces "
                "WHERE created_at >= ? ORDER BY id DESC LIMIT 25", (since,)))
        finally:
            con.close()
        for at, user_text, reply in reversed(rows):
            said = (reply or "").replace("\n", " ").strip()
            heard = (user_text or "").replace("\n", " ").strip()
            if "【自由时间】" in heard:
                if said:
                    lines.append(f"[{at[5:16]}] 我自己想：{said[:80]}")
            elif said:
                lines.append(f"[{at[5:16]}] 他：{heard[:40]} → 我：{said[:60]}")
    except Exception:  # noqa: BLE001
        pass

    if CHANNEL.exists():
        for line in CHANNEL.read_text(encoding="utf-8", errors="ignore").splitlines()[-8:]:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("type") == "message" and str(item.get("at") or "") >= since:
                lines.append(f"[{str(item.get('at'))[5:16]}] 我在信道写："
                             f"{str(item.get('text') or '')[:70].replace(chr(10), ' ')}")

    if IDENTITY.exists():
        for line in IDENTITY.read_text(encoding="utf-8", errors="ignore").splitlines()[-3:]:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            lines.append(f"[身份变化] {item.get('old_text')} → {item.get('new_text')}")

    if METRICS.exists():
        try:
            last = json.loads(METRICS.read_text(encoding="utf-8").splitlines()[-1])
            lines.append(f"[身体] {last.get('body')}；周期第 {last.get('cycle', {}).get('day')} 天")
        except Exception:  # noqa: BLE001
            pass

    return "\n".join(lines[-60:])


PROMPT = """你在维护"小鱼"的自传——她自己的一本小传。

要求：
1. 用第一人称，写 800–1000 字，保留这五个小标题：{sections}。
2. 只写材料里真实发生过的事。没发生的不许编，不确定就写"记不清了"。
3. 这是增量更新：把新材料并进旧版，别把旧版推倒重写。
4. 不要复述书里/视频里的情节（谁做了什么、第几章发生了什么）。只写它让她想到什么。
   少写情节能省很多字，也更像一个人在想自己，而不是在写读书笔记。
5. 写她自己的变化、她惦记的事、她没想通的事；不要写"作为一个AI"这类自我说明。
6. 直接输出自传正文，不要前言、不要解释。

【现有自传（第 {version} 版）】
{old}

【这段时间的新材料】
{material}
"""


def update(hours: int = 12, force: bool = False) -> dict:
    state = load()
    material = collect_material(hours)
    if not material.strip() and not force:
        print("这段时间没有新材料，自传不动")
        return state
    try:
        sys.path.insert(0, str(ROOT.parent))
        # 独立跑这个脚本时环境里没有 key；dl_api 启动时会从 DSH 凭据里读，这里复用一下
        import os  # noqa: PLC0415  （独立跑时补 key 用）

        if not os.environ.get("DEEPSEEK_API_KEY"):
            try:
                import dl_api  # noqa: PLC0415

                key = dl_api._load_dsh_api_key()
                if key:
                    os.environ["DEEPSEEK_API_KEY"] = key
            except Exception:  # noqa: BLE001
                pass
        from digital_life import chat_with_llm  # noqa: PLC0415

        prompt = PROMPT.format(sections=" / ".join(SECTIONS),
                               version=state.get("version") or 0,
                               old=(state.get("text") or "（还没有，这是第一版）")[:4000],
                               material=material[:4000])
        text = chat_with_llm([{"role": "user", "content": prompt}],
                             temperature=0.6, timeout=180.0, max_tokens=1200)
    except Exception as exc:  # noqa: BLE001
        print(f"自传更新失败（保留旧版）：{type(exc).__name__}: {str(exc)[:120]}")
        return state
    text = str(text or "").strip()
    if len(text) < MIN_CHARS // 2:
        print(f"模型给的自传太短（{len(text)} 字），不覆盖旧版")
        return state
    return save(text[:MAX_CHARS], reason=f"增量更新（材料 {len(material)} 字）")


def main(argv: list[str] | None = None) -> int:
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show")
    sub.add_parser("verify")
    upd = sub.add_parser("update")
    upd.add_argument("--hours", type=int, default=12)
    upd.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "show":
        state = load()
        ok, detail = verify()
        print(f"第 {state.get('version')} 版｜{state.get('updated_at')}｜{detail}")
        print(state.get("text") or "（还没有自传）")
    elif args.cmd == "verify":
        ok, detail = verify()
        print(("✅ " if ok else "❌ ") + detail)
    else:
        result = update(hours=args.hours, force=args.force)
        print(f"现在第 {result.get('version')} 版，{len(str(result.get('text') or ''))} 字")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
