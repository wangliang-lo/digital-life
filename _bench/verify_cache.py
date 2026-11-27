# -*- coding: utf-8 -*-
"""验一件关键的事：常驻日记块到底能不能走前缀缓存。

背景（Claude 2026-11-25）：
  她以前每 20 轮才塞一次日记（3623 token），现在是三层常驻（2273 token）。
  名义 token 变多了，Claude 的判断是「小而常驻优于大而间歇」——
  因为**稳定的前缀能走缓存**（DeepSeek 缓存命中约 1/10 价）。

  但这只是推算。这个脚本直接量：用她真实的 system prompt 连打两次，
  看 usage 里的 prompt_cache_hit_tokens / prompt_cache_miss_tokens。

用法：
    python verify_cache.py
    python verify_cache.py --repeat 3
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(r"<repo>")
BENCH = ROOT / "_exp" / "bench"
EXP = ROOT / "_exp" / "xiaoyu-exp"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(EXP))
sys.path.insert(0, str(BENCH))

API = "https://api.deepseek.com/chat/completions"


def call(system: str, user: str, key: str, timeout: float = 120.0) -> dict:
    body = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.7,
        "max_tokens": 200,
        "stream": False,
    }
    req = urllib.request.Request(
        API, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=3)
    args = ap.parse_args()

    from memory import RetrievalResult
    from digital_life import DigitalLifeV2, embed_text
    from run_membench_pipeline import deepseek_key

    key = deepseek_key()
    if not key:
        print("  拿不到 key"); return 1

    life = DigitalLifeV2(user_id="default", db_path=EXP / "chat_bridge" / "data" / "xiaoyu.db",
                         offline_llm=False, embed_on_write=False, embedding_fn=embed_text)
    life.turn_count = 5
    rr = RetrievalResult(hits=[], deep=False, impulse=0.0, retrieve_prob=1.0, roll=0.0, note="")

    msgs = life._build_messages("今天有点累", rr, None)
    system = next((m["content"] for m in msgs if m.get("role") == "system"), "")
    print(f"  system {len(system)} 字 ≈ {int(len(system)/1.6)} token")
    diary_len = len(life.growth_diary_block())
    print(f"  其中日记块 {diary_len} 字 ≈ {int(diary_len/1.6)} token")
    print()

    # 第一次：写缓存；之后几次：应该命中
    for i in range(args.repeat):
        user = f"随便说一句，第 {i+1} 次。"   # 只变尾部，前缀不变
        try:
            out = call(system, user, key)
        except Exception as exc:  # noqa: BLE001
            print(f"  第 {i+1} 次失败：{type(exc).__name__}: {exc}")
            continue
        u = out.get("usage") or {}
        hit = int(u.get("prompt_cache_hit_tokens") or 0)
        miss = int(u.get("prompt_cache_miss_tokens") or 0)
        total = int(u.get("prompt_tokens") or 0)
        rate = 100.0 * hit / total if total else 0.0
        reply = (out.get("choices") or [{}])[0].get("message", {}).get("content", "")
        print(f"  第 {i+1} 次: prompt={total}  hit={hit}  miss={miss}  命中率 {rate:.0f}%")
        print(f"    回复: {reply[:60]!r}")
        if i == 0:
            time.sleep(2)   # 给缓存一点落地时间

    print()
    print("  判读：")
    print("    第一次 miss 多、从第二次起 hit 接近 system 长度 → 常驻方案省钱成立")
    print("    一直 hit=0 → 缓存没生效（可能是频繁变动或服务端未缓存），要重新算账")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
