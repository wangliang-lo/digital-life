#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""让小鱼从自己的聊天记录里学习「说话习惯」。

做法：定期抽样最近的对话（traces 表），让模型点评「哪里像客服/AI」，
提炼成最多 10 条可执行的说话规则，写进 data/style_profile.md。
dl_api 每次回复时会把这几条拼进人格，规则越具体，她越像人。

    python tools/style_trainer.py --once          # 现在训练一轮
    python tools/style_trainer.py --watch 21600   # 每 6 小时看看有没有新料

不会读取别人的私聊，只分析她自己的对话记录；不保存原文到磁盘。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))

from digital_life import chat_with_llm  # noqa: E402

DEFAULT_DB = ROOT / "data" / "xiaoyu.db"
DEFAULT_PROFILE = ROOT / "data" / "style_profile.md"
STATE_FILE = ROOT / "data" / ".style_trainer_state.json"

# 先给一份「人类聊天常识」，训练出来的规则会追加在后面
SEED_RULES = [
    "每条消息不超过 20 个字，像真人一样分几条发",
    "不要每条都问问题，也别连续追问",
    "不要总结对方的话，不要复述",
    "不说「理解你」「慢慢来」「希望对你有帮助」",
    "偶尔只回「嗯」「啊？」「真的假的」",
    "有情绪就直说，不用铺垫和解释",
    "不主动介绍自己是 AI 或助手",
    "可以用语气词和口头禅，但别每条都用",
]

PROMPT = """你在帮我调教一个IM聊天机器人（她叫小鱼，傲娇、嘴硬心软）。
下面是最近的聊天记录（用户 -> 她的回复）。请指出她哪里像客服/AI，然后总结可执行的说话习惯。

要求：
- 只输出 JSON 数组，6 条以内，每项一句话，不超过 30 个字；
- 要具体可执行（比如「别用分号列点」「少用感叹号」），不要「更自然一点」这种空话；
- 不要复述聊天内容，不要提到隐私细节；
- 不要和已有规则重复。

已有规则：
{existing}

最近的聊天：
{samples}
"""


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def load_rules(path: Path) -> list[str]:
    if not path.exists():
        return list(SEED_RULES)
    rules = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("- "):
            rules.append(line[2:].strip())
    return rules or list(SEED_RULES)


def save_rules(path: Path, rules: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "# 小鱼的说话习惯（由 style_trainer 自动维护）\n\n"
    body += "\n".join(f"- {rule}" for rule in rules)
    path.write_text(body + "\n", encoding="utf-8")


def read_samples(db_path: Path, limit: int) -> tuple[list[tuple[str, str]], int]:
    import sqlite3

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT id, user_text, reply FROM traces WHERE reply != '' ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        con.close()
    samples = [(str(r["user_text"])[:120], str(r["reply"]).replace("|||", "\n")[:160]) for r in rows]
    last_id = int(rows[0]["id"]) if rows else 0
    return samples, last_id


def parse_rules(text: str) -> list[str]:
    text = text.strip()
    match = re.search(r"\[[\s\S]*\]", text)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, list):
                return [str(item).strip() for item in data if str(item).strip()]
        except ValueError:
            pass
    rules = []
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d+[.、)])\s*", "", line).strip()
        if 4 <= len(line) <= 40:
            rules.append(line)
    return rules


def normalize(rule: str) -> str:
    return re.sub(r"[\s，。！？、,.!?；;：:]+", "", rule).casefold()


def train_once(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    profile = Path(args.profile)
    state = load_state()
    samples, last_id = read_samples(db_path, args.samples)

    if len(samples) < args.min_samples:
        print(f"样本不足（{len(samples)} < {args.min_samples}），先不训练")
        return 0
    if last_id <= int(state.get("last_trace_id", 0)) and not args.force:
        print("没有新的对话，跳过")
        return 0

    existing = load_rules(profile)
    sample_text = "\n".join(f"用户：{u}\n小鱼：{r}" for u, r in samples[: args.samples])
    prompt = PROMPT.format(existing="\n".join(f"- {r}" for r in existing), samples=sample_text)

    try:
        text = chat_with_llm(
            [{"role": "user", "content": prompt}],
            temperature=0.4,
            timeout=90.0,
        )
    except Exception as exc:  # noqa: BLE001
        print("训练失败（模型不可用？）：", exc)
        return 1

    new_rules = [r for r in parse_rules(text) if 4 <= len(r) <= 40]
    merged, seen = [], set()
    for rule in existing + new_rules:
        key = normalize(rule)
        if key and key not in seen:
            seen.add(key)
            merged.append(rule)
    merged = merged[: args.max_rules]
    save_rules(profile, merged)

    state.update(
        {
            "last_trace_id": last_id,
            "last_run": datetime.now().isoformat(timespec="seconds"),
            "rules": len(merged),
        }
    )
    save_state(state)
    print(f"训练完成：新增 {len(new_rules)} 条，当前 {len(merged)} 条 → {profile}")
    for rule in merged:
        print("  -", rule)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="小鱼说话风格训练")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--profile", default=str(DEFAULT_PROFILE))
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument("--min-samples", type=int, default=12)
    parser.add_argument("--max-rules", type=int, default=10)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--watch", type=int, default=0, help="每 N 秒检查一次（0=只跑一次）")
    args = parser.parse_args()

    key = os.environ.get("DEEPSEEK_API_KEY") or ""
    if not key:
        try:
            cred = Path.home() / ".dsh" / ".credentials.yaml"
            match = re.search(r"DEEPSEEK_API_KEY:\s*['\"]?([^'\"\s]+)", cred.read_text(encoding="utf-8"))
            if match:
                os.environ["DEEPSEEK_API_KEY"] = match.group(1)
        except OSError:
            pass

    if args.watch <= 0:
        return train_once(args)

    print(f"每 {args.watch}s 检查一次，Ctrl+C 退出")
    while True:
        try:
            train_once(args)
        except Exception as exc:  # noqa: BLE001
            print("训练异常：", exc)
        time.sleep(max(60, args.watch))


if __name__ == "__main__":
    raise SystemExit(main())
