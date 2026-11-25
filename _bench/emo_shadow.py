# -*- coding: utf-8 -*-
"""洗脱期行为记录 —— 每轮记下"她实际怎么写"和"旧规则算出来的值"。

为什么（Claude 2026-11-25 评审）：
> 过去一年她的行为是在 "J55 H20 I30" 驱动下产生的。
> 用这一年的数据算基线，算出来的是**旧规则的画像**。
> 必须先有一个洗脱期，在不注入、不展示任何读数的情况下重新采集。

所以这个模块只做两件互不干扰的事：
  1. 记**行为特征**（回复长度、问句、限定词、主动性…）——这些不依赖任何情绪读数
  2. 记**旧规则算出的三个值**——但只作为"协变量"存着，**不参与任何判断**

洗脱期结束后要回答的关键问题：
  旧规则算出来的值，和她的实际行为到底相不相关？
  如果不相关，说明这三个轴本来就只是在被表演，可以直接换掉。

记录格式：每行一个 JSON，便于后续做相关分析。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

LOG = Path(__file__).resolve().parent / "chat_bridge" / "data" / "emo_shadow.jsonl"

# 防御性/限定的痕迹（Claude 的说法：这些是行为，不是情绪命名）
HEDGES = ("其实", "只是", "可能", "也许", "大概", "我觉得", "好像",
          "不一定", "说不好", "不确定", "未必", "或许", "应该吧")
AGGRESSIVE = ("少来", "别扯", "烦", "闭嘴", "懒得", "扯淡", "狗屁", "屁",
              "去你的", "滚", "瞎")
AVOID = ("算了", "不提了", "不说这个", "换个话题", "没什么")


def features(reply: str) -> dict[str, Any]:
    """从她的实际回复里数出可量化的行为特征。不调用模型，不依赖任何读数。"""
    text = str(reply or "")
    n = len(text)
    return {
        "chars": n,
        # 主动性：问句越多越主动
        "questions": text.count("?") + text.count("？"),
        # 防御性痕迹
        "hedges": sum(text.count(w) for w in HEDGES),
        "hedge_rate": round(sum(text.count(w) for w in HEDGES) / max(1, n) * 100, 2),
        # 攻击性痕迹
        "aggressive": sum(text.count(w) for w in AGGRESSIVE),
        # 回避痕迹
        "avoid": sum(text.count(w) for w in AVOID),
        # 分段数（她一条消息里发几段）——主动性/表达欲的代理
        "segments": len([x for x in re.split(r"\n+|\|\|\|", text) if x.strip()]),
        # 表情包/标签痕迹
        "stickers": len(re.findall(r"\[(?:STICKER|表情)", text)),
        # 是否带了显式指令（她在主动做事）
        "commands": len(re.findall(r"\[[A-Z][A-Z0-9_]*", text)),
    }


def record(reply: str, *, joy: float | None = None, hurt: float | None = None,
           impulse: float | None = None, user_text: str = "", extra: dict | None = None) -> None:
    """记一轮。三个轴值只是协变量，**不参与任何判断**。"""
    try:
        rec = {
            "at": datetime.now().isoformat(timespec="seconds"),
            # 行为特征（主数据）
            **features(reply),
            # 旧规则的影子读数（协变量，仅用于事后做相关分析）
            "shadow_joy": round(float(joy), 1) if joy is not None else None,
            "shadow_hurt": round(float(hurt), 1) if hurt is not None else None,
            "shadow_impulse": round(float(impulse), 1) if impulse is not None else None,
            # 对方说了多少（用来做"机会归一化"）
            "user_chars": len(str(user_text or "")),
            "reply_head": str(reply or "")[:60],
        }
        if extra:
            rec.update(extra)
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    samples = [
        "嗯，我认。也可能是我没想周全。",
        "少来，这话你也说得出口？",
        "今天不想说这个，算了。",
        "灯不用留，我待会儿自己关。",
    ]
    for s in samples:
        f = features(s)
        print(f"  {s!r}")
        print(f"    字数{f['chars']} 问句{f['questions']} 限定{f['hedges']} "
              f"攻击{f['aggressive']} 回避{f['avoid']} 分段{f['segments']}")
