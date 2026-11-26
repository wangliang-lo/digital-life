# -*- coding: utf-8 -*-
"""主张核实器：她说出「没核实过的具体细节」时，改成先问一句。

为什么做这个（2026-11-26）：
  GPT 读了她和林舟的读书对话，指出一条真的成立的问题：
  > 她把「那辆轮椅」理解成「终于能自己出门」，随后又说「她等了那么久」。
  > 没核实「下一辆」的具体所指，**存在顺着情绪补全细节的风险**。

  而今天的 benchmark 工作量到了同一件事：
  **她倾向于「补全」而不是「确认」。**

  但关键发现是——**她有这个能力，只是不触发。**
  矛盾题里她能准确说「两条记忆分别说是六周和八周，无法判断」。
  聊天里不会主动去核。

为什么不做纯提示词：
  今天六次实验证明「加规则普遍有害」（5 段清单 −5、两次调用 −4）。
  所以做成**生成后检查**，而且**只在她说出具体细节时才触发**。

触发条件（便宜的启发式，不花调用）：
  · 出现引号内的细节「…」
  · 出现「书中/他写/那段/第 N 章」
  · 出现具体的专名（人名/地名）但不在当轮上下文里

流程：
  1. 启发式判断要不要查 → 不触发就直接放行（多数回复走这条，零成本）
  2. 触发后用一次便宜调用抽「具体断言」
  3. **代码**检查每个断言是否在上下文里出现
  4. 有找不到的 → 一次改写调用，要求改成提问

用法：
    from claim_check import check_and_fix
    ok, fixed, detail = check_and_fix(reply, context_text)
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, r".")
sys.path.insert(0, r".")

# 便宜触发：这些词意味着她在说具体内容，不是闲聊
TRIGGER = re.compile(
    r"「[^」]{4,}」|书中|书里|他写|她写|那段|这一章|第\s*\d+\s*章|"
    r"作者|原文|台词|细节|伏笔"
)

EXTRACT = """下面是一段回复。请抽出其中**关于具体内容**的断言（不是感受、不是评价）。

回复：
{reply}

只抽这几类：
- 书里写了什么具体情节
- 某个具体的人做了什么
- 某个具体的词/句子是什么意思

每条一行，只写断言本身，不要解释。没有就输出 NONE。"""

REWRITE = """你刚才说：
{reply}

但下面这些细节在你当时看到的内容里**找不到依据**：
{missing}

请**重写整段**（不是追加，是替换）：

- **把没依据的那部分删掉**，不要让它在回复里留下任何痕迹
- 如果那句话本来是想回应对方，就换成一句**问对方**的话（问他指的是哪一段/哪个细节）
- 有依据的部分保留
- 短，还是你说话的语气
- 不要解释你为什么改，不要道歉，不要提「找不到依据」这种系统话

只输出重写后的回复，一两句。"""


def _llm(prompt: str, timeout: float = 90.0) -> str:
    from digital_life import chat_with_llm  # noqa: PLC0415

    try:
        return str(chat_with_llm([{"role": "user", "content": prompt}],
                                 model="deepseek-chat", temperature=0.0,
                                 timeout=timeout) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _norm(s: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", str(s or "")).lower()


def unverified_claims(reply: str, context: str) -> list[str]:
    """抽出她关于具体内容的断言，返回那些在上下文里找不到依据的。"""
    out = _llm(EXTRACT.format(reply=reply[:1200]))
    if not out or out.strip().upper() == "NONE":
        return []
    ctx = _norm(context)
    bad = []
    for line in out.splitlines():
        s = line.strip().lstrip("-·*• 0123456789.、)）").strip()
        if len(s) < 4:
            continue
        # 用断言里的关键词去上下文里撞（取 3~6 字的片段）
        key_hits = 0
        for L in (6, 4, 3):
            for i in range(0, max(1, len(s) - L + 1)):
                frag = _norm(s[i:i + L])
                if frag and frag in ctx:
                    key_hits += 1
                    break
            if key_hits:
                break
        if not key_hits:
            bad.append(s[:60])
    return bad


def check_and_fix(reply: str, context: str) -> tuple[bool, str, dict]:
    """返回 (是否改过, 最终回复, 详情)。"""
    detail: dict = {"triggered": False, "claims": [], "missing": []}
    if not reply or len(reply) < 10:
        return False, reply, detail
    if not TRIGGER.search(reply):
        return False, reply, detail
    detail["triggered"] = True

    bad = unverified_claims(reply, context)
    detail["missing"] = bad
    if not bad:
        return False, reply, detail

    fixed = _llm(REWRITE.format(reply=reply[:1000],
                                missing="\n".join(f"- {b}" for b in bad[:4])))
    if not fixed or len(fixed) < 4:
        return False, reply, detail
    detail["fixed"] = fixed
    return True, fixed, detail


if __name__ == "__main__":
    # 自测：一条「补全」的、一条正常的
    ctx = "（2024-10-01）我读了《我与地坛》第三章。他写母亲在园子里找他。"
    tests = [
        ("他把那辆轮椅写成了「终于能自己出门」，她等了那么久。", "疑似补全"),
        ("嗯，我在。", "正常闲聊"),
        ("书中那段「扶轮问路」，我觉得是在问有没有人一起走。", "有依据"),
    ]
    for text, label in tests:
        changed, final, d = check_and_fix(text, ctx)
        print(f"\n  [{label}] 触发={d['triggered']} 改过={changed}")
        print(f"    原文: {text[:60]}")
        if d["missing"]:
            print(f"    缺依据: {d['missing']}")
        if changed:
            print(f"    改后: {final[:80]}")
