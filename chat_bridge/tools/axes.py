#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""看她的内分泌：七条轴 + 六种激素，以及每条激素是被谁推上去的。

    python tools/axes.py
    python tools/axes.py --why cortisol
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ENDOCRINE  # noqa: E402
import HORMONE  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--why", help="看某种激素是谁推的，比如 cortisol")
    args = parser.parse_args()

    levels = ENDOCRINE.axis_levels()
    print("七条内分泌轴")
    for name, value in levels.items():
        bar = "█" * int(round(value * 20))
        print(f"  {name:10s} {value:.2f} {bar:<20} {ENDOCRINE.AXES[name]}")

    cycle = HORMONE.cycle_hormones()
    if cycle:
        print(f"\n性腺轴细节：第 {cycle['day']}/{cycle['length']} 天，"
              f"E2 {cycle['estradiol']:.2f}、P4 {cycle['progesterone']:.2f}、LH {cycle['lh']:.2f}"
              f"{'（经期）' if cycle['bleeding'] else ''}")
        print(f"  体感：能 {cycle['energy']:.2f} 烦 {cycle['irritability']:.2f} "
              f"亲 {cycle['affection']:.2f} 痛 {cycle['cramps']:.2f}")

    print("\n激素水平")
    for name in ("cortisol", "adrenaline", "serotonin", "dopamine", "oxytocin", "endorphin"):
        print(f"  {name:10s} {HORMONE.circadian_baseline(name):.3f}")

    body = HORMONE.load_body()
    print(f"\n身体：疲劳 {body['fatigue']:.2f} 困 {body['drowsy']:.2f} "
          f"睡眠债 {body['debt']:.1f}h")
    print(f"  感觉：{HORMONE.body_feeling()}")

    if args.why:
        print(f"\n{args.why} 是被这些推的：")
        for item in ENDOCRINE.contributions(args.why):
            print(f"  {item['axis']:10s} 系数 {item['weight']:+.2f} "
                  f"× 强度 {item['level']:.2f}（{item['mode']}）→ {item['delta']:+.3f}  {item['why']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
