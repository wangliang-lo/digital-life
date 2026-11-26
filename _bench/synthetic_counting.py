# -*- coding: utf-8 -*-
"""控制变量实验：计数失败到底来自「漏/多」还是「合并/拆分」？

为什么做（GPT 审稿的核心批评）：
  > pⁿ 假说预测的是「n 个局部判断**全对**」，**不是最终计数正确率**。
  > 反例：漏算一个 + 多算一个 → 清单错两处，答案值照样对。
  > 在固定候选、只做二元筛选的简化任务里，计数正确的条件是
  > **假阳性数 = 假阴性数**，不是两者都为零。
  > 而且当前的 pⁿ 写法「什么结果都能事后解释」，不可证伪。

所以要造一个**完全可控**的实验：
  · 合成候选清单（人名 + 城市 + 年龄），数量 n 可控
  · 谓词简单（"来自德州的"），答案值 k 可控
  · **没有重复、没有更新、没有时间** —— 只测纯筛选
  · 要求模型输出**符合条件的名字清单**，不是数字

三个测量：
  1. **清单全对率**（set match）—— 和 pⁿ 对应的指标
  2. **计数正确率**（count correct）—— 和最终答题对应的指标
  3. **假阳性 / 假阴性分布** —— 看是漏还是多

判据：
  · 如果「清单全对率」随 n 快速下降，而「计数正确率」下降更平缓
    → 说明**漏和多互相抵消**，这正是 GPT 说的 pⁿ 预测错了指标
  · 如果 FP 和 FN 系统性偏向一边（总是少或多）
    → 说明是单向偏差，不是随机误差

用法：
    python synthetic_counting.py --n-list 10 20 40 --reps 6
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import statistics
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
BENCH = Path(r"_bench")
sys.path.insert(0, r".")
sys.path.insert(0, str(BENCH))

NAMES = ["李明", "王芳", "张伟", "刘洋", "陈静", "杨帆", "赵磊", "孙娜",
         "周涛", "吴敏", "郑昊", "冯雪", "蒋鹏", "韩梅", "曹阳", "许琳"]
CITIES = ["德州奥斯汀", "德州休斯顿", "加州洛杉矶", "纽约布鲁克林",
          "华盛顿西雅图", "伊利诺伊芝加哥"]

PROMPT = """下面是一份名单。

{rows}

问题：**有几个人的出生地是德州？他们分别是谁？**

请只输出符合条件的人名，一行一个，不要编号，不要解释。
如果没有人符合，只输出 NONE。"""


def make_case(n: int, rng: random.Random, messy: bool = False, pack: int = 1) -> tuple[str, list[str], int]:
    picks = rng.sample(NAMES, min(n, len(NAMES)))
    rows, gold = [], []
    for i, name in enumerate(picks):
        city = rng.choice(CITIES)
        age = rng.randint(22, 45)
        if messy:
            # 2026-11-26：模仿 MemBench 的压缩摘要格式——
            # 一行塞多个属性、加方括号标签、混入无关信息
            job = rng.choice(["在我手下工作", "是我大学同学", "刚搬过去", "在那边开书店", ""])
            tag = rng.choice(["人名,地名,职场", "地点,同事", "人名,家庭", "地名"])
            rows.append(f"{name}来自{city}，{age}岁{('，' + job) if job else ''}。［{tag}］")
        else:
            rows.append(f"{name}，{age}岁，出生地{city}")
        if "德州" in city:
            gold.append(name)
    # 2026-11-26：把多个人**打包进同一行**，模仿 MemBench 真实记忆
    # （"Sienna 162，Jasper 166，Lila 170，Dorian 163..." 一行 8 个人）
    if pack > 1 and len(rows) > pack:
        packed = []
        for i in range(0, len(rows), pack):
            chunk = rows[i:i + pack]
            packed.append("；".join(chunk))
        rows = packed
    return "\n".join(rows), gold, len(gold)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-list", type=int, nargs="+", default=[10, 20, 40])
    ap.add_argument("--reps", type=int, default=6)
    ap.add_argument("--pack", type=int, default=1,
                    help="每行塞几个人（MemBench 真实记忆里一行常有好几个）")
    ap.add_argument("--messy", action="store_true",
                    help="用 MemBench 风格的混排格式（一行多属性+方括号标签）")
    args = ap.parse_args()

    from run_membench_pipeline import deepseek_key
    from digital_life import chat_with_llm
    os.environ["DEEPSEEK_API_KEY"] = deepseek_key()

    rng = random.Random(20261126)
    rows_out = []
    print(f"  候选长度 {args.n_list}，每种 {args.reps} 次\n")

    for n in args.n_list:
        set_ok = cnt_ok = 0
        fps, fns, ks = [], [], []
        for r in range(args.reps):
            block, gold, k = make_case(n, rng, args.messy, args.pack)
            try:
                out = str(chat_with_llm([{"role": "user", "content": PROMPT.format(rows=block)}],
                                        model="deepseek-chat", temperature=0.0,
                                        timeout=120.0) or "")
            except Exception:  # noqa: BLE001
                out = ""
            got = [x.strip() for x in out.splitlines()
                   if x.strip() and x.strip() != "NONE"]
            # 只保留名单里的人名（防止它写解释）
            got = [x for x in got if any(nm in x for nm in NAMES)]
            got_set, gold_set = set(got), set(gold)
            fp = len(got_set - gold_set)      # 多算
            fn = len(gold_set - got_set)      # 漏算
            exact = got_set == gold_set
            count_ok = len(got_set) == len(gold_set)
            set_ok += 1 if exact else 0
            cnt_ok += 1 if count_ok else 0
            fps.append(fp); fns.append(fn); ks.append(k)
            rows_out.append({"n": n, "rep": r, "k": k, "got_n": len(got_set),
                             "fp": fp, "fn": fn, "exact": exact, "count_ok": count_ok})
            print(f"  n={n:2} #{r+1}  金标 {k} 个，它列 {len(got_set)} 个"
                  f"（多 {fp} 漏 {fn}） {'清单全对' if exact else ('数量对' if count_ok else '数量错')}")

        print(f"  → n={n}: **清单全对 {set_ok}/{args.reps}**，"
              f"**数量正确 {cnt_ok}/{args.reps}**，"
              f"平均多 {statistics.mean(fps):.1f} 漏 {statistics.mean(fns):.1f}\n")

    # 汇总
    print("=" * 66)
    print(f"  {'n':>4} {'清单全对率':>10} {'数量正确率':>10} {'平均多':>7} {'平均漏':>7}")
    for n in args.n_list:
        sub = [x for x in rows_out if x["n"] == n]
        if not sub:
            continue
        se = 100 * sum(1 for x in sub if x["exact"]) / len(sub)
        co = 100 * sum(1 for x in sub if x["count_ok"]) / len(sub)
        fp = statistics.mean(x["fp"] for x in sub)
        fn = statistics.mean(x["fn"] for x in sub)
        print(f"  {n:>4} {se:>9.0f}% {co:>9.0f}% {fp:>7.1f} {fn:>7.1f}")
    print()
    print("  判读：")
    print("    清单全对率随 n 掉得比数量正确率快 → **漏和多互相抵消**（pⁿ 预测错了指标）")
    print("    平均多/漏都接近 0 但数量仍错     → 抵消现象")
    print("    平均漏 >> 平均多                 → **单向漏检**，该修的是覆盖策略")
    print("=" * 66)
    out = BENCH / "synthetic-counting.json"
    out.write_text(json.dumps(rows_out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  已存 {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
