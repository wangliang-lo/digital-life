# -*- coding: utf-8 -*-
"""选项位置置换：证明模型是「认内容」还是「偏好字母位置」。

为什么做：
  全 300 隐藏选项实验显示——模型自己算出正确值只有 15%，但给选项能选对 72%。
  所以它在「认」。但「认」有两种可能：
    (a) 真在比对内容（把选项内容和记忆对上）
    (b) 只是位置偏好（总选 B，恰好 B 常是对的）
  区分方法：**保持四个选项内容不变，只把字母顺序旋转**。
    (a) 的话，模型的答案会跟着正确内容搬到新字母；
    (b) 的话，模型还会选原来那个字母。

用法： python permute_options.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
BENCH = Path(r"_bench")
sys.path.insert(0, r"<repo>")
sys.path.insert(0, str(BENCH))

ASK = """下面是你记得的事。记忆前面的括号是发生的时间。
{mem}

问题：{q}

{opts}

只输出一个字母。"""


def rotate(choices: dict[str, str], k: int) -> tuple[dict[str, str], dict[str, str]]:
    """把选项内容整体旋转 k 位，返回新选项表和「旧字母→新字母」映射。"""
    keys = sorted(choices)
    vals = [choices[x] for x in keys]
    k %= len(keys)
    new_vals = vals[k:] + vals[:k]
    new = {keys[i]: new_vals[i] for i in range(len(keys))}
    old2new = {keys[i]: keys[(i - k) % len(keys)] for i in range(len(keys))}
    return new, old2new


def main() -> int:
    from run_membench_pipeline import deepseek_key
    from digital_life import chat_with_llm
    os.environ["DEEPSEEK_API_KEY"] = deepseek_key()

    data = json.loads((BENCH / "ckpt-osf300.json").read_text(encoding="utf-8"))
    items = data if isinstance(data, list) else data.get("items") or []

    rows = []
    for k, x in enumerate(items, 1):
        ch = x.get("choices") or {}
        if len(ch) != 4:
            continue
        q = str(x.get("question") or "")
        truth = str(x.get("truth") or "")
        mem = "\n".join(f"- {str(m)[:120]}" for m in (x.get("used_memories") or [])[:20])
        base_pred = str(x.get("pred") or "")
        truth_val = str(ch.get(truth) or "")

        # 旋转 1 位（确定性，不随机）
        new_ch, old2new = rotate(ch, 1)
        opts = "\n".join(f"{kk}. {vv}" for kk, vv in sorted(new_ch.items()))
        try:
            raw = str(chat_with_llm([{"role": "user", "content": ASK.format(mem=mem, q=q, opts=opts)}],
                                    model="deepseek-chat", temperature=0.0, timeout=90.0) or "")
        except Exception:  # noqa: BLE001
            raw = ""
        m = re.search(r"[ABCD]", raw.upper())
        pick = m.group(0) if m else ""

        truth_now = old2new.get(truth, "")          # 金标内容现在在哪个字母
        base_now = old2new.get(base_pred, "")       # 基线选的字母内容搬到哪了
        rows.append({
            "q": q[:60], "truth_orig": truth, "truth_new": truth_now,
            "base_pred": base_pred, "base_new": base_now,
            "pick": pick, "truth_value": truth_val[:26],
            "follows_content": pick == base_now,
            "finds_truth": pick == truth_now,
            "same_letter": pick == base_pred,
        })
        if k % 50 == 0:
            print(f"  {k}/300", flush=True)

    n = len(rows)
    follows = sum(1 for r in rows if r["follows_content"])
    finds = sum(1 for r in rows if r["finds_truth"])
    same = sum(1 for r in rows if r["same_letter"])
    print("\n" + "=" * 60)
    print(f"  n={n}（选项内容整体旋转 1 位，字母不变）")
    print(f"  跟随正确内容搬到新字母（finds_truth）: {finds}/{n} = {100*finds/n:.1f}%")
    print(f"  跟随「基线选的那个内容」:              {follows}/{n} = {100*follows/n:.1f}%")
    print(f"  死守原字母不变（位置偏好）:            {same}/{n} = {100*same/n:.1f}%")
    print("=" * 60)
    if finds >= 0.7 * n:
        print("  → **认内容**：旋转后仍按内容找对答案（这是真能力）")
    elif same >= 0.7 * n:
        print("  → **位置偏好**：旋转后还选原字母，说明之前的高分是字母偏好在起作用")
    else:
        print("  → **混合**：既跟内容也受位置影响")
    (BENCH / "permute-options.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print("  已存 permute-options.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
