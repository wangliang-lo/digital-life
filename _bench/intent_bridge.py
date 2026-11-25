# -*- coding: utf-8 -*-
"""谓词翻译层 —— 把她写的【读书】翻译成旧标签 [READ_BOOK: ...]。

为什么这样接：
  她的旧解析正则本来就接受三种括号 `[\\[［【]`，
  所以只要把行首的【谓词】换成对应的 [TAG: 参数]，
  现有的分发管线**一行都不用改**，所有功能照常。

这是「换语法」和「不丢失能力」之间最省事的桥。

用法（插在她回复生成之后、旧解析之前）：
    from intent_bridge import translate_reply
    reply, notes = translate_reply(reply)
    # notes 是给下一轮的反馈（"指令要放行首"/"没有这个功能"）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from intent_mapper import map_intent  # noqa: E402

# 行首【谓词】：吞掉这一行的方括号部分，保留后面的话
_LINE_BRACKET = re.compile(r"(?m)^([ \t]*)【([^】\n]{1,24})】")


def _param_text(pred: str, rest: str, params: dict) -> str:
    """把抽到的参数拼成旧标签要的样子。"""
    rest = (rest or "").strip()

    def g(*keys: str) -> str:
        for k in keys:
            v = params.get(k)
            if isinstance(v, (str, int, float)) and str(v).strip():
                return str(v).strip()
        return ""

    # 读书 / 读原文：书名, 章号
    if pred in ("读书", "读原文"):
        book = g("书名", "book", "title")
        ch = g("章号", "章", "chapter") or "1"
        if not book:
            # 从正文里找《》
            m = re.search(r"《([^》]{1,30})》", rest)
            book = m.group(1) if m else ""
        return f"{book}, {ch}" if book else ""
    if pred == "下棋":
        who = g("对手", "对手名字", "名字", "name")
        return who or "小玩具"
    if pred == "落子":
        r_, c_ = g("行", "row"), g("列", "col")
        return f"{r_},{c_}" if r_ and c_ else ""
    if pred in ("睡觉",):
        h = g("小时数", "小时", "hours") or "6"
        return str(h)
    if pred in ("暂停轮询",):
        return g("分钟", "minutes") or "30"
    if pred in ("晚点说",):
        return g("秒数", "秒", "seconds") or "600"
    if pred == "记住":
        w = g("重要度", "weight")
        return f"{rest}, {w or '0.1'}" if rest else ""
    if pred in ("留话", "公开留话", "语音", "独白", "搜索", "IM", "发私信",
                "朋友圈", "画画", "哼歌", "唱歌", "表情"):
        got = g("内容", "正文", "歌词", "情绪", "画什么", "查什么", "具体问题")
        return got or rest
    return rest


def translate_reply(reply: str) -> tuple[str, list[str]]:
    """把行首【谓词】翻译成旧标签。返回 (新回复, 给她的提示列表)。

    安全策略：
      - 只处理**行首**的【】（爸爸定的规则）
      - 映射不出来的**不执行**，只给一句提示
      - 完全找不到谓词就原样返回
    """
    text = str(reply or "")
    notes: list[str] = []
    if "【" not in text:
        return text, notes

    out_lines: list[str] = []
    changed = False
    for line in text.split("\n"):
        m = re.match(r"^([ \t]*)【([^】\n]{1,24})】[ \t]*(.*)$", line)
        if not m:
            out_lines.append(line)
            continue
        indent, inner, rest = m.group(1), m.group(2).strip(), m.group(3)
        r = map_intent(line)
        pred, tag = r.get("predicate"), r.get("tag")
        src = r.get("source")

        if pred in ("NONE", "", None) and src == "description":
            # 神情描写（【叹气】）→ 原样保留文字，不执行
            out_lines.append(line)
            continue
        if src == "misplaced":
            notes.append(str(r.get("hint") or "指令要放在行首"))
            out_lines.append(line)
            continue
        if pred == "UNKNOWN":
            sug = "、".join(r.get("suggest") or [])
            notes.append(f"似乎没有这个功能" + (f"（可以试试：{sug}）" if sug else ""))
            out_lines.append(f"{indent}{rest}".rstrip())
            changed = True
            continue
        if not tag:
            out_lines.append(line)
            continue

        # 命中：拼成旧标签
        body = _param_text(pred, rest, r.get("params") or {})
        if body:
            legacy = f"[{tag}: {body}]"
            # 留话/独白这类纯文本标签，参数就是后面那句话
            out_lines.append(f"{indent}{legacy}")
            # 有些标签的"后面那句话"本身就是参数（留话内容、表情情绪、画什么），
            # 再当正文发一遍就成了重复。只有"参数另给、后面还有闲聊"的才保留。
            _REST_IS_PARAM = {
                "SOLVER_MESSAGE", "SOLVER_PUBLIC", "THOUGHT", "KEEP_MEMORY",
                "VOICE", "SING", "HUM", "STICKER", "DOODLE", "MOMENTS",
                "WEB_SEARCH", "DOODLE", "SEND_DRAWING",
            }
            if rest and tag not in _REST_IS_PARAM:
                out_lines.append(rest)
        else:
            out_lines.append(f"{indent}[{tag}]")
            if rest:
                out_lines.append(rest)
        changed = True

    new_text = "\n".join(out_lines) if changed else text
    return new_text, notes


if __name__ == "__main__":
    cases = [
        "【读书】那本《小王子》还没看完",
        "今天好累\n【留话】你早点睡",
        "【表情】开心",
        "【画画】画个路灯",
        "【跳舞】转个圈",
        "[STICKER: 开心] 老写法",
        "我今天【读书】了",
        "【叹气】又下雨了",
    ]
    for c in cases:
        out, notes = translate_reply(c)
        print(f"  {c!r}")
        print(f"    → {out!r}   notes={notes}")
