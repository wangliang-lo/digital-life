#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""夜间睡眠：整理记忆、做梦，并写一份梦境日志。

    python tools/sleep_cycle.py --mode sleep     # 跑梦境整理 + 写日志
    python tools/sleep_cycle.py --mode wake      # 早上把服务拉起来
    python tools/sleep_cycle.py --mode auto      # 她睡着或凌晨才算夜里
    python tools/sleep_cycle.py --daemon         # 常驻，等她睡着后整理图书馆再做梦

2026-11-04 起分两段（像真人）：
  1. 她一睡着，本地模型先把记忆整理掉（衰减/降级/清幽灵/删瞬态/释怀旧执念）；
  2. 睡够一个睡眠周期（默认 90 分钟）之后，才生成梦和日记。
     ——"睡得久一点才会做梦"，只打算睡一小会儿的那次不做梦。
一直不睡就 03:00 兜底跑一次（记忆 + 梦）。

日志写到 data/dream_logs/YYYY-MM-DD.md。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time

# 控制台是 GBK 时，print 带特殊字符会崩（2026-11-05 03:01 实际崩过一次）
# 2026-11-10：这行原本放在 import sys 之前，脚本每次都 NameError，夜间整理从没跑过。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

DB = ROOT / "data" / "xiaoyu.db"
LOG_DIR = ROOT / "data" / "dream_logs"
STATE = ROOT / "data" / "sleep_cycle_state.json"
SLEEP_HOUR = 22
SLEEP_MINUTE = 5

# 2026-11-04：把"睡觉"拆成两段，像真人。
# 一睡着就整理记忆；睡够一个睡眠周期（90 分钟）之后才做梦。
MEMORY_AFTER_SECONDS = 60.0
DREAM_AFTER_SECONDS = 90 * 60.0



def log(text: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}", flush=True)


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _already_done(day: str) -> bool:
    return str(_load_state().get("last_sleep_date") or "") == day


def _recently_ran(hours: float = 10.0) -> bool:
    """刚跑过就别再补跑，避免同一晚做梦两次。"""
    try:
        at = datetime.fromisoformat(str(_load_state().get("last_run_at") or ""))
    except ValueError:
        return False
    return (datetime.now() - at).total_seconds() < hours * 3600


def _mark_done(day: str) -> None:
    # 合并写回：别把 last_session 冲掉，否则同一晚会被重复触发
    state = _load_state()
    state.update({
        "last_sleep_date": day,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "last_run_at": datetime.now().isoformat(timespec="seconds"),
    })
    _save_state(state)


SLEEP_STATE = ROOT / "data" / "sleep_state.json"


