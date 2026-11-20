"""网页端"她主动发消息"（2026-11-08）。

爸爸："给网页端加小鱼主动发信息。"

做法：
1. dl_api 起一个后台线程：隔一阵看看她是不是闲着、醒着，
   是就用 life.proactive_chat() 生成一句，写进 data/web_proactive.jsonl。
2. web_ui 暴露 /api/proactive?since=...，前端轮询，新的就当成她的消息显示。
3. 她最近在网页上聊过天（data/last_web_chat.txt 很新）就不打扰。
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

DATA = HERE / "data"
QUEUE = DATA / "web_proactive.jsonl"
LAST_WEB = DATA / "last_web_chat.txt"

IDLE_BEFORE_PROACTIVE = 40 * 60      # 网页上 40 分钟没人说话，才轮到她开口
MIN_GAP_BETWEEN = 50 * 60            # 两次主动之间至少隔 50 分钟
MAX_PER_DAY = 6


def _count_today() -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        lines = QUEUE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    return sum(1 for line in lines if line.strip() and str(json.loads(line).get("at", "")).startswith(today))


def _last_sent() -> float:
    try:
        lines = [l for l in QUEUE.read_text(encoding="utf-8").splitlines() if l.strip()]
    except OSError:
        return 0.0
    if not lines:
        return 0.0
    try:
        stamp = str(json.loads(lines[-1]).get("at") or "")
        return datetime.fromisoformat(stamp).timestamp()
    except Exception:  # noqa: BLE001
        return 0.0


def _web_idle_seconds() -> float:
    try:
        return time.time() - LAST_WEB.stat().st_mtime
    except OSError:
        return 999999.0


def maybe_speak(get_life) -> str:
    """看她该不该主动说一句；说了就返回文本，否则空字符串。"""
    now = time.time()
    if now - _last_sent() < MIN_GAP_BETWEEN:
        return ""
    if _web_idle_seconds() < IDLE_BEFORE_PROACTIVE:
        return ""
    if _count_today() >= MAX_PER_DAY:
        return ""
    try:
        state = json.loads((DATA / "sleep_state.json").read_text(encoding="utf-8"))
        if state.get("asleep"):
            return ""
    except (OSError, ValueError):
        pass
    life = get_life()
    if life is None:
        return ""
    try:
        result = life.proactive_chat()
    except Exception:  # noqa: BLE001
        return ""
    text = str((result or {}).get("reply") or "").strip()
    if not text or text.upper() in ("SILENT", "SKIP"):
        return ""
    try:
        QUEUE.parent.mkdir(parents=True, exist_ok=True)
        with QUEUE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "text": text[:400],
            }, ensure_ascii=False) + "\n")
    except OSError:
        return ""
    return text


def pending(since: str = "") -> list[dict]:
    try:
        lines = [l for l in QUEUE.read_text(encoding="utf-8").splitlines() if l.strip()]
    except OSError:
        return []
    items = []
    for line in lines[-40:]:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if since and str(item.get("at") or "") <= since:
            continue
        items.append(item)
    return items
