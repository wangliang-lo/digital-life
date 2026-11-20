"""接口形状快照：证明拆 handler 前后对外表现一致。

只比对"结构"（键名、类型、必需字段），不比内容，避免模型输出差异干扰。
"""
from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path

BASE = __import__("os").environ.get("XIAOYU_API_BASE") or "http://127.0.0.1:8000"


def shape(value, depth=0):
    if depth > 3:
        return "…"
    if isinstance(value, dict):
        return {k: shape(v, depth + 1) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [shape(value[0], depth + 1)] if value else []
    if isinstance(value, bool) or value is None:
        return type(value).__name__
    if isinstance(value, (int, float)):
        return "number"
    return "string"


def get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=20) as r:
        return r.status, json.loads(r.read().decode())


def post(path: str, payload: dict):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.status, json.loads(r.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--base", default=None)
    args = ap.parse_args()
    global BASE
    if args.base:
        BASE = args.base

    out = {}
    for path in ("/health", "/status", "/v1/models"):
        try:
            status, data = get(path)
            out[path] = {"status": status, "shape": shape(data)}
        except Exception as exc:  # noqa: BLE001
            out[path] = {"error": f"{type(exc).__name__}: {exc}"}

    try:
        status, data = post("/chat", {"message": "在吗", "user_id": "conv:snapshot", "sender": "snapshot"})
        out["POST /chat"] = {"status": status, "shape": shape(data)}
    except Exception as exc:  # noqa: BLE001
        out["POST /chat"] = {"error": f"{type(exc).__name__}: {exc}"}

    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("接口快照已写:", args.out)
    for k, v in out.items():
        print(" ", k, v.get("status", v.get("error"))[:80] if isinstance(v.get("status", v.get("error")), str) else v.get("status"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
