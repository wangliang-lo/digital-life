#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""给她自己的表情库重打标签：画面 + 图上文字 + 是不是她自己。

2026-11-07 爸爸："她竟然不知道她发的表情包是什么……那个鲸鱼娘是她的虚拟形象，
上面的文案也让她看清楚。"

以前 note 里大多是文件名（dafeiyu-001/happy），tags 只有情绪词，
所以她按"开心"挑的时候等于在二十张里随机——而且不知道挑中哪张。

现在每张写清：
    <画面>；图上写着「<逐字文案>」；这是她自己的虚拟形象（她／鲸鱼娘）
标签追加情绪词和 `我自己`。

    python tools/relabel_stickers.py --limit 5 --dry-run
    python tools/relabel_stickers.py            # 全部（跳过已经写好的）
    python tools/relabel_stickers.py --force    # 全部重写
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LIB = ROOT / "stickers" / "index.json"

# 打标签的逻辑和提示词都从 dl_api.py 里切出来用，避免两处漂移。
_src = (ROOT / "dl_api.py").read_text(encoding="utf-8")
_block = _src[_src.index("_STICKER_TAG_WORDS = (") : _src.index("# 激素事件")]
_ns: dict[str, Any] = {"re": re, "Any": Any}
exec(compile(_block, "dl_api:sticker", "exec"), _ns)  # noqa: S102
sticker_label = _ns["sticker_label"]

# 她＝鲸鱼娘，是她自己的虚拟形象（2026-11-07 爸爸当面确认；图我也看过：
# 蓝发、鲸鱼鳍耳、鲸鱼尾巴、围裙上印着示例昵称）。按**包名**认人比让视觉模型猜可靠。
SELF_PACKS = ("dafeiyu",)


def pack_of(item: dict) -> str:
    """老 note 里存的是 dafeiyu-001/happy 这种包名/文件名，覆盖前先留下来。"""
    saved = str(item.get("pack") or "").strip()
    if saved:
        return saved
    note = str(item.get("note") or "")
    return note.split("/", 1)[0].strip() if "/" in note else ""


def is_self_pack(pack: str) -> bool:
    return any(pack.startswith(name) for name in SELF_PACKS)


def make_describer():
    from bridge.config import VisionConfig  # noqa: PLC0415
    from bridge.media.vision import ImageDescriber  # noqa: PLC0415

    import os  # noqa: PLC0415

    model = os.environ.get("OLLAMA_VISION_MODEL") or "qwen2.5vl:3b"
    return ImageDescriber(VisionConfig(provider="ollama", model=model, base_url="http://127.0.0.1:11434"))


def looks_described(note: str) -> bool:
    """已经写过一句中文画面描述的，就不再重复打。"""
    text = str(note or "").strip()
    if len(text) < 12:
        return False
    return bool(re.search(r"[\u4e00-\u9fff]", text)) and "/" not in text


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="最多处理几张（0=全部）")
    parser.add_argument("--force", action="store_true", help="已经写过描述的也重打")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sleep", type=float, default=0.2)
    args = parser.parse_args()

    data = json.loads(LIB.read_text(encoding="utf-8"))
    items = [s for s in (data.get("stickers") or []) if isinstance(s, dict)]
    todo = [
        item
        for item in items
        if args.force or not looks_described(item.get("note") or "")
    ]
    print(f"表情库 {len(items)} 张，待处理 {len(todo)} 张")
    if args.limit:
        todo = todo[: args.limit]

    if not args.dry_run:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = LIB.with_suffix(f".json.bak-{stamp}-relabel")
        shutil.copy(LIB, backup)
        print("备份：", backup.name)

    describer = make_describer()
    done = 0
    started = time.time()
    for index, item in enumerate(todo, 1):
        path = ROOT / "stickers" / str(item.get("file") or "")
        if not path.exists():
            print(f"  [{index}/{len(todo)}] 缺文件 {item.get('file')}")
            continue
        try:
            label = sticker_label(describer, str(path))
        except Exception as exc:  # noqa: BLE001
            print(f"  [{index}/{len(todo)}] 失败 {str(item.get('id'))[:8]}: {type(exc).__name__}: {exc}")
            continue

        note = str(label["description"])[:120]
        pack = pack_of(item)
        if is_self_pack(pack):
            # 视觉没认出来也能定：这是她自己。
            note = re.sub(r"；?这是她自己的虚拟形象（她／鲸鱼娘）$", "", note).rstrip("；")
            note = f"{note}；这是你自己的虚拟形象（鲸鱼娘）"[:120]
            label["is_self"] = True
        tags = list(
            dict.fromkeys(list(label["tags"]) + [str(t) for t in (item.get("tags") or [])])
        )[:6]
        if label.get("is_self") and "我自己" not in tags:
            tags = (tags + ["我自己"])[:6]
        print(f"  [{index}/{len(todo)}] {str(item.get('id'))[:8]} -> {note}")
        if args.dry_run:
            continue
        item["note"] = note
        item["tags"] = tags
        if pack and not item.get("pack"):
            item["pack"] = pack
        if label.get("caption"):
            item["caption"] = str(label["caption"])[:60]
        if label.get("is_self"):
            item["is_self"] = True
        done += 1
        # 每 10 张落一次盘，别跑一半全丢
        if done % 10 == 0:
            LIB.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        time.sleep(max(0.0, args.sleep))

    if not args.dry_run:
        LIB.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"完成 {done} 张，用时 {time.time() - started:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
