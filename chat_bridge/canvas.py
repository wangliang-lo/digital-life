"""她自己的画布（2026-11-11）。

爸爸："画画不要 AI 生成，她应该自己涂鸦、自己端详、自己修改。"

所以给的不是"一句提示词出一张图"，而是一块**一直存在**的画布：
  [DRAW]                 起一张新画（清空画布）
  [PAINT: 红圆 左边；蓝线 下面]  加上几笔（不清空，累加）
  [PAINT_LOOK]           端详：数一遍画了什么、哪儿空着、颜色偏不偏
  [PAINT_UNDO]           擦掉刚画的那一批
  [PAINT_DONE]           画完，收进作品集

画布状态存在 data/canvas.json，每次加笔重画整张（0.1 秒），
所以她是"一笔一笔画出来的"，不是一次生成。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import doodle

DATA = Path(__file__).resolve().parent / "data"
STATE = DATA / "canvas.json"
PREVIEW = DATA / "drawn" / "canvas-live.png"
FINAL_DIR = DATA / "drawn"

PLACE_ZONES = {
    "左上": "左上方", "中上": "正上方", "右上": "右上方",
    "左": "左边", "中": "中间", "右": "右边",
    "左下": "左下方", "中下": "正下方", "右下": "右下方",
    "上": "上方", "下": "下方",
}


def _load() -> dict[str, Any]:
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("strokes"), list):
            return data
    except Exception:  # noqa: BLE001
        pass
    return {"strokes": [], "batches": [], "updated_at": ""}


def _save(data: dict[str, Any]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _render(data: dict[str, Any]) -> str:
    """把整张画布重画一遍，返回预览图路径。"""
    strokes = data.get("strokes") or []
    if not strokes:
        try:
            PREVIEW.unlink(missing_ok=True)
        except OSError:
            pass
        return ""
    image = doodle.paint_strokes(strokes)
    PREVIEW.parent.mkdir(parents=True, exist_ok=True)
    image.save(PREVIEW, "PNG")
    return str(PREVIEW)


def start(title: str = "") -> dict[str, Any]:
    """起一张新画。"""
    data = {"strokes": [], "batches": [], "title": str(title or "")[:40],
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "updated_at": ""}
    _save(data)
    return {"ok": True, "empty": True, "caption": "空画布，等她下笔"}


def add(spec: str) -> dict[str, Any]:
    """加几笔（累加，不清空）。"""
    strokes = doodle.parse(str(spec or ""))
    if not strokes:
        return {"ok": False, "error": "这几笔没看懂（形状/颜色/位置都认不出）"}
    data = _load()
    data.setdefault("strokes", []).extend(strokes)
    data.setdefault("batches", []).append(len(strokes))
    data["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    _save(data)
    path = _render(data)
    captions = "、".join(doodle.describe_stroke(stroke) for stroke in strokes)
    return {
        "ok": True,
        "path": path,
        "added": len(strokes),
        "total": len(data["strokes"]),
        "caption": captions,
    }


def undo() -> dict[str, Any]:
    """擦掉最后一批。"""
    data = _load()
    batches = data.get("batches") or []
    strokes = data.get("strokes") or []
    if not batches:
        return {"ok": False, "error": "画布上还没有能擦的"}
    count = batches.pop()
    del strokes[max(0, len(strokes) - count):]
    data["batches"] = batches
    data["strokes"] = strokes
    data["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    _save(data)
    _render(data)
    return {"ok": True, "removed": count, "total": len(strokes)}


def look() -> dict[str, Any]:
    """端详：说出画上有什么、哪儿空着、颜色偏不偏。"""
    data = _load()
    strokes = data.get("strokes") or []
    if not strokes:
        return {"ok": True, "empty": True,
                "text": "画布是空的，一笔都没有。"}

    counts: dict[str, int] = {}
    zones: dict[str, int] = {}
    for stroke in strokes:
        shape = doodle.SHAPE_LABELS.get(stroke.get("shape", ""), "笔")
        color = stroke.get("color") or "默认色"
        counts[f"{color}{shape}"] = counts.get(f"{color}{shape}", 0) + 1
        zone = PLACE_ZONES.get(stroke.get("place") or "中", "中间")
        zones[zone] = zones.get(zone, 0) + 1

    pieces = []
    for name, count in sorted(counts.items(), key=lambda item: -item[1])[:5]:
        pieces.append(f"{name}×{count}" if count > 1 else name)
    zone_text = "、".join(f"{zone}{count}笔" for zone, count in sorted(zones.items(), key=lambda item: -item[1]))
    empty_zones = [zone for zone in ("左上方", "正上方", "右上方", "左边", "右边", "左下方", "正下方", "右下方")
                   if zone not in zones]

    text = f"画上现在有 {len(strokes)} 笔：" + "、".join(pieces) + "。"
    text += f"分布：{zone_text}。"
    if empty_zones:
        text += "空着的地方：" + "、".join(empty_zones[:4]) + "。"
    return {"ok": True, "path": _render(data), "total": len(strokes),
            "zones": zones, "counts": counts, "text": text}


def finish(title: str = "") -> dict[str, Any]:
    """画完：存成一张正式作品，收进作品集。"""
    data = _load()
    strokes = data.get("strokes") or []
    if not strokes:
        return {"ok": False, "error": "画布是空的，还没画"}
    image = doodle.paint_strokes(strokes)
    FINAL_DIR.mkdir(parents=True, exist_ok=True)
    name = f"her-{datetime.now():%Y%m%d-%H%M%S}.png"
    path = FINAL_DIR / name
    image.save(path, "PNG")

    caption = str(title or data.get("title") or "").strip() or f"{len(strokes)} 笔"
    try:
        with (FINAL_DIR.parent / "drawings.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "name": name,
                "caption": caption[:60],
                "strokes": len(strokes),
                "by": "herself",
                "origin": "canvas",
            }, ensure_ascii=False) + "\n")
        with (FINAL_DIR.parent / "doodles.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "name": name,
                "caption": caption[:60],
                "origin": "canvas",
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass

    STATE.unlink(missing_ok=True)
    return {"ok": True, "path": str(path), "name": name, "caption": caption, "strokes": len(strokes)}


def snapshot() -> dict[str, Any]:
    data = _load()
    return {"strokes": len(data.get("strokes") or []), "title": data.get("title") or "",
            "updated_at": data.get("updated_at") or ""}
