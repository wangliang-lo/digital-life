# -*- coding: utf-8 -*-
"""TOST 等价性检验 —— 把「不是瓶颈」从「没测到差异」改成「等价性成立」。

为什么必须改（GPT 审稿）：
  > 「p = 1.000」不能证明「不是瓶颈」，只能说明「没检测到差异」。
  > 所有「不是瓶颈」的结论都要改用**等价性检验（TOST）**，
  > 预设等价边界（比如 ±3pp），同题配对置信区间。
  > 精度不足就写「等价性未确立」，别写「不是瓶颈」。

TOST 怎么算（配对二分类）：
  两组跑同一批题，记：
    b = A 对 B 错
    c = A 错 B 对
    n = 总题数
  配对差 d = (b - c) / n
  等价边界 margin（预先设定，这里用 3pp）
  两个单侧检验都要拒绝：
    H0_下: d <= -margin
    H0_上: d >= +margin
  等价成立 ⟺ d 的 (1-2α) 置信区间完全落在 (-margin, +margin) 内。

  配对差的 CI 用精确方法（基于 b 的二项分布）：
    b ~ Binomial(b+c, 0.5) 在 H0: d=0 下
  这里用 Wilson/Clopper-Pearson 近似构造 d 的 CI。

用法：
    python tost_equivalence.py ckpt-A.json ckpt-B.json --margin 3
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
BENCH = Path(r"_bench")


def load(p: Path) -> list[dict]:
    d = json.loads(p.read_text(encoding="utf-8"))
    return d if isinstance(d, list) else d.get("items") or []


def binom_ci(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Clopper-Pearson 区间（用正态近似，n≥20 够用）。"""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    z = 1.959963984540054 if alpha == 0.05 else 2.5758293035489004
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def tost(a: list[dict], b: list[dict], margin_pp: float = 3.0) -> dict:
    """同题配对 TOST。按 gid 对齐。"""
    ma = {str(x.get("gid")): bool(x.get("correct")) for x in a}
    mb = {str(x.get("gid")): bool(x.get("correct")) for x in b}
    common = [k for k in ma if k in mb]
    n = len(common)
    if not n:
        return {"error": "没有可对齐的题（gid 不匹配）"}
    bb = sum(1 for k in common if ma[k] and not mb[k])   # A 对 B 错
    cc = sum(1 for k in common if not ma[k] and mb[k])   # A 错 B 对
    acc_a = sum(1 for k in common if ma[k]) / n
    acc_b = sum(1 for k in common if mb[k]) / n
    d = acc_a - acc_b                                     # 配对差（点估计）
    # 一致性对数（不一致对）的 CI：b/(b+c) 的比例 → 换算回 d 的区间
    m = bb + cc
    if m == 0:
        lo_d = hi_d = 0.0
    else:
        lo_p, hi_p = binom_ci(bb, m)
        lo_d = (2 * lo_p - 1) * m / n
        hi_d = (2 * hi_p - 1) * m / n
    margin = margin_pp / 100.0
    equiv = (lo_d > -margin) and (hi_d < margin)
    return {
        "n": n, "acc_a": acc_a, "acc_b": acc_b,
        "a_only": bb, "b_only": cc, "discordant": m,
        "diff_pp": d * 100, "ci_lo_pp": lo_d * 100, "ci_hi_pp": hi_d * 100,
        "margin_pp": margin_pp, "equivalent": equiv,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--margin", type=float, default=3.0, help="等价边界，单位 pp")
    args = ap.parse_args()

    pa, pb = BENCH / args.a, BENCH / args.b
    for p in (pa, pb):
        if not p.exists():
            print(f"  缺 {p}"); return 1
    r = tost(load(pa), load(pb), args.margin)
    if "error" in r:
        print(f"  {r['error']}"); return 1

    print("=" * 66)
    print(f"  A = {args.a}")
    print(f"  B = {args.b}")
    print(f"  同题配对 n = {r['n']}")
    print()
    print(f"  A 准确率 {100*r['acc_a']:.1f}%   B 准确率 {100*r['acc_b']:.1f}%")
    print(f"  仅 A 对 {r['a_only']} 题   仅 B 对 {r['b_only']} 题   不一致对 {r['discordant']}")
    print()
    print(f"  配对差 {r['diff_pp']:+.2f}pp   95%CI [{r['ci_lo_pp']:+.2f}, {r['ci_hi_pp']:+.2f}]pp")
    print(f"  等价边界 ±{r['margin_pp']:.1f}pp")
    print()
    if r["equivalent"]:
        print(f"  → **等价性成立**：CI 完全落在 ±{r['margin_pp']:.1f}pp 内。")
        print("     可以说「两者等价」，不是「没测到差异」。")
    else:
        print(f"  → **等价性未确立**：CI 超出 ±{r['margin_pp']:.1f}pp。")
        print("     只能写「在本实验精度下未检测到差异」，不能写「等价」或「不是瓶颈」。")
    print("=" * 66)
    out = BENCH / f"tost-{Path(args.a).stem}-vs-{Path(args.b).stem}.json"
    out.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  已存 {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
