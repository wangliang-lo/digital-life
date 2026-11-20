# -*- coding: utf-8 -*-
"""把"整段历史被当成一句话"的旧记忆收成"最后那一句"。

2026-11-04：早期的桥把插件给的"近期聊天原文"整段当成用户消息，于是记忆里
一条就塞了十几轮对话，前面还挂着"爸爸说："。这里只保留最后那个发言人那句，
恢复成"一次对话一条记忆"。
"""
import re
import shutil
import sqlite3
import sys
import io
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

DB = Path(r"PROJECT_ROOT\chat_bridge\data\xiaoyu.db")
DRY = "--apply" not in sys.argv
SPEAKER = re.compile(r"(?:^|\s)((?:你（我刚才说的）|爸爸|阿哦|熟人B|爸爸|你)\s*[:：]\s*)")

backup = DB.with_suffix(f".db.bak-{datetime.now():%Y%m%d%H%M%S}")
if not DRY:
    shutil.copy2(DB, backup)

con = sqlite3.connect(DB)
rows = list(con.execute("select id, folder_user, content from books where user_id like 'conv:9e52%'"))
fixed = 0
for book_id, folder_user, content in rows:
    text = str(content or "")
    match = re.search(r"<user_turn>(.*?)</user_turn>", text, re.S)
    if not match:
        continue
    inner = match.group(1).strip()
    hits = list(SPEAKER.finditer(inner))
    if len(hits) < 2:
        continue
    tail = inner[hits[-1].end():].strip()
    if not tail:
        continue
    who_prefix = "爸爸说：" if "爸爸说：" in inner[:20] else ""
    new_inner = f"{who_prefix}{tail}"[:600]
    new_text = text[:match.start(1)] + new_inner + text[match.end(1):]
    fixed += 1
    if not DRY:
        con.execute("update books set content=? where id=?", (new_text, book_id))

if not DRY:
    con.commit()
con.close()
print(f"收成最后一句：{fixed} 条" + ("（预览）" if DRY else f"，备份 {backup.name}"))
