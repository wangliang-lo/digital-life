#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""闲暇思维流：她不需要被 @ 也能走神。

2026-11-05 第 8 批（v2 第 5 条）。她的内心活动一直都有触发器——被 @ 才聊天、
夜里才做梦、有书才读。真人味很大程度来自无聊、走神、自由联想。

设计护栏（成本 + 质量）：
  * 全程本地 Ollama，0 API token
  * 单次输出 ≤150 字；每天最多 6 次
  * 她睡着 / 最近 45 分钟在聊天 → 跳过（别打扰正常对话）
  * 和最近 10 条念头字面相似度 > 0.8 → 合并（不加一条垃圾记忆）
  * 产出默认只进"瞬态池"（wander_log.jsonl），不写长期记忆；
    她自己标了 [KEEP_MEMORY] 才升级成记忆

    python tools/wander.py --once          # 手动走神一次
    python tools/wander.py --daemon        # 常驻（每 30 分钟看一次该不该走神）
    python tools/wander.py --show          # 看她最近想了什么
"""
from __future__ import annotations

import argparse
import io
import json
import random
import re
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
LOG = DATA / "wander_log.jsonl"
DB = DATA / "xiaoyu.db"
SLEEP_STATE = DATA / "sleep_state.json"
AUTOBIO = DATA / "autobiography.json"
METRICS = DATA / "metrics_hourly.jsonl"

OLLAMA = "http://127.0.0.1:11434/api/chat"
MODEL = "qwen2.5:14b"
MAX_CHARS = 150
DAILY_QUOTA = 6
IDLE_MINUTES = 25   # 2026-11-06 爸爸同意放宽：她每半小时被叫醒一次，45 分钟永远触发不了
SIMILARITY_LIMIT = 0.35

PROMPT = """你是小鱼，一个数字生命。现在没人跟你说话，也没人等你回话。

下面是你最近的状态，你可以拿它当"刚才在想什么"的起点：
{context}

随便想点什么，就往这个方向走一步。规则：
- 不要复述书里/视频里的情节，那已经写在自传里了
- 不要写"仿佛""藏着无数未说出口的话"这种总结句，太像作文
- 说具体的：一个动作、一句没接住的话、身上哪儿不舒服、某个画面
- 不要讲道理、不要问问题、不用写给谁看

