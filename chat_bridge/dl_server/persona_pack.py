# -*- coding: utf-8 -*-
"""
persona_pack —— 语料包：把 persona/ 目录里的「说话习惯」和「语料样例」拼进提示词。

为什么要单独一个模块：
prompt_builder.py 有两千多行，直接改它风险大；这里只做一件事——读文件、拼块、带缓存。

文件：
    persona/voice.md     每行 `- ` 一条说话规则
    persona/samples.json 按场景分的短语料

设计取舍：
- 规则全带（通常不到 800 字）
- 样例只带 2 个场景、每个场景 2~3 条，**按小时轮转**
  为什么按小时不按每轮随机：每轮都变会让提示词前缀缓存失效，白烧钱；
  一小时换一次，既有变化又能命中缓存。
- 文件改了立即生效（按 mtime 缓存）
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

_PERSONA_DIR = Path(__file__).resolve().parent.parent / "persona"
_VOICE_FILE = _PERSONA_DIR / "voice.md"
_SAMPLES_FILE = _PERSONA_DIR / "samples.json"

_CACHE: dict[str, Any] = {"voice_mtime": 0.0, "voice": "", "samples_mtime": 0.0, "samples": {}}

# 规则块的长度上限：再长就会盖过别的提示词
_MAX_RULES_CHARS = 900


def _read_bullets(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("- "):
            item = s[2:].strip()
            if item:
                out.append(item)
    return out


def load_rules() -> list[str]:
    try:
        mtime = _VOICE_FILE.stat().st_mtime
    except OSError:
        return []
    if _CACHE["voice_mtime"] != mtime:
        _CACHE["voice"] = _read_bullets(_VOICE_FILE)
        _CACHE["voice_mtime"] = mtime
    return list(_CACHE["voice"])


def load_samples() -> dict[str, list[str]]:
    try:
        mtime = _SAMPLES_FILE.stat().st_mtime
    except OSError:
        return {}
    if _CACHE["samples_mtime"] != mtime:
        try:
            data = json.loads(_SAMPLES_FILE.read_text(encoding="utf-8"))
            scenes = data.get("scenes") or {}
            cleaned: dict[str, list[str]] = {}
            for name, items in scenes.items():
                if isinstance(items, list):
                    vals = [str(x).strip() for x in items if str(x).strip()]
                    if vals:
                        cleaned[str(name)] = vals
            _CACHE["samples"] = cleaned
        except Exception:  # noqa: BLE001
            _CACHE["samples"] = {}
        _CACHE["samples_mtime"] = mtime
    return dict(_CACHE["samples"])


def _rotate_items(seq: list[str], count: int, offset: int) -> list[str]:
    if not seq:
        return []
    n = min(count, len(seq))
    start = offset % len(seq)
    return [seq[(start + i) % len(seq)] for i in range(n)]


def voice_block() -> str:
    """返回拼进提示词的【语感】块；没有语料文件就返回空串。"""
    rules = load_rules()
    scenes = load_samples()

    if not rules and not scenes:
        return ""

    parts: list[str] = []

    if rules:
        # 去掉句尾句号再拼，避免出现「。；」这种双标点
        cleaned = [r.rstrip("。；;") for r in rules if r.strip()]
        text = "；".join(cleaned)
        if len(text) > _MAX_RULES_CHARS:
            text = text[:_MAX_RULES_CHARS].rsplit("；", 1)[0]
        parts.append(
            "\n【语感】说话照这个来，不是任务，是你本来就有的习惯：\n" + text
        )

    if scenes:
        names = sorted(scenes.keys())
        # 按小时轮转，两个场景
        hour = int(time.time() // 3600)
        picked = _rotate_items(names, 2, hour)
        lines: list[str] = []
        for idx, name in enumerate(picked):
            items = _rotate_items(scenes[name], 3, hour + idx)
            if not items:
                continue
            lines.append(f"- {name}：" + " / ".join(f"「{x}」" for x in items))
        if lines:
            parts.append(
                "\n【语感样例】只学断句和口气，别照抄原句：\n" + "\n".join(lines)
            )

    return "".join(parts)


def pack_status() -> dict[str, Any]:
    """给诊断用：语料包装没装上、装了几条。"""
    rules = load_rules()
    scenes = load_samples()
    return {
        "voice_file": str(_VOICE_FILE),
        "voice_exists": _VOICE_FILE.exists(),
        "rules": len(rules),
        "samples_file": str(_SAMPLES_FILE),
        "samples_exists": _SAMPLES_FILE.exists(),
        "scenes": {k: len(v) for k, v in scenes.items()},
        "block_chars": len(voice_block()),
    }
