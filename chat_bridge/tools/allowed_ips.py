"""管理网页访问白名单（2026-11-15）。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

FILE = Path(r"PROJECT_ROOT\chat_bridge\data\allowed_ips.txt")
BLOCKED = Path(r"PROJECT_ROOT\chat_bridge\data\web_blocked.jsonl")


def read_entries() -> list[str]:
    if not FILE.exists():
        return []
    return [ln.strip() for ln in FILE.read_text(encoding="utf-8-sig").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def write_entries(entries: list[str]) -> None:
    header = "# 允许访问网页的 IP / 网段，一行一个；留空=全部放行；改完不用重启\n"
    FILE.write_text(header + "\n".join(entries) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="网页访问白名单")
    parser.add_argument("action", choices=["list", "add", "remove", "blocked"])
    parser.add_argument("value", nargs="?", default="")
    args = parser.parse_args()

    entries = read_entries()
    if args.action == "list":
        print("白名单（空=全部放行）：")
        for item in entries:
            print(" ", item)
        return 0
    if args.action == "blocked":
        print("最近被挡的 IP：")
        if not BLOCKED.exists():
            print("  （还没有）")
            return 0
        for line in BLOCKED.read_text(encoding="utf-8").splitlines()[-15:]:
            try:
                row = json.loads(line)
            except Exception:
                continue
            print(" ", row.get("at"), row.get("ip"), row.get("path"))
        return 0
    if not args.value:
        print("要给一个 IP 或网段，例如 58.247.22.212 或 192.168.1.0/24")
        return 2
    if args.action == "add":
        if args.value not in entries:
            entries.append(args.value)
            write_entries(entries)
            print("已加：", args.value)
        else:
            print("已在名单里")
        return 0
    if args.action == "remove":
        if args.value in entries:
            entries.remove(args.value)
            write_entries(entries)
            print("已删：", args.value)
        else:
            print("不在名单里")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
