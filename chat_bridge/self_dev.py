#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""让她自己改 chat_bridge 里的代码：模型出补丁 → 备份 → 落盘 → 语法检查 → 失败回滚。

用法（由 dl_api 在识别到修改请求时调用）：
    from self_dev import handle_dev_request
    ok, detail = handle_dev_request("把网页标题改成她", llm_call)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent
BACKUP = ROOT / "data" / "backups"
ALLOWED_SUFFIX = {".py", ".mjs", ".cjs", ".js", ".html", ".css", ".json", ".md", ".ps1", ".cmd"}
SKIP_DIRS = {"data", "logs", "stickers", "node_modules", "__pycache__", ".git", "web_src", "bridge", "legacy", "archive", "tmp", "backup", "backups"}

# ---------------------------------------------------------------- 禁区
# 2026-11-05：原来只有目录级 SKIP_DIRS，没有文件级禁区。
# 自我进化可以，自我瓦解不行——这几个地方她不许改：
PROTECTED_FILES = {
    "data/core.json",            # 骨头层：她的身份锚
    "self_dev.py",               # 改自己改代码的能力 = 无限自举
    "tools/system_events.py",    # 她对自己身世的认知来源
    "ENDOCRINE.py",              # 七条轴的耦合表：改了她会"情绪不对但谁也说不清"
}
# 文件内的受保护代码块：用首尾标记圈起来，补丁只要碰到其中一行就拒绝
PROTECTED_BLOCKS = {
    "config.py": ("# ==== PROTECTED: CORE_CONSTITUTION ====", "# ==== /PROTECTED ===="),
    "digital_life.py": ("# ==== PROTECTED: MEMORY_WRITE ====", "# ==== /PROTECTED ===="),
}

DEV_HINTS = re.compile(r"改代码|修.*bug|修复|改一下|改造|加个功能|加一个功能|优化一下|调整.*逻辑|写个脚本|改脚本|把.*改成|帮我看.*代码")
NOT_DEV = re.compile(r"改说?话|改口|心情|兴趣|日记|日程|表情包|睡|吃什么|在哪")


def detect(text: str) -> bool:
    return bool(DEV_HINTS.search(text)) and not bool(NOT_DEV.search(text))


EVOLUTION = ROOT / "data" / "self_evolution.jsonl"
PROPOSALS = ROOT / "data" / "self_proposals.jsonl"


def log_evolution(event: dict[str, Any]) -> None:
    """一次自我修改的全生命周期：欲望 → 提案 → 结果 → 事后评价。"""
    try:
        EVOLUTION.parent.mkdir(parents=True, exist_ok=True)
        row = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), **event}
        with EVOLUTION.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _protected_reason(rel: str, find: str, replace: str) -> str:
    """这段改动是不是动了禁区。返回原因，空字符串表示放行。"""
    clean = rel.replace("\\", "/").lstrip("./")
    name = Path(clean).name
    if clean in PROTECTED_FILES or name in PROTECTED_FILES:
        return f"{clean} 是受保护文件（她的身份/记忆/身世来源）"
    bounds = PROTECTED_BLOCKS.get(Path(clean).name)
    if bounds:
        start, end = bounds
        # 2026-11-05：config.py / digital_life.py 在项目根上，不在 bridge 目录里，
        # 只查 ROOT 会导致"受保护块永远找不到"，等于没保护。
        candidates = [ROOT / clean, ROOT.parent / clean]
        content = ""
        for candidate in candidates:
            if candidate.exists():
                try:
                    content = candidate.read_text(encoding="utf-8", errors="replace")
                    break
                except OSError:
                    continue
        if not content:
            return ""
        begin = content.find(start)
        finish = content.find(end, begin + 1)
        if begin >= 0 and finish > begin:
            block = content[begin:finish + len(end)]
            for needle in (find, replace):
                if needle and (needle in block or block in needle):
                    return f"碰了 {clean} 里受保护的那一段"
    return ""


def _file_map(limit: int = 60) -> str:
    items = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in ALLOWED_SUFFIX:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > 400_000:
            continue
        items.append(f"{path.relative_to(ROOT)} ({size}B)")
        if len(items) >= limit:
            break
    return "\n".join(items)