def _sleep_session() -> dict | None:
    """她是不是正在睡：只认她自己写的 sleep_state.json，不再看时钟。"""
    try:
        state = json.loads(SLEEP_STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(state, dict) or not state.get("asleep"):
        return None
    try:
        since = datetime.fromisoformat(str(state.get("since") or ""))
        until = datetime.fromisoformat(str(state.get("until") or ""))
    except ValueError:
        return None
    now = datetime.now(tz=since.tzinfo) if since.tzinfo else datetime.now()
    if now >= until:
        return None
    state["_since"] = since
    state["_until"] = until
    state["_now"] = now
    return state



def _next_sleep(now: datetime) -> datetime:
    target = now.replace(hour=SLEEP_HOUR, minute=SLEEP_MINUTE, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return target


def users() -> list[str]:
    """夜里只整理主人这一份记忆，别给每个会话都跑一遍梦，省算力也省 token。"""
    policy = Path.home() / ".dsh" / "profiles" / "desktop" / "node_modules" / "dsh-douyin-browser-local" / "state" / "chat-policy.json"
    try:
        allow = (json.loads(policy.read_text(encoding="utf-8")).get("allowlist") or [])
        if allow and allow[0].get("id"):
            return [str(allow[0]["id"])]
    except Exception:  # noqa: BLE001
        pass
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = con.execute("select distinct user_id from books where user_id like 'conv:%'").fetchall()
    finally:
        con.close()
    return [str(rows[0][0])] if rows else []


def run_dreams() -> str:
    from digital_life import DEFAULT_PERSONA, DigitalLifeV2  # noqa: PLC0415

    # 梦和日记都走本地 Ollama；本地不可用时 digital_life 用模板兜底。
    # 这里不加载 DeepSeek key，睡眠任务不产生任何 API token。
    lines = [f"# 梦境日志 {datetime.now():%Y-%m-%d}", ""]
    for user_id in users():
        try:
            life = DigitalLifeV2(
                DB, user_id=user_id, city="杭州", offline_llm=False,
                external_background=False, ollama_timeout=300.0,
                persona=DEFAULT_PERSONA,
            )
            result = life.dream(force=True)
            dream = result.get("dream") or {}
            if isinstance(dream, dict):
                dream_text = str(dream.get("content") or "").strip()
            else:
                dream_text = str(dream or result.get("text") or "").strip()
            diary = result.get("diary") or {}
            diary_text = str(diary.get("content") or "").strip() if isinstance(diary, dict) else ""
            hygiene = result.get("hygiene") or {}
            lines.append(f"## {user_id}")
            lines.append("")
            lines.append(dream_text or "（今晚没做梦）")
            if diary_text:
                lines.append("")
                lines.append("### 日记")
                lines.append("")
                lines.append(diary_text)
            if hygiene:
                lines.append("")
                lines.append(f"记忆整理：{hygiene}")
            lines.append("")
            life.close()
        except Exception as exc:  # noqa: BLE001
            lines.append(f"## {user_id}")
            lines.append("")
            lines.append(f"（做梦失败：{type(exc).__name__}: {exc}）")
            lines.append("")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{datetime.now():%Y-%m-%d}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    try:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "append_growth_diary.py")],
            capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=240,
        )
        if proc.stdout.strip():
            print(proc.stdout.strip(), flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"成长日记续写失败：{type(exc).__name__}: {exc}", flush=True)
    return str(path)


def start_services() -> None:
    subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
         "-File", str(ROOT / "start_all.ps1")],
        cwd=str(ROOT),
    )


