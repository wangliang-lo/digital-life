# -*- coding: utf-8 -*-
"""把 ~/.dsh/meme-packs 里的表情包导入小鱼自己的表情库。

她的库是 files/<hash>.<ext> + index.json：[{id, tags, note, source, uses}]
meme 包是 <pack>/<tag>/*.jpg + manifest.json
按 tag 名翻译成她的检索标签（开心/生气/难过/害羞/困惑/日常）。
"""
import hashlib
import json
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

PACKS = Path.home() / ".dsh" / "meme-packs"
STORE = Path(r"PROJECT_ROOT\chat_bridge\stickers")
INDEX = STORE / "index.json"
FILES = STORE / "files"

TAG_MAP = {
    "happy": ["开心"], "like": ["喜欢", "开心"], "meow": ["开心", "可爱"],
    "angry": ["生气"], "sad": ["难过"], "shy": ["害羞"], "confused": ["困惑"],
    "surprised": ["困惑", "惊讶"], "sigh": ["无语"], "baka": ["无语", "困惑"],
    "fool": ["无语"], "daily": ["日常"], "sleep": ["日常", "睡觉"], "morning": ["日常"],
    "work": ["日常"], "cpu": ["日常"], "color": ["日常"], "givemoney": ["日常"], "see": ["日常"],
}


def main() -> int:
    if not PACKS.exists():
        print("没有 meme-packs 目录")
        return 1
    FILES.mkdir(parents=True, exist_ok=True)
    try:
        raw = json.loads(INDEX.read_text(encoding="utf-8")) if INDEX.exists() else {"version": 1, "stickers": []}
    except Exception:
        raw = {"version": 1, "stickers": []}
    if isinstance(raw, list):
        raw = {"version": 1, "stickers": raw}
    stickers = raw.setdefault("stickers", [])
    index = stickers
    have = {item.get("id") for item in stickers if isinstance(item, dict)}
    added = 0
    for pack in sorted(p for p in PACKS.iterdir() if p.is_dir()):
        # 有的包是 <pack>/<tag>/，有的是 <pack>/memes/<tag>/
        roots = [pack / "memes"] if (pack / "memes").is_dir() else [pack]
        for root in roots:
          for tag_dir in sorted(d for d in root.iterdir() if d.is_dir()):
              tags = TAG_MAP.get(tag_dir.name.lower(), ["日常"])
              for src in sorted(tag_dir.iterdir()):
                  if not src.is_file() or src.suffix.lower() not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
                      continue
                  data = src.read_bytes()
                  digest = hashlib.sha256(data).hexdigest()[:12]
                  if digest in have:
                      continue
                  dest = FILES / f"{digest}{src.suffix.lower()}"
                  if not dest.exists():
                      shutil.copyfile(src, dest)
                  import time
                  index.append({
                      "id": digest,
                      "file": f"files/{digest}{src.suffix.lower()}",
                      "tags": tags,
                      "note": f"{pack.name}/{tag_dir.name}",
                      "added_at": time.time(),
                      "source": "meme-pack",
                      "uses": 0,
                  })
                  have.add(digest)
                  added += 1
    INDEX.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"导入 {added} 张；表情库现在共 {len(index)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