def make_llm_call() -> Callable[[str], str]:
    """用当前配置的 OpenAI 兼容接口（DeepSeek 或本地 Ollama）做一次补丁生成。"""
    import urllib.request

    base = (os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")
    model = os.environ.get("DEEPSEEK_MODEL") or "deepseek-chat"
    key = os.environ.get("DEEPSEEK_API_KEY") or ""

    def call(prompt: str) -> str:
        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 1800,
        }).encode()
        req = urllib.request.Request(
            f"{base}/chat/completions", data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode())
        return str((data.get("choices") or [{}])[0].get("message", {}).get("content") or "")

    return call


def _ask(llm_call: Callable[[str], str], prompt: str, max_tokens: int = 1600) -> str:
    try:
        return llm_call(prompt)
    except Exception as exc:  # noqa: BLE001
        return f"__ERROR__ {exc}"


def _syntax_ok(path: Path) -> tuple[bool, str]:
    try:
        if path.suffix == ".py":
            proc = subprocess.run([sys.executable, "-m", "py_compile", str(path)], capture_output=True, timeout=60)
        elif path.suffix in {".mjs", ".cjs", ".js"}:
            proc = subprocess.run(["node", "--check", str(path)], capture_output=True, timeout=60)
        else:
            return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    if proc.returncode == 0:
        return True, ""
    return False, (proc.stderr or proc.stdout).decode("utf-8", "replace")[:400]


def queue_request(text: str, reason: str = "") -> None:
    """她自己改不了时，把请求记到开发队列，等开发窗口（DSH 里的我）来处理。"""
    path = ROOT / "data" / "dev_requests.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "request": text[:1000], "reason": reason[:300], "status": "pending"}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def propose(text: str, llm_call: Callable[[str], str]) -> dict[str, Any]:
    """从她"想变得不一样"里生成一个提案，但**不动任何文件**。

    2026-11-05：主动通道只到提案为止。落盘要她再确认一次（或者我来确认），
    否则她一句"我想少说点话"就能把自己的提示词改掉。
    """
    prompt = (
        "你在维护一个 Python/JS 项目，根目录是 C:\\aihuman\\chat_bridge。\n"
        "下面是小鱼自己说的一段话，她可能想改点什么：\n" + text + "\n\n"
        "如果她的话里确实有一个可以落成代码的改动，给出方案；如果只是情绪或想法，就返回空。\n"
        "输出严格 JSON，不要解释：\n"
        '{"file":"相对路径","find":"文件里原样存在的片段","replace":"替换后的片段","reason":"一句话"}\n'
        "判断不了就输出 {\"file\":\"\"}。\n可用文件：\n" + _file_map(40)
    )
    raw = _ask(llm_call, prompt)
    plan: dict[str, Any] = {"file": "", "find": "", "replace": "", "reason": ""}
    match = re.search(r"\{.*\}", raw or "", re.S)
    if match:
        try:
            plan = json.loads(match.group(0))
        except Exception:  # noqa: BLE001
            plan = {"file": ""}
    rel = str(plan.get("file") or "").strip()
    blocked = _protected_reason(rel, str(plan.get("find") or ""), str(plan.get("replace") or "")) if rel else ""
    row = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "desire": text[:600],
        "file": rel,
        "find": str(plan.get("find") or "")[:500],
        "replace": str(plan.get("replace") or "")[:500],
        "reason": str(plan.get("reason") or "")[:200],
        "blocked": blocked,
        "status": "proposed" if rel and not blocked else ("blocked" if blocked else "no_code"),
    }
    try:
        PROPOSALS.parent.mkdir(parents=True, exist_ok=True)
        with PROPOSALS.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass
    log_evolution({"kind": "proposal", **row})
    return row


