#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""给IM表情池里没标签的表情补描述/标签。

DSH 原来由模型在聊天里顺手打标签；IM的大脑换成小鱼之后这条断了，
表情池里的图只有编号没有描述，App 就不会把它们当候选给她。

做法：读本机读取服务的表情池 → 没标签的逐张取图 → 调 dl_api /describe →
用 sticker manage/edit 写回 name/description/tags。

    python tools/label_stickers.py            # 补最多 5 张
    python tools/label_stickers.py --limit 20
    python tools/label_stickers.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# IM读取服务的配置在 IMAgent 的工作区里，不在本项目目录。
STATE = Path(r"C:\Users\Lenovo\Desktop\aihuman\data\workspace\state")


def reader() -> tuple[str, str]:
    io = json.loads((STATE / "chat-io-config.json").read_text(encoding="utf-8"))
    return io["reader_base_url"].rstrip("/"), io["reader_token"]


def call(base: str, token: str, method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Authorization": "Bearer " + token}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--watch", type=int, default=0, help="常驻：每 N 分钟补一批")
    args = parser.parse_args()

    if args.watch > 0:
        print(f"[sticker-labeler] 每 {args.watch} 分钟补一批标签", flush=True)
        while True:
            try:
                run_once(args)
            except Exception as exc:  # noqa: BLE001
                print(f"[sticker-labeler] 本轮失败：{type(exc).__name__}: {exc}", flush=True)
            time.sleep(max(60, args.watch * 60))
    return run_once(args)


def run_once(args) -> int:
    base, token = reader()
    listing = call(base, token, "GET", "/api/v1/stickers/manage")
    items = listing.get("items") or []
    todo = [item for item in items if not (item.get("description") or "").strip()]
    print(f"表情池 {len(items)} 张，其中 {len(todo)} 张没描述；这次处理 {min(args.limit, len(todo))} 张")

    done = 0
    for item in todo[: args.limit]:
        digest = item.get("id")
        try:
            img = call(base, token, "GET", f"/api/v1/stickers/manage/image?id={digest}&scope={listing['scope']}")
            import base64

            raw = base64.b64decode(img.get("base64") or "")
            tmp = STATE / "xiaoyu-images" / f"pool-{digest[:16]}.png"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(raw)
        except Exception as exc:  # noqa: BLE001
            print(f"  取图失败 {digest[:10]}: {type(exc).__name__}: {exc}")
            continue
        try:
            # task=sticker：两次提问（画面 / 图上文字），比通用描述准得多。
            body = json.dumps({"image_path": str(tmp), "task": "sticker"}, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request("http://127.0.0.1:8000/describe", data=body,
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=180) as resp:
                info = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"  描述失败 {digest[:10]}: {type(exc).__name__}: {exc}")
            continue
        description = (info.get("description") or "").strip()[:120]
        tags = [str(tag)[:16] for tag in (info.get("tags") or [])][:6] or ["表情"]
        if not description:
            continue
        if args.dry_run:
            print(f"  [dry] {digest[:10]} -> {description} {tags}")
            continue
        fresh = call(base, token, "GET", "/api/v1/stickers/manage")
        try:
            call(base, token, "POST", "/api/v1/stickers/manage/edit", {
                "scope": fresh["scope"],
                "revision": fresh["revision"],
                "id": digest,
                "name": description[:40],
                "description": description,
                "tags": tags,
            })
            print(f"  ✓ {digest[:10]} -> {description}")
            done += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  写回失败 {digest[:10]}: {type(exc).__name__}: {exc}")
        time.sleep(0.3)

    print(f"完成 {done} 张")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
