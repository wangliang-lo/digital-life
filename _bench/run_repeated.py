# -*- coding: utf-8 -*-
"""重复随机划分评估 —— 报**均值 ± 95% 置信区间**，不报单次结果。

为什么（2026-11-25 爸爸给的《小样本机器学习顶刊评估规范》）：
  1. 单次随机划分在小样本下会刷出虚高指标 —— 我今天一直在「取前 N 条」，
     而且**在同一个 n=100 上反复调参**，等于拿测试集当调参集（数据泄漏）。
  2. 规范要求：重复执行多次分层划分，报告均值 ± 置信区间。
  3. 预处理/调参只能在训练侧，评估集必须没被看过。

实测噪声底（同配置 n=200 跑两次）：81.5% vs 83.0%，7/200 翻转，差 1.5pp。
所以任何小于这个的差异都不能当结论。

用法：
    python run_repeated.py --file ThirdAgentDataLowLevel.json --n 100 --reps 5
    # 自动用 5 个不同 seed 随机抽样，报均值 ± 95%CI
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
BENCH = Path(r"C:\aihuman\_exp\bench")
PY = r"C:\Users\Lenovo\anaconda3\python.exe"

# 95% CI 的 t 值（双侧，df = reps-1）
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
       6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228}


def run_once(file: str, n: int, seed: int, extra: list[str]) -> dict:
    env = dict(os.environ)
    env["XIAOYU_EXP"] = "1"
    env["XIAOYU_SPLIT_SEED"] = str(seed)
    env["XIAOYU_SPLIT_POOL"] = "300"
    env.pop("XIAOYU_COUNT_LIST", None)
    env.pop("XIAOYU_UPDATE_RULE", None)
    cmd = [PY, "run_membench_pipeline.py", "--stage", "eval", "--file", file,
           "--n", str(n), "--embed", "--topk", "25", "--raw-content", *extra]
    proc = subprocess.run(cmd, cwd=str(BENCH), env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="ignore", timeout=5400)
    out = (proc.stdout or "") + (proc.stderr or "")
    acc = re.search(r"准确率\s+([\d.]+)%", out)
    r5 = re.search(r"Recall@5\s+([\d.]+)%", out)
    r10 = re.search(r"Recall@10\s+([\d.]+)%", out)
    ckpt = sorted(BENCH.glob("ckpt-eval-*.json"))
    saved = ""
    if ckpt:
        dst = BENCH / f"ckpt-rep-{Path(file).stem}-n{n}-s{seed}.json"
        dst.write_bytes(ckpt[0].read_bytes())
        saved = dst.name
        ckpt[0].unlink()
    return {"seed": seed, "acc": float(acc.group(1)) if acc else None,
            "r5": float(r5.group(1)) if r5 else None,
            "r10": float(r10.group(1)) if r10 else None, "ckpt": saved}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--topic", action="store_true")
    args = ap.parse_args()

    extra = ["--topic"] if args.topic else []
    rows = []
    for k in range(args.reps):
        seed = 101 + k * 17          # 固定一组可复现的 seed
        print(f"  第 {k+1}/{args.reps} 次（seed={seed}）……", flush=True)
        r = run_once(args.file, args.n, seed, extra)
        rows.append(r)
        print(f"    准确率 {r['acc']}%  Recall@5 {r['r5']}%  Recall@10 {r['r10']}%")

    # 守卫：随机抽样抽到没整理过的题时，库里没记忆，分数会是 0/near-0。
    # 那不是模型差，是量具坏了。必须先 --stage ingest 把池子做大。
    broken = [r for r in rows if (r["r10"] or 0) == 0.0]
    if broken:
        print()
        print("  ✗ 有 {} 次 Recall@10 = 0% —— 说明随机抽样抽到了".format(len(broken)))
        print("    **没整理过的题**。ingest 缓存是按前缀存的（前 N 条），")
        print("    随机划分必须先跑 `--stage ingest` 把池子做大，再抽。")
        print("    这些数字不可用，已中止。")
        return 2

    good = [r["acc"] for r in rows if r["acc"] is not None]
    if not good:
        print("  全部失败")
        return 1
    mean = statistics.mean(good)
    sd = statistics.stdev(good) if len(good) > 1 else 0.0
    t = T95.get(len(good) - 1, 2.0)
    ci = t * sd / (len(good) ** 0.5) if len(good) > 1 else 0.0
    print()
    print("=" * 62)
    print(f"  {args.file}  n={args.n}  重复 {len(good)} 次（随机划分）")
    print(f"  准确率  **{mean:.1f}% ± {ci:.1f}pp**（95%CI，sd={sd:.1f}）")
    print(f"  单次范围 {min(good):.1f}% ~ {max(good):.1f}%")
    for key, lab in (("r5", "Recall@5"), ("r10", "Recall@10")):
        vals = [r[key] for r in rows if r[key] is not None]
        if vals:
            print(f"  {lab}: 均值 {statistics.mean(vals):.1f}%  "
                  f"（范围 {min(vals):.1f}~{max(vals):.1f}）")
    print("=" * 62)
    out = BENCH / f"repeated-{Path(args.file).stem}-n{args.n}.json"
    out.write_text(json.dumps({"file": args.file, "n": args.n, "rows": rows,
                               "mean": mean, "ci95": ci, "sd": sd},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  明细已存 {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
