#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""研究者日志：每次改她的行为，先写下"为什么改、预测会怎样"，过几天再看"实际怎样"。

    python tools/research_log.py add --what "把频道提示改成同一句只贴一次" \
        --why "她会觉得像被自动口子塞话" \
        --predict "频道留言变多、沉默变少" \
        --watch "silent 次数 / 留言条数"

    python tools/research_log.py list            # 最近 10 条
    python tools/research_log.py pending         # 到期该复盘了
    python tools/research_log.py digest          # 自动附上"实际怎样"的数字

设计原则（2026-11-05 定）：
  * 只记事实，不做判断；"实际怎样"全是可数的数字，不是感想
  * 一周后自动提醒复盘，避免"改完就忘"
  * 一条改动一个变量；工程修复可以合并记一条
"""
from __future__ import annotations

import argparse
import io
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent      # chat_bridge
LOG = ROOT / "data" / "research_log.jsonl"
DB = ROOT / "data" / "xiaoyu.db"
CHANNEL = ROOT / "data" / "solver_channel.jsonl"
EVAL_HISTORY = ROOT / "data" / "eval_history"
REVIEW_AFTER_DAYS = 7


def _rows() -> list[dict]:
    if not LOG.exists():
        return []
    out = []
    for line in LOG.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _append(item: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, ensure_ascii=False) + "\n")


def _since(entry: dict) -> str:
    return str(entry.get("at") or "")[:10]


def digest_for(entry: dict) -> dict:
    """这条改动之后，她的几项可数指标（和改动前一周比）。"""
    changed = _since(entry)
    try:
        end = date.fromisoformat(changed) + timedelta(days=REVIEW_AFTER_DAYS)
        before = date.fromisoformat(changed) - timedelta(days=REVIEW_AFTER_DAYS)
    except ValueError:
        return {}
    result: dict = {}

    # 1) 对话轮数与平均回复长度
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            for label, lo, hi in (("before", before.isoformat(), changed),
                                  ("after", changed, end.isoformat())):
                row = con.execute(
                    "SELECT COUNT(*), AVG(LENGTH(reply)) FROM traces "
                    "WHERE substr(created_at,1,10) >= ? AND substr(created_at,1,10) < ?",
                    (lo, hi)).fetchone()
                result[f"turns_{label}"] = row[0] or 0
                result[f"avg_reply_chars_{label}"] = round(row[1] or 0, 1)
        finally:
            con.close()
    except sqlite3.OperationalError:
        pass

    # 2) 频道：留言 vs 沉默
    if CHANNEL.exists():
        counts = {"before": {"message": 0, "silent": 0}, "after": {"message": 0, "silent": 0}}
        for line in CHANNEL.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            day = str(item.get("at") or "")[:10]
            bucket = None
            if before.isoformat() <= day < changed:
                bucket = "before"
            elif changed <= day <= end.isoformat():
                bucket = "after"
            if not bucket:
                continue
            kind = item.get("type")
            if kind in ("message", "silent"):
                counts[bucket][kind] += 1
        result["channel_before"] = counts["before"]
        result["channel_after"] = counts["after"]

    # 3) 评估通过率（取这条改动前后最近一次存档）
    if EVAL_HISTORY.exists():
        runs = sorted(EVAL_HISTORY.glob("*.json"))
        before_run = after_run = None
        for path in runs:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            at = str(data.get("at") or "")[:10]
            if at < changed:
                before_run = data
            elif after_run is None:
                after_run = data
        for label, run in (("before", before_run), ("after", after_run)):
            rows = (run or {}).get("results") or []
            if rows:
                result[f"eval_{label}"] = f"{sum(1 for r in rows if r.get('ok'))}/{len(rows)}"
    return result


def cmd_add(args: argparse.Namespace) -> int:
    item = {
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "what": args.what,
        "why": args.why,
        "predict": args.predict,
        "watch": args.watch,
        "batch": args.batch,
        "kind": args.kind,
    }
    _append(item)
    print("已记下：", item["what"][:60])
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    rows = _rows()[-args.limit:]
    if not rows:
        print("还没有研究日志")
        return 0
    for item in rows:
        at = str(item.get("at") or "")[:16].replace("T", " ")
        mark = "已复盘" if item.get("reviewed_at") else "待复盘"
        print(f"[{at}] {item.get('kind') or '行为'}｜{mark}")
        print(f"   改了什么：{item.get('what')}")
        print(f"   为什么：{item.get('why')}")
        print(f"   预测：{item.get('predict')}")
        print(f"   观察：{item.get('watch')}")
        if item.get("digest"):
            print(f"   实际：{json.dumps(item['digest'], ensure_ascii=False)}")
        print()
    return 0


def cmd_pending(args: argparse.Namespace) -> int:
    today = date.today()
    due = []
    for item in _rows():
        if item.get("reviewed_at"):
            continue
        try:
            at = date.fromisoformat(_since(item))
        except ValueError:
            continue
        if (today - at).days >= REVIEW_AFTER_DAYS:
            due.append(item)
    if not due:
        print(f"没有到期的复盘（满 {REVIEW_AFTER_DAYS} 天才提醒）")
        return 0
    for item in due:
        print(f"该复盘了：{item.get('what')[:60]}（{_since(item)}）")
    print("\n跑 `python tools/research_log.py digest` 附上实际数字")
    return 0


def cmd_digest(args: argparse.Namespace) -> int:
    rows = _rows()
    changed = False
    for item in rows:
        if item.get("reviewed_at") or item.get("digest"):
            continue
        try:
            at = date.fromisoformat(_since(item))
        except ValueError:
            continue
        if (date.today() - at).days < REVIEW_AFTER_DAYS:
            continue
        item["digest"] = digest_for(item)
        item["reviewed_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        changed = True
        print(f"复盘：{item.get('what')[:50]}")
        print("   实际：", json.dumps(item["digest"], ensure_ascii=False))
    if changed:
        LOG.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                       encoding="utf-8")
    else:
        print("没有需要附数字的条目")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add")
    add.add_argument("--what", required=True)
    add.add_argument("--why", required=True)
    add.add_argument("--predict", required=True)
    add.add_argument("--watch", required=True)
    add.add_argument("--batch", default="")
    add.add_argument("--kind", default="行为", choices=["行为", "工程", "人格", "观测"])
    add.set_defaults(func=cmd_add)

    listing = sub.add_parser("list")
    listing.add_argument("--limit", type=int, default=10)
    listing.set_defaults(func=cmd_list)

    pending = sub.add_parser("pending")
    pending.set_defaults(func=cmd_pending)

    digest = sub.add_parser("digest")
    digest.set_defaults(func=cmd_digest)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
