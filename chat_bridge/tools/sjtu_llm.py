"""交大模型服务（免费额度）的小封装：只给后台任务用，不碰她的主对话。

2026-11-16 爸爸拿到了 SJTU 的 API：
    base  https://models.sjtu.edu.cn/api/v1
    model deepseek-chat / deepseek-reasoner / qwen
    限制：10 次/分钟、30 万 token/分钟、每周 10 亿 token

延迟不稳（实测 8s~25s/百字），所以只用来写日记、做摘要、跑意图识别这类
"不急"的活；她的对话仍走原来的主模型。
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

BASE = os.environ.get("SJTU_BASE_URL", "https://models.sjtu.edu.cn/api/v1").rstrip("/")
KEY_FILE = Path(r"PROJECT_ROOT\chat_bridge\data\sjtu_api_key.txt")
DEFAULT_MODEL = os.environ.get("SJTU_MODEL", "deepseek-chat")
TIMEOUT = float(os.environ.get("SJTU_TIMEOUT", "60"))

_last_call = [0.0]
_min_interval = 6.5  # 10 次/分钟，留点余量


def api_key() -> str:
    key = str(os.environ.get("SJTU_API_KEY") or "").strip()
    if key:
        return key
    try:
        return KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def available() -> bool:
    return bool(api_key())


def chat(prompt: str, *, model: str = "", max_tokens: int = 800,
         temperature: float = 0.8, timeout: float | None = None) -> str:
    key = api_key()
    if not key:
        raise RuntimeError("没有 SJTU key")
    wait = _min_interval - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    body = json.dumps({
        "model": model or DEFAULT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode("utf-8")
    req = urllib.request.Request(
        BASE + "/chat/completions", data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    _last_call[0] = time.time()
    with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return str(data["choices"][0]["message"]["content"] or "").strip()


if __name__ == "__main__":
    print("available:", available())
    print(chat("回四个字：连接正常", max_tokens=20))
