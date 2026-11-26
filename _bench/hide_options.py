# -*- coding: utf-8 -*-
"""隐藏选项：先算出答案值，再看选项选字母。

为什么做（GPT 审稿时指出）：
  > 同题先隐藏选项，锁定答案值，再映射选项。
  > 你的 30/30 没检验**选项是否在推理阶段造成锚定**。
  > 这个对照比上概率聚合或改写入架构便宜得多。

  逻辑：
    如果「不给选项」能算对，而「给选项」算错 → **选项在锚定推理**
    如果两边都错 → 是真的算不出来

三段：
  ① 只给记忆 + 问题，**不给选项**，要求输出答案值（数字/短语）
  ② 把①的值和选项一起给，选字母（这条路径已知 100% 正确）
  ③ 对比：②的最终字母对错 vs 基线

用法：
    python hide_options.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
BENCH = Path(r"_bench")
sys.path.insert(0, r".")
sys.path.insert(0, str(BENCH))

NO_OPT = """下面是你记得的事。记忆前面的括号是发生的时间。
{mem}

问题：{q}

**先别看任何选项**（这里也没给你）。请直接算出答案，只输出那个值本身。
如果是数量，只输出数字（例如 3）。
如果是一个名字或短语，只输出那几个字。
不要解释，不要写单位。"""

MAP = """问题：{q}

选项：
{opts}

已知答案的值是：**{value}**

请选出与这个值对应的选项。只输出一个字母（A/B/C/D）。"""


def main() -> int:
    from run_membench_pipeline import deepseek_key
    from digital_life import chat_with_llm
    os.environ["DEEPSEEK_API_KEY"] = deepseek_key()

    data = json.loads((BENCH / "ckpt-osf300.json").read_text(encoding="utf-8"))
    items = data if isinstance(data, list) else data.get("items") or []
    cand = [x for x in items
            if not x.get("correct")
            and re.search(r"how many|altogether|in total", str(x.get("question")), re.I)
            and x.get("choices")]
    print(f"  计数类错题 {len(cand)} 道（都有选项数据）\n")

    rows = []
    for k, x in enumerate(cand, 1):
        q = str(x.get("question") or "")
        ch = x.get("choices") or {}
        truth = str(x.get("truth") or "")
        mem = "\n".join(f"- {str(m)[:120]}" for m in (x.get("used_memories") or [])[:20])
        opts = "\n".join(f"{kk}. {vv}" for kk, vv in sorted(ch.items()))
        truth_value = str(ch.get(truth) or "")

        # ① 不给选项，直接算值
        try:
            v = str(chat_with_llm([{"role": "user", "content": NO_OPT.format(mem=mem, q=q)}],
                                  model="deepseek-chat", temperature=0.0,
                                  timeout=120.0) or "").strip()
        except Exception:  # noqa: BLE001
            v = ""
        v1 = v.splitlines()[0].strip()[:40] if v else ""

        # ② 值 + 选项 → 字母
        try:
            pick = str(chat_with_llm([{"role": "user", "content": MAP.format(q=q, opts=opts, value=v1)}],
                                     model="deepseek-chat", temperature=0.0,
                                     timeout=90.0) or "")
        except Exception:  # noqa: BLE001
            pick = ""
        m = re.search(r"\b([A-D])\b", pick.upper())
        letter = m.group(1) if m else ""

        # ③ 值对不对？（用金标值做宽松比对：数字相等，或关键片段包含）
        nums_v = [int(t) for t in re.findall(r"\d+", v1)]
        nums_t = [int(t) for t in re.findall(r"\d+", truth_value)]
        value_ok = bool(nums_v and nums_t and set(nums_v) & set(nums_t))

        ok = letter == truth
        rows.append({"q": q[:66], "truth": truth, "truth_value": truth_value[:30],
                     "hidden_value": v1, "value_ok": value_ok,
                     "letter": letter, "ok": ok})
        print(f"  [{k}/{len(cand)}] 金标 {truth}({truth_value[:14]}) "
              f"| 隐藏选项算出 {v1[:20]!r} {'✓值对' if value_ok else '✗值错'} "
              f"| 最终选 {letter} {'✓' if ok else '✗'}")

    n = len(rows)
    ok_n = sum(1 for r in rows if r["ok"])
    vok = sum(1 for r in rows if r["value_ok"])
    # 关键交叉：值对但字母错 = 映射问题；值错 = 计算问题
    val_ok_letter_bad = sum(1 for r in rows if r["value_ok"] and not r["ok"])
    print()
    print("=" * 62)
    print(f"  {n} 道计数错题（基线全错）：")
    print(f"    隐藏选项后算对值的：**{vok}/{n} = {100*vok/n:.0f}%**")
    print(f"    最终字母对的：      **{ok_n}/{n} = {100*ok_n/n:.0f}%**")
    print(f"    值对但字母错的：    {val_ok_letter_bad} 道")
    print()
    if vok / n > 0.35:
        print("  → **隐藏选项后能算对相当一部分**：选项在锚定推理。")
        print("     那就该改答题方式（先算值再看选项），而不是改记忆架构。")
    else:
        print("  → 隐藏选项也算不对：**不是选项锚定，是真的算不出来**。")
        print("     瓶颈在运算本身，与选项呈现无关。")
    print("=" * 62)
    out = BENCH / "hide-options.json"
    out.write_text(json.dumps({"rows": rows, "value_ok": vok, "letter_ok": ok_n, "n": n},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  已存 {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