def handle_dev_request(text: str, llm_call: Callable[[str], str], restart_api: Callable[[], None] | None = None) -> tuple[bool, str]:
    prompt = (
        "你在维护一个 Python/JS 项目，根目录是 C:\\aihuman\\chat_bridge。\n"
        "用户的要求：" + text + "\n\n"
        "只能改一个文件，必须给出精确的原文片段和替换片段。\n"
        "输出严格 JSON，不要解释：\n"
        '{"file":"相对路径","find":"文件里原样存在的片段","replace":"替换后的片段","reason":"一句话"}\n'
        "如果这个请求不该改代码，输出 {\"file\":\"\"}。\n"
        "可用文件：\n" + _file_map()
    )
    raw = _ask(llm_call, prompt)
    if raw.startswith("__ERROR__"):
        return False, "模型调用失败：" + raw[9:].strip()
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return False, "模型没给出可用的修改方案。"
    try:
        plan = json.loads(match.group(0))
    except Exception:  # noqa: BLE001
        return False, "模型返回的 JSON 解析失败。"
    rel = str(plan.get("file") or "").strip()
    if not rel:
        return False, "判断不需要改代码。"
    target = (ROOT / rel).resolve()
    if not str(target).startswith(str(ROOT.resolve())) or target.suffix.lower() not in ALLOWED_SUFFIX:
        return False, f"不允许改这个文件：{rel}"
    if not target.exists():
        return False, f"文件不存在：{rel}"
    find = str(plan.get("find") or "")
    replace = str(plan.get("replace") or "")
    content = target.read_text(encoding="utf-8", errors="replace")
    blocked = _protected_reason(rel, find, replace)
    if blocked:
        log_evolution({"kind": "blocked", "request": text[:500], "file": rel, "reason": blocked})
        return False, f"这块我不能动：{blocked}"
    if not find or find not in content:
        # 第一轮没给准：把文件内容给它看，再要一次原文片段
        second = (
            "下面是要修改的文件内容（可能被截断），请给出精确的原文片段和替换片段。\n"
            "用户要求：" + text + "\n\n"
            "只输出 JSON：{\"find\":\"原文片段\",\"replace\":\"替换片段\"}\n"
            "find 必须能从下面的内容里原样找到，尽量短（1-3 行）。\n\n"
            "文件 " + rel + "：\n```\n" + content[:9000] + "\n```"
        )
        raw2 = _ask(llm_call, second)
        match2 = re.search(r"\{.*\}", raw2, re.S)
        if match2:
            try:
                plan2 = json.loads(match2.group(0))
                find = str(plan2.get("find") or find)
                replace = str(plan2.get("replace") or replace)
            except Exception:  # noqa: BLE001
                pass
    blocked = _protected_reason(rel, find, replace)
    if blocked:
        log_evolution({"kind": "blocked", "request": text[:500], "file": rel, "reason": blocked})
        return False, f"这块我不能动：{blocked}"
    if not find or find not in content:
        return False, "模型给的原文片段在文件里找不到，未改动。"

    log_evolution({
        "kind": "applied",
        "request": text[:500],
        "file": rel,
        "reason": str(plan.get("reason") or "")[:200],
        "diff": {"find": find[:400], "replace": replace[:400]},
    })
    BACKUP.mkdir(parents=True, exist_ok=True)
    backup = BACKUP / f"{target.stem}.{time.strftime('%Y%m%d-%H%M%S')}{target.suffix}.bak"
    shutil.copy2(target, backup)
    original = target.read_text(encoding="utf-8")
    try:
        target.write_text(original.replace(find, replace, 1), encoding="utf-8")
        ok, err = _syntax_ok(target)
        if not ok:
            target.write_text(original, encoding="utf-8")
            return False, f"改完语法检查没过，已回滚：{err}"
        # 再核对一下：用户明确要改成的内容，必须真的出现在文件里
        wanted = re.search(r"(?:改成|换成|改为)\s*[「\"“']?([^，。！？\n「」\"“”'，]{1,30})", text)
        if wanted:
            needle = wanted.group(1).strip().strip("「」\"'“”")
            if needle and needle not in target.read_text(encoding="utf-8", errors="replace"):
                target.write_text(original, encoding="utf-8")
                return False, f"改完没找到「{needle}」，已回滚，可能是改错文件了。"
        if target.name == "dl_api.py" and restart_api:
            restart_api()
        return True, f"改了 {rel}（备份：{backup.name}）"
    except Exception as exc:  # noqa: BLE001
        try:
            target.write_text(original, encoding="utf-8")
        except OSError:
            pass
        return False, f"改动失败，已回滚：{exc}"
