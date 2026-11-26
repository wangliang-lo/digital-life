# -*- coding: utf-8 -*-
"""金标审计 v2 —— 修掉脆的数字提取，并把原文完整打印出来人工核。

第一版的毛病：
  用 `re.findall(r"\d+")` 取**输出里最后一个数字**。
  但推导过程里到处是数字（身高 162、年龄 33、年份 2024、步骤编号…），
  取到的极可能不是最终计数 → 那 14 道「偏离金标」里混了提取错误。

修正：
  只认这几种明确写法（按优先级）：
    「共 N 个」「一共 N 个」「总共 N 个」「总数是 N」「答案是 N」
  抓不到就标 `提取失败`，不再瞎猜。

用法：
    python gold_audit2.py --show 5      # 打印前 5 题的原文 + 推导全文
    python gold_audit2.py --all         # 跑全部 19 题并统计
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
BENCH = Path(r"_bench")
sys.path.insert(0, r".")
sys.path.insert(0, str(BENCH))

DERIVE = """下面是你记得的事。记忆前面的括号是发生的时间。
{mem}

问题：{q}

**严格按三步做，不许跳步：**

第一步：把记忆里提到的**每一个可能相关的对象**单独列出来，一行一个：
        `对象名 | 相关值 | 出自哪条记忆`
        （问人数就把每条记忆里的每个人名都列上，一个都别漏）

第二步：逐个判断是否满足问题条件，写 `是` 或 `否` + 一句理由。

第三步：数 是 的个数，**必须单独写一行**：`共 N 个`

不要输出选项。"""

# 只认这些写法
FINAL = re.compile(r"(?:共|一共|总共|总数(?:是|为)?|合计)\s*[:：]?\s*(\d+)\s*(?:个|人|项|events?|people|条|次)?")


def extract_final(text: str) -> int | None:
    ms = FINAL.findall(text)
    return int(ms[-1]) if ms else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", type=int, default=0, help="打印前 N 题的原文和推导全文")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    from run_membench_pipeline import deepseek_key
    from digital_life import chat_with_llm
    os.environ["DEEPSEEK_API_KEY"] = deepseek_key()

    data = json.loads((BENCH / "ckpt-osf300.json").read_text(encoding="utf-8"))
    items = data if isinstance(data, list) else data.get("items") or []
    cand = [x for x in items
            if not x.get("correct")
            and re.search(r"how many|altogether|in total", str(x.get("question")), re.I)
            and x.get("choices")]

    limit = args.show or (len(cand) if args.all else 0)
    if not limit:
        print("  加 --show N 或 --all"); return 1

    rows = []
    for k, x in enumerate(cand[:limit], 1):
        q = str(x.get("question") or "")
        ch = x.get("choices") or {}
        gl = str(x.get("truth") or "")
        gv = str(ch.get(gl) or "")
        mems = [str(m) for m in (x.get("used_memories") or [])[:20]]
        mem = "\n".join(f"- {m[:220]}" for m in mems)

        try:
            out = str(chat_with_llm([{"role": "user", "content": DERIVE.format(mem=mem, q=q)}],
                                    model="deepseek-chat", temperature=0.0,
                                    timeout=180.0) or "")
        except Exception:  # noqa: BLE001
            out = ""

        n = extract_final(out)
        gnums = [int(t) for t in re.findall(r"\d+", gv)]
        gn = gnums[0] if gnums else None

        if n is None:
            verdict = "提取失败"
        elif gn is not None and n == gn:
            verdict = "同金标"
        else:
            verdict = "偏离金标"

        rows.append({"i": k, "q": q, "gold_letter": gl, "gold_val": gv,
                     "derived": n, "gold_n": gn, "verdict": verdict,
                     "n_mems": len(mems)})
        print(f"\n{'='*70}")
        print(f"  [{k}] {q}")
        print(f"  金标 {gl} = {gv}")
        print(f"  记忆 {len(mems)} 条：")
        for i, m in enumerate(mems, 1):
            print(f"    {i:2}. {m[:180]}")
        print(f"  --- 推导全文 ---")
        for line in out.splitlines():
            if line.strip():
                print(f"    {line[:160]}")
        print(f"  >>> 推导出 {n}，金标 {gn} → **{verdict}**")

    cnt = Counter(r["verdict"] for r in rows)
    print(f"\n{'='*70}")
    total = len(rows)
    print(f"  {total} 题：")
    for v, c in cnt.most_common():
        print(f"    {v:10} {c}  ({100*c/total:.0f}%)")
    print("=" * 70)

    out_p = BENCH / "gold-audit2.json"
    out_p.write_text(json.dumps({"rows": rows, "counts": dict(cnt)},
                                ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  已存 {out_p.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
