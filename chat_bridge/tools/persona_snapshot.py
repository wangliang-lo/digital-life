"""把她的提示词快照下来，用来验证"拆分前后一个字都没变"。

用法：
    python tools/persona_snapshot.py --out snapshot_before.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BRIDGE))
sys.path.insert(0, str(BRIDGE.parent))

import dl_api  # noqa: E402

SCENARIOS = [
    ("owner_web", dict(is_owner=True, channel="web", speaker="爸爸")),
    ("owner_private", dict(is_owner=True, channel="chat_private", speaker="爸爸")),
    ("group", dict(is_owner=False, channel="chat_group", speaker="路人甲", group="测试群")),
    ("guest", dict(is_owner=False, channel="web_guest", speaker="熟人A")),
    ("douyin", dict(is_owner=False, channel="douyin", speaker="熟人B")),
]


def norm(text: str) -> str:
    """把随时间变的部分抹掉，剩下的必须逐字一致。"""
    text = re.sub(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", "<TIME>", text)
    text = re.sub(r"周[一二三四五六日]", "<WEEKDAY>", text)
    text = re.sub(r"已醒 \d+ 小时", "已醒 <N> 小时", text)
    text = re.sub(r"已待 \d+ 分钟", "已待 <N> 分钟", text)
    text = re.sub(r"上次睡了 [\d.]+ 小时", "上次睡了 <N> 小时", text)
    text = re.sub(r"共欠觉 [\d.]+ 小时", "共欠觉 <N> 小时", text)
    text = re.sub(r"褪黑素 \d+/100", "褪黑素 <N>/100", text)
    return text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    snap = {}
    for name, kwargs in SCENARIOS:
        try:
            text = dl_api.persona_with_time("（人格核心）", digest="（记忆摘要）", **kwargs)
        except Exception as exc:  # noqa: BLE001
            text = f"__ERROR__ {type(exc).__name__}: {exc}"
        snap[name] = norm(text)

    Path(args.out).write_text(json.dumps(snap, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("快照已写:", args.out, "| 场景", len(snap), "| 总字数", sum(len(v) for v in snap.values()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