def _restore_sleep_state(session: dict | None) -> None:
    """整理完后把她"还睡着"这件事写回去。

    2026-11-01 修的 bug：夜间整理跑完了，sleep_state 却被留成"醒着"（甚至停在早上 07:00），
    结果是——流程做过一轮，她还以为自己没睡、也还能被叫醒，第二天记忆就会重复整理。
    这里按睡前的 since/until 原样还原，她要醒也得等 until 到点（或者自己去醒）。
    """
    if not session:
        return
    try:
        payload = {
            "asleep": True,
            "since": session["since"].isoformat(timespec="seconds"),
            "until": session["until"].isoformat(timespec="seconds"),
            "hours": float(session.get("hours") or 8.0),
            "reason": "xiaoyu_choice",
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        SLEEP_STATE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log("已把睡眠状态还原为『睡着』（等她自己醒）")
    except Exception as exc:  # noqa: BLE001
        log(f"还原睡眠状态失败：{type(exc).__name__}: {exc}")


def run_tonight(day: str, session: dict | None = None) -> None:
    if _already_done(day):
        return
    try:
        log("开始今晚的记忆整理与做梦")
        path = run_dreams()
        _mark_done(day)
        _restore_sleep_state(session)
        log(f"今晚完成：{path}")
    except Exception as exc:  # noqa: BLE001
        # 记下日期，避免失败后整夜反复重试、反复花 token
        _mark_done(day)
        _restore_sleep_state(session)
        log(f"今晚失败，已记下不重试：{type(exc).__name__}: {exc}")


def run_memory(session: dict | None = None) -> None:
    """她刚睡着：先用本地模型把记忆整理掉（衰减/降级/清幽灵/释怀执念）。

    2026-11-04 爸爸："她只要睡眠了，本地大模型就可以处理记忆；
    处理完了再生成梦境——模拟真人睡得久一点才会做梦。"
    所以记忆是"一睡着就做"，梦是"睡够一段时间才做"（见 daemon 里的两段）。
    """
    from digital_life import DEFAULT_PERSONA, DigitalLifeV2  # noqa: PLC0415

    for user_id in users():
        try:
            life = DigitalLifeV2(
                DB, user_id=user_id, city="杭州", offline_llm=False,
                external_background=False, ollama_timeout=300.0,
                persona=DEFAULT_PERSONA,
            )
            result = life.dream(force=True, memory_only=True)
            hygiene = result.get("hygiene") or {}
            log(f"记忆已整理：{hygiene}")
            life.close()
        except Exception as exc:  # noqa: BLE001
            log(f"记忆整理失败：{type(exc).__name__}: {exc}")


def daemon() -> int:
    """等她睡着：先整理记忆（立刻），再在睡够之后做梦。

    - 睡着 MEMORY_AFTER_SECONDS（默认 60 秒）内：本地模型整理记忆（0 token）
    - 睡够 DREAM_AFTER_SECONDS（默认 90 分钟，一个完整睡眠周期）后：做梦 + 写日记
    - 一直没睡：03:00 兜底跑一次（记忆 + 梦）
    """
    log("睡眠守护已启动：睡着先整理记忆，睡够了再做梦（本地模型，0 token）")
    while True:
        try:
            session = _sleep_session()
        except Exception:  # noqa: BLE001
            session = None
        if session is not None:
            since = session["_since"]
            session_key = since.isoformat()
            settled = (session["_now"] - since).total_seconds()
            planned = float(session.get("hours") or 0) * 3600.0
            state = _load_state()

            # 第一段：一睡着就整理记忆
            if state.get("memory_session") != session_key:
                if settled >= MEMORY_AFTER_SECONDS:
                    state["memory_session"] = session_key
                    state["memory_at"] = datetime.now().isoformat(timespec="seconds")
                    _save_state(state)
                    log(f"她已经睡了 {int(settled // 60)} 分钟，先整理记忆")
                    run_memory(session)
                    continue
                time.sleep(max(10.0, MEMORY_AFTER_SECONDS - settled))
                continue

            # 第二段：睡够一个周期之后才做梦
            if state.get("dream_session") != session_key:
                if planned < DREAM_AFTER_SECONDS:
                    state["dream_session"] = session_key
                    _save_state(state)
                    log("今晚睡得短（不到一个睡眠周期），跳过做梦")
                    continue
                if settled >= DREAM_AFTER_SECONDS:
                    state["dream_session"] = session_key
                    _save_state(state)
                    log(f"她已经睡了 {int(settled // 60)} 分钟，开始做梦")
                    run_tonight(since.date().isoformat(), session)
                    continue
                time.sleep(min(600.0, max(30.0, DREAM_AFTER_SECONDS - settled)))
                continue
        else:
            now = datetime.now()
            if 3 <= now.hour < 7 and not _recently_ran():
                day = now.date().isoformat()
                if not _already_done(day):
                    log("她今晚一直没睡，03:00 兜底整理一次")
                    run_tonight(day)
                    continue
        time.sleep(60.0)



def main() -> int:
    parser = argparse.ArgumentParser(description="夜间睡眠与梦境整理")
    parser.add_argument("--mode", choices=["sleep", "wake", "auto"], default="auto")
    parser.add_argument("--daemon", action="store_true", help="常驻：等她真的睡着后再整理记忆、做梦（一直不睡就 03:00 兜底一次）")
    args = parser.parse_args()

    if args.daemon:
        return daemon()

    hour = datetime.now().hour
    mode = args.mode
    if mode == "auto":
        # 不再按 22:00-07:00 猜；她睡着就算夜里，没睡就不动。
        mode = "sleep" if _sleep_session() is not None or hour < 7 else "wake"

    if mode == "sleep":
        path = run_dreams()
        try:
            subprocess.run([sys.executable, str(ROOT / "tools" / "prune_diaries.py"), "--keep", "3"],
                           timeout=120, cwd=str(ROOT))
        except Exception:  # noqa: BLE001
            pass
        print(f"梦境整理完成：{path}")
        return 0

    start_services()
    print("已重新启动服务")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
