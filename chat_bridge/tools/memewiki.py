# -*- coding: utf-8 -*-
"""梗百科：从 dsh-memewiki 的词库里查梗。

2026-11-04 爸爸："给小鱼接上让她更有梗一点"（https://github.com/thawline/dsh-memewiki）。

那个项目是 DSH 的 Node 插件，会在模型看到消息前把词条塞进 system prompt。
小鱼的脑子不在 DSH 里（是 dl_api 的 Python），所以这里直接读它的词库
`data/memes.db`（6696 条，MIT，词源 cnmeme.wiki），自己做一个轻量查询。
不联网、零依赖（sqlite3 是标准库）。

用法：
    from memewiki import find_in_text, look_up, describe
    hit = find_in_text("去码头整点薯条")     # 消息里出现词条 → 命中
    entry = look_up("蚌埠住了")               # 精确/包含查询
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "memes.db"

_CACHE: list[dict[str, Any]] | None = None


def _load() -> list[dict[str, Any]]:
    """整库读进内存（6696 条，几 MB），之后匹配都是纯字符串操作。"""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    rows: list[dict[str, Any]] = []
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            for row in con.execute("select title, aliases, tags, usage, origin, summary from memes"):
                aliases = [a.strip() for a in str(row["aliases"] or "").replace("，", ",").split(",") if a.strip()]
                rows.append({
                    "title": str(row["title"] or "").strip(),
                    "aliases": aliases,
                    "tags": str(row["tags"] or "").strip(),
                    "usage": str(row["usage"] or "").strip(),
                    "origin": str(row["origin"] or "").strip(),
                    "summary": str(row["summary"] or "").strip(),
                })
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        print(f"梗库读取失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        rows = []
    _CACHE = rows
    return rows


def count() -> int:
    return len(_load())


def describe(entry: dict[str, Any], limit: int = 220) -> str:
    """拼成给她看的一句话。"""
    parts = [f"「{entry['title']}」"]
    if entry.get("summary"):
        parts.append(entry["summary"][:limit])
    if entry.get("usage"):
        parts.append("怎么用：" + entry["usage"][:120])
    return "，".join(parts)


def look_up(query: str) -> dict[str, Any] | None:
    """精确 → 别名 → 词条名包含查询词（≥2 字）→ 查询词包含词条名。"""
    text = str(query or "").strip()
    if not text:
        return None
    rows = _load()
    for entry in rows:  # 精确
        if entry["title"] == text or text in entry["aliases"]:
            return entry
    lowered = text.lower()
    for entry in rows:  # 双向包含
        title = entry["title"]
        if len(title) >= 2 and (title in text or (len(text) >= 2 and lowered in title.lower())):
            return entry
    return None


def find_in_text(message: str, limit: int = 1) -> list[dict[str, Any]]:
    """在一句话里找已经存在的梗（长词条优先，避免"码头"抢了"去码头整点薯条"）。"""
    text = str(message or "").strip()
    if not text or len(text) < 2:
        return []
    hits: list[dict[str, Any]] = []
    rows = _load()
    for entry in rows:
        title = entry["title"]
        if len(title) < 2:
            continue
        if title in text or any(len(a) >= 2 and a in text for a in entry["aliases"]):
            hits.append(entry)
    hits.sort(key=lambda item: len(item["title"]), reverse=True)
    return hits[:limit]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("词条数：", count())
    for probe in ("蚌埠住了", "示例词", "去码头整点薯条", "YYDS"):
        entry = look_up(probe)
        print(f"  {probe} → " + (describe(entry) if entry else "（没查到）"))
    sample = "今天群里有人说什么去码头整点薯条，我没懂"
    hit = find_in_text(sample)
    print("句子命中：", describe(hit[0]) if hit else "（无）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
