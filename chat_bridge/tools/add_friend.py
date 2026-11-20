#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把一个人加进小鱼的聊天白名单，再让她自己写一段自我介绍发过去。

    python tools/add_friend.py --name "Zero°"
    python tools/add_friend.py --name "Zero°" --no-intro

2026-11-04 爸爸："有一个新好友 Zero° 加上了小鱼，可以让她列入聊天白名单，
然后和她做个有趣点的自我介绍。"
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = Path(r"C:\Users\Lenovo\Desktop\aihuman\data\workspace\config\chatagent.json")
IO_CFG = Path(r"C:\Users\Lenovo\Desktop\aihuman\data\workspace\state\chat-io-config.json")
sys.path.insert(0, str(ROOT))

MAIN = "conv:9e52b4549f424e730d8871644385bcce63768cc74b8f8ec7ffa895f62ca73eeb"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def reader_get(path: str) -> dict:
    cfg = json.loads(IO_CFG.read_text(encoding="utf-8"))
    req = urllib.request.Request(cfg["reader_base_url"].rstrip("/") + path,
                                 headers={"Authorization": "Bearer " + cfg["reader_token"]})
    with OPENER.open(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def find_person(name: str) -> dict | None:
    target = name.strip().lower()
    # 联系人接口支持 keyword 搜索；全量拉取会被截断在 500 条，所以先搜
    try:
        data = reader_get("/api/v1/contacts?keyword=" + urllib.parse.quote(name.strip()))
        rows = data.get("contacts") or []
        for row in rows:
            if str(row.get("displayName") or "").strip().lower() == target:
                return row
        if rows:
            return rows[0]
    except Exception:  # noqa: BLE001
        pass
    for source in ("/api/v1/sessions?limit=200", "/api/v1/contacts?limit=1000"):
        try:
            data = reader_get(source)
        except Exception:  # noqa: BLE001
            continue
        rows = data.get("sessions") or data.get("contacts") or []
        for row in rows:
            display = str(row.get("displayName") or "").strip().lower()
            if display == target:
                return row
        for row in rows:
            if target and target in str(row.get("displayName") or "").strip().lower():
                return row
    return None


def add_whitelist(conversation_id: str) -> bool:
    backup = CONFIG.with_suffix(f".json.bak-{datetime.now():%Y%m%d%H%M%S}")
    shutil.copy2(CONFIG, backup)
    cfg = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    private = cfg.setdefault("chat", {}).setdefault("whitelist", {}).setdefault("private", [])
    if conversation_id in [str(item) for item in private]:
        print("已经在白名单里了")
        return True
    private.append(str(conversation_id))
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已加入白名单：{conversation_id}（备份 {backup.name}）")
    return True


def ask_her_intro(display_name: str) -> str:
    prompt = (
        f"IM上新加了一个好友，叫「{display_name}」。"
        "你主动跟她打个招呼，介绍下自己是谁、你平时在干嘛。"
        "要有点意思：别背简历，别用『很高兴认识你』这种套话，两三句就够。"
    )
    body = json.dumps({"message": prompt, "user_id": MAIN, "sender": display_name},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request("http://127.0.0.1:8000/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with OPENER.open(req, timeout=180) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return str(data.get("reply") or "").strip()


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--no-intro", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    person = find_person(args.name)
    if not person:
        print(f"没在IM联系人里找到「{args.name}」——可能要等电脑版IM同步一下，"
              "或者对方先给小鱼发一条消息。")
        return 2
    display = person.get("displayName") or args.name
    conversation = str(person.get("conversationId") or "")
    print(f"找到：{display}（wxid={person.get('username')}，会话={conversation}）")
    if args.dry_run:
        return 0
    if conversation:
        add_whitelist(conversation)
    if args.no_intro:
        return 0

    intro = ask_her_intro(display)
    if not intro:
        print("她这次没写出来")
        return 1
    print("她要说的：", intro)
    from chat_send import send_text  # noqa: PLC0415

    result = send_text(display, intro)
    print("发送结果：", result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
