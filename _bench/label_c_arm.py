# -*- coding: utf-8 -*-
r"""人工标注 C 臂：逐题显示记忆，你输入应计入的个数。

为什么要有这个工具：
  三臂分解里 C 臂（人工盲标纳入项）是唯一能区分「模型判错」和「题目/金标有问题」的那一臂。
  你标的时候**不要看金标、不要看模型答案**——工具默认也不显示它们，
  标完之后才一次性对答案。

标的时候盯住这几类坑（我预标三题时总结的）：
  1. **等于不算超过**：「七天」不算「超过一周」；「162cm」不算「超过 162」
  2. **同一实体多条记忆**：8 个人在 8 条记忆里重复出现 → 只算一遍
  3. **单位换算**：两周=14天、九天=9天，别被「七周」看漏
  4. **没写时长的不算**：问「超过一周的活动」，没提时长的记忆不算
  5. **实体混淆**：`Real Estate Hub` ≠ `Real Estate Summit`
  6. **没把握就标 `?`**，后面单独人工复核

用法（在终端里跑，逐题交互）：
    & 'python' label_c_arm.py

    # 从第 N 题开始（标过一部分后续接）
    ... label_c_arm.py --start 5

    # 只看某道题的详情不标
    ... label_c_arm.py --peek 7
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
BENCH = Path(r"_bench")
CKPT = BENCH / "ckpt-osf300.json"
OUT = BENCH / "c-arm-labels.json"
# 2026-11-26（开发者指出）：记忆是中文压缩摘要，但实体名还是英文，中英混排容易标错。
# 改用 **英文原文** —— MemBench 本身是英文基准，原文是全英文的、短、好读。
CACHE = BENCH / "cache" / "ingest-ThirdAgentDataLowLevel-n300.json"


def english_memories(cache: dict, gid: str, limit: int = 20) -> list[str]:
    """取英文原文；没有就退回压缩摘要。"""
    group = cache.get(gid) or {}
    out = []
    for m in (group.get("items") or [])[:limit]:
        time = str(m.get("time") or "")
        raw = str(m.get("raw") or "").strip()
        comp = str(m.get("compressed") or "").strip()
        text = raw or comp
        if not text:
            continue
        out.append(f"({time}) {text}" if time else text)
    return out

HINTS = """
  标之前默念这几条（都是我们踩过的坑）：
    ① 等于不算超过：「七天」不算「超过一周」，「162cm」不算「超过162」
    ② 同一实体在多条记忆里重复出现 → 只算一遍
    ③ 单位要换算清楚：两周=14天、七周=49天
    ④ 没写时长/没提那个属性的记忆，不算
    ⑤ 名字相近但不同的实体不要混（Hub ≠ Summit）
    ⑥ 没把握就输入 ?，别硬猜
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=1, help="从第几题开始")
    ap.add_argument("--limit", type=int, default=999)
    ap.add_argument("--peek", type=int, default=0, help="只看这一题，不标注")
    args = ap.parse_args()

    data = json.loads(CKPT.read_text(encoding="utf-8"))
    cache = {}
    try:
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    items = data if isinstance(data, list) else data.get("items") or []
    cq = [x for x in items
          if re.search(r"how many|altogether|in total", str(x.get("question")), re.I)
          and not x.get("correct")]

    done: dict = {}
    if OUT.exists():
        try:
            done = json.loads(OUT.read_text(encoding="utf-8")).get("labels") or {}
        except Exception:  # noqa: BLE001
            done = {}

    if args.peek:
        idx = args.peek - 1
        if 0 <= idx < len(cq):
            x = cq[idx]
            print(f"\n  #{args.peek}  {x.get('question')}")
            _mems = english_memories(cache, str(x.get("gid") or ""))
            for i, m in enumerate(_mems or (x.get("used_memories") or [])[:20], 1):
                print(f"    {i:2}. {str(m)[:190]}")
            ch = x.get("choices") or {}
            print(f"\n  选项: {ch}")
            print(f"  金标 {x.get('truth')} = {ch.get(x.get('truth'))}")
            print(f"  模型 {x.get('pred')} = {ch.get(x.get('pred'))}")
        return 0

    print(f"\n  共 {len(cq)} 道计数错题，从第 {args.start} 题开始")
    print(f"  已标过 {len(done)} 道")
    print("  (memories below are ENGLISH originals)")
    print("  输入数字标注 / ? 表示没把握 / q 退出 / s 跳过")
    print("=" * 72)

    for idx in range(args.start - 1, min(len(cq), args.start - 1 + args.limit)):
        x = cq[idx]
        key = str(idx + 1)
        print(f"\n  #{idx+1}/{len(cq)}   {x.get('question')}")
        print("  " + "-" * 68)
        _mems = english_memories(cache, str(x.get("gid") or ""))
        for i, m in enumerate(_mems or (x.get("used_memories") or [])[:20], 1):
            print(f"    {i:2}. {str(m)[:190]}")
        prev = done.get(key)
        tip = f"（上次标的是 {prev}，直接回车保留）" if prev else ""
        ans = input(f"\n  应计入几个？{tip} > ").strip()
        if ans.lower() == "q":
            break
        if ans.lower() == "s":
            continue
        if not ans and prev is not None:
            continue
        done[key] = ans if ans else "?"
        OUT.write_text(json.dumps({"labels": done, "total": len(cq)},
                                  ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n  已存 {OUT.name}，标了 {len(done)} 道")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
