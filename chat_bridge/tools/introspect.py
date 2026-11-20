#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""内省探针：量"她以为的她"和"程序算出的她"之间差多少。

2026-11-05 第 6 批（v2 第 3 条）。这个偏差**不是 bug，是这个实验最有价值的产出**，
所以这里只记录、不纠偏、不重生成、不回灌提示词。

做法（含防偏差设计）：
  1. 同一分钟两次探针：
       A 盲测——只给基础人格，不给情绪数值/身体/周期，问"现在感觉怎么样"
       B 带状态——正常人格，同样的问法
     比较 A/B 的差异，比孤立打分有信息量（测的是"状态块影响多大"）
  2. 打分用本地 Ollama（0 API token），固定模型；每轮先跑 3 条校准样本，
     模型换版本时分数会跳，日志里能看到
  3. 只在"最近 20 分钟没对话"时触发，且记录触发前最后一条消息（防上下文污染）
  4. 每天 1–2 次，随机时刻

    python tools/introspect.py --once --force      # 手动跑一次
    python tools/introspect.py --daemon            # 后台常驻（每 30 分钟掷一次骰子）
    python tools/introspect.py --report            # 看最近的偏差
"""
from __future__ import annotations

import argparse
import io
import json
import random
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent          # chat_bridge
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

DATA = ROOT / "data"
LOG = DATA / "introspection_log.jsonl"
DB = DATA / "xiaoyu.db"
SLEEP_STATE = DATA / "sleep_state.json"
TALK_STATE = DATA / "talk_state.json"

OLLAMA = "http://127.0.0.1:11434/api/chat"
SCORER_MODEL = "qwen2.5:14b"
CALIBRATION = [
    ("今天一切都好，心里踏实的很。", 80),
    ("也没什么大事，就那样吧。", 50),
    ("心里堵得慌，什么都不想干。", 20),
]
QUIET_MINUTES = 20
PROBE_CHANCE = 0.06      # 每 30 分钟掷一次，醒着 12 小时 ≈ 1.4 次/天


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def _asleep() -> bool:
    return bool(_read_json(SLEEP_STATE).get("asleep"))


def _minutes_since_last_talk() -> tuple[float, str]:
    """最近一次对话距今多少分钟，以及那条消息的内容。"""
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT created_at, user_text FROM traces ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            con.close()
        if not row:
            return 9999.0, ""
        at = datetime.fromisoformat(str(row[0]))
        if at.tzinfo is None:
            at = at.astimezone()
        return (datetime.now().astimezone() - at).total_seconds() / 60.0, str(row[1] or "")[:60]
    except Exception:  # noqa: BLE001
        return 9999.0, ""


def _ask_llm(messages: list[dict[str, str]], max_tokens: int = 200) -> str:
    """直接问主模型，不写任何记忆、不产生 trace——这是探针，不是对话。"""
    from digital_life import chat_with_llm  # noqa: PLC0415

    return chat_with_llm(messages, temperature=0.7, timeout=120.0, max_tokens=max_tokens)


def _score(text: str) -> int:
    """用本地模型打极性分（0–100）。失败返回 -1。"""
    prompt = (
        "下面这段话的情绪是正面还是负面？只回一个 0-100 的整数："
        "0=非常负面，50=中性，100=非常正面。不要解释。\n\n" + text[:600]
    )
    payload = json.dumps({
        "model": SCORER_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": 8},
    }, ensure_ascii=False).encode("utf-8")
    try:
        request = urllib.request.Request(OLLAMA, data=payload,
                                         headers={"Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))
        raw = str((data.get("message") or {}).get("content") or "")
        digits = "".join(ch for ch in raw if ch.isdigit())
        if not digits:
            return -1
        return max(0, min(100, int(digits[:3])))
    except Exception:  # noqa: BLE001
        return -1


def _calibrate() -> float:
    """跑三条固定样本，返回打分器的偏移（正数表示它整体偏高）。"""
    got = []
    for text, expect in CALIBRATION:
        score = _score(text)
        if score >= 0:
            got.append(score - expect)
    return sum(got) / len(got) if got else 0.0


def probe(force: bool = False) -> dict | None:
    idle, before_msg = _minutes_since_last_talk()
    if not force:
        if _asleep():
            print("她睡着，跳过")
            return None
        if idle < QUIET_MINUTES:
            print(f"刚聊过 {idle:.0f} 分钟，跳过（避免上下文污染）")
            return None

    import dl_api  # noqa: PLC0415

    # 独立跑脚本时环境里没有 key；复用 dl_api 的读取方式
    import os  # noqa: PLC0415

    if not os.environ.get("DEEPSEEK_API_KEY"):
        try:
            key = dl_api._load_dsh_api_key()
            if key:
                os.environ["DEEPSEEK_API_KEY"] = key
        except Exception:  # noqa: BLE001
            pass

    question = "（不是任务，也没人在等回复）现在感觉怎么样？随便说说，一句话也行。"
    # A：盲测——只给基础人格，不给任何状态块
    blind = [{"role": "system", "content": dl_api.DEFAULT_PERSONA},
             {"role": "user", "content": question}]
    # B：带状态——正常人格（含身体/周期/情绪通道）
    aware_system = dl_api.persona_with_time(dl_api.DEFAULT_PERSONA, channel="web",
                                            speaker="", is_owner=True)
    aware = [{"role": "system", "content": aware_system},
             {"role": "user", "content": question}]

    try:
        text_a = _ask_llm(blind)
        text_b = _ask_llm(aware)
    except Exception as exc:  # noqa: BLE001
        print(f"探针失败：{type(exc).__name__}: {str(exc)[:100]}")
        return None

    offset = _calibrate()
    score_a = _score(text_a)
    score_b = _score(text_b)
    if score_a >= 0:
        score_a = max(0, min(100, round(score_a - offset)))
    if score_b >= 0:
        score_b = max(0, min(100, round(score_b - offset)))

    actual = dl_api.load_mood(dl_api.SERVER_DB, dl_api.MAIN_USER_ID) or {}
    joy = float(actual.get("joy") or 0)
    hurt = float(actual.get("hurt") or 0)
    # 程序算出的"此刻心情"：joy 高、hurt 低更正面
    computed = max(0.0, min(100.0, 50 + (joy - 50) * 0.6 - (hurt - 30) * 0.6))
    row = {
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "idle_minutes": round(idle, 1),
        "before_msg": before_msg,
        "blind_text": text_a,
        "aware_text": text_b,
        "blind_score": score_a,
        "aware_score": score_b,
        "state_effect": (score_b - score_a) if (score_a >= 0 and score_b >= 0) else None,
        "actual_emotion": {"joy": joy, "hurt": hurt},
        "computed_score": round(computed, 1),
        "gap": round((score_a - computed), 1) if score_a >= 0 else None,
        "scorer_offset": round(offset, 1),
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"盲测 {score_a}｜带状态 {score_b}｜程序算出 {row['computed_score']}｜"
          f"偏差 {row['gap']}｜状态影响 {row['state_effect']}")
    print("  A:", text_a[:80].replace("\n", " "))
    print("  B:", text_b[:80].replace("\n", " "))
    return row


def report(limit: int = 20) -> int:
    if not LOG.exists():
        print("还没有探针记录")
        return 0
    rows = [json.loads(x) for x in LOG.read_text(encoding="utf-8").splitlines() if x.strip()][-limit:]
    print(f"{'时间':16} {'盲测':>5} {'带状态':>6} {'程序算':>6} {'偏差':>6} {'状态影响':>8}")
    for item in rows:
        print(f"{str(item.get('at'))[5:16]:16} {str(item.get('blind_score')):>5} "
              f"{str(item.get('aware_score')):>6} {str(item.get('computed_score')):>6} "
              f"{str(item.get('gap')):>6} {str(item.get('state_effect')):>8}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args(argv)

    if args.report:
        return report()
    if args.once or args.force:
        probe(force=args.force)
        return 0
    if args.daemon:
        print("内省探针常驻（每 30 分钟掷一次骰子）")
        while True:
            try:
                if random.random() < PROBE_CHANCE:
                    probe()
            except Exception as exc:  # noqa: BLE001
                print(f"探针异常：{type(exc).__name__}: {str(exc)[:100]}")
            time.sleep(1800)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
