"""她自己练出手法，再记住（2026-11-11）。

爸爸："太阳这些不是你给她准备好的，而是让她自己控制笔画练几份，
把最得意的一份拿出来，再记住这份是怎么画的。"

所以：
  1. 不再有"太阳=黄圆+八条射线"这种预置图形（从提示词里撤掉）
  2. 她练：`[PRACTICE: 太阳]` 起一张练习，自己用圆/线/颜色画；
     画完 `[PRACTICE_SAVE]` 存一份（可以存好几份）
  3. 挑：`[PRACTICE_PICK: 太阳, 2]` 把第 2 份定为"最得意的一份"，
     这份的每一笔进她的手法库（data/gestures.json）
  4. 以后她写 `[PAINT: 太阳 右上]`，系统就用**她自己练出来的笔画**复制过去
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import canvas
import doodle

DATA = Path(__file__).resolve().parent / "data"
PRACTICE_DIR = DATA / "practice"
GESTURES = DATA / "gestures.json"

PLACE_CENTERS = {
    "左上": (0.2, 0.2), "中上": (0.5, 0.18), "右上": (0.8, 0.2),
    "左": (0.2, 0.5), "中": (0.5, 0.5), "右": (0.8, 0.5),
    "左下": (0.2, 0.8), "中下": (0.5, 0.85), "右下": (0.8, 0.8),
    "上": (0.5, 0.2), "下": (0.5, 0.85),
}


def _load() -> dict[str, Any]:
    try:
        data = json.loads(GESTURES.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:  # noqa: BLE001
        pass
    return {}


def _save(data: dict[str, Any]) -> None:
    GESTURES.parent.mkdir(parents=True, exist_ok=True)
    GESTURES.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def library() -> list[str]:
    return sorted(_load().keys())


def practice_start(name: str) -> dict[str, Any]:
    """起一份新的练习。"""
    name = str(name or "").strip()[:20]
    if not name:
        return {"ok": False, "error": "练习要有名字（比如 太阳）"}
    started = canvas.start(name)
    PRACTICE_DIR.mkdir(parents=True, exist_ok=True)
    return {"ok": True, "name": name, "note": f"开始练「{name}」：自己画，画好就 [PRACTICE_SAVE]"}


def practice_save() -> dict[str, Any]:
    """把当前画布存成一份练习稿。"""
    snapshot = canvas.snapshot()
    if not snapshot.get("strokes"):
        return {"ok": False, "error": "画布是空的，先画几笔"}
    name = str(snapshot.get("title") or "练习").strip()[:20]
    folder = PRACTICE_DIR / name
    folder.mkdir(parents=True, exist_ok=True)
    existing = sorted(folder.glob("attempt-*.json"))
    index = len(existing) + 1
    state = json.loads((DATA / "canvas.json").read_text(encoding="utf-8"))
    (folder / f"attempt-{index}.json").write_text(
        json.dumps({"name": name, "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "strokes": state.get("strokes") or []}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"ok": True, "name": name, "attempt": index,
            "note": f"存下第 {index} 份「{name}」练习稿"}


def practice_pick(name: str, attempt: int) -> dict[str, Any]:
    """挑一份最得意的，记进手法库。"""
    name = str(name or "").strip()[:20]
    folder = PRACTICE_DIR / name
    file = folder / f"attempt-{int(attempt)}.json"
    if not file.exists():
        available = sorted(path.stem for path in folder.glob("attempt-*.json")) if folder.exists() else []
        return {"ok": False, "error": f"没有第 {attempt} 份「{name}」练习",
                "available": available}
    data = json.loads(file.read_text(encoding="utf-8"))
    strokes = data.get("strokes") or []
    if not strokes:
        return {"ok": False, "error": "这份是空白的"}
    library_data = _load()
    library_data[name] = {
        "strokes": strokes,
        "attempt": int(attempt),
        "picked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    _save(library_data)
    # 把这份练习重新铺到画布上，让她看到挑中的样子
    state = json.loads((DATA / "canvas.json").read_text(encoding="utf-8")) if (DATA / "canvas.json").exists() else {}
    canvas.start(name)
    for stroke in strokes:
        canvas.add(_stroke_to_spec(stroke))
    return {"ok": True, "name": name, "attempt": int(attempt), "strokes": len(strokes),
            "note": f"记住了：以后画「{name}」就用这一份（{len(strokes)} 笔）"}


def _stroke_to_spec(stroke: dict[str, Any]) -> str:
    """把一笔还原成她能看懂的写法（用于回放）。"""
    shape = doodle.SHAPE_LABELS.get(str(stroke.get("shape") or ""), "线")
    parts = []
    if stroke.get("color"):
        parts.append(str(stroke["color"]))
    parts.append(shape)
    if stroke.get("place"):
        parts.append(str(stroke["place"]))
    if stroke.get("size"):
        parts.append(str(stroke["size"]))
    if stroke.get("points"):
        for px, py in stroke["points"]:
            parts.append(f"{int(round(px * 100))},{int(round(py * 100))}")
    return " ".join(parts)


def draw_learned(name: str, place: str = "") -> dict[str, Any]:
    """用她练出来的手法画一样东西。

    只有整套手法都是坐标画的时候才整体平移；用格子的就按原位画（不硬搬，免得画歪）。
    """
    name = str(name or "").strip()[:20]
    library_data = _load()
    entry = library_data.get(name)
    if not entry:
        known = "、".join(library_data.keys()) or "还没有"
        return {"ok": False, "error": f"「{name}」还没练过，先 [PRACTICE: {name}] 练几份再挑。手法库里现在有：{known}"}

    strokes = entry.get("strokes") or []
    all_coords = all(stroke.get("points") for stroke in strokes)
    shift = (0.0, 0.0)
    note = f"用你练出来的手法画了「{name}」"
    if place and place in PLACE_CENTERS:
        if all_coords:
            xs = [px for stroke in strokes for px, _ in stroke["points"]]
            ys = [py for stroke in strokes for _, py in stroke["points"]]
            center = (sum(xs) / len(xs), sum(ys) / len(ys))
            goal = PLACE_CENTERS[place]
            shift = (goal[0] - center[0], goal[1] - center[1])
        else:
            note += "（这套是用格子记的，换位置会变形，所以照着原位画了）"

    for stroke in strokes:
        spec = _stroke_to_spec(stroke)
        if shift != (0.0, 0.0):
            spec = _shift_spec(spec, shift)
        canvas.add(spec)
    return {"ok": True, "name": name, "strokes": len(strokes), "note": note}


def _shift_spec(spec: str, shift: tuple[float, float]) -> str:
    """把写法里的位置/坐标按偏移挪一下。"""
    words = spec.split()
    out = []
    dx, dy = shift
    for word in words:
        if word in PLACE_CENTERS:
            x, y = PLACE_CENTERS[word]
            nx, ny = x + dx, y + dy
            best = min(PLACE_CENTERS.items(), key=lambda item: (item[1][0] - nx) ** 2 + (item[1][1] - ny) ** 2)
            out.append(best[0])
            continue
        if "," in word and word.replace(",", "").isdigit():
            px, py = word.split(",")[:2]
            out.append(f"{min(100, max(0, int(px) + int(dx * 100)))},{min(100, max(0, int(py) + int(dy * 100)))}")
            continue
        out.append(word)
    return " ".join(out)
