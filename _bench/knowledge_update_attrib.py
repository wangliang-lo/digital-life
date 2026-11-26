# -*- coding: utf-8 -*-
"""知识更新类归因：她选错的时候，选的是「旧值」还是别的东西？

为什么查这个：
  失败归因里「知识更新」也占 38%（和计数并列）。计数类已经穷尽排查且基本见底，
  但知识更新类**还没有单独归因过**。

  核心问题：如果她系统性地选了旧值 → 那是一个明确的机制（时间顺序敏感度不足），
  解法方向就清楚（显式表达"这条被那条取代了"）。
  如果她选的是别的东西 → 那就不是"新旧"问题，是别的。

做法（不用重跑模型，全从 ckpt 里算）：
  对每道 knowledge_updating 题：
    ① 金标值出现在哪条记忆里？那条的时间是？
    ② 她选的那个选项的值出现在哪条记忆里？那条的时间是？
    ③ 比较两个时间 → 她选的是更旧还是更新，还是找不到
    ④ 金标记忆的检索排名（rank）

用法：
    python knowledge_update_attrib.py
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
BENCH = Path(r"_bench")
CACHE = BENCH / "cache" / "ingest-ThirdAgentDataLowLevel-n300.json"


def norm(s: str) -> str:
    return re.sub(r"\s+", "", str(s or "")).lower()


def find_mem(mems: list[dict], value: str) -> dict | None:
    """在记忆里找哪一条包含这个值（宽松：取最长公共片段）。"""
    v = norm(value)
    if len(v) < 2:
        return None
    best, best_len = None, 0
    for m in mems:
        text = norm(m.get("compressed") or "")
        # 用值里的 2~4 字片断去撞
        for L in (6, 4, 3, 2):
            hit = False
            for i in range(0, max(1, len(v) - L + 1)):
                if v[i:i + L] and v[i:i + L] in text:
                    if L > best_len:
                        best, best_len = m, L
                    hit = True
                    break
            if hit:
                break
    return best


def main() -> int:
    data = json.loads((BENCH / "ckpt-osf300.json").read_text(encoding="utf-8"))
    items = data if isinstance(data, list) else data.get("items") or []
    cache = json.loads(CACHE.read_text(encoding="utf-8"))

    ku = [x for x in items if "knowledge_updating" in str(x.get("category") or "")]
    wrong = [x for x in ku if not x.get("correct")]
    print(f"  知识更新类 {len(ku)} 道，错 {len(wrong)} 道\n")
    if not ku:
        print("  没有这一类题"); return 1

    print(f"  金标排名分布（全部 {len(ku)} 道）：")
    ranks = Counter()
    for x in ku:
        r = x.get("rank") or 0
        ranks["第1名" if r == 1 else ("第2-5名" if 1 < r <= 5 else ("第6-10名" if 5 < r <= 10 else ("第10名以后" if r > 10 else "未命中")))] += 1
    for k, v in ranks.most_common():
        print(f"    {k:10} {v}")

    rows = []
    chosen_older = chosen_newer = not_found = 0
    for k, x in enumerate(wrong, 1):
        gid = str(x.get("gid") or "")
        q = str(x.get("question") or "")
        ch = x.get("choices") or {}
        gl, pl = str(x.get("truth") or ""), str(x.get("pred") or "")
        gv, pv = str(ch.get(gl) or ""), str(ch.get(pl) or "")
        group = cache.get(gid) or {}
        mems = [m for m in (group.get("items") or []) if str(m.get("compressed") or "").strip()]
        mems.sort(key=lambda m: str(m.get("time") or ""))

        gm = find_mem(mems, gv)
        pm = find_mem(mems, pv)
        g_time = str((gm or {}).get("time") or "")
        p_time = str((pm or {}).get("time") or "")

        if not pm:
            tag = "她选的找不到出处"
            not_found += 1
        elif not g_time or not p_time:
            tag = "时间缺失"
        elif p_time < g_time:
            tag = "**她选了更旧的**"
            chosen_older += 1
        elif p_time > g_time:
            tag = "她选了更新的（金标才是旧的）"
            chosen_newer += 1
        else:
            tag = "同一时间"

        rows.append({"q": q[:60], "gold": gv[:26], "gold_time": g_time, "pred": pv[:26],
                     "pred_time": p_time, "tag": tag, "rank": x.get("rank")})
        print(f"  [{k}/{len(wrong)}] {tag}")
        print(f"       金标 {gv[:24]!r} @ {g_time}")
        print(f"       她选 {pv[:24]!r} @ {p_time}")

    print()
    print("=" * 66)
    print(f"  {len(wrong)} 道知识更新错题：")
    print(f"    **她选了更旧的：{chosen_older}**")
    print(f"    她选了更新的（金标旧）：{chosen_newer}")
    print(f"    她选的找不到出处：{not_found}")
    print()
    if chosen_older and chosen_older >= len(wrong) * 0.5:
        print("  → **主导机制：她倾向选旧值**（时间顺序敏感度不足）")
        print("     解法方向：显式表达「这条被那条取代」，而不是靠她自己去比时间")
    elif not_found >= len(wrong) * 0.5:
        print("  → 多数选中项在记忆里找不到出处：**可能金标/选项有问题**，需人工核")
    else:
        print("  → 机制不单一，需要人工逐题看")
    print("=" * 66)
    out = BENCH / "knowledge-update-attrib.json"
    out.write_text(json.dumps({"rows": rows, "chosen_older": chosen_older,
                               "chosen_newer": chosen_newer, "not_found": not_found,
                               "rank_dist": dict(ranks)},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  已存 {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
