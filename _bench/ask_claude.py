# -*- coding: utf-8 -*-
"""调 Claude（中转站版）。给小满自己用的命令行入口。

Key 存在仓库外：C:\\Users\\Lenovo\\.claude-relay-key（一行）。
也可以用环境变量 CLAUDE_KEY 覆盖。

接口是 OpenAI 兼容：POST https://xxtf.baby/v1/chat/completions
注意：这台机器的 DNS 走代理软件 TUN 的假 IP（198.18.x.x），
      系统代理是 127.0.0.1:8890，但 urllib 不读系统代理，必须显式指定，
      否则报 Errno 10053 连接被重置。

用法：
    python ask_claude.py "你的问题"
    python ask_claude.py "这段代码有什么问题" --file path.py --lines 100-180
    python ask_claude.py "..." --model claude-opus-5-5 --max-tokens 4000
    python ask_claude.py "..." --system "你是资深 Python 架构师"
    python ask_claude.py "..." --write out.txt
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

URL = "https://xxtf.baby/v1/chat/completions"
KEY_FILE = Path(r"<your-key-file>")
PROXY = os.environ.get("CLAUDE_PROXY", "http://127.0.0.1:8890")
if PROXY.strip().lower() in ("", "none", "off"):
    PROXY = ""
DEFAULT_MODEL = "claude-sonnet-5-5"


def api_key() -> str:
    key = str(os.environ.get("CLAUDE_KEY") or "").strip()
    if key:
        return key
    try:
        return KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def ask(prompt: str, *, model: str = DEFAULT_MODEL, max_tokens: int = 2000,
        system: str = "", temperature: float = 0.3, timeout: float = 300.0) -> str:
    key = api_key()
    if not key:
        raise SystemExit(f"没有 key：写进 {KEY_FILE} 或设环境变量 CLAUDE_KEY")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"https": PROXY, "http": PROXY}))
    # 2026-11-25：代理没开时退回直连（实测直连可用）
    if not PROXY:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # 2026-11-24：这个站的 sonnet 会先"思考"再输出。max_tokens 给小了（比如 200），
    # 预算全在思考里烧完，返回 content: null + finish_reason: length，
    # 看起来像"模型没回话"。实测给到 800 就正常。
    # 所以：预算下限设 1024，被截断且内容为空时自动翻倍重试。
    budget = max(1024, int(max_tokens))
    last_finish = ""
    # 2026-11-27 修：新一代 Claude（opus-5-5 / sonnet-5-5 / opus-4-8 …）
    # **不接受 temperature 参数**，传了就 HTTP 400：
    #   "`temperature` is deprecated for this model"
    # 而管道里传的是 temperature=0.0 → 10 次里 8 次失败、静默退回 DeepSeek，
    # 结果「Claude 的分数」其实是 DeepSeek 的分数。
    # 改成：先带 temperature 试；一旦命中这个 400，就记住该模型不用 temperature。
    _no_temp_models: set[str] = set()
    for attempt in range(3):
        payload = {"model": model, "max_tokens": budget, "messages": messages}
        if model not in _no_temp_models:
            payload["temperature"] = temperature
        req = urllib.request.Request(
            URL, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        )
        try:
            with opener.open(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as _he:
            _body = ""
            try:
                _body = _he.read().decode("utf-8", "ignore")
            except Exception:  # noqa: BLE001
                pass
            if _he.code == 400 and "temperature" in _body and "deprecated" in _body:
                _no_temp_models.add(model)
                continue
            raise
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"返回格式异常: {str(data)[:400]}") from exc
        last_finish = str(choice.get("finish_reason") or "")
        text = str(content).strip() if content else ""
        if text:
            return text
        if last_finish != "length" or attempt == 2:
            raise RuntimeError(f"空回复（finish_reason={last_finish}，max_tokens={budget}）")
        budget *= 2
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="调 Claude 中转站")
    ap.add_argument("prompt")
    ap.add_argument("--file", default="")
    ap.add_argument("--lines", default="")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--max-tokens", type=int, default=2000)
    ap.add_argument("--system", default="")
    ap.add_argument("--temperature", type=float, default=0.3)
    ap.add_argument("--write", default="", help="把回复写到文件")
    ap.add_argument("--quiet", action="store_true", help="只输出内容，不打印耗时")
    args = ap.parse_args()

    prompt = args.prompt
    if args.file:
        path = Path(args.file)
        if not path.exists():
            print("文件不存在：", path)
            return 2
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start, end = 1, len(lines)
        if args.lines and "-" in args.lines:
            a, b = args.lines.split("-", 1)
            start, end = int(a), int(b)
        snippet = "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, min(end, len(lines)) + 1))
        prompt = (f"下面是 {path.name} 第 {start}-{end} 行：\n```\n{snippet[:60000]}\n```\n\n"
                  f"{args.prompt}")

    t0 = time.perf_counter()
    try:
        text = ask(prompt, model=args.model, max_tokens=args.max_tokens,
                   system=args.system, temperature=args.temperature)
    except Exception as exc:  # noqa: BLE001
        print(f"调用失败：{type(exc).__name__}: {exc}")
        detail = getattr(exc, "read", None)
        if detail:
            try:
                print(detail().decode()[:400])
            except Exception:  # noqa: BLE001
                pass
        return 1
    ms = (time.perf_counter() - t0) * 1000

    if args.write:
        Path(args.write).write_text(text, encoding="utf-8")
        if not args.quiet:
            print(f"[{args.model} {ms:.0f}ms] 已写入 {args.write}")
    else:
        print(text)
        if not args.quiet:
            print(f"\n[{args.model} {ms:.0f}ms]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
