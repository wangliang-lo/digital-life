#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""给每个好友慢慢攒一张小卡片：外号 + 一句话印象。

    python tools/profile_builder.py            # 常驻，每 30 分钟看一次
    python tools/profile_builder.py --once

数据来源：state/chat-events.jsonl 里对方真实说过的话；用本地 Ollama 生成，不花 API token。
积累够 8 条新消息才重写，避免拿几句话就给人贴夸张标签。
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = Path.home() / ".dsh" / "profiles" / "desktop" / "node_modules" / "dsh-douyin-browser-local" / "state"
EVENTS = STATE / "chat-events.jsonl"
OUT = ROOT / "data" / "contact_profiles.json"
OLLAMA = "http://127.0.0.1:11434/api/generate"
MODEL = "qwen2.5:14b"
MIN_MESSAGES = 12
MIN_NEW_MESSAGES = 8


def load_profiles() -> dict:
    try:
        return json.loads(OUT.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_profiles(data: dict) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def collect() -> dict[str, dict]:
    """按联系人收集最近的 incoming 文本。"""
    contacts: dict[str, dict] = {}
    try:
        lines = EVENTS.read_text(encoding="utf-8").splitlines()
    except OSError:
        return contacts
    for line in lines[-4000:]:
        try:
            row = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        rid = row.get("recipientId")
        if not rid:
            continue
        item = contacts.setdefault(rid, {"texts": [], "name": ""})
        for msg in row.get("messages") or []:
            text = str(msg.get("text") or "").strip()
            if text:
                item["texts"].append(text)
        # 只看对方说的话，不把自己的回复混进去，画像才准
    return contacts


def ask(prompt: str) -> str:
    body = json.dumps({"model": MODEL, "prompt": prompt, "stream": False, "options": {"num_predict": 120}}).encode()
    req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return str(json.loads(resp.read().decode()).get("response") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def build_one(name: str, texts: list[str]) -> str:
    sample = "\n".join(texts[-14:])[:900]
    prompt = (
        "下面只有这个人在抖音私信里说过的话，不含我的回复。"
        "请用一句中文概括他/她平时的聊天风格和爱聊什么，再给一个不带贬义的小外号。"
        "不要判断性别、年龄、外貌、职业、人品，也不要从几句话推断心理问题或私生活。"
        "证据不够就写“爱聊日常”。只输出「外号：X；印象：Y」，外号不超过6字，印象不超过30字。\n\n"
        f"{sample}"
    )
    raw = ask(prompt)
    raw = re.sub(r"[\r\n]+", " ", raw).strip()
    # 模型有时一口气给好几个备选，只要第一个
    if raw.count("外号：") > 1:
        parts = raw.split("外号：")
        raw = "外号：" + parts[1]
    return raw[:70]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=1800)
    args = parser.parse_args()

    while True:
        profiles = load_profiles()
        try:
            names = {item["id"]: item.get("name") or "" for item in json.loads((STATE / "chat-policy.json").read_text(encoding="utf-8")).get("allowlist") or []}
        except Exception:
            names = {}
        contacts = collect()
        changed = 0
        for cid, item in contacts.items():
            texts = item["texts"]
            if len(texts) < MIN_MESSAGES:
                continue
            old = profiles.get(cid) or {}
            old_count = int(old.get("message_count") or 0)
            if old.get("updated_at") and len(texts) - old_count < MIN_NEW_MESSAGES:
                continue
            display = names.get(cid) or old.get("name") or ""
            card = build_one(display or cid[-6:], texts)
            if not card:
                continue
            profiles[cid] = {
                "name": display,
                "card": card,
                "message_count": len(texts),
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            changed += 1
        if changed:
            save_profiles(profiles)
            print(f"更新了 {changed} 张人物卡片", flush=True)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
