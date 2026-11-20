"""她发表情包：按自己的喜好来，不是掷骰子（2026-11-12）。

爸爸："发表情包不要变成纯概率，按她喜好。"

做法：
  * 心情决定想不想发（开心/委屈/上火 越浓越想发）
  * 冷却：刚发过就不发，隔一会儿才又想发
  * 偏好：发过的标签记分（data/sticker_prefs.json），
    以后优先挑偏爱的标签；同一张图尽量不重复（store.pick 自己也会避开）
  * 留一点随机，但阈值和选图都由状态决定
"""
from __future__ import annotations

import json
import random
from datetime import datetime
from pathlib import Path
from typing import Any

DATA = Path(__file__).resolve().parent / "data"
STATE = DATA / "sticker_prefs.json"

MOOD_TAGS = (
    ("joy", 62, ("开心", "可爱", "搞笑")),
    ("hurt", 55, ("委屈", "求饶", "难过")),
    ("impulse", 65, ("无语", "生气")),
)
FALLBACK_TAGS = ("日常", "可爱")


def _load() -> dict[str, Any]:
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:  # noqa: BLE001
        pass
    return {"tag_score": {}, "last_sent_at": "", "sent": 0}


def _save(data: dict[str, Any]) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def _minutes_since(stamp: str) -> float:
    if not stamp:
        return 9999.0
    try:
        when = datetime.fromisoformat(str(stamp))
    except ValueError:
        return 9999.0
    if when.tzinfo is None:
        when = when.astimezone()
    return (datetime.now().astimezone() - when).total_seconds() / 60.0


def favorite_tags(limit: int = 3) -> list[str]:
    """她偏爱的标签（发得多的排前面）。"""
    scores = _load().get("tag_score") or {}
    ranked = sorted(((str(k), float(v)) for k, v in scores.items() if float(v) > 0),
                    key=lambda item: item[1], reverse=True)
    return [tag for tag, _ in ranked[:limit]]


def mood_tags(mood: dict[str, Any] | None) -> list[str]:
    mood = mood or {}
    tags: list[str] = []
    for key, threshold, names in MOOD_TAGS:
        try:
            value = float(mood.get(key) or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value >= threshold:
            tags.extend(names)
    for tag in favorite_tags():
        if tag not in tags:
            tags.append(tag)
    if not tags:
        tags.extend(FALLBACK_TAGS)
    return tags


def desire(mood: dict[str, Any] | None, is_owner: bool = False) -> float:
    """此刻有多想甩一张表情包（0~0.75）。心情浓 + 有偏好 + 隔得久 → 更想。"""
    mood = mood or {}
    try:
        joy = float(mood.get("joy") or 0)
        hurt = float(mood.get("hurt") or 0)
        impulse = float(mood.get("impulse") or 0)
    except (TypeError, ValueError):
        joy = hurt = impulse = 0.0

    # 只算"超出平常"的那部分：平静的时候本来就不太想甩图
    joy_high = max(0.0, joy - 60.0) / 40.0
    hurt_high = max(0.0, hurt - 50.0) / 50.0
    impulse_high = max(0.0, impulse - 55.0) / 45.0
    intensity = max(joy_high, hurt_high, impulse_high)

    value = 0.06 + intensity * 0.55
    if is_owner:
        value += 0.04
    if favorite_tags():
        value += 0.05          # 她本来就有爱用的图

    waited = _minutes_since(_load().get("last_sent_at") or "")
    if waited < 8:
        value *= 0.15          # 刚发过，别刷屏
    elif waited < 20:
        value *= 0.55
    return max(0.0, min(0.75, value))


def should_send(mood: dict[str, Any] | None, is_owner: bool = False) -> bool:
    return random.random() < desire(mood, is_owner)


def choose(store: Any, mood: dict[str, Any] | None, is_owner: bool = False) -> Any:
    """挑一张：先按心情 + 偏好的标签找，找不到再退到全库。"""
    tags = mood_tags(mood)
    sticker = None
    try:
        sticker = store.pick(tags)
    except Exception:  # noqa: BLE001
        sticker = None
    if sticker is None:
        try:
            sticker = store.pick_random()
        except Exception:  # noqa: BLE001
            return None
    note_sent(sticker)
    return sticker


def note_sent(sticker: Any) -> None:
    """记下这一张：标签加分、更新冷却。"""
    data = _load()
    tag_score = data.setdefault("tag_score", {})
    for tag in (getattr(sticker, "tags", None) or [])[:3]:
        key = str(tag)
        if not key:
            continue
        tag_score[key] = float(tag_score.get(key) or 0) + 1.0
    data["last_sent_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    data["sent"] = int(data.get("sent") or 0) + 1
    # 分数别无限涨：每 20 次衰减一点，保持"最近爱用"的意味
    if data["sent"] % 20 == 0:
        for key in list(tag_score):
            tag_score[key] = round(float(tag_score[key]) * 0.7, 2)
    _save(data)
    try:
        store.mark_used(getattr(sticker, "id", ""))
    except Exception:  # noqa: BLE001
        pass
