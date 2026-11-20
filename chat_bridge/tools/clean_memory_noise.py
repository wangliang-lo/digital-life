# -*- coding: utf-8 -*-
"""把旧记忆里混进去的"系统注入"洗掉（[系统：…]、[梗百科：…]、提示尾巴等）。

2026-11-04：归一时发现更早的记忆里存的是"她当时的输入原文"，里面混着
我们塞给她的提示，比如：
    <user_turn>爸爸说：【IM上最近这几条是原文…】 你（我刚才说的）: …
这些不是他的原话，也不是经历。洗掉之后，她想起的才是"当时真正发生了什么"。
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
MAIN = "conv:9e52b4549f424e730d8871644385bcce63768cc74b8f8ec7ffa895f62ca73eeb"
DRY = "--apply" not in sys.argv

NOISE = [
    re.compile(r"\[系统：[^\]]*\]"),
    re.compile(r"\[梗百科：[^\]]*\]"),
    re.compile(r"（想发表情包[^）]*）"),
    re.compile(r"【IM上最近这几条[^】]*】"),
    re.compile(r"【他刚发的】"),
    re.compile(r"【你们刚聊的那段上下文断了[^】]*】"),
    re.compile(r"（这是一次你自己想说话的机会[^）]*）"),
    re.compile(r"\[他发的这条没解析出来[^\]]*\]"),
    re.compile(r"【自由时间】[^\n]*"),
]


def clean(text: str) -> str:
    value = text
    for pattern in NOISE:
        value = pattern.sub(" ", value)
    return re.sub(r"[ \t]{2,}", " ", value).strip()


backup = DB.with_suffix(f".db.bak-{datetime.now():%Y%m%d%H%M%S}")
if not DRY:
    shutil.copy2(DB, backup)

con = sqlite3.connect(DB)
rows = list(con.execute("select id, content from books where user_id like 'conv:9e52%'"))
changed = 0
emptied = 0
for book_id, content in rows:
    text = str(content or "")
    match = re.search(r"<user_turn>(.*?)</user_turn>", text, re.S)
    if not match:
        continue
    inner = match.group(1)
    cleaned = clean(inner)
    if cleaned == inner.strip():
        continue
    if not cleaned:
        emptied += 1
        continue
    new_text = text[:match.start(1)] + cleaned + text[match.end(1):]
    changed += 1
    if not DRY:
        con.execute("update books set content=? where id=?", (new_text, book_id))

if not DRY:
    con.commit()
con.close()

print(f"洗掉系统注入：{changed} 条（其中 {emptied} 条洗完全是空的，先留着没删）")
if DRY:
    print("（预览模式；加 --apply 才真改）")
else:
    print("已写入，备份:", backup.name)
