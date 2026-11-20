#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""程序性记忆：她"会做一件事"的知识，和做砸过的原因。

2026-11-04 从 whale-card 借的第四点（它管这个叫 procedural memory）：
她现在"怎么操作"只活在三处——提示词里的标签说明、tools/*.mjs 的步骤、
还有我临时改的代码。今天抖音那个坑（要读会话头部名字才算切对人）就是
典型的"操作知识只活在代码里，她不知道，也不会积累"。

这里记两类东西：
  能力  她做过什么、成功几次
  教训  上次为什么失败（下次做之前先提醒一句）

    from skills import record, block
    record("抖音发消息", True)
    record("抖音发消息", False, "切错会话：当前停在熟人B")
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATH = ROOT / "data" / "skills.json"

# 做这件事的要点（写死在代码里，因为这是"系统事实"，不是学来的）
STEPS = {
    "抖音发消息": "先确认会话头部显示的是对的人，再点表情/输入框；切不到就报错，不能发到当前打开的会话",
    "抖音搜索": "搜索页偶尔会弹验证码，看不到结果就照实说，别猜",
    "抖音刷一会儿": "给自己定个时长，到点就收起来",
    "IM发消息": "用 [IM_SEND: 名字 | 内容]，一次一条",
    "发表情包": "写 [STICKER: 关键词]，从库里挑带标签的；截图和视频画面不算表情",
    "看书": "按顺序读下一章，别跳；换书用 [BOOK_SWITCH: 书名]，进度会留着",
}


def _load() -> dict:
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def record(name: str, ok: bool, error: str = "") -> None:
    name = str(name or "").strip()
    if not name:
        return
    data = _load()
    item = data.setdefault(name, {"ok": 0, "fail": 0, "last_error": "", "last_at": ""})
    if ok:
        item["ok"] = int(item.get("ok") or 0) + 1
        item["last_error"] = ""
    else:
        item["fail"] = int(item.get("fail") or 0) + 1
        item["last_error"] = str(error or "")[:160]
    item["last_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    data[name] = item
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def block(limit: int = 5) -> str:
    """给她看的"操作要点"：只在她做砸过的事情上提醒，别把清单塞满。"""
    data = _load()
    lines = []
    for name, item in sorted(data.items(), key=lambda kv: -int(kv[1].get("fail") or 0)):
        fail = int(item.get("fail") or 0)
        ok = int(item.get("ok") or 0)
        if fail and item.get("last_error"):
            # 2026-11-05：以前把原始报错原样塞给她（"browserType.connectOverCDP: connect
            # ECONNREFUSED 127.0.0.1:9227"），她看这个没有用。现在只留一句人话。
            err = str(item["last_error"])
            if "ECONNREFUSED" in err or "connect" in err.lower():
                err = "连不上专用浏览器（没开或者掉了）"
            elif "timeout" in err.lower() or "超时" in err:
                err = "超时了"
            elif "会话" in err or "conv" in err.lower():
                err = "切错了会话"
            lines.append(f"{name}：上次没成（{err[:24]}）。" + (STEPS.get(name, "")))
        elif ok >= 2 and fail == 0:
            continue
        if len(lines) >= limit:
            break
    if not lines:
        return ""
    return "\n【你在这几件事上踩过坑，做之前先照要点来】\n- " + "\n- ".join(lines)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    data = _load()
    if not data:
        print("还没有记录")
        return 0
    for name, item in data.items():
        print(f"{name}: ok={item.get('ok')} fail={item.get('fail')} last_error={item.get('last_error') or '-'}")
    print()
    print(block() or "(暂时没有要提醒的)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