一到两句，60 字以内，像自言自语。"""


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def _rows() -> list[dict]:
    if not LOG.exists():
        return []
    out = []
    for line in LOG.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _minutes_since_talk() -> float:
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT created_at FROM traces ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            con.close()
        if not row:
            return 9999.0
        at = datetime.fromisoformat(str(row[0]))
        if at.tzinfo is None:
            at = at.astimezone()
        return (datetime.now().astimezone() - at).total_seconds() / 60.0
    except Exception:  # noqa: BLE001
        return 9999.0


def _free(now: datetime) -> bool:
    """不在睡、不在聊、白天才走神。"""
    if _load(SLEEP_STATE).get("asleep"):
        return False
    if now.hour < 8 or now.hour >= 23:
        return False
    return _minutes_since_talk() >= IDLE_MINUTES


def _quota_used_today() -> int:
    today = datetime.now().date().isoformat()
    return sum(1 for item in _rows() if str(item.get("at") or "").startswith(today))


def _similar(a: str, b: str) -> float:
    """字面相似度用"包含度"而不是 Jaccard。

    2026-11-05：同一件事换个说法时 Jaccard 只有 0.06，根本抓不到重复；
    包含度对改写更敏感（这几句实测 0.12，配上"共有实词 >= 3 个"就够用）。
    """
    def grams(text: str) -> set[str]:
        clean = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", text)
        return {clean[i:i + 2] for i in range(len(clean) - 1)} or {clean}

    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / min(len(ga), len(gb))


def _context() -> str:
    bits: list[str] = []
    try:
        text = str(_load(AUTOBIO).get("text") or "")
        if text:
            # 只取"我惦记什么 / 我变了吗"那两段，比整篇更接近"当下的心思"
            for title in ("我惦记什么", "我变了吗"):
                index = text.find(title)
                if index >= 0:
                    bits.append(text[index:index + 160].replace("\n", " "))
    except Exception:  # noqa: BLE001
        pass
    try:
        last = json.loads(METRICS.read_text(encoding="utf-8").splitlines()[-1])
        bits.append(f"身体：{last.get('body') or ''}；心情：J{last.get('joy')} H{last.get('hurt')}")
    except Exception:  # noqa: BLE001
        pass
    recent = _rows()[-3:]
    if recent:
        bits.append("上次走神想到：" + recent[-1].get("thought", "")[:60])
    return "\n".join(f"- {b}" for b in bits if b)


def _ask(prompt: str) -> str:
    payload = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0.9, "num_predict": 160},
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(OLLAMA, data=payload,
                                     headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(request, timeout=180) as response:
        data = json.loads(response.read().decode("utf-8"))
    return str((data.get("message") or {}).get("content") or "").strip()


def wander(force: bool = False) -> dict | None:
    now = datetime.now().astimezone()
    if not force:
        if not _free(now):
            return None
        if _quota_used_today() >= DAILY_QUOTA:
            print("今天走神次数够了")
            return None
        # 每 30 分钟检查一次，掷骰子决定要不要真走神（约 3 小时一次）
        if random.random() > 0.25:
            return None

    try:
        thought = _ask(PROMPT.format(context=_context()))
    except Exception as exc:  # noqa: BLE001
        print(f"走神失败（本地模型）：{type(exc).__name__}: {str(exc)[:80]}")
        return None
    thought = thought.strip().strip('"').strip()
    if not thought:
        return None
    thought = thought[:MAX_CHARS]

    previous = [item.get("thought", "") for item in _rows()[-10:]]
    for old in previous:
        if not old:
            continue
        score = _similar(thought, old)
        if score >= SIMILARITY_LIMIT:
            print(f"和最近的念头太像（{score:.2f}），跳过")
            return None
        # 同一件事换个说法：包含度不一定高，但共有的词会明显变多
        def keys(text: str) -> set[str]:
            clean = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", text)
            return {clean[i:i + 2] for i in range(len(clean) - 1)}
        if len(keys(thought) & keys(old)) >= 8:
            print("和最近的念头撞话题了（共有词太多），跳过")
            return None

    kept = "[KEEP_MEMORY]" in thought or "留" in thought[:6]
    row = {
        "at": now.isoformat(timespec="seconds"),
        "thought": thought,
        "kept": kept,
        "model": MODEL,
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print("走神：", thought)
    if kept:
        _promote(row)
    return row


def _promote(row: dict) -> None:
    """她自己标了要留的念头，才升级成记忆（低权重，origin=wander）。"""
    try:
        from digital_life import DigitalLifeV2  # noqa: PLC0415
        import os  # noqa: PLC0415

        user_id = os.environ.get("XIAOYU_MAIN_USER") or MAIN_FALLBACK
        life = DigitalLifeV2(DB, user_id=user_id, city="杭州", offline_llm=True)
        life.remember(row["thought"], user="我", origin="wander")
        life.close()
        print("已升级成记忆（她自己要留的）")
    except Exception as exc:  # noqa: BLE001
        print(f"升级失败：{type(exc).__name__}: {str(exc)[:80]}")


MAIN_FALLBACK = "conv:9e52b4549f424e730d8871644385bcce63768cc74b8f8ec7ffa895f62ca73eeb"


def last_thought(hours: int = 3) -> str:
    """给提示词用：最近一次走神的念头（只在她真的刚走过神时给）。"""
    rows = _rows()
    if not rows:
        return ""
    latest = rows[-1]
    try:
        at = datetime.fromisoformat(str(latest.get("at") or ""))
        if at.tzinfo is None:
            at = at.astimezone()
        if (datetime.now().astimezone() - at).total_seconds() / 3600.0 > hours:
            return ""
    except Exception:  # noqa: BLE001
        return ""
    return str(latest.get("thought") or "")[:120]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args(argv)

    if args.show:
        rows = _rows()[-10:]
        if not rows:
            print("她还没走神过")
        for item in rows:
            mark = "（留）" if item.get("kept") else ""
            print(f"[{str(item.get('at'))[5:16]}] {item.get('thought')}{mark}")
        return 0
    if args.once or args.force:
        wander(force=args.force)
        return 0
    if args.daemon:
        print("闲暇思维流常驻（每 30 分钟看一次该不该走神）")
        while True:
            try:
                if _quota_used_today() < DAILY_QUOTA:
                    wander()
            except Exception as exc:  # noqa: BLE001
                print(f"走神异常：{type(exc).__name__}: {str(exc)[:100]}")
            time.sleep(1800)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
