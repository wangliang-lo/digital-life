# -*- coding: utf-8 -*-
"""表情包仓库。

目录结构：
    stickers/
      index.json          # 元数据
      files/<id>.gif      # 实际文件

一条表情 = {id, tags, note, source, uses}，按标签（开心/委屈/生气/日常…）检索。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..logs import get_logger

log = get_logger("stickers")

ALLOWED_EXT = {".gif", ".png", ".jpg", ".jpeg", ".webp"}
DEFAULT_TAG = "日常"
UNSORTED_TAG = "未分类"


@dataclass(slots=True)
class Sticker:
    id: str
    file: str
    tags: list[str] = field(default_factory=list)
    note: str = ""
    added_at: float = 0.0
    uses: int = 0
    last_used: float = 0.0
    source: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _norm_tags(tags) -> list[str]:
    if isinstance(tags, str):
        tags = [part for part in tags.replace("，", ",").replace(" ", ",").split(",")]
    result: list[str] = []
    seen: set[str] = set()
    for tag in tags or []:
        clean = str(tag).strip()
        if clean and clean.casefold() not in seen:
            seen.add(clean.casefold())
            result.append(clean)
    return result or [UNSORTED_TAG]


def sniff_ext(data: bytes, fallback: str = "") -> str | None:
    """按文件头判断格式，避免把 .exe 改成 .gif 塞进来。"""
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    suffix = fallback.lower()
    return suffix if suffix in ALLOWED_EXT else None


class StickerStore:
    def __init__(self, root: Path, max_kb: int = 2048, rng: random.Random | None = None) -> None:
        self.root = Path(root)
        self.files_dir = self.root / "files"
        self.index_path = self.root / "index.json"
        self.max_bytes = max(64, int(max_kb)) * 1024
        self.rng = rng or random.Random()
        self._lock = threading.RLock()
        self._items: dict[str, Sticker] = {}
        self._loaded = False

    # ------------------------------------------------------------------ 读写

    def load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            self.files_dir.mkdir(parents=True, exist_ok=True)
            if self.index_path.exists():
                try:
                    raw = json.loads(self.index_path.read_text(encoding="utf-8"))
                    for item in raw.get("stickers", []):
                        sticker = Sticker(
                            id=str(item["id"]),
                            file=str(item["file"]),
                            tags=_norm_tags(item.get("tags", [])),
                            note=str(item.get("note", "")),
                            added_at=float(item.get("added_at", 0)),
                            uses=int(item.get("uses", 0)),
                            last_used=float(item.get("last_used", 0)),
                            source=str(item.get("source", "")),
                        )
                        if (self.root / sticker.file).exists():
                            self._items[sticker.id] = sticker
                except (OSError, ValueError, KeyError) as exc:
                    log.warning("读取表情索引失败，将从空仓库开始：%s", exc)
            self._loaded = True
            log.info("表情包仓库：%s（%d 个）", self.root, len(self._items))

    def reload(self) -> None:
        """热重载索引（CLI 导入新表情后不用重启）。"""
        with self._lock:
            self._items.clear()
            self._loaded = False
        self.load()

    def _save(self) -> None:
        payload = {
            "version": 1,
            "stickers": [sticker.to_dict() for sticker in self._items.values()],
        }
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.index_path)

    # ------------------------------------------------------------------ 增删

    def add_file(self, path: str | Path, tags, source: str = "manual", note: str = "") -> Sticker | None:
        source_path = Path(path)
        if not source_path.exists():
            log.warning("表情文件不存在：%s", path)
            return None
        try:
            data = source_path.read_bytes()
        except OSError as exc:
            log.warning("读表情文件失败：%s", exc)
            return None
        return self.add_bytes(data, source_path.suffix, tags, source=source, note=note)

    def add_bytes(self, data: bytes, suffix: str = "", tags=None, source: str = "manual",
                  note: str = "") -> Sticker | None:
        if not data:
            return None
        if len(data) > self.max_bytes:
            log.info("表情太大（%.1fMB），跳过：> %dKB", len(data) / 1024 / 1024, self.max_bytes // 1024)
            return None
        ext = sniff_ext(data, suffix)
        if ext is None:
            log.info("不认识的表情格式（suffix=%s），跳过", suffix)
            return None

        sticker_id = hashlib.sha1(data).hexdigest()[:12]
        normalized = _norm_tags(tags)

        with self._lock:
            self.load()
            existing = self._items.get(sticker_id)
            if existing is not None:
                merged = _norm_tags(existing.tags + normalized)
                if merged != existing.tags:
                    existing.tags = merged
                    self._save()
                return existing

            self.files_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{sticker_id}{ext}"
            (self.files_dir / filename).write_bytes(data)
            sticker = Sticker(
                id=sticker_id,
                file=f"files/{filename}",
                tags=normalized,
                note=note,
                added_at=time.time(),
                source=source,
            )
            self._items[sticker_id] = sticker
            self._save()
            log.info("收录表情 %s（%s，来源 %s）", sticker_id, "/".join(normalized), source)
            return sticker

    def add_remote_file(self, tmp_path: Path, tags, source: str = "download") -> Sticker | None:
        """外部下载好再入库，避免在锁里做网络 IO。"""
        sticker = self.add_file(tmp_path, tags, source=source)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return sticker

    def remove(self, sticker_id: str) -> bool:
        with self._lock:
            self.load()
            sticker = self._items.pop(sticker_id, None)
            if sticker is None:
                return False
            try:
                (self.root / sticker.file).unlink(missing_ok=True)
            except OSError:
                pass
            self._save()
            return True

    # ------------------------------------------------------------------ 选择

    def pick(self, tags: list[str] | None = None) -> Sticker | None:
        with self._lock:
            self.load()
            items = list(self._items.values())
        if not items:
            return None

        wanted = {tag.casefold() for tag in (tags or [])}
        if wanted:
            matched = [s for s in items if wanted & {tag.casefold() for tag in s.tags}]
            if matched:
                items = matched
            elif DEFAULT_TAG in {tag.casefold() for tag in wanted}:
                pass  # 想要"日常"但没有，就退化成全体

        # 用得越少越容易被挑中，避免总是同一张
        weights = [1.0 / (1.0 + max(0, sticker.uses) * 0.6) for sticker in items]
        return self.rng.choices(items, weights=weights, k=1)[0]

    def pick_random(self) -> Sticker | None:
        return self.pick([])

    def mark_used(self, sticker_id: str) -> None:
        with self._lock:
            sticker = self._items.get(sticker_id)
            if sticker is None:
                return
            sticker.uses += 1
            sticker.last_used = time.time()
            self._save()

    # ------------------------------------------------------------------ 查询

    def list(self) -> list[Sticker]:
        with self._lock:
            self.load()
            return sorted(self._items.values(), key=lambda s: s.added_at, reverse=True)

    def get(self, sticker_id: str) -> Sticker | None:
        with self._lock:
            self.load()
            return self._items.get(sticker_id)

    def resolve(self, sticker: Sticker) -> Path:
        return self.root / sticker.file

    def tag_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for sticker in self.list():
            for tag in sticker.tags:
                counts[tag] = counts.get(tag, 0) + 1
        return counts

    def count(self) -> int:
        with self._lock:
            self.load()
            return len(self._items)

    def import_dir(self, directory: str | Path, tags=None) -> int:
        """整目录导入（附带把子目录名当标签）。"""
        base = Path(directory)
        if not base.exists():
            return 0
        added = 0
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in ALLOWED_EXT:
                continue
            extra = list(tags or [])
            if path.parent != base:
                extra.append(path.parent.name)
            if self.add_file(path, extra or [UNSORTED_TAG], source="import"):
                added += 1
        return added
