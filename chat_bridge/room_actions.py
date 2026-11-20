"""
她房间的动作库（2026-11-11 第二版，加入豆包补的 10 段循环）。

两类素材：
  loops/（新）：单一姿势的无缝循环，首末差 < 0.2%，可以硬循环
  clips/（旧）：位置之间的过渡（起身、走、坐下），首尾不一致，不能循环

文件目录：
  /roomv/clips2/  新的循环片段
  /roomv/clips/   旧的过渡片段
"""
from typing import Any

NEW_DIR = "clips2"
OLD_DIR = "clips"

# 姿势常量
POSES = ["sit_bed", "sit_desk", "lie_bed", "easel", "window", "garden", "floor"]

# 循环片段：一个姿势一段（亮/暗各一版时都列出来）
LOOPS: list[dict[str, Any]] = [
    {"id": "idle_bed_phone_lit", "dir": NEW_DIR, "file": "01_床上静坐抱手机_亮.mp4",
     "pose": "sit_bed", "lit": True, "ms": 6000, "loop": True, "label": "床上抱着手机"},
    {"id": "idle_bed_phone_dark", "dir": NEW_DIR, "file": "02_床上静坐抱手机_暗.mp4",
     "pose": "sit_bed", "lit": False, "ms": 6000, "loop": True, "label": "床上抱着手机"},
    {"id": "idle_typing_lit", "dir": NEW_DIR, "file": "03_桌前打字_亮.mp4",
     "pose": "sit_desk", "lit": True, "ms": 6000, "loop": True, "label": "桌前打字"},
    {"id": "idle_typing_dark", "dir": NEW_DIR, "file": "04_桌前打字_暗.mp4",
     "pose": "sit_desk", "lit": False, "ms": 6000, "loop": True, "label": "桌前打字"},
    {"id": "idle_sleep", "dir": NEW_DIR, "file": "05_躺床睡觉_暗.mp4",
     "pose": "lie_bed", "lit": False, "ms": 8000, "loop": True, "label": "躺着睡了"},
    {"id": "idle_draw", "dir": NEW_DIR, "file": "06_画架前画画_亮.mp4",
     "pose": "easel", "lit": True, "ms": 6000, "loop": True, "label": "画架前画画"},
    {"id": "idle_window", "dir": NEW_DIR, "file": "07_窗边看窗外_暗.mp4",
     "pose": "window", "lit": False, "ms": 6000, "loop": True, "label": "站在窗边看外面"},
    {"id": "idle_plants", "dir": NEW_DIR, "file": "08_窗台弄植物_暗.mp4",
     "pose": "garden", "lit": False, "ms": 6000, "loop": True, "label": "在窗台弄植物"},
    {"id": "idle_stretch", "dir": NEW_DIR, "file": "09_伸懒腰_暗.mp4",
     "pose": "floor", "lit": False, "ms": 4000, "loop": True, "label": "在地毯上伸懒腰"},
    {"id": "idle_drink", "dir": NEW_DIR, "file": "16_床边喝水_亮.mp4",
     "pose": "sit_bed", "lit": True, "ms": 3000, "loop": True, "label": "坐在床边喝水"},
]

# 位置之间的过渡（旧素材，不能循环）
TRANSITIONS: list[dict[str, Any]] = [
    {"id": "get_up_walk_desk", "dir": OLD_DIR, "file": "03_起身走到书桌.mp4",
     "from": "sit_bed", "to": "stand_desk", "ms": 6000},
    {"id": "sit_open_laptop", "dir": OLD_DIR, "file": "04_坐下打开电脑.mp4",
     "from": "stand_desk", "to": "sit_desk", "ms": 5000},
    {"id": "stand_walk_bed", "dir": OLD_DIR, "file": "06_起身走回床.mp4",
     "from": "sit_desk", "to": "bed_edge", "ms": 7000},
    {"id": "sit_down_bed", "dir": OLD_DIR, "file": "07_床上坐下.mp4",
     "from": "bed_edge", "to": "sit_bed", "ms": 8000},
    {"id": "get_up_walk_desk_b", "dir": OLD_DIR, "file": "10_起身走到书桌_B.mp4",
     "from": "sit_bed", "to": "stand_desk", "ms": 7000},
    {"id": "sit_open_laptop_b", "dir": OLD_DIR, "file": "11_坐下打开电脑_B.mp4",
     "from": "stand_desk", "to": "sit_desk", "ms": 4000},
    {"id": "stand_walk_bed_b", "dir": OLD_DIR, "file": "13_起身走回床_B.mp4",
     "from": "sit_desk", "to": "bed_edge", "ms": 7000},
    {"id": "sit_down_bed_b", "dir": OLD_DIR, "file": "14_床上坐下_B.mp4",
     "from": "bed_edge", "to": "sit_bed", "ms": 8000},
]

# 额外的姿势（过渡图里的中间节点）
EXTRA_POSES = ["stand_desk", "bed_edge"]


def actions_payload() -> dict[str, Any]:
    """给前端的整份数据。"""
    return {
        "loops": LOOPS,
        "transitions": TRANSITIONS,
        "poses": POSES + EXTRA_POSES,
    }


def loop_for(pose: str, lit: bool) -> dict[str, Any] | None:
    """挑一个循环：同姿势优先，亮暗不匹配时退回另一版。"""
    candidates = [item for item in LOOPS if item["pose"] == pose]
    if not candidates:
        return None
    exact = [item for item in candidates if item["lit"] == lit]
    pool = exact or candidates
    # 同姿势里多个（比如床上有抱手机/喝水），随机一个
    import random

    return random.choice(pool)


def plan(start: str, target: str, variant: str = "a") -> dict[str, Any]:
    """过渡段寻路（只走 TRANSITIONS），找不到就返回 ok=False 让前端直接叠化。"""
    if start == target:
        return {"ok": True, "path": [], "total_ms": 0}
    edges: dict[str, list[dict[str, Any]]] = {}
    for item in TRANSITIONS:
        edges.setdefault(item["from"], []).append(item)
    queue = [(start, [])]
    seen = {start}
    while queue:
        pose, path = queue.pop(0)
        for item in edges.get(pose, []):
            if item["to"] in seen:
                continue
            new_path = path + [item]
            if item["to"] == target:
                return {"ok": True,
                        "path": [a["id"] for a in new_path],
                        "total_ms": sum(a["ms"] for a in new_path)}
            seen.add(item["to"])
            queue.append((item["to"], new_path))
    return {"ok": False, "reason": "no_route"}


def context_for_prompt() -> str:
    """给提示词用：她此刻能用哪些姿势。"""
    lines = ["【房间姿势】"]
    for item in LOOPS:
        lines.append(f"- {item['pose']}：{item['label']}")
    return "\n".join(lines)
