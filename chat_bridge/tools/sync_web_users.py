"""把公网服务器上的自助注册名单拉到本地（只读缓存）。"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(r"PROJECT_ROOT\chat_bridge")
TARGET = ROOT / "data" / "web_users_public.json"
KEY = Path(r"PROJECT_ROOT\deploy\.tunnel_key")
HOST = "root@YOUR_SERVER_IP"
REMOTE = "/root/xiaoyu/chat_bridge/data/web_users.json"
SCP = r"C:\Windows\System32\OpenSSH\scp.exe"
COMMON = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-i", str(KEY)]


def main() -> int:
    tmp = TARGET.with_suffix(".tmp")
    try:
        proc = subprocess.run(
            [SCP, *COMMON, f"{HOST}:{REMOTE}", str(tmp)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        )
    except subprocess.TimeoutExpired:
        print("同步超时")
        return 1
    if proc.returncode != 0 or not tmp.exists():
        print("同步失败:", (proc.stderr or proc.stdout or "")[:200])
        tmp.unlink(missing_ok=True)
        return 1
    try:
        data = json.loads(tmp.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print("远端文件不是合法 JSON:", exc)
        tmp.unlink(missing_ok=True)
        return 1
    users = data.get("users") if isinstance(data, dict) else {}
    if not isinstance(users, dict):
        users = data if isinstance(data, dict) else {}
    payload = {
        "_synced_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "users": users,
    }
    TARGET.write_text(json.dumps(users, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.unlink(missing_ok=True)
    print(f"已同步 {len(payload['users'])} 个公网账号 -> {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
