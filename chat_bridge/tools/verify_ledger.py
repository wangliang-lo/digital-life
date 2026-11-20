#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""校验记忆账本：哈希链有没有断、有没有被偷偷改过。

    python tools/verify_ledger.py
    python tools/verify_ledger.py --tail 10     # 顺便看最后几笔

账本在 data/memory_ledger.jsonl，一次写入/删除记一笔，每笔接上一笔的哈希。
思路借自 whale-card（SYNAPSE-1024）的"出生证"：忘掉可以，但要有账可查。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))

from digital_life import MemoryLedger  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--tail", type=int, default=0)
    args = parser.parse_args(argv)

    ledger = MemoryLedger(ROOT / "data" / "memory_ledger.jsonl")
    ok, detail = ledger.verify()
    print(("✅ 账本完好：" if ok else "❌ 账本有问题：") + detail)
    if args.tail:
        try:
            lines = ledger.path.read_text(encoding="utf-8").splitlines()[-args.tail:]
        except OSError:
            lines = []
        for line in lines:
            try:
                item = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            payload = item.get("payload") or {}
            print(f"  {item.get('at')} {item.get('op')} #{item.get('id')} "
                  f"{str(payload.get('summary') or '')[:40]}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
