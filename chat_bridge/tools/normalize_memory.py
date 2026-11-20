# -*- coding: utf-8 -*-
"""记忆归属归一：把 folder_user 的六种写法统一，并给旧记忆补上"谁说的"。

2026-11-04 爸爸同意："问题二我同意了。"
背景：977 条记忆里 789 条没写说话人，folder_user 有 father / 爸爸 / 爸爸 /
conv:9e52… / 你 / solver 六种写法——她分不清"谁跟我说的"。
"""
import shutil
import sqlite3
import sys
import io
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

DB = Path(r"PROJECT_ROOT\chat_bridge\data\xiaoyu.db")
MAIN = "conv:9e52b4549f424e730d8871644385bcce63768cc74b8f8ec7ffa895f62ca73eeb"
ALIASES = {
    "father": "爸爸",
    "爸爸": "爸爸",
    "爸爸": "爸爸",
    "你": "爸爸",
    MAIN: "爸爸",
    "solver": "SOLVER",
    "solver-channel": "SOLVER",
}
DRY = "--apply" not in sys.argv

backup = DB.with_suffix(f".db.bak-{datetime.now():%Y%m%d%H%M%S}")
if not DRY:
    shutil.copy2(DB, backup)

con = sqlite3.connect(DB)
rows = list(con.execute(
    "select id, folder_user, content from books where user_id like 'conv:9e52%'"))
print(f"她的记忆 {len(rows)} 条，模式：{'预览' if DRY else '写入'}")

renamed = 0
attributed = 0
for book_id, folder_user, content in rows:
    who = ALIASES.get(str(folder_user or "").strip())
    text = str(content or "")
    new_who = who or folder_user
    new_text = text
    if who and "<user_turn>" in text:
        # 只在还没写说话人的那些上加前缀
        head, _, tail = text.partition("<user_turn>")
        if not tail.lstrip().startswith(f"{who}说"):
            new_text = head + "<user_turn>" + f"{who}说：" + tail.lstrip()
            attributed += 1
    if str(new_who) != str(folder_user):
        renamed += 1
    if not DRY and (new_text != text or str(new_who) != str(folder_user)):
        con.execute("update books set folder_user=?, content=? where id=?",
                    (new_who, new_text, book_id))

if not DRY:
    con.commit()
con.close()

print(f"归一 folder_user：{renamed} 条")
print(f"补说话人前缀：{attributed} 条")
if DRY:
    print("（预览模式，没有写库；加 --apply 才真改）")
else:
    print("已写入，备份:", backup.name)
