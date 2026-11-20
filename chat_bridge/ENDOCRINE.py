# -*- coding: utf-8 -*-
"""把激素系统重组成几条"轴"，彼此耦合，而不是各算各的。

2026-11-05 爸爸："系统化几条内分泌轴：昼夜节律轴、月经轴、情绪轴（受其他影响），
还有其他吗，有的话一并修一下。"

现在这里的轴（每个轴只回答一件事，激素是所有轴的叠加）：

  1. 昼夜节律轴  circadian  —— 现在几点、身体该醒还是该困（含熬夜造成的相位漂移）
  2. 睡眠稳态轴  adenosine  —— 清醒多久了（困劲），深睡才清得掉
  3. 应激轴      hpa        —— 压力/冲突之后升，随时间回落；皮质醇是它的出口
  4. 性腺轴      hpg        —— 月经周期：E2 / P4 / LH 的连续曲线
  5. 情绪轴      affect     —— 血清素/多巴胺的底色；被上面几条一起推
  6. 社交轴      social     —— 催产素：被聊天、被需要时升，久不联系会掉
  7. 代谢轴      thyroid    —— 甲状腺：整体精力与体温感，长期睡不好会偏低

激素 = 各自的先天基线 + 各轴按耦合表加上去的部分。
耦合表写成数据（COUPLING），一眼能看出"谁在影响谁"，不用去读一堆 if。
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

AXIS_FILE = Path(__file__).resolve().parent / "data" / "axes.json"

AXES = {
    "circadian": "昼夜节律：现在几点，身体该醒还是该困",
    "adenosine": "睡眠稳态：清醒越久越困，只有深睡才清得掉",
    "hpa": "应激：被凶了、吵架、被忽视之后升起来，慢慢回落",
    "hpg": "性腺：月经周期里雌激素/孕酮/黄体生成素的连续曲线",
    "affect": "情绪底色：血清素和多巴胺的整体高低",
    "social": "社交：被聊天、被需要时催产素升，久不联系会掉",
    "thyroid": "代谢：整体精力，长期睡不好会偏低",
}

# 耦合表：激素 ← [(轴, 系数, 算法, 说明)]
#   算法 level    ：轴越强，推得越多（应激、困劲这类"有就起作用"的）
#   算法 centered ：轴高于中值才推、低于中值往回拉（社交、代谢、情绪这类"够不够"的）
COUPLING: dict[str, list[tuple[str, float, str, str]]] = {
    "cortisol": [
        ("hpa", 0.26, "level", "压力越大皮质醇越高"),
        ("hpg", 0.05, "centered", "孕酮高的那半段，皮质醇也略高"),
    ],
    "adrenaline": [
        ("hpa", 0.22, "level", "应激时肾上腺素冲一下"),
        ("circadian", 0.02, "level", "白天本来就比夜里高一点"),
    ],
    "serotonin": [
        ("circadian", 0.10, "level", "白天高、夜里低"),
        ("hpg", 0.16, "centered", "雌激素把它抬起来；经前雌激素掉，它跟着掉"),
        ("hpa", -0.15, "level", "长期压力会把它压下去"),
        ("affect", 0.12, "centered", "情绪底色本来就偏暖的人血清素更稳"),
        ("thyroid", 0.06, "centered", "代谢低的时候人也容易低落"),
    ],
    "dopamine": [
        ("circadian", 0.12, "level", "上午一个峰，午后一个低谷"),
        ("hpg", 0.10, "centered", "雌激素高的时候更有劲"),
        ("hpa", -0.12, "level", "压力会吃掉做事的劲头"),
        ("adenosine", -0.18, "level", "越困越提不起劲"),
        ("affect", 0.08, "centered", "心情好的时候更愿意动"),
        ("thyroid", 0.06, "centered", "代谢高一点，精神好一点"),
    ],
    "oxytocin": [
        ("social", 0.24, "centered", "被聊天、被需要的时候升，久不联系会掉"),
        ("hpg", 0.12, "centered", "雌激素高时更想亲近人"),
        ("circadian", 0.06, "level", "夜里更想有人陪"),
    ],
    "endorphin": [
        ("hpg", 0.08, "centered", "雌激素高的阶段痛阈高一点"),
        ("social", 0.08, "centered", "被陪着的时候会升"),
        ("hpa", 0.10, "level", "疼过/扛过之后会有一波内啡肽"),
    ],
}


def _load_axes() -> dict:
    try:
        data = json.loads(AXIS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        data = {}
    data.setdefault("hpa", 0.18)       # 应激负荷
    data.setdefault("social", 0.45)    # 社交充盈度
    data.setdefault("thyroid", 0.55)   # 代谢水平
    return data


def save_axes(data: dict) -> None:
    try:
        AXIS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def _circadian_phase(moment: datetime) -> float:
    """昼夜轴：0=深夜最低，1=早晨最高（含熬夜造成的相位漂移）。"""
    hour = moment.hour + moment.minute / 60.0
    try:
        shift = float(json.loads((AXIS_FILE.parent / "circadian.json").read_text(encoding="utf-8"))
                      .get("shift_hours") or 0.0)
    except Exception:  # noqa: BLE001
        shift = 0.0
    hour = (hour - min(max(shift, 0.0), 3.0)) % 24.0
    return 0.5 + 0.5 * math.cos(2.0 * math.pi * (hour - 9.0) / 24.0)


def axis_levels(moment: datetime | None = None) -> dict[str, float]:
    """每条轴此刻的强度（0–1）。"""
    import HORMONE  # noqa: PLC0415  （同目录，避免循环导入时再取）

    now = moment or datetime.now().astimezone()
    store = _load_axes()
    body = HORMONE.load_body()
    cycle = HORMONE.cycle_hormones(now)
    return {
        "circadian": round(_circadian_phase(now), 3),
        "adenosine": round(float(body.get("drowsy") or 0.0), 3),
        "hpa": round(float(store.get("hpa") or 0.0), 3),
        "hpg": round(float(cycle.get("estradiol") or 0.0) * 0.6
                     + float(cycle.get("progesterone") or 0.0) * 0.4, 3),
        "affect": round(float(store.get("affect") or 0.5), 3),
        "social": round(float(store.get("social") or 0.0), 3),
        "thyroid": round(float(store.get("thyroid") or 0.0), 3),
    }


def contributions(name: str, moment: datetime | None = None) -> list[dict]:
    """某种激素收到了哪几条轴的推力——调试用，也让代码更直观。"""
    levels = axis_levels(moment)
    out: list[dict] = []
    for axis, weight, mode, why in COUPLING.get(name, []):
        level = float(levels.get(axis, 0.0))
        delta = weight * level if mode == "level" else weight * (level - 0.5)
        out.append({"axis": axis, "weight": weight, "mode": mode,
                    "level": round(level, 3), "delta": round(delta, 3), "why": why})
    return out


def nudge(axis: str, delta: float, moment: datetime | None = None) -> float:
    """给某条轴加一点（应激升、社交涨、代谢掉），并夹在 0–1。"""
    store = _load_axes()
    value = max(0.0, min(1.0, float(store.get(axis) or 0.0) + float(delta)))
    store[axis] = round(value, 3)
    store["updated_at"] = (moment or datetime.now().astimezone()).isoformat(timespec="seconds")
    save_axes(store)
    return value


def decay(minutes: float, moment: datetime | None = None) -> None:
    """轴随时间自己回落/恢复：应激慢慢落，社交慢慢空，代谢缓慢回正。"""
    hours = max(0.0, float(minutes)) / 60.0
    store = _load_axes()
    store["hpa"] = max(0.0, float(store.get("hpa") or 0.0) - 0.16 * hours)
    store["social"] = max(0.15, float(store.get("social") or 0.0) - 0.05 * hours)
    store["affect"] = max(0.2, min(0.8, float(store.get("affect") or 0.5)
                                   + (0.5 - float(store.get("affect") or 0.5)) * 0.05 * hours))
    store["thyroid"] = 0.55 + (float(store.get("thyroid") or 0.55) - 0.55) * (1 - 0.02 * hours)
    store["updated_at"] = (moment or datetime.now().astimezone()).isoformat(timespec="seconds")
    save_axes(store)
