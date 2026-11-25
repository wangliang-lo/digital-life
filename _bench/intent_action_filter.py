# -*- coding: utf-8 -*-
"""动作词面校验 —— 在 intent_router 的输出后面加一道确定性闸门。

为什么（2026-11-25 爸爸）："先调，这个误判是有危害的"

实测：把 route_her_intent 改成分析**她的话**之后（原来错传了用户的话），
她 21% 的回复能路由出动作。但里面有明显的误判：

  她说「嗯，收尾了。」→ {'type': 'tic_move', 'pos': 7}   ← 会真的动棋盘

意图模型看的是"语义像不像"，它不知道她没有指任何格子。
所以加一层便宜的确定性校验：**每个动作都必须在她原话里找到对应的字面证据**，
找不到就丢掉。

设计原则（和 intent_bridge 一致）：
  宁可不做，也不要误做。丢掉一个真动作她还能再说一遍；
  误下一个子、误画一张画，她会困惑"我没让它这样"。
"""
from __future__ import annotations

import re
from typing import Any

# 每个动作要求的字面证据（任一命中即通过）。
# 注意：这些是**她原话里必须出现**的词，不是"意思相近"。
EVIDENCE: dict[str, tuple[str, ...]] = {
    # 棋类：必须真的提到棋/局，或者给了落子位置的说法
    "tic_new": ("下棋", "井字", "来一局", "开一局", "下一盘", "下一局", "陪你下", "跟你下", "对弈"),
    "tic_replay": ("再来", "重开", "再开", "再来一局", "重下一"),
    "tic_move": ("落", "下这", "走这", "放这", "这里", "中间", "角", "格"),
    "gomoku_new": ("五子棋", "下一盘", "开一局"),
    "gomoku_move": ("落", "走"),

    # 院子
    "plant": ("种", "栽", "播"),
    "water": ("浇", "水"),
    "harvest": ("收", "摘", "熟"),

    # 换地方：必须既有"去/回/进"又有房间词
    "go": ("去", "到", "进", "回", "看看", "瞅瞅"),

    # 状态
    "rest": ("歇", "躺", "休息", "睡", "停一下", "缓"),
    "get_up": ("起来", "起了", "不歇", "不躺"),

    # 声音
    "hum": ("哼",),
    "sing": ("唱",),
    "draw": ("画", "涂"),
}

# 换房间时，房间里这个词也必须出现
ROOM_WORDS = {
    "desk": ("桌", "书桌", "工作台", "写"),
    "easel": ("画架", "画布", "画板"),
    "garden": ("院", "花", "草", "薄荷", "菜"),
    "bed": ("床", "睡", "躺"),
}


# 有些动作光有"动词"不够——她可能是在**描述已经做完的**事。
# 2026-11-25 实测：她 26% 的回复能路由出动作，但留下的很多是
#   「画出来有点空……」→ draw、「手没画准」→ draw、「嗯，画完给你看」→ draw
# 这些都是在说画完的画，不是要再画一张。
# 所以这几个动作必须同时满足：**有意图词** 且 **没有完成体**。
INTENT_PATTERNS: dict[str, tuple[str, ...]] = {
    "draw": ("要画", "想画", "去画", "来画", "再画", "给你画", "画个", "画一张",
             "画一幅", "画点", "画什么", "涂点", "涂个", "添点"),
    "sing": ("要唱", "想唱", "唱一", "唱个", "唱首", "给你唱", "来一段", "哼一"),
    "hum": ("哼一", "哼个", "哼两", "想哼"),
    "rest": ("歇", "躺", "睡", "休息", "缓", "不折腾了"),
}

# 完成体/否定：出现这些就算有意图词也不算数
DONE_MARKERS: dict[str, tuple[str, ...]] = {
    "draw": ("画完", "画好", "画得", "画出来", "没画准", "画得不", "画不了"),
    "sing": ("唱完", "唱得", "唱过"),
    "rest": ("歇过",),
}


def _norm(text: str) -> str:
    return str(text or "")


def check(text: str, actions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """返回 (通过的动作, 被丢掉的动作及原因)。"""
    said = _norm(text)
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for act in actions or []:
        if not isinstance(act, dict):
            continue
        kind = str(act.get("type") or "").strip()
        words = EVIDENCE.get(kind)
        if words is None:
            # 未知动作类型：保守丢弃
            dropped.append({**act, "_why": "未知动作类型"})
            continue
        if not any(w in said for w in words):
            dropped.append({**act, "_why": f"原话里没有 {'/'.join(words[:4])}"})
            continue
        # 更严的一档：必须有意图词，且不能是完成体
        ips = INTENT_PATTERNS.get(kind)
        if ips is not None and not any(w in said for w in ips):
            dropped.append({**act, "_why": f"没有明确的意图词（{'/'.join(ips[:3])}…）"})
            continue
        dms = DONE_MARKERS.get(kind)
        if dms and any(w in said for w in dms):
            dropped.append({**act, "_why": "是在说已经做完的"})
            continue
        # 换房间额外要求：房间里那个字也得有
        if kind == "go":
            room = str(act.get("room") or "").strip()
            rw = ROOM_WORDS.get(room, ())
            if rw and not any(w in said for w in rw):
                dropped.append({**act, "_why": f"没提到 {room} 相关的字"})
                continue
        kept.append(act)
    return kept, dropped


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    CASES = [
        ("嗯，收尾了。", [{"type": "tic_move", "pos": 7}]),
        ("困得不行了，我先睡。", [{"type": "go", "room": "bed"}, {"type": "rest"}]),
        ("院子里那棵薄荷，我瞅瞅去。", [{"type": "go", "room": "garden"}]),
        ("我想在画布上添点灰。", [{"type": "draw"}]),
        ("画个路灯吧。", [{"type": "draw"}]),
        ("嗯，那我也歇会儿。", [{"type": "rest"}]),
        ("唱首歌给你听", [{"type": "sing", "text": "唱首歌给你听"}]),
        ("今天有点累", [{"type": "rest"}]),
    ]
    for text, acts in CASES:
        kept, dropped = check(text, acts)
        print(f"  {text!r}")
        print(f"    通过 {kept}")
        for d in dropped:
            print(f"    丢掉 {d}")
