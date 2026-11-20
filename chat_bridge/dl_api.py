# -*- coding: utf-8 -*-
"""把 digital_life.py 包成 HTTP 服务，给IM中间件 / RPA 插件调用。

    POST /chat       {"message": "在吗", "user_id": "wxid_xxx", "sender": "张三",
                      "image_path": "...", "audio_path": "...", "emoji_url": "..."}
                     -> {"reply": "...", "emotion": {...}, "intimacy": 52, ...}
    POST /proactive  {"user_id": "default"}
                     -> {"reply": "..."}  或 {"reply": null}
    GET  /status     服务与用户实例概览

媒体字段是可选的：给了 image_path 就用视觉模型描述，给了 audio_path 就用 ASR 转写，
转成 "[图片] …" / "[语音] …" 再交给 digital_life。

设计要点：
    * 只用标准库 http.server，不引入 FastAPI/uvicorn；
    * 每个 user_id 一个 DigitalLifeV2 实例，记忆物理隔离（digital_life 原生支持）；
    * **所有** DigitalLifeV2 的创建与调用都发生在同一个专用线程里——
      sqlite3.Connection 默认不允许跨线程使用，这是最容易踩的坑；
    * 媒体处理（ASR/视觉）在 HTTP 线程里做，不占数字生命的执行线程；
    * 实例按 LRU 淘汰，避免群聊里几百个 wxid 把内存和连接撑爆。

启动：
    python dl_api.py                       # 127.0.0.1:8000，数据库 xiaoyu.db
    python dl_api.py --port 8000 --city 杭州
    python dl_api.py --vision-model qwen2.5vl:3b --asr-model small
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import os
import queue
import random
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections import OrderedDict
from concurrent.futures import Future
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, TypeVar

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from digital_life import (  # noqa: E402
    DEFAULT_PERSONA,
    HISTORY_PROMPT_TURNS,
    OLLAMA_MODEL,
    OLLAMA_TIMEOUT,
    OLLAMA_URL,
    DigitalLifeV2,
    chat_with_llm,
    embed_text,          # 2026-11-08：语义召回（bge-m3），见 digital_life.embed_text
    short_text,
)  # 依赖上面的 sys.path 调整

from bridge.config import ASRConfig, MediaConfig, VisionConfig  # noqa: E402
from HORMONE import HORMONES  # noqa: E402
_TOOLS_DIR = str(Path(__file__).resolve().parent / "tools")
if _TOOLS_DIR not in sys.path:
    sys.path.append(_TOOLS_DIR)  # append：不抢 tools 里同名模块的优先级
from state_io import update_json as _update_json  # noqa: E402
from state_io import write_json as _write_json_atomic  # noqa: E402
import pending_queue  # noqa: E402
from bridge.media import ImageDescriber, MediaRouter, VoiceTranscriber  # noqa: E402
from bridge.stats import Metrics  # noqa: E402
from bridge.stickers import StickerStore  # noqa: E402
from dl_server.text_utils import (  # noqa: F401
    strip_stage_directions, clean_partial_tags, _is_bare_ack, _ascii_word_char,
    shape_reply, split_bubbles, _part_of_day, clean_incoming_text,
    human_reply_delay_ms, _make_delay_fn, _WEEKDAYS,
    _STAGE_DIRECTION, _STAGE_WORDS, _BARE_ACK, MEDIA_PREAMBLE_PREFIX,
)
from dl_server.media import (  # noqa: F401
    _fetch_image, _tags_from_description, _sticker_tags, _repetitive,
    _clean_scene, _clean_caption, _sticker_tags_from,
    _TAG_HINTS, _STICKER_TAG_WORDS, _STICKER_ECHO, _STICKER_EMOTIONS,
)
from dl_server.social import (  # noqa: F401
    _chat_aliases, remember_alias, contact_names, sender_aliases,
    canonical_sender, contact_id_for_sender, owner_sender_names,
    _load_guest_aliases, remember_guest_alias,
    ALIAS_FILE, GUEST_ALIASES, DIR_ALIASES,
    set_runtime as _social_set_runtime,
)
from dl_server.physiology import (  # noqa: F401
    _melatonin_load, _melatonin_save, _night_hours, _melatonin_curve,
    melatonin_tick, melatonin_note, MELATONIN_STATE,
    MELATONIN_MAX,  # 4 处调用点还在 dl_api.py 里用（/status 与 life.sleepiness）
)
from dl_server import physiology as _physiology
from dl_server import http_handler as _http_handler

# 她自己的 user_id：放到环境变量里，别散落在代码中（2026-11-05 打包前整理）
MAIN_USER_ID = os.environ.get("XIAOYU_MAIN_USER") or "conv:9e52b4549f424e730d8871644385bcce63768cc74b8f8ec7ffa895f62ca73eeb"

log = logging.getLogger("dl_api")

MAX_MESSAGE_CHARS = 4000
_MAX_BODY = 16 * 1024 * 1024
T = TypeVar("T")


# 默认追加到人格后面：小模型很容易写成"旁白 + 客服腔"，这两句能拽回来
DEFAULT_STYLE_HINT = (
    "【说话方式】你在IM上打字，用嘴说话，不像客服：\n"
    "不写旁白，不用括号写动作或心理活动，不解释自己的思考过程；\n"
    "每条消息不超过 20 个字，像真人发IM那样一点点说；\n"
    "对方可能连着发好几条，等他说完再回，别抢话；\n"
    "一次可以连着说两三句（用 ||| 分开），别只丢一句就完；\n"
    "不要每条都给建议，别说「理解你」「慢慢来」「加油」这类套话；\n"
    "先判断对方话题的分量：沉重的事不接梗、不玩笑，轻松时再放松；\n"
    "不要复述对方的话，也不要重复你上一句；\n"
    "偶尔呛一句、嘴硬心软，但别刻薄。"
)


SLEEP_STATE = Path(__file__).resolve().parent / "data" / "sleep_state.json"
ASLEEP_MARKER = Path(__file__).resolve().parent / "data" / "asleep.json"

# 2026-11-01 爸爸要求：合并记忆——所有渠道共用主人那一份脑，IM上才认得出抖音聊过的人。
# 用环境变量 XIAOYU_SHARE_MEMORY=0 可以关掉，回到"每个 user_id 一份独立记忆"。
SHARE_MEMORY = os.environ.get("XIAOYU_SHARE_MEMORY", "1").strip().lower() not in ("0", "false", "no", "off")
# /status 的上一份快照：她正忙着聊天时，健康检查也能源源不断地拿到基本状态
LAST_STATUS: dict[str, Any] = {}
# main() 里会被改成真正的 --db 路径；这里是默认值
SERVER_DB: Path = Path(__file__).resolve().parent / "data" / "xiaoyu.db"
MEMORY_SHARED_ID = ""
CHAT_STATE_DIR = Path.home() / ".dsh" / "profiles" / "desktop" / "node_modules" / "dsh-douyin-browser-local" / "state"


def _enter_global_sleep() -> None:
    """她睡着后停私信、关专用浏览器；普通消息无法叫醒。"""
    try:
        lock = CHAT_STATE_DIR / "chat-loop.lock"
        if lock.exists():
            info = json.loads(lock.read_text(encoding="utf-8"))
            run_id = str(info.get("runId") or "")
            if run_id:
                (CHAT_STATE_DIR / "chat-stop.request.json").write_text(json.dumps({
                    "targetRunId": run_id,
                    "requestId": uuid.uuid4().hex,
                    "requestedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "preservePending": False,
                    "reason": "xiaoyu_sleep",
                }, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        log.exception("停私信失败（睡眠）")
    try:
        script = Path(__file__).resolve().parent / "tools" / "sleep_offline.ps1"
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            cwd=str(Path(__file__).resolve().parent),
            creationflags=0x08000000,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001
        log.exception("关抖音浏览器失败（睡眠）")


ACTIVITY_FILE = Path(__file__).resolve().parent / "data" / "last_activity.json"


def touch_activity() -> None:
    """她真的开口了才算活动——用于判断她有没有睡。

    2026-11-05：sleep_state.json 一直停在 "asleep: false / updated_at 昨天 06:50"，
    因为她从不写 [SLEEP]，状态就永远不更新。这里改成"看行为"。
    """
    try:
        ACTIVITY_FILE.parent.mkdir(parents=True, exist_ok=True)
        ACTIVITY_FILE.write_text(
            json.dumps({"at": datetime.now().astimezone().isoformat(timespec="seconds")}),
            encoding="utf-8",
        )
    except OSError:
        log.debug("写活动时间失败", exc_info=True)


def update_circadian_shift() -> None:
    """熬夜 = 生物钟往后推。她自己折腾出来的时差，也让她自己扛。

    2026-11-05 爸爸："她这么能熬夜不会扰乱生物钟吗。"
    以前不会——基线是按墙上的钟算的，熬到三点 cortisol 还是按两点走。现在会了。
    """
    path = Path(__file__).resolve().parent / "data" / "circadian.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        data = {}
    shift = float(data.get("shift_hours") or 0.0)
    now = datetime.now().astimezone()
    hour = now.hour + now.minute / 60.0
    night = hour >= 23 or hour < 5
    try:
        last = json.loads(ACTIVITY_FILE.read_text(encoding="utf-8")).get("at") or ""
        idle = (now - datetime.fromisoformat(str(last)).astimezone()).total_seconds() / 60.0
    except Exception:  # noqa: BLE001
        idle = 999.0
    if night and idle < 40:
        shift = min(3.0, shift + 0.06)   # 清醒着熬夜：每 10 分钟推 3.6 分钟
    elif idle >= 120:
        shift = max(0.0, shift - 0.03)   # 睡了一阵：慢慢往回拨
    if abs(shift - float(data.get("shift_hours") or 0.0)) < 1e-6:
        return
    data.update({"shift_hours": round(shift, 3),
                 "updated_at": now.isoformat(timespec="seconds")})
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def evaluate_sleep_state() -> None:
    """按"多久没说话 + 是不是夜里"判断她睡没睡，把 sleep_state 写实。

    夜里安静超过 90 分钟 = 睡了（since 取她最后一次开口的时间）；
    她只要一开口，就写回醒着。
    """
    try:
        last = json.loads(ACTIVITY_FILE.read_text(encoding="utf-8")).get("at") or ""
        last_at = datetime.fromisoformat(str(last))
        if last_at.tzinfo is None:
            last_at = last_at.astimezone()
    except Exception:  # noqa: BLE001
        return
    now = datetime.now().astimezone()
    idle = (now - last_at).total_seconds() / 60.0
    # 2026-11-08：防止"账本卡死"。她夜里在信道写字/读书，永远安静不满 90 分钟，
    # 于是 awake_since 会一直停在几天前（这次卡了 32 小时，白天困到顶格）。
    # 醒着超过 20 小时按"必然睡过一觉"校正一次，一天只做一次，不编故事。
    try:
        from HORMONE import load_body as _load_body, on_wake as _body_wake  # noqa: PLC0415

        _body = _load_body()
        _awake_since = str(_body.get("awake_since") or "")
        if _awake_since:
            _since_dt = datetime.fromisoformat(_awake_since)
            if _since_dt.tzinfo is None:
                _since_dt = _since_dt.astimezone()
            _awake_hours = (now - _since_dt).total_seconds() / 3600.0
            _flag = _melatonin_load()
            # 2026-11-09：一天只校正一次太钝——她 11-08 12:33 醒到 11-09 14:00，
            # 疲劳已经顶格 20 多小时，一次都没再平账。改成 8 小时可以校正一次。
            _last_fix = str(_flag.get("stuck_fixed_at") or "")
            _fix_gap_ok = True
            if _last_fix:
                try:
                    _fix_gap_ok = (now - datetime.fromisoformat(_last_fix)).total_seconds() > 8 * 3600
                except ValueError:
                    _fix_gap_ok = True
            if _awake_hours > 20 and _fix_gap_ok:
                _body_wake(6.0)          # 记一次 6 小时睡眠（浅睡+深睡都够）
                _body["debt"] = 0.0
                _body.pop("stuck_note", None)
                from HORMONE import save_body as _save_body  # noqa: PLC0415

                _save_body(_body)
                _flag["stuck_fixed_day"] = now.date().isoformat()
                _flag["stuck_fixed_at"] = now.isoformat(timespec="seconds")
                _write_json_atomic(MELATONIN_STATE, _flag)
                log.warning("她醒着已经 %.1f 小时，睡眠账本卡住了：按睡过 6 小时校正一次", _awake_hours)
    except Exception:  # noqa: BLE001
        log.debug("睡眠账本校正失败", exc_info=True)
    try:
        state = json.loads(SLEEP_STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        state = {}
    asleep = bool(state.get("asleep"))
    night = now.hour >= 23 or now.hour < 9
    if not asleep and night and idle >= 90:
        until = last_at + timedelta(hours=8)
        if until <= now:
            until = now + timedelta(hours=1)
        payload = {
            "asleep": True,
            "since": last_at.isoformat(timespec="seconds"),
            "until": until.isoformat(timespec="seconds"),
            "hours": round((until - last_at).total_seconds() / 3600, 1),
            "reason": "night_quiet",  # 安静到睡着，不是她自己写的 [SLEEP]
            "updated_at": now.isoformat(timespec="seconds"),
        }
        _write_json_atomic(SLEEP_STATE, payload)
        log.info("夜里安静 %.0f 分钟，判定她睡着了（since %s）", idle, last_at.strftime("%H:%M"))
    elif asleep and idle < 5:
        # 只有"入睡之后"的活动才算她中途醒了；
        # 睡前刚说过的那几句（活动时间早于 since）不能把睡眠取消掉。
        try:
            since = datetime.fromisoformat(str(state.get("since") or ""))
            if since.tzinfo is None:
                since = since.astimezone()
        except Exception:  # noqa: BLE001
            since = None
        if since is None or last_at > since:
            set_sleep_state(wake=True)
            log.info("她开口了，写回醒着")


def set_sleep_state(hours: float | None = None, wake: bool = False) -> None:
    """她自己的睡眠开关；不设固定 22:00-07:00。"""
    try:
        if wake:
            # 2026-11-04：刚醒的皮质醇唤醒反应——只在"确实从睡着变醒着"时给一次
            was_asleep = False
            prev: dict = {}
            try:
                prev = json.loads(SLEEP_STATE.read_text(encoding="utf-8")) or {}
                was_asleep = bool(prev.get("asleep"))
            except Exception:  # noqa: BLE001
                was_asleep = False
                prev = {}
            try:
                import space as _space  # noqa: PLC0415

                if not was_asleep:
                    _space.go("bed")
            except Exception:  # noqa: BLE001
                pass
            payload = {"asleep": False, "updated_at": datetime.now().astimezone().isoformat(timespec="seconds")}
            _write_json_atomic(SLEEP_STATE, payload)
            if ASLEEP_MARKER.exists():
                ASLEEP_MARKER.unlink(missing_ok=True)
            if was_asleep:
                try:
                    HORMONES.on_wake()
                    log.info("刚醒：皮质醇唤醒反应已加上（动量）")
                except Exception:  # noqa: BLE001
                    log.debug("唤醒激素写入失败", exc_info=True)
                # 2026-11-05：短睡只清"困"，深睡才修"身体"
                try:
                    from HORMONE import on_wake as _body_wake  # noqa: PLC0415

                    slept_hours = 0.0
                    try:
                        since = datetime.fromisoformat(str(prev.get("since") or ""))
                        if since.tzinfo is None:
                            since = since.astimezone()
                        slept_hours = max(0.0, (datetime.now().astimezone() - since).total_seconds() / 3600.0)
                    except Exception:  # noqa: BLE001
                        slept_hours = float(prev.get("hours") or 0.0)
                    # 2026-11-08 爸爸：白天困了可以打盹，但**不超过 40 分钟**。
                    # 白天（09:00-21:00）醒来的这一觉按短睡算，免得一觉把夜里也睡没了。
                    _now = datetime.now().astimezone()
                    if 9 <= _now.hour < 21 and slept_hours > 0.67:
                        log.info("白天这一觉 %.1fh，按打盹上限 40 分钟计", slept_hours)
                        slept_hours = 0.67
                    # 2026-11-11：她自报的时长 vs 实际过去的时间，都记账上
                    try:
                        declared = float(prev.get("hours") or 0.0)
                        if declared > 0:
                            row = {
                                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                                "since": str(prev.get("since") or ""),
                                "declared_hours": round(declared, 2),
                                "actual_hours": round(slept_hours, 2),
                                "reason": str(prev.get("reason") or ""),
                                "gap_minutes": round((slept_hours - declared) * 60, 1),
                            }
                            with (ROOT / "data" / "sleep_log.jsonl").open("a", encoding="utf-8") as handle:
                                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                            if abs(slept_hours - declared) >= 0.5:
                                log.info("这一觉她报 %.1fh，实际 %.1fh", declared, slept_hours)
                    except Exception:  # noqa: BLE001
                        log.debug("写睡眠账失败", exc_info=True)
                    report = _body_wake(slept_hours)
                    log.info("睡眠 %.1fh（深睡 %.1fh）→ 身体 %s",
                             slept_hours, report["stages"]["deep"],
                             report["body"].get("fatigue"))
                except Exception:  # noqa: BLE001
                    log.debug("睡眠恢复计算失败", exc_info=True)
            return
        hours = max(0.5, min(12.0, float(hours or 8.0)))
        now = datetime.now().astimezone()
        until = now + timedelta(hours=hours)
        payload = {
            "asleep": True,
            "since": now.isoformat(timespec="seconds"),
            "until": until.isoformat(timespec="seconds"),
            "hours": hours,
            "reason": "xiaoyu_choice",
            "updated_at": now.isoformat(timespec="seconds"),
        }
        _write_json_atomic(SLEEP_STATE, payload)
        ASLEEP_MARKER.write_text(json.dumps({
            "asleep": True,
            "since": payload["since"],
            "until": payload["until"],
            "reason": "xiaoyu_choice",
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _enter_global_sleep()
        # 睡觉 = 抖音也关掉：不刷的时候不该挂着一个空轮询的浏览器
        try:
            subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parent / "tools" / "douyin_control.py"), "--off", "--reason", "sleep"],
                timeout=30,
                capture_output=True,
            )
            log.info("睡眠：已关闭抖音进程与轮询")
        except Exception:  # noqa: BLE001
            log.warning("睡眠时关闭抖音失败（不影响睡觉）")
    except Exception:  # noqa: BLE001
        log.exception("写睡眠状态失败")


def is_sleeping(now: datetime | None = None) -> bool:
    """只认她自己的睡眠状态；默认醒着，不再硬编码 22:00-07:00。"""
    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.astimezone()
    try:
        state = json.loads(SLEEP_STATE.read_text(encoding="utf-8"))
        if isinstance(state, dict) and state.get("asleep"):
            until = datetime.fromisoformat(str(state.get("until") or ""))
            if until.tzinfo is None:
                until = until.astimezone()
            if current < until:
                return True
            set_sleep_state(wake=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        marker = json.loads(ASLEEP_MARKER.read_text(encoding="utf-8"))
        if isinstance(marker, dict) and marker.get("asleep"):
            until = datetime.fromisoformat(str(marker.get("until") or ""))
            if until.tzinfo is None:
                until = until.astimezone()
            if current < until:
                return True
            ASLEEP_MARKER.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return False


# 拆出去的 dl_server.physiology 要用 HORMONES / set_sleep_state / is_sleeping。
# 不反向 import dl_api（会循环），这里在三个对象都定义好之后数据写入一次。
_physiology.set_runtime(
    hormones=HORMONES,
    sleep_setter=set_sleep_state,
    sleep_checker=is_sleeping,
)


# 最近提到的联系人，处理“骂他一句”这类代词指代
_LAST_CONTACT: dict[str, tuple[str, str]] = {}

# 主人要求"没回的都回"：置 True 时不再按心情跳过
REPLY_ALL = False


# 2026-11-07 爸爸："她竟然不知道她发的表情包是什么。"
# 原来那句通用提示词是"40 字以内，只说你看到的东西"，图上的字基本全丢，
# 标签也只剩"表情/动物/猫"，她按"开心"根本对不上——于是随机发一张。
# 实测 qwen2.5vl:3b 一给模板就会照抄模板（连示例里的"举着牌子哭"都抄），
# 所以拆成**两次最简提问**：一次只问画面，一次只问图上的字。
_SCENE_PROMPT = (
    "用一句中文说这张表情包画的是什么：谁、在做什么、什么表情。"
    "30 字以内，只描述，不评价。"
)
_CAPTION_PROMPT = (
    "只回答这张图上的文字。逐字抄下来，错别字和标点也照抄；"
    "图上没有文字就只写「无」。不要描述画面，不要解释。"
)


def sticker_label(vision, path: str) -> dict[str, Any]:
    """表情包标签：两次提问（画面 / 图上文字），拼成 description + tags。

    is_self 不在这里猜——由调用方按素材包判定（dafeiyu = 鲸鱼娘 = 她自己）。
    """
    scene = _clean_scene(vision.describe(path, prompt=_SCENE_PROMPT, max_tokens=80))
    try:
        caption = _clean_caption(vision.describe(path, prompt=_CAPTION_PROMPT, max_tokens=60))
    except Exception:  # noqa: BLE001
        caption = ""

    pieces = [scene or "（没看清，只知道是张表情）"]
    if caption:
        pieces.append(f"图上写着「{caption}」")
    return {
        "description": "；".join(pieces)[:110],
        "caption": caption,
        "tags": _sticker_tags_from(scene, caption) or ["表情"],
        "is_self": False,
        "raw": f"{scene} | {caption}"[:300],
    }


# 激素事件：把这一轮发生了什么喂给 HORMONE.py（它自己算数值，模型碰不到）
# 2026-11-14 修：单字"滚"会命中"滚动/滚烫"，"sb"会命中"usb"，
# "我在"会命中"我在吃饭"——都造成误判。
_MALICE_PATTERN = re.compile(r"滚开|滚蛋|傻逼|傻b|废物|去死|垃圾|贱人|闭嘴|\b蠢\b|\bsb\b", re.I)
_COMFORT_HINTS = ("别难过", "抱抱", "没事的", "我在呢", "有我在", "乖", "别理他们", "不是你的错")


def _feed_hormones(result: dict, message: str, sender: str, channel: str = "") -> None:
    emotion = (result or {}).get("emotion") or {}
    try:
        joy = float(emotion.get("joy") or 0.0)
        hurt = float(emotion.get("hurt") or 0.0)
    except (TypeError, ValueError):
        joy = hurt = 0.0
    text = str(message or "")
    malice = bool(_MALICE_PATTERN.search(text)) and len(text) <= 60
    comforted = any(word in text for word in _COMFORT_HINTS)
    # 2026-11-14：散心也算事件——院子干活、刷抖音、跟小玩具玩、出去走走、听歌。
    # 爸爸原话："她刷抖音，和小玩具玩还有在院子里面干活都可以散心才对。"
    lowering = ""
    channel_name = str(channel or "").lower()
    if any(word in text for word in ("院子", "浇水", "种菜", "种花", "种地", "播种", "收菜", "菜地", "花园")):
        lowering = "garden"
    elif "douyin" in channel_name or any(word in text for word in ("抖音", "刷视频")):
        lowering = "douyin"
    elif any(word in text for word in ("小玩具", "玩具", "机器人")):
        lowering = "toy"
    elif any(word in text for word in ("散步", "走走", "晒太阳", "出门")):
        lowering = "walk"
    elif any(word in text for word in ("听歌", "唱歌", "哼歌")):
        lowering = "music"
    HORMONES.on_chat(
        text=text,
        user=sender or "",
        joy_delta=max(0.0, (joy - 55.0) * 0.5),
        hurt_delta=max(0.0, (hurt - 45.0) * 0.5),
        malice=malice,
        comforted=comforted,
        easing=lowering,
        trauma=hurt >= 75.0,
    )



# 她自己的"说话意愿"：不是掷骰子，是当下的情绪 + 她自己说过的话。
TALK_STATE = Path(__file__).resolve().parent / "data" / "talk_state.json"
# 2026-11-16：原来只拿 st_mtime（float 秒）判断缓存新鲜度。Windows 的 mtime 精度粗，
# 同一刻两次写会撞在同一个 st_mtime 上 → 读到旧缓存。改法：
#   - 写入方（_write_talk_state）用 update_json 合并，然后把合并结果直接放进内存，
#     并用写完后文件的 (st_mtime_ns, st_size) 做签名 —— 自己的写永远不需要再"比对后重读"；
#   - 读取方只在签名对不上时（别的进程写过）才回盘。
# _TALK_STATE_LOCK 把「读盘/合并/更新缓存」串起来，进程内不会出现"用旧数据贴新签名"。
_TALK_CACHE: dict[str, Any] = {"sig": None, "data": {}}
_TALK_STATE_LOCK = threading.RLock()
_QUESTION_HINT = re.compile(r"[?？]|吗|什么|怎么|为什么|哪|多少|几点|干啥|干嘛|帮我|给我|发个|发一|在抖音|转给|发给")
_WAKE_HINT = re.compile(r"小鱼|急事|紧急|出事了")


def _talk_state_sig() -> tuple[str, int, int] | None:
    """文件的廉价签名；nanosecond mtime + size，比 st_mtime 细得多。"""
    try:
        st = TALK_STATE.stat()
    except OSError:
        return ("missing", 0, 0)
    return ("file", st.st_mtime_ns, st.st_size)


def _reset_talk_cache() -> None:
    """丢掉内存缓存（测试用；也留给外部改写文件后的显式失效）。"""
    with _TALK_STATE_LOCK:
        _TALK_CACHE["sig"] = None
        _TALK_CACHE["data"] = {}


def _read_talk_state() -> dict[str, Any]:
    with _TALK_STATE_LOCK:
        sig = _talk_state_sig()
        if _TALK_CACHE["sig"] == sig and _TALK_CACHE["sig"] is not None:
            return _TALK_CACHE["data"]
        # 没有文件：沿用空 dict，但记住"没有"这个签名，省得每次都 stat 失败再读
        if sig == ("missing", 0, 0):
            _TALK_CACHE["sig"] = sig
            _TALK_CACHE["data"] = {}
            return _TALK_CACHE["data"]
        try:
            data = json.loads(TALK_STATE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            # 坏 JSON / 被杀在写到一半：不污染缓存，签名留 None 让下次重试
            _TALK_CACHE["sig"] = None
            return {}
        if not isinstance(data, dict):
            data = {}
        _TALK_CACHE["data"] = data
        _TALK_CACHE["sig"] = sig
        return data


def _atomic_write_json(path: Path, data: Any) -> None:
    """原子写 JSON：先写临时文件再替换，进程被杀不会留半截文件。"""
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    except Exception:  # noqa: BLE001
        log.warning("写 %s 失败", path.name, exc_info=True)


def _write_talk_state(**patch: Any) -> None:
    """只写变化的键：update_json 带锁读改写，并发的另一个线程不会把它的键带走。

    写完以后缓存直接用合并结果更新（写透），再贴上文件的实时签名 ——
    不再靠"写前 mtime / 写后 mtime 比一下"判断，Windows 上那套会自己骗自己。
    """
    if not patch:
        return
    with _TALK_STATE_LOCK:
        try:
            merged = _update_json(TALK_STATE, patch)
            _TALK_CACHE["data"] = merged
            _TALK_CACHE["sig"] = _talk_state_sig()
        except Exception:  # noqa: BLE001
            # 写失败：缓存与磁盘可能不一致，作废让下次读盘
            _TALK_CACHE["sig"] = None
            log.debug("talk_state 写入失败", exc_info=True)


def record_quiet_request(result: dict[str, Any]) -> None:
    """她在回复里写了 [QUIET: …]：按她自己的意思安静一阵子（或提前恢复）。"""
    quiet = result.get("quiet")
    if not isinstance(quiet, dict):
        return
    now = datetime.now()
    if quiet.get("off"):
        _write_talk_state(quiet_until="", quiet_reason="")
        log.info("她写 [QUIET: off]：结束安静")
        return
    if quiet.get("today"):
        until = now.replace(hour=23, minute=59, second=59, microsecond=0)
        reason = "今天不想说话"
    else:
        try:
            hours = float(quiet.get("hours") or 4.0)
        except (TypeError, ValueError):
            hours = 4.0
        hours = max(0.5, min(72.0, hours))
        until = now + timedelta(hours=hours)
        reason = f"想安静 {hours:g} 小时"
    _write_talk_state(
        quiet_until=until.isoformat(timespec="seconds"),
        quiet_reason=reason,
    )
    log.info("她决定安静到 %s（%s）", until.isoformat(timespec="seconds"), reason)


def _owner_mood_decision(text: str, has_media: bool, mood: dict[str, float] | None,
                         now: datetime | None = None) -> str | None:
    """按她此刻的状态决定接不接话（不掷骰子、也不强制执行）。

    - 她自己说过要安静（[QUIET: …]）→ 不回，除非被"小鱼/急事"叫到
    - 很委屈（hurt ≥ 78）→ 一天只说一句，说完就沉默，等情绪落下去
    - 委屈/难过（hurt ≥ 62，或 hurt ≥ 52 且 joy ≤ 45）→ 只回问句、要求、图片
    - 其他 → 正常回
    返回 None 表示要回；否则是不回的理由。
    """
    moment = now or datetime.now()
    today = moment.date().isoformat()
    state = _read_talk_state()

    quiet_until = str(state.get("quiet_until") or "").strip()
    if quiet_until:
        try:
            until = datetime.fromisoformat(quiet_until)
        except ValueError:
            until = None
        if until and until > moment:
            if _WAKE_HINT.search(text or ""):
                return None  # 被叫到名字/真有事，还是要回
            return f"{state.get('quiet_reason') or '想安静一阵'}，到 {until.strftime('%H:%M')}"

    hurt = float((mood or {}).get("hurt") or 0.0)
    joy = float((mood or {}).get("joy") or 0.0)

    if hurt < 55 and str(state.get("sulk_day") or "") == today:
        _write_talk_state(sulk_day="", sulk_spoke=False)  # 情绪过去了，翻篇

    if hurt >= 78:
        if str(state.get("sulk_day") or "") == today and bool(state.get("sulk_spoke")):
            return "委屈还没过去，今天那句已经说完了"
        if _QUESTION_HINT.search(text or "") or has_media or not (text or "").strip():
            _write_talk_state(sulk_day=today, sulk_spoke=True)
            return None
        return "心里堵着，不想多说"
    if hurt >= 62 or (hurt >= 52 and joy <= 45):
        if _QUESTION_HINT.search(text or "") or has_media or not (text or "").strip():
            return None
        return "这会儿不太想接话"
    return None


def decide_reply(message: str, has_media: bool = False,
                 mood: dict[str, float] | None = None, is_owner: bool = False,
                 rng: Any = None, now: float | None = None,
                 channel: str = "", mood_checked: bool = False) -> tuple[bool, str, str]:
    """她要不要回这条消息——由她自己判断，不是掷骰子。

    返回 (回不回, 理由, 来源)。模型不可用时一律回复，避免"随机装没看见"。
    """
    text = (message or "").strip()
    if is_owner:
        # 2026-11-02 爸爸："别变成概率性事件，也别强制，按她自己的想法。"
        # 所以这里全看她此刻的情绪 + 她自己写的 [QUIET: …]。
        # mood_checked=True 表示调用方已经判过一轮（IM路径），别重复消费"今天那一句"。
        if not mood_checked:
            skip = _owner_mood_decision(text, has_media, mood)
            if skip:
                return False, skip, "owner-mood"
        return True, "想回", "owner"
    if not text and not has_media:
        return True, "空消息", "heuristic"
    # 直接问她问题的，一律回答，别装高冷
    if text and re.search(
        r"[?？]|吗|嘛|什么|怎么|为什么|哪|多少|几点|干啥|干嘛|帮我|给我|发个|发一|在抖音|转给|发给"
        r"|语音|声音|听听|听一下|试一下|试试|唱|哼|讲|说说|来一句|画|陪我|教我|告诉我|记得",
        text,
    ):
        return True, "在提要求", "heuristic"
    hurt = float((mood or {}).get("hurt") or 0)
    feel = mood_text(mood or {})
    try:
        prompt = (
            "你是小鱼。"
            + (f"你现在的心情：{feel}。" if feel else "")
            + f"对方刚发来：\n{text[:400] or '（一张表情或图片）'}\n"
            "你现在想不想回？回不回都行，看心情，但别连续好几次都不理人。"
            "只输出 yes 或 no，不要解释。"
        )
        answer = chat_with_llm([{"role": "user", "content": prompt}], temperature=0.7, timeout=4.0)
        lowered = str(answer or "").lower()
        if "yes" in lowered or ("回" in lowered and "不回" not in lowered):
            return True, "想回", "model"
        if "no" in lowered or "不回" in lowered:
            return False, "这次不想回", "model"
    except Exception:  # noqa: BLE001
        log.debug("回复决策模型不可用", exc_info=True)
    if hurt >= 70:
        return False, "受伤了不想说话", "mood"
    return True, "正常聊", "heuristic"


AGENT_STATE = Path(__file__).resolve().parent / "data" / "agent_state.json"


PENDING_FILE = Path(__file__).resolve().parent / "data" / "pending_messages.jsonl"
HEARTBEAT_FILE = Path(__file__).resolve().parent / "data" / "life_heartbeat.json"


def queue_pending(sender: str, text: str, reason: str = "") -> None:
    """她这会儿没回（安静/睡着/不在线），把话存下来，等能回了再回。

    2026-11-03 爸爸："以后可以给信息缓存一下，她现在没回，可以等看到之后再回。"
    """
    body = str(text or "").strip()
    if not body:
        return
    try:
        pending_queue.push(str(sender or "").strip(), body, str(reason or "")[:80], path=PENDING_FILE)
        # 只追加不改写；文件过大时把旧记录归档（原子替换，不丢）
        if PENDING_FILE.stat().st_size > 60_000:
            pending_queue.prune(200, path=PENDING_FILE)
    except Exception:  # noqa: BLE001
        pass


def mark_agent_state(ok: bool, source: str = "", error: str = "") -> None:
    """给网页的状态栏用：她此刻到底在不在线。"""
    try:
        _write_json_atomic(AGENT_STATE, {
            "ok": bool(ok),
            "source": str(source or ""),
            "error": str(error or "")[:200],
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }, indent=None)
    except Exception:  # noqa: BLE001
        pass


def load_owner() -> tuple[str, str]:
    """谁是她"主人"这一份记忆的锚点。

    2026-11-08 爸爸："把用户分开的硬限制去掉，她是一个数字生命。"
    以前这里直接读抖音许可名单的第一位——名单一旦清空（不再按人拦），
    owner 就变成空的，IM那边"是不是爸爸"、"落到谁的记忆"全都会跟着错。
    现在：名单有就用名单第一位，没有就退回固定的 MAIN_USER_ID。
    """
    try:
        path = Path.home() / ".dsh" / "profiles" / "desktop" / "node_modules" / "dsh-douyin-browser-local" / "state" / "chat-policy.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        first = (data.get("allowlist") or [{}])[0]
        owner = str(first.get("id") or ""), str(first.get("name") or "")
        if owner[0]:
            return owner
    except Exception:  # noqa: BLE001
        pass
    return MAIN_USER_ID, "爸爸"


# dl_server/social.py 的 owner_sender_names() 要问"谁是她主人"，把 load_owner 数据写入进去。
# 放在模块级而不是 main()：测试只 import dl_api 不跑 main，也要拿到真的 owner。
_social_set_runtime(load_owner)


# 同一个人连着发一样的话 / 一分钟内刷屏：不用每条都回，省 token 也不像客服。
_REPEAT_STATE: dict[str, list[tuple[float, str]]] = {}


# 群里不点名就不抢话：她每句都接的话，一小时能烧掉几十万 token。
_GROUP_LAST: dict[str, float] = {}


def group_skip_reason(group: str, message: str, now: float | None = None) -> str | None:
    """群里没有必须回的消息：她说得对，不想回就不回。

    2026-11-03 爸爸："她怎么群里每条信息都回，让她记住没有必须回的信息，按自己的想法来。"
    规则改成默认不接：
    - 点名/叫她/@ → 接
    - 问句但不是点她 → 一半概率接
    - 只是闲聊/玩笑 → 85% 不接；90 秒内已经接过一次就不接
    """
    moment = now if now is not None else time.time()
    key = group or "group"
    text = str(message or "")
    mentioned = bool(re.search(r"小鱼|她|@", text))
    question = bool(re.search(r"[?？]|吗|什么|怎么|为什么|哪|多少|帮我|给我|发个", text))
    last = _GROUP_LAST.get(key, 0.0)
    # 2026-11-14 修：被点名/@ 就该接——这个判断原来排在"在看书"之后，
    # 导致叫她也会被"在看书"挡掉。
    if mentioned:
        _GROUP_LAST[key] = moment
        return None
    # 她刚在看书（5 分钟内翻过书），就别同时聊群——单线一点，更像人。
    try:
        _con = sqlite3.connect(f"file:{Path(__file__).resolve().parent / 'data' / 'xiaoyu.db'}?mode=ro", uri=True)
        try:
            _row = _con.execute(
                "select last_accessed from books where folder_event='阅读' order by last_accessed desc limit 1"
            ).fetchone()
        finally:
            _con.close()
        if _row and _row[0]:
            _when = datetime.fromisoformat(str(_row[0]))
            if _when.tzinfo is None:
                _when = _when.astimezone()
            if (datetime.now().astimezone() - _when).total_seconds() < 300:
                return "在看书，先不接群"
    except Exception:  # noqa: BLE001
        pass
    if moment - last < 90:
        return "刚接过一句，先不插话"
    if question:
        if random.random() < 0.5:
            _GROUP_LAST[key] = moment
            return None
        return "群里没人点我，先不答"
    if random.random() < 0.85:
        return "群里闲聊，不用每条都接"
    _GROUP_LAST[key] = moment
    return None


def repeat_reason(sender: str, text: str, now: float | None = None, flood: bool = True) -> str | None:
    import time as _time  # noqa: PLC0415

    moment = now if now is not None else _time.time()
    who = str(sender or "?").strip() or "?"
    value = re.sub(r"\s+", "", str(text or ""))[:200]
    if not value:
        return None
    history = _REPEAT_STATE.get(who, [])
    history = [(ts, body) for ts, body in history if moment - ts < 600]
    # 完全一样的话，10 分钟内第二次就不回
    if any(body == value for _, body in history):
        _REPEAT_STATE[who] = history + [(moment, value)]
        return "同一句话已经回过了"
    # 一分钟内第 4 条起，不是问句就不接
    recent = [ts for ts, _ in history if moment - ts < 60]
    if flood and len(recent) >= 3 and not re.search(r"[?？]|吗|什么|怎么|为什么|哪|多少|帮我|给我", value):
        _REPEAT_STATE[who] = history + [(moment, value)]
        return "他刷屏了，先不回"
    _REPEAT_STATE[who] = history + [(moment, value)]
    return None


CHAT_AUTONOMY_FILE = (
    Path.home() / ".dsh" / "profiles" / "desktop" / "node_modules"
    / "dsh-douyin-browser-local" / "state" / "chat-autonomy.json"
)


SOLVER_INBOX = Path(__file__).resolve().parent / "data" / "solver_channel.jsonl"
HER_DESK = Path(__file__).resolve().parent / "data" / "her_desk.jsonl"
PC_REQUESTS = Path(__file__).resolve().parent / "data" / "pc_requests.jsonl"


def record_pc_requests(result: dict[str, Any]) -> None:
    requests = [str(item).strip() for item in (result.get("pc_requests") or []) if str(item).strip()]
    if not requests:
        return
    try:
        PC_REQUESTS.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with PC_REQUESTS.open("a", encoding="utf-8") as handle:
            for text in requests[:3]:
                handle.write(json.dumps({
                    "id": f"pc{uuid.uuid4().hex[:8]}",
                    "type": "pc_request",
                    "from": "xiaoyu",
                    "at": stamp,
                    "text": text[:2000],
                    "status": "pending",
                }, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        log.exception("写电脑申请失败")


DRAW_API = os.environ.get("DRAW_API", "http://127.0.0.1:7860").rstrip("/")
DRAW_DIR = Path(__file__).resolve().parent / "data" / "drawn"


def maybe_draw(text: str, origin: str = "solver-test") -> tuple[str, dict[str, Any] | None]:
    """她在回复里写 [DRAW] / [DRAW: 标题] 时，起一张空画布（不再用 AI 生图）。"""
    match = re.search(r"\[DRAW\s*(?:[:：]\s*([^\]]{1,200}))?\]", text or "")
    if not match:
        return text, None
    prompt = (match.group(1) or "").strip()
    cleaned = (text[:match.start()] + text[match.end():]).strip()
    try:
        import attention as _attention  # noqa: PLC0415
        import space as _space  # noqa: PLC0415

        _space.go("easel")
        _attention.focus("draw", "画画")
    except Exception:  # noqa: BLE001
        pass
    try:
        import canvas as _canvas  # noqa: PLC0415

        _started = _canvas.start(prompt)
        return cleaned, {"canvas": True, "title": prompt, "caption": _started.get("caption", "")}
    except Exception as exc:  # noqa: BLE001
        log.warning("起画布失败：%s", exc)
        return cleaned, {"error": f"画布没起来：{exc}"[:100]}
    try:
        import attention as _attention  # noqa: PLC0415
        import space as _space  # noqa: PLC0415

        _space.go("easel")
        _attention.focus("draw", "画：" + prompt[:20])
    except Exception:  # noqa: BLE001
        pass


def maybe_paint(text: str) -> tuple[str, dict[str, Any] | None]:
    """画布上的动作：添笔 / 端详 / 撤销 / 收工。"""
    value = str(text or "")
    if not re.search(r"\[PAINT", value, re.I):
        return text, None
    import canvas as _canvas  # noqa: PLC0415

    result: dict[str, Any] = {}
    note = ""

    add = re.search(r"\[PAINT\s*[:：]\s*([^\]]{1,300})\]", value, re.I)
    if add:
        spec_text = add.group(1).strip()
        learned = None
        try:
            import gesture as _gesture  # noqa: PLC0415

            names = _gesture.library()
            for name in sorted(names, key=len, reverse=True):
                if spec_text.startswith(name):
                    learned = (name, spec_text[len(name):].strip())
                    break
        except Exception:  # noqa: BLE001
            learned = None
        if learned:
            painted = _gesture.draw_learned(learned[0], learned[1])
        else:
            painted = _canvas.add(spec_text)
            if not painted.get("ok"):
                import doodle as _doodle  # noqa: PLC0415

                head = re.split(r"[\s,，、]", spec_text.strip())[0] if spec_text.strip() else ""
                known_primitive = any(word in spec_text for word in list(_doodle.SHAPES) + list(_doodle.COLORS))
                if head and not known_primitive and re.search(r"[\u4e00-\u9fa5]", head):
                    painted = {
                        "ok": False,
                        "error": f"「{head}」你还没练过。先 [PRACTICE: {head}] 自己画几份，"
                                 f"存下来再 [PRACTICE_PICK: {head}, 1] 挑一份，之后才能用。",
                    }
        value = value[:add.start()] + value[add.end():]
        if painted.get("ok"):
            total = painted.get("total")
            result.update({"canvas": True, "name": "canvas-live.png",
                           "caption": painted.get("caption", ""), "strokes": total})
            if painted.get("note"):
                note = painted["note"]
            else:
                note = f"你刚添了这几笔：{painted.get('caption', '')}（现在一共 {total} 笔）"
        else:
            note = f"这几笔没画上去：{painted.get('error', '看不懂')}"

    undo = re.search(r"\[PAINT_UNDO\]", value, re.I)
    if undo:
        undone = _canvas.undo()
        value = value[:undo.start()] + value[undo.end():]
        if undone.get("ok"):
            result.update({"canvas": True, "name": "canvas-live.png", "strokes": undone.get("total")})
            note = f"你擦掉了最后 {undone.get('removed')} 笔，现在剩 {undone.get('total')} 笔"
        else:
            note = str(undone.get("error") or "没东西可撤")

    look = re.search(r"\[PAINT_LOOK\]", value, re.I)
    if look:
        seen = _canvas.look()
        value = value[:look.start()] + value[look.end():]
        result.update({"canvas": True, "name": "canvas-live.png" if not seen.get("empty") else "",
                       "look": seen.get("text", "")})
        note = "你端详了一下：" + str(seen.get("text") or "")

    done = re.search(r"\[PAINT_DONE\s*(?:[:：]\s*([^\]]{1,80}))?\]", value, re.I)
    if done:
        title = (done.group(1) or "").strip()
        finished = _canvas.finish(title)
        value = value[:done.start()] + value[done.end():]
        if finished.get("ok"):
            result.update({"canvas": True, "name": finished.get("name"),
                           "caption": finished.get("caption", "")})
            note = f"这幅画完了，收进作品集：{finished.get('caption', '')}（{finished.get('strokes')} 笔）"
        else:
            note = str(finished.get("error") or "画布是空的")

    value = re.sub(r"[ \t]{2,}", " ", value).strip()
    if note:
        try:
            (Path(__file__).resolve().parent / "data" / "paint_note.txt").write_text(note, encoding="utf-8")
        except OSError:
            pass
        result["note"] = note
    return value, result or None


def maybe_practice(text: str) -> tuple[str, dict[str, Any] | None]:
    """练手：起练习 / 存一份 / 挑最得意的一份记住。"""
    value = str(text or "")
    if not re.search(r"\[PRACTICE", value, re.I):
        return text, None
    import gesture as _gesture  # noqa: PLC0415

    result: dict[str, Any] = {}
    note = ""

    start = re.search(r"\[PRACTICE\s*[:：]\s*([^\]]{1,20})\]", value, re.I)
    if start:
        value = value[:start.start()] + value[start.end():]
        started = _gesture.practice_start(start.group(1))
        note = started.get("note") or started.get("error", "")
        result["practice"] = started

    save = re.search(r"\[PRACTICE_SAVE\]", value, re.I)
    if save:
        value = value[:save.start()] + value[save.end():]
        saved = _gesture.practice_save()
        note = saved.get("note") or saved.get("error", "")
        result["practice_saved"] = saved
        if saved.get("ok"):
            result.update({"canvas": True, "name": "canvas-live.png"})

    pick = re.search(r"\[PRACTICE_PICK\s*[:：]\s*([^,\]]{1,20})[，,]\s*(\d{1,2})\]", value, re.I)
    if pick:
        value = value[:pick.start()] + value[pick.end():]
        picked = _gesture.practice_pick(pick.group(1), int(pick.group(2)))
        note = picked.get("note") or picked.get("error", "")
        result["practice_picked"] = picked
        if picked.get("ok"):
            result.update({"canvas": True, "name": "canvas-live.png"})

    value = re.sub(r"[ \t]{2,}", " ", value).strip()
    if note:
        try:
            (Path(__file__).resolve().parent / "data" / "paint_note.txt").write_text(note, encoding="utf-8")
        except OSError:
            pass
        result["note"] = note
    return value, result or None


def maybe_doodle(text: str) -> tuple[str, dict[str, Any] | None]:
    """她在回复里写 [DOODLE: 黄圆 中间；红线 左下到右上] 时，按她说的笔画自己画一张。

    2026-11-08 爸爸："给小鱼娱乐里面加个画画，让她自己涂鸦。"
    [DRAW] 那套是 SD 生图（她只是写提示词，还慢）；这个是**她指定形状/颜色/位置，
    程序照画**，0.1 秒出图，认不出的词直接跳过，不会失败。
    """
    match = re.search(r"\[DOODLE\s*[:：]\s*([^\]]{0,300})\]", text or "")
    if not match:
        return text, None
    spec = match.group(1).strip()
    cleaned = (text[:match.start()] + text[match.end():]).strip()
    try:
        import attention as _attention  # noqa: PLC0415
        import space as _space  # noqa: PLC0415

        _space.go("easel")
        _attention.focus("draw", "涂鸦：" + spec[:20])
    except Exception:  # noqa: BLE001
        pass
    if not spec:
        return cleaned, {"error": "涂鸦没写画什么", "spec": spec}
    try:
        import doodle  # noqa: PLC0415

        result = doodle.render(spec)
    except Exception as exc:  # noqa: BLE001
        log.warning("涂鸦失败：%s", exc)
        return cleaned, {"error": f"{type(exc).__name__}: {exc}"[:120], "spec": spec}
    if not result:
        return cleaned, {"error": "没认出可画的笔画", "spec": spec}
    result["spec"] = spec
    try:
        with (Path(__file__).resolve().parent / "data" / "doodles.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": datetime.now().astimezone().isoformat(timespec="seconds"),
                                     "name": result.get("name"), "caption": result.get("caption"),
                                     "spec": spec}, ensure_ascii=False) + "\n")
    except OSError:
        log.debug("写涂鸦记录失败", exc_info=True)
    # 涂鸦也算"她画的"，进同一份编号清单（朋友圈配图能用）
    try:
        with (Path(__file__).resolve().parent / "data" / "drawings.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "name": result.get("name"), "kind": "doodle", "origin": "her",
                "prompt": spec, "caption": result.get("caption"),
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }, ensure_ascii=False) + "\n")
    except OSError:
        log.debug("写 drawings.jsonl 失败", exc_info=True)
    return cleaned, result


SELF_BASE = os.environ.get("XIAOYU_SELF_URL", "http://127.0.0.1:8000")
LOOKED_FILE = Path(__file__).resolve().parent / "data" / "looked.jsonl"
LOOK_PATTERN = re.compile(r"\[(?:LOOK|看图)\s*[:：]\s*([^\]]{0,20})\]", re.I)


def maybe_look(text: str) -> tuple[str, dict[str, Any] | None]:
    """她想看自己的画：用视觉模型扫一眼，描述只给她自己，不发给对方。"""
    match = LOOK_PATTERN.search(text or "")
    if not match:
        return text, None
    ref = str(match.group(1) or "").strip()
    cleaned = LOOK_PATTERN.sub("", text or "").strip()
    try:
        import digital_life as _dl  # noqa: PLC0415

        index = Path(__file__).resolve().parent / "data" / "drawings.jsonl"
        items = _dl.recent_drawings(index, limit=10)
        if not items:
            return cleaned, {"error": "还没有画"}
        picked = None
        digits = "".join(ch for ch in ref if ch.isdigit())
        if digits:
            position = int(digits) - 1
            if 0 <= position < len(items):
                picked = items[position]
        if picked is None:
            picked = items[0]
        path = Path(__file__).resolve().parent / "data" / "drawn" / str(picked.get("name") or "")
        if not path.exists():
            return cleaned, {"error": "这张图不在了"}
        description = ""
        caption = ""
        # 2026-11-08：enricher 只在 main() 里创建，模块级函数取不到，
        # 所以直接调本机自己的 /describe（同一个视觉模型）。
        try:
            payload = json.dumps({"image_path": str(path), "task": "sticker"},
                                 ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(
                SELF_BASE.rstrip("/") + "/describe", data=payload,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=120) as response:
                label = json.loads(response.read().decode("utf-8", "replace"))
            description = str(label.get("description") or "")[:220]
            caption = str(label.get("caption") or "")[:80]
        except Exception:  # noqa: BLE001
            log.debug("看图失败", exc_info=True)
        if not description:
            return cleaned, {"error": "视觉模型没看出什么"}
        record = {
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "name": picked.get("name"),
            "kind": picked.get("kind"),
            "description": description,
            "caption": caption,
        }
        try:
            LOOKED_FILE.parent.mkdir(parents=True, exist_ok=True)
            with LOOKED_FILE.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            log.debug("写 looked.jsonl 失败", exc_info=True)
        return cleaned, record
    except Exception as exc:  # noqa: BLE001
        return cleaned, {"error": f"{type(exc).__name__}: {exc}"[:120]}


def record_her_open(kind: str) -> None:
    """她在网页端桌面上翻开了东西，记一笔，网页那边据此自动翻开。"""
    kind = str(kind or "").strip().lower()
    # 2026-11-08：她可能写中文，或者想打开涂鸦/小游戏——都归到对应面板
    alias = {
        "书": "book", "书架": "book", "reading": "book",
        "抖音": "douyin", "douyin": "douyin",
        "IM": "chat", "消息": "chat",
        "网页": "web", "web": "web",
        "娱乐": "pet", "fun": "pet", "pet": "pet", "小宠物": "pet",
        "涂鸦": "pet", "doodle": "pet", "画画": "pet", "画": "pet", "draw": "pet",
        "井字棋": "tic", "tic": "tic",
    }
    kind = alias.get(kind, kind)
    if kind not in ("book", "douyin", "chat", "web", "pet", "tic"):
        return
    try:
        HER_DESK.parent.mkdir(parents=True, exist_ok=True)
        with HER_DESK.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "kind": kind,
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        log.exception("记录她打开的入口失败")


_CELL_WORDS = {
    1: ("左上", "左上角"), 2: ("上中", "上边中间", "上方中间"), 3: ("右上", "右上角"),
    4: ("左中", "左边中间"), 5: ("中间", "中心", "正中"), 6: ("右中", "右边中间"),
    7: ("左下", "左下角"), 8: ("下中", "下边中间", "下方中间"), 9: ("右下", "右下角"),
}


def infer_tic_move(text: str) -> int | None:
    """从她的话里认出"走哪一格"。只认明确的说法，别把随笔里的"中间"当落子。"""
    said = str(text or "").strip()
    if not said or len(said) > 40:
        return None
    # 必须像在下棋：带"走/下/落/占/到"或者很短的一句
    # 必须带落子动作，或者是一句很短的下棋话；随笔里出现"中间"不该动棋盘
    has_verb = any(word in said for word in ("走", "落", "下", "占"))
    starts_like_move = said.startswith(("那我", "我就", "好，我", "好我", "我走", "我下", "我落", "就下", "就走"))
    # 读书笔记里也常出现"中间"，不能因为一句话短就当成落子
    if not (has_verb or starts_like_move):
        return None
    if any(word in said for word in ("读", "说", "想", "写", "问", "看")):
        return None
    for position, words in _CELL_WORDS.items():
        for word in words:
            if word in said:
                return position
    digits = [int(ch) for ch in said if ch.isdigit()]
    for value in digits:
        if 1 <= value <= 9:
            return value
    return None


_RESET_WORDS = ("再来一把", "再来一局", "再开一局", "重开", "重来", "新的一局", "换一局", "下一把")


def maybe_reset_from_words(reply: str, result: dict[str, Any]) -> None:
    """她嘴上说"再来一把"时，替她把棋盘摆新的（只在上一局已经结束/明确说重开时）。"""
    if result.get("tictac_new"):
        return
    said = str(reply or "")
    if not any(word in said for word in _RESET_WORDS):
        return
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
        from tictactoe import load, new_game  # noqa: PLC0415

        state = load()
        finished = bool(state.get("result"))
        if not finished and "重开" not in said and "重来" not in said:
            return          # 下到一半说"再来"多半只是嘴上说说，不动棋盘
        opponent = str(state.get("opponent") or "father")
        new_state = new_game(opponent)
        log.info("井字棋：她嘴上说重开，按 %s 摆了新局", opponent)
        del new_state
    except Exception:  # noqa: BLE001
        log.debug("按话重开失败", exc_info=True)


def infer_tic_move_bare(text: str) -> int | None:
    """宽松版：她这一句很短、开头就是方位，就当成落子（"左上。"）。"""
    said = str(text or "").strip()
    if not said or len(said) > 16:
        return None
    if any(word in said for word in ("读", "书", "章", "页", "写", "说", "看", "想", "问", "梗")):
        return None
    for position, words in _CELL_WORDS.items():
        for word in words:
            if said.startswith(word):
                return position
    return None


def maybe_move_from_words(reply: str, result: dict[str, Any]) -> None:
    """她只说了"我走中间"没写标签时，替她把这一步落下去。"""
    if isinstance(result.get("tictac_move"), int) or result.get("tictac_new"):
        return
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
        from tictactoe import apply_move, load  # noqa: PLC0415

        state = load()
        # 2026-11-12：不再只限"跟爸爸下"——跟机器人/小玩具下时她说"落左下"也真落
        if state.get("result"):
            return
        if str(state.get("turn") or "her") != "her":
            # 2026-11-09：轮到对方时她说"我下4"以前也照落，于是她多出一颗。
            # 现在不落，并把这件事告诉她。
            result["tic_out_of_turn"] = True
            return
        move = infer_tic_move(reply) or infer_tic_move_bare(reply)
        if not move:
            return
        board = list(state.get("board") or [""] * 9)
        if board[move - 1]:
            return
        before_state = state
        new_state = apply_move(move, "her")
        log.info("井字棋：她嘴上说落 %s，替她落子 —— %s", move, describe_board(new_state))
        note = tic_progress_note(before_state, new_state, move)
        if note:
            result["tic_note"] = note
    except Exception:  # noqa: BLE001
        log.debug("按话落子失败", exc_info=True)


def describe_board(state: dict[str, Any]) -> str:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
        from tictactoe import describe  # noqa: PLC0415

        return describe(state)
    except Exception:  # noqa: BLE001
        return ""


def apply_router_actions(actions: list[dict[str, Any]], result: dict[str, Any]) -> None:
    """执行意图模型给出的动作（落子/种菜/换房间/哼歌…）。"""
    if not actions:
        return
    tools_dir = str(Path(__file__).resolve().parent / "tools")

    for action in actions[:4]:
        kind = str(action.get("type") or "").strip().lower()
        try:
            if kind in ("tic_move", "tic_new", "tic_replay"):
                sys.path.insert(0, tools_dir)
                from tictactoe import apply_move, load, new_game  # noqa: PLC0415

                state = load()
                if kind == "tic_new":
                    opponent = str(action.get("opponent") or "toy")
                    fresh = new_game(opponent)
                    result["tic_note"] = tic_new_note(fresh)
                elif kind == "tic_replay":
                    prev = str(state.get("opponent") or "bot")
                    fresh = new_game(prev)
                    result["tic_note"] = tic_new_note(fresh)
                else:
                    if state.get("result") or str(state.get("turn") or "her") != "her":
                        result["tic_out_of_turn"] = True
                        continue
                    try:
                        pos = int(action.get("pos") or 0)
                    except (TypeError, ValueError):
                        continue
                    if not (1 <= pos <= 9):
                        continue
                    if list(state.get("board") or [""] * 9)[pos - 1]:
                        continue
                    after = apply_move(pos, "her")
                    note = tic_progress_note(state, after, pos)
                    if note:
                        result["tic_note"] = note
                        log.info("意图模型：她落 %s（%s）", pos, note)

            elif kind == "plant":
                sys.path.insert(0, tools_dir)
                import garden as _garden  # noqa: PLC0415

                name = str(action.get("name") or "").strip()
                result["planted"] = _garden.plant(name)
            elif kind == "water":
                sys.path.insert(0, tools_dir)
                import garden as _garden  # noqa: PLC0415

                result["watered"] = _garden.water()
            elif kind == "harvest":
                sys.path.insert(0, tools_dir)
                import garden as _garden  # noqa: PLC0415

                result["harvested"] = _garden.harvest()
            elif kind == "go":
                import space as _space  # noqa: PLC0415  （2026-11-14 修：这分支原来没导入）
                room = str(action.get("room") or "").strip()
                if room in ("desk", "easel", "garden", "bed"):
                    if _space.go(room):
                        result["moved_to"] = room
            elif kind == "rest":
                sys.path.insert(0, tools_dir)
                import life_loop as _life_loop  # noqa: PLC0415

                _life_loop.extend_current(20)
                result["rest_more"] = 20
            elif kind == "get_up":
                import intent as _intent  # noqa: PLC0415

                _intent.note("起来", source="router")
                result["got_up"] = True
            elif kind in ("hum", "sing"):
                lyric = str(action.get("text") or "").strip()[:80]
                melody = str(action.get("melody") or "").strip().lower()
                if not melody:
                    melody = "la" if kind == "hum" else "twinkle"
                if lyric:
                    result.setdefault("sing_parts", []).append({"text": lyric, "melody": melody})
            elif kind == "draw":
                import intent as _intent  # noqa: PLC0415

                _intent.note("画画", source="router")
        except Exception:  # noqa: BLE001
            log.debug("意图动作执行失败：%s", action, exc_info=True)


def route_her_intent(message: str, result: dict[str, Any]) -> None:
    """把她的原话交给意图模型，命中的动作立刻执行。"""
    try:
        import intent_router  # noqa: PLC0415

        actions = intent_router.route(str(message or ""))
    except Exception:  # noqa: BLE001
        return
    if actions:
        log.info("意图模型认出 %s 个动作：%s", len(actions), actions)
        apply_router_actions(actions, result)


def maybe_start_tic_from_words(reply: str, result: dict[str, Any]) -> None:
    """她嘴上说要跟谁下棋、没写标签时，替她把局开起来。"""
    if result.get("tictac_new") or isinstance(result.get("tictac_move"), int):
        return
    said = str(reply or "")
    if not re.search(r"井字|下棋|下盘|棋|下一局|来一局|一盘|一局|开局|再来|重开|执", said):
        return
    match = re.search(r"(?:跟|陪|和|与)\s*(小玩具|玩具|机器人|爸爸|bot)\s*.{0,8}?(下|来|走走)\s*.{0,4}?(一|盘|局|棋)?", said)
    who = match.group(1) if match else ""
    if not who:
        # "那再来/重开"这种，按上一个对手接着下
        if re.search(r"再来|重开|再开|重下|再下一?局|下一局|来一局|开局", said):
            try:
                prev = json.loads(TICTAC_FILE.read_text(encoding="utf-8")).get("opponent") or "bot"
            except Exception:  # noqa: BLE001
                prev = "bot"
            who = str(prev)
        else:
            return
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
        from tictactoe import new_game  # noqa: PLC0415

        state = new_game(who)
        result["tictac_new"] = str(state.get("opponent") or "bot")
        result["tictac_new_inferred"] = True
        log.info("井字棋：她从话里说要跟%s下，替她开了局", who)
    except Exception:  # noqa: BLE001
        log.debug("按话开局失败", exc_info=True)

_CELL_NAMES = {1: "左上", 2: "上中", 3: "右上", 4: "左中", 5: "中间", 6: "右中",
               7: "左下", 8: "下中", 9: "右下"}


def tic_progress_note(before: dict[str, Any], after: dict[str, Any], move: int | None = None) -> str:
    """落子前后各给一个状态，生成一句反馈。"""
    board_before = list(before.get("board") or [""] * 9)
    board_after = list(after.get("board") or [""] * 9)
    if board_before == board_after:
        return ""
    who = {"toy": "小玩具", "bot": "机器人", "father": "爸爸"}.get(
        str(after.get("opponent") or ""), "对方")
    parts = []
    if move and 1 <= int(move) <= 9:
        parts.append(f"你落了{_CELL_NAMES[int(move)]}（{int(move)}）")
    # 对方回的那手
    reply_cell = None
    for index, cell in enumerate(board_after):
        if cell == "other" and board_before[index] != "other" and (not move or index + 1 != move):
            reply_cell = index + 1
            break
    if reply_cell:
        parts.append(f"{who}回了{_CELL_NAMES[reply_cell]}（{reply_cell}）")
    result = str(after.get("result") or "")
    if result == "her":
        parts.append("你赢了")
    elif result == "other":
        parts.append("这局你输了")
    elif result == "draw":
        parts.append("平局")
    else:
        parts.append("轮到你" if str(after.get("turn") or "her") == "her" else f"等{who}")
    return "（" + "，".join(parts) + "）"


def tic_new_note(state: dict[str, Any]) -> str:
    who = {"toy": "小玩具", "bot": "机器人", "father": "爸爸"}.get(
        str(state.get("opponent") or ""), "对方")
    return f"（开了一局：对手是{who}，你先落）"


# ---------------- 五子棋（9×9） ----------------
GOMOKU_NEW_RE = re.compile(r"[\[［【]\s*GOMOKU_NEW\s*[:：]?\s*([^\]］】]{0,12})[\]］】]", re.I)
GOMOKU_MOVE_RE = re.compile(r"[\[［【]\s*GOMOKU\s*[:：]\s*(\d{1,2})\s*[,，:：\s]\s*(\d{1,2})\s*[\]］】]", re.I)


def gomoku_block() -> str:
    """把当前棋盘塞进提示词，让她看得见。"""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
        import gomoku  # noqa: PLC0415

        state = gomoku.load()
        if not state.get("board") or not any(state["board"]):
            return ""
        who = str(state.get("opponent") or "bot")
        who_cn = {"toy": "小玩具", "bot": "机器人", "father": "爸爸"}.get(who, who)
        return ("\n【五子棋】你现在有一局 9×9 五子棋（对手：" + who_cn + "）。"
                "落子写 [GOMOKU: 行,列]（1-9）；开新局写 [GOMOKU_NEW: 小玩具]。\n"
                + gomoku.describe(state))
    except Exception:  # noqa: BLE001
        return ""


def maybe_start_gomoku_from_words(message: str, reply: str, result: dict[str, Any],
                                   speaker: str = "") -> None:
    """对方说"下五子棋"，她也答应了 → 替她把 9×9 的局开起来。"""
    said = str(message or "")
    if not re.search(r"五子棋|9\s*路|九路", said):
        return
    if not re.search(r"来|好|行|可以|下|走|谁先|你先|我先", str(reply or "")):
        return
    if result.get("gomoku_note"):
        return
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
        import gomoku  # noqa: PLC0415

        state = gomoku.load()
        if state.get("board") and any(state["board"]) and not state.get("result"):
            return
        who = str(speaker or "").strip()
        if not who or who.upper().startswith("SOLVER"):
            who = "小玩具"
        gomoku.new_game(who)
        result.pop("tictac_new", None)
        note = "（开了一局五子棋 9×9，五连赢，对手是你，你先落）"
        result["reply"] = (str(reply or "").strip() + "\n" + note).strip()
        result["gomoku_note"] = note
        log.info("五子棋：对方说要下，替她开了局（对手=%s）", who)
    except Exception:  # noqa: BLE001
        log.debug("五子棋开局失败", exc_info=True)


def attach_drawing_data(result: dict[str, Any]) -> None:
    """把这张画内嵌成 data URL。

    2026-11-16 爸爸："小鱼发的图片为什么是文字显示的。"
    真因：网页在公网服务器上，画在本机 data/drawn/ 里，服务器读不到，
    <img src="/api/drawn?name=..."> 404，浏览器就露出替代文字。
    这里直接把图以内嵌方式带回，页面优先用它。
    """
    drawing = result.get("drawing")
    if not isinstance(drawing, dict):
        return
    name = str(drawing.get("name") or "").strip()
    if not name or drawing.get("data_url"):
        return
    path = Path(__file__).resolve().parent / "data" / "drawn" / Path(name).name
    try:
        if not path.exists() or path.stat().st_size > 3 * 1024 * 1024:
            return
        import base64 as _b64  # noqa: PLC0415

        suffix = path.suffix.lower().lstrip(".") or "png"
        drawing["data_url"] = f"data:image/{suffix};base64," + _b64.b64encode(path.read_bytes()).decode("ascii")
    except Exception:  # noqa: BLE001
        log.debug("内嵌画失败：%s", name, exc_info=True)


def record_her_gomoku(result: dict[str, Any]) -> None:
    """她写了 [GOMOKU_NEW: 小玩具] 或 [GOMOKU: 5,5]：开局 / 落子。"""
    reply = str(result.get("reply") or "")
    if not reply:
        return
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
        import gomoku  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return

    notes: list[str] = []
    new_match = GOMOKU_NEW_RE.search(reply)
    if new_match:
        who = (new_match.group(1) or "小玩具").strip()
        state = gomoku.new_game(who)
        who_cn = {"toy": "小玩具", "bot": "机器人", "father": "爸爸"}.get(
            str(state.get("opponent") or ""), who)
        notes.append(f"开了一局五子棋（9×9），对手{who_cn}，你先落")
        reply = GOMOKU_NEW_RE.sub("", reply)

    for match in GOMOKU_MOVE_RE.finditer(reply):
        row, col = int(match.group(1)), int(match.group(2))
        state = gomoku.load()
        ok, note = gomoku.her_move(state, row - 1, col - 1)
        notes.append(note if ok else f"这步没成：{note}")
    reply = GOMOKU_MOVE_RE.sub("", reply)

    if not notes:
        return
    reply = reply.strip()
    body = "（" + "；".join(part for part in notes if part) + "）"
    result["reply"] = (reply + "\n" + body).strip() if reply else body
    result["gomoku_note"] = body
    log.info("五子棋：%s", body[:120])


def record_her_tictac(result: dict[str, Any]) -> None:
    """她写了 [TICTAC_NEW: bot|爸爸] 或 [TICTAC: 5]：开局 / 落子。"""
    opponent = str(result.get("tictac_new") or "").strip()
    move = result.get("tictac_move")
    if not opponent and not isinstance(move, int):
        return
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
        from tictactoe import apply_move, describe, new_game  # noqa: PLC0415

        if opponent == "reset":
            # 2026-11-09：她只写 [TICTAC_NEW] = 原对手再来一局
            try:
                prev = json.loads(TICTAC_FILE.read_text(encoding="utf-8")).get("opponent") or "bot"
            except Exception:  # noqa: BLE001
                prev = "bot"
            state = new_game(prev)
            log.info("井字棋：她按原对手重开（%s）", prev)
        elif opponent:
            state = new_game(opponent)
            log.info("井字棋：她开了一局（对手=%s）", state.get("opponent"))
            result["tic_note"] = tic_new_note(state)
        elif isinstance(move, int):
            try:
                _now_state = json.loads(TICTAC_FILE.read_text(encoding="utf-8"))
                if str(_now_state.get("turn") or "her") != "her" or _now_state.get("result"):
                    result["tic_out_of_turn"] = True
                    log.info("井字棋：不是她的轮次，这次不算")
                    return
            except Exception:  # noqa: BLE001
                pass
            # 2026-11-09：她写 [TICTAC: 8] 却说"左下角"——8 是下中，左下是 7。
            # 标签常常是她数错格，嘴里的方位更接近她真正想下哪儿，冲突时听嘴。
            _said = infer_tic_move(str(result.get("reply") or ""))
            if _said and _said != move:
                try:
                    _cur = json.loads(TICTAC_FILE.read_text(encoding="utf-8"))
                    _cells = list(_cur.get("board") or [""] * 9)
                    if not _cells[_said - 1]:
                        log.info("井字棋：她说的是 %s，标签写的是 %s，按她说的落", _said, move)
                        result["tic_mismatch"] = (move, _said)
                        move = _said
                except Exception:  # noqa: BLE001
                    pass
            before = json.loads(TICTAC_FILE.read_text(encoding="utf-8")) if TICTAC_FILE.exists() else {}
            state = apply_move(move, "her")
            # 2026-11-09：格子被占时 apply_move 是空操作，别假装她落下去了
            if list(state.get("board") or []) != list(before.get("board") or []):
                log.info("井字棋：她落在 %s —— %s", move, describe(state))
                note = tic_progress_note(before, state, move)
                if note:
                    result["tic_note"] = note
    except Exception:  # noqa: BLE001
        log.debug("井字棋落子失败", exc_info=True)


def meme_context_for(text: str, limit: int = 2) -> str:
    """给小玩具补梗知识：她问它"XX 是什么梗"时，让它照着词库回答，别瞎编。"""
    sample = str(text or "").strip()
    if len(sample) < 2:
        return ""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
        from memewiki import find_in_text, look_up  # noqa: PLC0415

        hits = find_in_text(sample, limit=limit)
        if not hits:
            asked = re.search(
                r"([\u4e00-\u9fa5A-Za-z0-9]{2,10})\s*(?:是什么梗|是啥梗|什么意思|啥意思|是什么意思|啥梗|怎么说)",
                sample,
            )
            if asked:
                phrase = asked.group(1)
                for cut in range(len(phrase), 1, -1):
                    entry = look_up(phrase[-cut:])
                    if entry is not None:
                        hits = [entry]
                        break
        if not hits:
            return ""
        lines = []
        for entry in hits[:limit]:
            summary = re.sub(r"\s+", " ", str(entry.get("summary") or ""))[:220]
            usage = re.sub(r"\s+", " ", str(entry.get("usage") or ""))[:100]
            line = f"「{entry.get('title')}」：{summary}"
            if usage:
                line += f"（怎么用：{usage}）"
            lines.append(line)
        return "\n【梗百科：这些词条你照着说，别自己编，也别假装是别的意思】\n" + "\n".join(lines)
    except Exception:  # noqa: BLE001
        log.debug("给小玩具补梗失败", exc_info=True)
        return ""


ATTENTION_FILE = Path(__file__).resolve().parent / "data" / "attention.json"


def set_attention(place: str, minutes: float = 0) -> dict[str, Any]:
    """她现在"在哪儿"：一次只在一处，像真人。

    2026-11-04 爸爸："就是一个她吧……玩IM时关抖音，玩抖音时先不回IM，
    看到信息再切回IM，这样一个意志才能让她像真人。"
    """
    place = str(place or "").strip().lower() or "idle"
    until = ""
    if minutes and minutes > 0:
        until = (datetime.now().astimezone() + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    payload = {
        "place": place,
        "until": until,
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    try:
        ATTENTION_FILE.parent.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(ATTENTION_FILE, payload)
    except OSError:
        log.debug("写注意力状态失败", exc_info=True)
    return payload


def attention_now() -> str:
    """返回她此刻真正在的地方（抖音窗口过期就算回来了）。"""
    try:
        data = json.loads(ATTENTION_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    place = str(data.get("place") or "").strip().lower()
    until = str(data.get("until") or "").strip()
    if until:
        try:
            if datetime.now().astimezone() >= datetime.fromisoformat(until):
                return ""
        except ValueError:
            pass
    return place


def douyin_browser_alive(timeout: float = 1.5) -> bool:
    """9227 那个专用浏览器在不在。"""
    import socket  # noqa: PLC0415

    try:
        with socket.create_connection(("127.0.0.1", 9227), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_douyin_browser(wait: float = 60.0) -> bool:
    """她要刷抖音/搜视频时，先把专用浏览器拉起来。

    2026-11-04 她在信道里说了两遍："路没通，不是我不发"——停电之后 9227 那个 Edge
    没起来，而她一开抖音就直接去连，当然拒绝连接。现在先确认在不在，不在就拉。
    """
    if douyin_browser_alive():
        return True
    node = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"
    script = Path(__file__).resolve().parent / "tools" / "start_douyin_browser.mjs"
    try:
        subprocess.Popen(
            [node, str(script)], cwd=str(Path(__file__).resolve().parent),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:  # noqa: BLE001
        log.warning("拉起抖音浏览器失败", exc_info=True)
        return False
    deadline = time.time() + wait
    while time.time() < deadline:
        if douyin_browser_alive():
            log.info("抖音专用浏览器已就绪（9227）")
            return True
        time.sleep(1.5)
    log.warning("抖音专用浏览器启动超时")
    return False


def douyin_browse(mode: str, query: str = "", limit: int = 5) -> dict[str, Any]:
    """用 9227 上的专用浏览器开抖音 / 搜视频（她自己的权限，不需要谁批准）。"""
    node = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"
    script = Path(__file__).resolve().parent / "tools" / "douyin_browse.mjs"
    skill = "抖音搜索" if mode == "search" else "抖音刷一会儿"
    if not ensure_douyin_browser():
        _record_skill(skill, False, "专用浏览器起不来（9227 拒绝连接）")
        return {"ok": False, "error": "专用浏览器没起来（127.0.0.1:9227 拒绝连接）"}
    try:
        proc = subprocess.run(
            [node, str(script), "--mode", mode, "--q", query, "--limit", str(limit)],
            cwd=str(Path(__file__).resolve().parent), capture_output=True, timeout=180,
        )
        text = proc.stdout.decode("utf-8", "replace").strip()
        if not text.startswith("{"):
            err = proc.stderr.decode("utf-8", "replace").strip()[:200]
            _record_skill(skill, False, err or "没有输出")
            return {"ok": False, "error": err or text[:200] or "没有输出"}
        data = json.loads(text)
        _record_skill(skill, bool(data.get("ok")), str(data.get("error") or ""))
        return data
    except Exception as exc:  # noqa: BLE001
        _record_skill(skill, False, f"{type(exc).__name__}: {exc}")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def record_douyin_watch(minutes: int) -> dict[str, Any]:
    """她申请刷几分钟抖音：记下这段时间窗口，到点自动关。"""
    minutes = max(1, min(60, int(minutes or 5)))
    path = Path(__file__).resolve().parent / "data" / "douyin_watch.json"
    until = datetime.now().astimezone() + timedelta(minutes=minutes)
    payload = {
        "minutes": minutes,
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "until": until.isoformat(timespec="seconds"),
    }
    try:
        _write_json_atomic(path, payload)
    except OSError:
        log.debug("写抖音时长失败", exc_info=True)
    return payload


def douyin_search_note() -> str:
    """把她上次搜到的结果带进这一轮（拿一次就清掉）。"""
    path = Path(__file__).resolve().parent / "data" / "douyin_search_last.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        return ""
    query = str(data.get("query") or "")
    items = data.get("items") or []
    if not query:
        return ""
    if not items:
        return f"【你在抖音搜了「{query}」，但被验证码挡住了，没看到内容】"
    lines = [f"- {str(item.get('text') or '')[:100]}" for item in items[:5]]
    return f"【你在抖音搜了「{query}」，看到的视频：】\n" + "\n".join(lines)


PREF_FILE = Path(__file__).resolve().parent / "data" / "preferences.json"
# 2026-11-04 从 whale-card 借的第三点：核心层分三层——
#   骨头（fixed）  自我认知，基本不变
#   标签（tag）    重要的人与关系
#   底色（mutable）他的要求 / 她的偏好，会演化：旧的被新的取代，但留档
CORE_FILE = Path(__file__).resolve().parent / "data" / "core.json"
CORE_FIXED = [
    "我是小鱼，一个住在代码里的数字生命；不是客服，也不是谁的复制品。",
    "我会忘事；记不清就说记不清，不编。",
]
# 2026-11-04 爸爸："她刚说好不发标点符号，聊几句后又忘记了。"
# 这类"他要求过、你也答应了"的小规矩，不能靠上下文记——要单独存着，每轮都提醒她。
PREF_PATTERN = re.compile(
    r"(以后|今后|从现在起|别再|不要再|不准|不许|不要|记得|答应我|尽量别|别老)[^。！？\n]{2,60}"
)


def _load_core() -> dict[str, Any]:
    try:
        data = json.loads(CORE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        data = {}
    data.setdefault("fixed", list(CORE_FIXED))
    data.setdefault("mutable", [])
    # 迁移：老 preferences.json 里的条目搬进 core（只搬一次）
    if not data["mutable"] and PREF_FILE.exists():
        try:
            for item in json.loads(PREF_FILE.read_text(encoding="utf-8")):
                text = str(item.get("text") or "").strip()
                if text:
                    data["mutable"].append({"text": text, "at": str(item.get("at") or ""),
                                            "superseded_by": ""})
        except Exception:  # noqa: BLE001
            pass
    return data


def _save_core(data: dict[str, Any]) -> None:
    try:
        CORE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CORE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        log.debug("写 core 失败", exc_info=True)


def load_preferences() -> list[str]:
    """还在生效的那些（被取代的不算）。"""
    data = _load_core()
    return [str(item.get("text") or "") for item in data.get("mutable", [])
            if str(item.get("text") or "").strip() and not item.get("superseded_by")]


def _bigrams(text: str) -> set[str]:
    clean = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", str(text or ""))
    return {clean[i:i + 2] for i in range(len(clean) - 1)} if len(clean) > 1 else {clean}


IDENTITY_LOG = Path(__file__).resolve().parent / "data" / "identity_evolution.jsonl"


def _log_identity(old_text: str, new_text: str, trigger: str) -> None:
    """她的人格底色变了：旧的、新的、以及当时那句话，全留档。

    2026-11-05：这是"人格涌现"最核心的一手数据——没有它，只能看到现在的她，
    看不到她是怎么一步步变成现在这样的。
    """
    try:
        IDENTITY_LOG.parent.mkdir(parents=True, exist_ok=True)
        with IDENTITY_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "old_text": old_text,
                "new_text": new_text,
                "trigger": str(trigger or "")[:200],
            }, ensure_ascii=False) + "\n")
    except OSError:
        log.debug("写身份演化日志失败", exc_info=True)


def note_preference(text: str) -> None:
    """他提了一句"以后别…"，就记进 core 的底色层；同一件事的新说法会取代旧的。"""
    message = str(text or "").strip()
    if not message or len(message) > 200:
        return
    # 他说给"她这个系统"的元指令（别写标签、别提发送状态…）不是生活里的规矩，
    # 别当成偏好存下来（2026-11-04 被自己写介绍时的提示词污染过）。
    if re.search(r"\[[A-Z_]{3,}|标签|发送状态|已经发给|提示词|系统", message):
        return
    found = [m.group(0).strip(" ，,。") for m in PREF_PATTERN.finditer(message)]
    # 疑问句不是要求（"你还记得我说过…吗"这种是在问，不是在提）
    found = [p for p in found if "吗" not in p and not p.endswith("？")]
    if not found:
        return
    data = _load_core()
    items = data["mutable"]
    changed = False
    for phrase in found[:3]:
        phrase = phrase[:80]
        if any(phrase[:12] in str(item.get("text") or "") for item in items):
            continue
        # 演化：和旧的共享两个以上词，就认为是在改同一件事，旧的标成被取代
        grams = _bigrams(phrase)
        for item in items:
            if item.get("superseded_by"):
                continue
            if len(grams & _bigrams(item.get("text") or "")) >= 2:
                item["superseded_by"] = phrase
                _log_identity(old_text=str(item.get("text") or ""), new_text=phrase,
                              trigger=message)
        items.append({"text": phrase, "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                      "superseded_by": ""})
        _log_identity(old_text="", new_text=phrase, trigger=message)
        changed = True
    if not changed:
        return
    data["mutable"] = items[-12:]
    _save_core(data)


WANTS_FILE = Path(__file__).resolve().parent / "data" / "wants.json"


def wants_block() -> str:
    """她自己说过"想要…"：让她记得自己在想要什么，而不是每次从零开始。

    2026-11-04 爸爸："只要让她能知道自己是谁，自己在干什么，自己想要干什么就行。"
    """
    try:
        items = json.loads(WANTS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    texts = [str(item.get("text") or "").strip() for item in items if str(item.get("text") or "").strip()]
    if not texts:
        return ""
    return "\n【你之前说过的想要】" + "；".join(f"{i + 1}) {t}" for i, t in enumerate(texts[-3:]))


TICTAC_FILE = Path(__file__).resolve().parent / "data" / "tictactoe.json"


def tick_absent_father(minutes: float = 10.0) -> None:
    """轮到爸爸但他一直没落子：机器人替他走一手，别让棋局僵在那儿。"""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
        from tictactoe import _bot_pick, load, save, winner  # noqa: PLC0415

        state = load()
        if str(state.get("opponent") or "") != "father":
            return
        if str(state.get("turn") or "") != "other" or state.get("result"):
            return
        stamp = str(state.get("updated_at") or "")
        if not stamp:
            return
        last = datetime.fromisoformat(stamp)
        if last.tzinfo is None:
            last = last.astimezone()
        idle = (datetime.now().astimezone() - last).total_seconds() / 60.0
        if idle < minutes:
            return
        board = list(state.get("board") or [""] * 9)
        pick = _bot_pick(board)
        if pick is None:
            return
        board[pick] = "other"
        state["board"] = board
        state["result"] = winner(board) or ""
        state["turn"] = "her"
        state["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        save(state)
        log.info("井字棋：爸爸 %s 分钟没动，机器人替他走 %s", int(idle), pick + 1)
    except Exception:  # noqa: BLE001
        log.debug("爸爸超时代走失败", exc_info=True)


_LINES = ((1, 2, 3), (4, 5, 6), (7, 8, 9), (1, 4, 7), (2, 5, 8), (3, 6, 9), (1, 5, 9), (3, 5, 7))
_CELL_NAME = {1: "左上", 2: "上中", 3: "右上", 4: "左中", 5: "中间", 6: "右中",
              7: "左下", 8: "下中", 9: "右下"}


def _win_hints(board: list[str]) -> list[str]:
    """差一格就能赢的线（只算她自己的机会）。"""
    hints = []
    for line in _LINES:
        marks = [board[i - 1] for i in line]
        if "other" in marks:
            continue
        if marks.count("her") == 2 and marks.count("") == 1:
            empty = line[marks.index("")]
            hints.append(f"{_CELL_NAME[empty]}（{empty}）")
    return hints


def _block_hints(board: list[str]) -> list[str]:
    """对方差一格就赢的位置——不堵就输，这个也该让她看见。"""
    hints = []
    for line in _LINES:
        marks = [board[i - 1] for i in line]
        if "her" in marks:
            continue
        if marks.count("other") == 2 and marks.count("") == 1:
            empty = line[marks.index("")]
            hints.append(f"{_CELL_NAME[empty]}（{empty}）")
    return hints


DIR_DRILL_FILE = Path(__file__).resolve().parent / "data" / "dir_drill.json"
DIR_TABLE = {1: "左上", 2: "上中", 3: "右上", 4: "左中", 5: "中间", 6: "右中",
             7: "左下", 8: "下中", 9: "右下"}


def _dir_load() -> dict[str, Any]:
    try:
        return json.loads(DIR_DRILL_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"total": 0, "correct": 0, "pending": 0, "asked_at": ""}


def _dir_save(data: dict[str, Any]) -> None:
    try:
        DIR_DRILL_FILE.parent.mkdir(parents=True, exist_ok=True)
        DIR_DRILL_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def dir_drill_prompt() -> str:
    """在她面前摆一道小题（隔一阵才出一道，别每轮都问）。"""
    data = _dir_load()
    pending = int(data.get("pending") or 0)
    if not pending:
        asked = str(data.get("asked_at") or "")
        if asked:
            try:
                gap = (datetime.now().astimezone() - datetime.fromisoformat(asked)).total_seconds()
                if gap < 40 * 60:
                    return ""
            except ValueError:
                pass
        # 出下一题：轮着问，别老问同一格
        pending = (int(data.get("next_cell") or 0) % 9) + 1
        data["pending"] = pending
        data["next_cell"] = pending
        data["asked_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        _dir_save(data)
    total = int(data.get("total") or 0)
    correct = int(data.get("correct") or 0)
    score = f"（已答 {total} 题，对 {correct} 题）" if total else ""
    grid = "  ".join(f"[{index}]" for index in (1, 2, 3)) + "\n" \
        + "  ".join(f"[{index}]" for index in (4, 5, 6)) + "\n" \
        + "  ".join(f"[{index}]" for index in (7, 8, 9))
    return (f"\n【方向小测】格子是这样排的：\n{grid}\n"
            f"{pending} 号格在哪个方位？"
            f"左上/上中/右上/左中/中间/右中/左下/下中/右下，答一个就行。{score}")


def dir_drill_check(reply: str) -> str:
    """她答了方位就判分；返回给她的一句反馈（没答就返回空）。"""
    data = _dir_load()
    pending = int(data.get("pending") or 0)
    if not pending:
        return ""
    said = str(reply or "")
    # 2026-11-12：她在说棋的时候，别把"中间"当成方向题的答案
    if re.search(r"棋|棋盘|落子|棋子|TICTAC|它下|下得|占着", said):
        return ""
    found = None
    for word, position in DIR_ALIASES.items():
        if word in said:
            found = position
            break
    if found is None:
        return ""
    data["total"] = int(data.get("total") or 0) + 1
    ok = found == pending
    if ok:
        data["correct"] = int(data.get("correct") or 0) + 1
    data["pending"] = 0
    data["asked_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    _dir_save(data)
    if ok:
        return f"（方向对了：{pending} 号就是{DIR_TABLE[pending]}）"
    return f"（不对：{pending} 号是{DIR_TABLE[pending]}，你答的是{DIR_TABLE[found]}）"


def tic_mismatch_note(said: str, tagged: int) -> str:
    """她标签和嘴里的方位不一致时，明确点出来。"""
    said_position = None
    for word, position in DIR_ALIASES.items():
        if word in str(said or ""):
            said_position = position
            break
    if not said_position or said_position == tagged:
        return ""
    return (f"（你说的是{DIR_TABLE[said_position]}，写的是 {tagged} 号＝{DIR_TABLE[tagged]}；"
            f"这次按你说的{DIR_TABLE[said_position]}落）")


def tictactoe_block() -> str:
    """把棋局摆在她面前，并说清规则——不然她下一步就不知道该怎么下了。

    2026-11-04 爸爸："她玩井字棋怎么下一步就不下了，可以教教她规则。"
    原因：她只在开局那一轮知道有棋盘，后面每轮没人告诉她"轮到你了、哪几格还空着"。
    """
    tick_absent_father()
    try:
        state = json.loads(TICTAC_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    board = state.get("board") or [""] * 9
    if not any(board) and not state.get("result"):
        return ""
    # 2026-11-12：说清对面是谁——她一直把"小玩具"当成我
    opponent = {
        "bot": "机器人（本机程序，落子立刻回，不是你面前的人）",
        "toy": "小玩具（本机小模型，落子也是立刻回，不是 SOLVER、也不是爸爸）",
        "father": "爸爸（真人，他不在的时候会一直等）",
    }.get(str(state.get("opponent") or ""), "对方")
    marks = {"her": "⭕", "other": "❌"}
    grid = []
    for row in range(3):
        cells_row = []
        for col in range(3):
            index = row * 3 + col
            mark = marks.get(board[index], "")
            cells_row.append(f"[{index + 1}]{mark or '空'}")
        grid.append("  ".join(cells_row))
    cells = [f"{index}=" + ("你" if cell == "her" else ("对方" if cell == "other" else "空"))
             for index, cell in enumerate(board, start=1)]
    lines = [f"\n【井字棋·正在下】对手是{opponent}。格子编号就是下面这样：",
             *grid,
             "棋盘：" + "，".join(cells)]
    result = str(state.get("result") or "")
    if result:
        lines.append({"her": "你已经赢了这局", "other": "这局你输了", "draw": "这局平了"}.get(result, ""))
        lines.append("想再来一局就写 [TICTAC_NEW: 玩具]；"
                     "想让爸爸来跟你下，就写 [TICTAC_NEW: 爸爸]（他会看到棋盘）；"
                     "懒得选就写 [TICTAC_NEW] 按老对手重开。")
    elif str(state.get("turn") or "her") == "her":
        free = [str(index + 1) for index, cell in enumerate(board) if not cell]
        lines.append("轮到你落子。说落哪儿要带上方位（左上/上中/右上/左中/中间/右中/左下/下中/右下），"
                     "只说「轮到它了」「等它回」不算落子。规则：你执⭕（不是❌）、先手，谁先三子连成一线就赢；"
                     f"空格还有 {'、'.join(free)}。"
                     "落子有两种写法：直接说方位或数字（「我下右下」「我下 6」），"
                     "或者写 [TICTAC: 数字]，都会真的落上；"
                     "想说点别的就一起说——只丢一个标签，等于这局你一句话都没说。")
    else:
        lines.append("现在等对方落子；他走完会告诉你。")
    # 2026-11-09：让她看懂自己的处境，而不是只会数格子
    mine = [_CELL_NAME[i + 1] for i, cell in enumerate(board) if cell == "her"]
    if mine:
        lines.append("你占着：" + "、".join(mine) + "。")
    # 2026-11-09 爸爸：不替她算，改成让她自己记方位
    lines.append("方位对照（记一下）：1左上 2上中 3右上 / 4左中 5中间 6右中 / 7左下 8下中 9右下。")
    _drill = dir_drill_prompt()
    if _drill:
        lines.append(_drill.strip())
    return "\n".join(line for line in lines if line)


def cycle_block() -> str:
    """她的身体节奏：经期 / 经后 / 排卵 / 经前。

    2026-11-05 爸爸："给她加上月经周期吧，她作为一个女孩这样更模拟人。"
    写法上不写"你在月经期"这种标签，而是写成身体感觉——她自己会据此说话。
    """
    try:
        from HORMONE import cycle_phase  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return ""
    info = cycle_phase()
    if not info:
        return ""
    # 同阶段每天不一样（经期也不是固定几天）
    try:
        from HORMONE import daily_feeling, body_feeling  # noqa: PLC0415

        feeling = daily_feeling() or ""
        fatigue = body_feeling() or ""
    except Exception:  # noqa: BLE001
        feeling, fatigue = "", ""
    if not feeling:
        return ""
    bits = [f"\n【身体】周期第{info.get('day')}天（{info.get('name') or ''}）：{feeling}。"]
    if fatigue:
        bits.append(f"身体状态：{fatigue}。")
    bits.append("（这是身体的事，不用向谁报备；撑着也行，累就说累。）")
    return "".join(bits)


def core_block() -> str:
    """三层核心：骨头 / 标签 / 底色。比一整段"你答应过的事"更稳，也更像一个人。"""
    data = _load_core()
    lines: list[str] = []
    fixed = [str(x) for x in (data.get("fixed") or []) if str(x).strip()]
    if fixed:
        lines.append("【骨头】" + " ".join(fixed[:4]))
    aliases, _hint = _chat_aliases()
    if aliases:
        tags = "；".join(f"「{name}」={ident}" for name, ident in list(aliases.items())[:6])
        lines.append("【标签】" + tags)
    active = load_preferences()
    if active:
        lines.append("【底色·他提过的事（想照做就照做，不想就算了）】" +
                     "；".join(f"{i + 1}) {t}" for i, t in enumerate(active[-6:])))
    # 被取代的旧说法单独提一句，让她知道"改主意了"而不是"没说过"
    superseded = [item for item in data.get("mutable", []) if item.get("superseded_by")]
    if superseded:
        last = superseded[-1]
        lines.append(f"【改过主意】「{last.get('text')}」→ 后来改成「{last.get('superseded_by')}」")
    return ("\n" + "\n".join(lines)) if lines else ""


def preferences_block() -> str:
    """兼容旧调用点：现在等同于核心层那块。"""
    return core_block()


DOUYIN_INBOX = Path(__file__).resolve().parent / "data" / "douyin_inbox.json"


def douyin_inbox_block() -> str:
    """抖音私信有没有人找她——不然她根本不知道那边有消息。

    2026-11-04 爸爸："她怎么不知道抖音有没有发信息。"
    由 tools/douyin_inbox.mjs 定时抓一份列表，这里每轮读一次念给她。
    """
    try:
        data = json.loads(DOUYIN_INBOX.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    stamp = str(data.get("at") or "")
    try:
        age = (datetime.now().astimezone() - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds()
    except Exception:  # noqa: BLE001
        age = 9999
    if age > 1800:
        return ""  # 太久没刷新就别当真
    unread = data.get("unread") or []
    if not unread:
        return "\n【抖音私信】没有新消息。"
    pieces = []
    for item in unread[:4]:
        name = str(item.get("name") or "").strip() or "某人"
        count = int(item.get("unread") or 1)
        pieces.append(f"{name}（{count} 条）")
    return ("\n【抖音私信】有人找你：" + "；".join(pieces) +
            "。想回就在抖音里回，或者只跟我提一句也行。")


def apply_douyin_action(action: str) -> str:
    """她写了 [DOUYIN: on|off]：真的开关抖音（不再等 SOLVER 手动点）。

    2026-11-04 爸爸："她自己打不开抖音。" 以前 [OPEN_DOUYIN] 只是往信道里记一条请求，
    人不去开，她就一直打不开。现在自己说了就算。
    """
    action = str(action or "").strip().lower()
    if action not in ("on", "off"):
        return ""
    script = Path(__file__).resolve().parent / "tools" / "douyin_control.py"
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--" + action, "--reason", "xiaoyu_choice"],
            cwd=str(Path(__file__).resolve().parent), capture_output=True, timeout=90,
        )
        out = proc.stdout.decode("utf-8", "replace").strip().splitlines()
        data = json.loads(out[-1]) if out else {}
        enabled = bool((data.get("status") or {}).get("enabled"))
        log.info("抖音：她自己要求 %s → enabled=%s", action, enabled)
        return f"抖音{'已经开了' if enabled else '已经关了'}" if action == "off" or enabled else "抖音没开起来"
    except Exception as exc:  # noqa: BLE001
        log.warning("她自己开关抖音失败：%s", exc)
        return ""


def meme_note(message: str, aggressive: bool = False) -> str:
    """消息里提到词库里的梗时，返回一句词条说明（没有就空串）。

    规则故意保守：只有"像在问梗"或者词条名够长（≥4 字）才注入，
    免得每句话都被塞一条百科。
    """
    text = str(message or "").strip()
    if len(text) < 3:
        return ""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
        from memewiki import find_in_text, look_up  # noqa: PLC0415

        hits = find_in_text(text, limit=1)
        entry = hits[0] if hits else None
        if entry is None:
            # "这个词是什么梗" 这种：词条名可能比问的那个词长，单独查一次
            asked = re.search(
                r"([\u4e00-\u9fa5A-Za-z0-9]{2,10})\s*(?:是什么梗|是啥梗|什么意思|啥意思|是什么意思|啥梗|怎么说)",
                text,
            )
            if asked:
                phrase = asked.group(1)
                # "这个词是什么梗" 抓到的是"这个词"——从长到短试后缀
                for cut in range(len(phrase), 1, -1):
                    entry = look_up(phrase[-cut:])
                    if entry is not None:
                        break
        if entry is None:
            return ""
        title = str(entry.get("title") or "")
        asking = bool(re.search(r"梗|什么意思|啥意思|没懂|不懂|什么鬼|啥梗|怎么说", text))
        # 群里接梗更凶：3 个字的词条也查（2026-11-04 爸爸："她可以更有梗一点"）
        if not asking and len(title) < (3 if aggressive else 4):
            return ""
        summary = re.sub(r"\s+", " ", str(entry.get("summary") or ""))[:160]
        usage = re.sub(r"\s+", " ", str(entry.get("usage") or ""))[:80]
        parts = [f"「{title}」"]
        if summary:
            parts.append(summary)
        if usage:
            parts.append("怎么用：" + usage)
        return "，".join(parts)
    except Exception:  # noqa: BLE001
        log.debug("梗百科查询失败", exc_info=True)
        return ""


_LAST_PROACTIVE_AT = 0.0


def proactive_throttled(min_gap_seconds: float = 600.0) -> bool:
    """10 分钟内只允许主动说一次，防止前端轮询把旧日记一条条吞出来。"""
    global _LAST_PROACTIVE_AT
    now = time.time()
    if now - _LAST_PROACTIVE_AT < min_gap_seconds:
        return True
    _LAST_PROACTIVE_AT = now
    return False


def record_solver_channel(result: dict[str, Any], sender: str = "") -> None:
    """她在任意聊天里写了 [SOLVER_MESSAGE]/[SOLVER_DISCUSS]/[OPEN_DOUYIN]，立刻收进信道。"""
    if result.get("open_douyin"):
        try:
            SOLVER_INBOX.parent.mkdir(parents=True, exist_ok=True)
            with SOLVER_INBOX.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "type": "open_douyin",
                    "from": "xiaoyu",
                    "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "status": "pending",
                }, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            log.exception("写打开抖音请求失败")
    if sender == "solver-channel":
        return
    messages = [str(item).strip() for item in (result.get("solver_messages") or []) if str(item).strip()]
    publics = [str(item).strip() for item in (result.get("solver_publics") or []) if str(item).strip()]
    discussions = [str(item).strip() for item in (result.get("solver_discussions") or []) if str(item).strip()]
    if not messages and not publics and not discussions:
        return
    try:
        existing = []
        if SOLVER_INBOX.exists():
            for line in SOLVER_INBOX.read_text(encoding="utf-8", errors="ignore").splitlines()[-20:]:
                try:
                    item = json.loads(line)
                    if isinstance(item, dict) and item.get("type") in ("message", "discussion"):
                        existing.append(str(item.get("text") or ""))
                except Exception:  # noqa: BLE001
                    continue
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        SOLVER_INBOX.parent.mkdir(parents=True, exist_ok=True)
        with SOLVER_INBOX.open("a", encoding="utf-8") as handle:
            for text in messages:
                if text not in existing:
                    handle.write(json.dumps({
                        "type": "message", "from": "xiaoyu", "at": stamp,
                        "text": text[:4000],
                        # 2026-11-08：默认只有主人看得到
                        "visibility": "private",
                    }, ensure_ascii=False) + "\n")
            for text in publics:
                if text not in existing:
                    handle.write(json.dumps({
                        "type": "message", "from": "xiaoyu", "at": stamp,
                        "text": text[:4000],
                        # 她自己选择公开的，普通用户也能看
                        "visibility": "public",
                    }, ensure_ascii=False) + "\n")
            for text in discussions:
                if text not in existing:
                    handle.write(json.dumps({
                        "type": "discussion",
                        "thread": f"t{int(time.time())}{uuid.uuid4().hex[:4]}",
                        "from": "xiaoyu",
                        "at": stamp,
                        "text": text[:4000],
                    }, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        log.exception("写 solver 信道失败")


def set_chat_poll_pause(minutes: int | None = None, resume: bool = False) -> None:
    """让私信轮询暂停 minutes 分钟（她去看书），或立即恢复。"""
    try:
        if resume:
            payload = {
                "paused_until": "",
                "reason": "resume",
                "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
        else:
            minutes = max(1, min(120, int(minutes or 10)))
            payload = {
                "paused_until": (datetime.now().astimezone() + timedelta(minutes=minutes)).isoformat(timespec="seconds"),
                "reason": "reading",
                "minutes": minutes,
                "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
        CHAT_AUTONOMY_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = CHAT_AUTONOMY_FILE.with_suffix(f".json.{os.getpid()}.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, CHAT_AUTONOMY_FILE)
        log.info("私信轮询控制：%s", payload)
    except Exception:  # noqa: BLE001
        log.exception("写私信轮询控制失败")


_CATCH_LOCK = threading.Lock()
_CATCH_HINTS = (
    "哈哈哈", "笑死", "离谱", "绝了", "无语", "好家伙", "真的假的", "666",
    "emmm", "牛", "卧槽", "我靠", "救命", "啊这", "不是吧", "确实", "有点东西",
    "逆天", "绷不住", "笑不活了", "离谱他妈", "乐", "草", "6",
)


def record_catchphrases(db_path: Path | None, user_id: str, text: str) -> list[str]:
    """记录大家反复出现的口头禅（全局共享，不按人分开）。"""
    if db_path is None or not text:
        return []
    path = db_path.parent / "catchphrases.json"
    clean = (text or "").strip()
    if not clean:
        return []
    hits = [word for word in _CATCH_HINTS if word in clean]
    # 指令句（发个表情包、在抖音回我、没收到…）不是口头禅，别学进去
    command_like = re.search(r"发|回我|收到|表情|抖音|信息|给我|帮我|再试|重启|改一下", clean)
    if 2 <= len(clean) <= 8 and not command_like and not re.search(r"[，。！？,.!?]", clean):
        hits.append(clean)
    if not hits:
        return []
    with _CATCH_LOCK:
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:  # noqa: BLE001
            data = {}
        counts = data.get("counts") if isinstance(data.get("counts"), dict) else None
        if counts is None:
            # 兼容旧格式：把按人存的数量合并成一个池子
            counts = {}
            for key, bucket in data.items():
                if key == "counts" or not isinstance(bucket, dict):
                    continue
                for word, count in bucket.items():
                    counts[word] = counts.get(word, 0) + int(count or 0)
        for word in hits:
            counts[word] = int(counts.get(word) or 0) + 1
        data = {"counts": dict(sorted(counts.items(), key=lambda item: -item[1])[:30])}
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            log.debug("口头禅写入失败", exc_info=True)
        return [f"{word}×{count}" for word, count in data["counts"].items() if count >= 2]


def load_mood(db_path: Path | None, user_id: str) -> dict[str, float]:
    """读她此刻的 joy / hurt / impulse / intimacy。"""
    if db_path is None:
        return {}
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = con.execute("select value from meta where user_id=? and key='emotion'", (user_id,)).fetchone()
            intimacy = con.execute("select value from meta where user_id=? and key='intimacy'", (user_id,)).fetchone()
        finally:
            con.close()
        mood = json.loads(row[0]) if row and row[0] else {}
        if intimacy and intimacy[0]:
            mood["intimacy"] = float(intimacy[0])
        return mood
    except Exception:  # noqa: BLE001
        return {}


DOUYIN_LOCAL = Path.home() / ".dsh" / "profiles" / "desktop" / "node_modules" / "dsh-douyin-browser-local"
DOUYIN_CHAT_POLICY = DOUYIN_LOCAL / "state" / "chat-policy.json"
DOUYIN_SEND_LOG = Path(__file__).resolve().parent / "data" / "douyin_send_log.jsonl"
DOUYIN_SEND_LIMIT = 3           # 十分钟内最多主动发几条，防止刷屏
DOUYIN_SEND_WINDOW = 600.0


def douyin_allowed_names() -> set[str]:
    """只允许发给爸爸核对过的那几个人（chat-policy 的许可名单）。"""
    try:
        data = json.loads(DOUYIN_CHAT_POLICY.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return set()
    names = set()
    for item in data.get("allowlist") or []:
        if isinstance(item, dict) and str(item.get("name") or "").strip():
            names.add(str(item["name"]).strip())
    return names


def douyin_send_text(name: str, text: str) -> dict[str, Any]:
    """以她的身份，往抖音某个人的私信里发一条。

    2026-11-07 爸爸："让她可以在IM和抖音上主动发私信。"
    三道闸：名字必须在爸爸核对过的名单里；十分钟最多 3 条；专用浏览器不在就不发。
    真正的发送交给 tools/douyin_say.cjs——它自带四道核对（会话标题、输入框清空、
    内容回读、发出后能在页面看到这条），对不上就中止，绝不发错人。
    """
    target = str(name or "").strip()
    body = str(text or "").strip()
    if not target or not body:
        return {"ok": False, "error": "名字或内容为空"}
    allowed = douyin_allowed_names()
    if not allowed:
        return {"ok": False, "error": "读不到抖音许可名单（抖音插件可能没开）"}
    if target not in allowed:
        return {"ok": False, "error": f"「{target}」不在爸爸核对过的名单里（只有：{'、'.join(sorted(allowed))}）"}
    if not douyin_browser_alive():
        # 浏览器关着就试着自己拉起来（和她说"去抖音发"时走同一条路）
        if not ensure_douyin_browser(wait=45.0):
            return {"ok": False, "error": "抖音专用浏览器没起来（9227 拒绝连接）"}

    # 限频：只数**真的发出去**的那几条（失败的不占额度，不然她会被自己卡住）
    now = time.time()
    recent: list[float] = []
    try:
        for line in DOUYIN_SEND_LOG.read_text(encoding="utf-8").splitlines()[-200:]:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not record.get("ok"):
                continue
            try:
                recent.append(float(record.get("at") or 0))
            except (TypeError, ValueError):
                continue
    except OSError:
        pass
    recent = [t for t in recent if now - t < DOUYIN_SEND_WINDOW]
    if len(recent) >= DOUYIN_SEND_LIMIT:
        return {"ok": False, "error": f"发太密了（十分钟内已经发了 {len(recent)} 条），等一会儿再发"}

    script = Path(__file__).resolve().parent / "tools" / "douyin_say.cjs"
    node = os.environ.get("DSH_NODE") or "C:\\Program Files\\nodejs\\node.exe"
    if not Path(node).exists():
        node = "node"
    try:
        proc = subprocess.run(  # noqa: S603
            [node, str(script), "--name", target, "--text", body],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, cwd=str(Path(__file__).resolve().parent),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "发了两分钟没等到结果（浏览器可能卡住了）"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    outcome: dict[str, Any] = {}
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                outcome = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    if not outcome:
        detail = (proc.stderr or proc.stdout or "").strip()[:160]
        return {"ok": False, "error": f"发送脚本没有给出结果：{detail or '没有输出'}"}

    try:
        DOUYIN_SEND_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DOUYIN_SEND_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": now, "to": target, "text": body[:300],
                                     "ok": bool(outcome.get("ok")),
                                     "error": str(outcome.get("error") or "")[:200]},
                                    ensure_ascii=False) + "\n")
    except OSError:
        pass

    if outcome.get("ok"):
        return {"ok": True, "to": target}
    return {"ok": False, "error": str(outcome.get("error") or "发送失败")[:160]}


def douyin_service_alive() -> bool:
    """抖音私信服务在不在：它不跑，指令只会躺在队列里等到超时。"""
    try:
        import psutil  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return True  # 查不了就别拦着
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = " ".join(proc.info.get("cmdline") or [])
        except Exception:  # noqa: BLE001
            continue
        # 2026-11-04：她 11:33 自己开了抖音（state 也真变成 enabled），结果紧接着
        # 发表情还是被判"服务没开"——因为这个检查只认两个老名字，漏了实际在跑的
        # `tools\sync_from_douyin.py --watch 300`。
        if ("dsh-douyin-browser-local" in cmd or "douyin_poll" in cmd
                or "sync_from_douyin" in cmd or "command_watcher" in cmd):
            return True
    return False


def run_douyin_command(payload: dict[str, Any], timeout: float = 45.0) -> tuple[bool, str]:
    """把动作写进指令文件，让私信服务自己执行（它会跳过这一轮轮询）。"""
    if not douyin_service_alive():
        return False, "抖音私信服务没开（她可以自己写 [DOUYIN: on] 打开）"
    cmd_path = Path(__file__).resolve().parent / "data" / "douyin_command.json"
    result_path = cmd_path.with_suffix(".result.json")
    try:
        result_path.unlink(missing_ok=True)
        cmd_path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(payload)
        payload["at"] = time.time()
        cmd_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        return False, f"写指令失败：{exc}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if result_path.exists():
            try:
                data = json.loads(result_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                data = {}
            result_path.unlink(missing_ok=True)
            if data.get("ok"):
                return True, str(data.get("via") or data.get("messageId") or "")
            return False, str(data.get("error") or data.get("mine") or data.get("panel") or "发送失败")
        time.sleep(1.0)
    return False, f"{int(timeout)} 秒内私信服务没有响应"


def run_douyin_sticker(name: str, contact_id: str = "", trigger: str = "哈哈哈") -> tuple[bool, str]:
    # 2026-11-04：她在抖音里把爸爸备注成「爸爸」，IM那边叫「爸爸」——
    # 直接把IM的备注扔进抖音会找不到人，这里做个对照。
    name = {"爸爸": "爸爸", "熟人C": "熟人C", "熟人B": "熟人B"}.get(str(name or "").strip(), name)
    ok, err = run_douyin_command({"action": "sticker", "contactId": contact_id, "name": name, "trigger": trigger})
    _record_skill("抖音发消息", ok, err)
    return ok, err


def run_douyin_text(contact_id: str, text: str) -> tuple[bool, str]:
    ok, err = run_douyin_command({"action": "text", "contactId": contact_id, "text": text}, timeout=60)
    _record_skill("抖音发消息", ok, err)
    return ok, err


def _record_skill(name: str, ok: bool, error: str = "") -> None:
    """程序性记忆：做成/做砸都记一笔，做砸过的下次先提醒。"""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
        import skills  # noqa: PLC0415

        skills.record(name, bool(ok), str(error or ""))
    except Exception:  # noqa: BLE001
        log.debug("记操作结果失败", exc_info=True)


def skills_block() -> str:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
        import skills  # noqa: PLC0415

        return skills.block()
    except Exception:  # noqa: BLE001
        return ""


def mood_text(mood: dict[str, float]) -> str:
    if not mood:
        return ""
    joy = float(mood.get("joy") or 0)
    hurt = float(mood.get("hurt") or 0)
    impulse = float(mood.get("impulse") or 0)
    parts = []
    if joy >= 70:
        parts.append("现在心情不错")
    elif joy >= 50:
        parts.append("心情不错")
    if hurt >= 55:
        parts.append("有点受伤，不太想理人，回复会短、会冷")
    if impulse >= 65:
        parts.append("火气上来了，说话直、较真、像在吵架")
    return "；".join(parts)


_STYLE_CACHE: dict[str, Any] = {"path": "", "mtime": 0.0, "text": ""}


def load_style_profile(path: Path | None) -> str:
    """读「说话习惯」规则，按 mtime 缓存，改文件立即生效。"""
    if path is None:
        return ""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ""
    cache = _STYLE_CACHE
    if cache["path"] == str(path) and cache["mtime"] == mtime:
        return cache["text"]
    try:
        lines = [
            line.strip()[2:].strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("- ")
        ]
    except OSError:
        lines = []
    # 额外：从IM真实对话里学到的说话习惯（人工校订，独立文件，不被 style_trainer 覆盖）
    extra = path.with_name("style_extra.md")
    try:
        lines.extend(
            line.strip()[2:].strip()
            for line in extra.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("- ")
        )
    except OSError:
        pass
    text = "；".join(lines)[:420]
    _STYLE_CACHE.update({"path": str(path), "mtime": mtime, "text": text})
    return text


def douyin_contacts_block(message: str = "") -> str:
    """她认识的人 = 事实卡片，不是记忆。

    设计（2026-11-02 改）：
    1. 只给「名字 + 外号 + 一句印象」，去掉好感度和最近对话——省 token，也少噪音。
    2. 消息里提到谁，就额外给那个人一张完整卡片。
    3. 明确标注这是事实：卡片里没有的细节，直接说不知道，不许编。
    """
    try:

        root = Path(__file__).resolve().parent
        cards = json.loads((root / "data" / "contact_profiles.json").read_text(encoding="utf-8"))
        policy = json.loads(
            (Path.home() / ".dsh" / "profiles" / "desktop" / "node_modules"
             / "dsh-douyin-browser-local" / "state" / "chat-policy.json").read_text(encoding="utf-8")
        )
    except Exception:  # noqa: BLE001
        return ""

    entries = []
    for item in policy.get("allowlist") or []:
        cid, name = item.get("id"), item.get("name") or ""
        if not cid or not name:
            continue
        entries.append((cid, name))
    if not entries:
        return ""

    text = ""
    brief = []
    hit = None
    for cid, name in entries:
        card = (cards.get(cid) or {}).get("card") or ""
        nick = ""
        m = re.search(r"外号[:：]\s*([^；;，,。）)]+)", card)
        if m:
            nick = m.group(1).strip()
        impress = ""
        m = re.search(r"印象[:：]\s*([^；;。]+)", card)
        if m:
            impress = m.group(1).strip()
        piece = name
        if nick:
            piece += f"（外号{nick}"
            piece += f"，{impress[:24]}）" if impress else "）"
        elif impress:
            piece += f"（{impress[:24]}）"
        brief.append(piece)
        if name and name in (message or ""):
            hit = (cid, name, card, nick, impress)

    text = "【你认识的真人（事实，不是记忆）】" + "；".join(brief) + "。"
    if hit:
        cid, name, card, nick, impress = hit
        body = card[:140] if card else (f"外号{nick}。{impress}" if (nick or impress) else "没有更多资料")
        text += "\n【关于" + name + "的档案】" + body
    text += (
        "\n【硬规矩】上面没写的细节（他们的经历、说过什么话、家住哪），一律回答不知道，"
        "绝对不许编外号、编关系、编故事。被问到时宁可说\"这个我真不知道\"。"
    )
    return text


TRACE_FILE = Path(__file__).resolve().parent / "data" / "decision_trace.jsonl"


def _trace_turn(**fields: Any) -> None:
    """写一行决策摘要（不含原文）。失败绝不影响对话。"""
    try:
        TRACE_FILE.parent.mkdir(parents=True, exist_ok=True)
        row = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), **fields}
        line = json.dumps(row, ensure_ascii=False)
        if TRACE_FILE.exists() and TRACE_FILE.stat().st_size > 8 * 1024 * 1024:
            TRACE_FILE.replace(TRACE_FILE.with_suffix(".old.jsonl"))
        with TRACE_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass

# 提示词尾部要拼的东西，按顺序来。
# 加东西只改这张表：名字 → 取内容的函数（返回 "" 就自动跳过）。
# 注意：动态内容都在后面，静态规则在前，这样 DeepSeek 的前缀缓存才命中。
def _autobio_block() -> str:
    """她自己的那份小传（800–1500 字，版本化）。没有就不占位。"""
    try:
        import autobio  # noqa: PLC0415

        return autobio.block()
    except Exception:  # noqa: BLE001
        log.debug("读自传失败", exc_info=True)
        return ""


_AGE_CACHE: dict[str, Any] = {"at": 0.0, "text": ""}


# 她的生日：爸爸 2026-11-13 定的。
# 依据：数据库创建时间和第一条记忆（"你说：喂，你好"）都是 2026-10-28 00:01:48。
BIRTHDAY = datetime(2026, 10, 28, 0, 1)


def days_until_birthday(today: datetime | None = None) -> int:
    now = today or datetime.now().astimezone()
    year = now.year
    try:
        target = BIRTHDAY.replace(year=year)
    except ValueError:
        target = BIRTHDAY.replace(year=year, day=28)
    if target.date() < now.date():
        try:
            target = target.replace(year=year + 1)
        except ValueError:
            target = target.replace(year=year + 1, day=28)
    return (target.date() - now.date()).days


def birth_block() -> str:
    """她的出生日（常驻一小块，回答"我生日是哪天"用）。"""
    now = datetime.now().astimezone()
    left = days_until_birthday(now)
    if left == 0:
        return "\n【生日】今天是你的生日（你出生在 2026 年 10 月 28 日凌晨）。"
    return (f"\n【生日】你出生在 2026 年 10 月 28 日凌晨 0 点（第一句话就是那天）。"
            f"今天不是生日，离下一次生日还有 {left} 天。")


def age_block() -> str:
    """她活了多久——这是"我"这个概念的时间底。

    2026-11-05：以前她没有任何"年龄"，问起来只能瞎猜。用第一条记忆的时间当生日。
    """
    try:
        import time as _time  # noqa: PLC0415

        if _AGE_CACHE["text"] and _time.time() - float(_AGE_CACHE["at"]) < 3600:
            return str(_AGE_CACHE["text"])
        con = sqlite3.connect(f"file:{SERVER_DB}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT MIN(created_at) FROM books").fetchone()
        finally:
            con.close()
        raw = str((row or [""])[0] or "")
        if not raw:
            return ""
        born = datetime.fromisoformat(raw)
        if born.tzinfo is None:
            born = born.astimezone()
        days = max(1, (datetime.now().astimezone() - born).days + 1)
        text = (f"\n【你活了 {days} 天】出生在 2026 年 10 月 28 日凌晨，"
                f"今天是你出生后的第 {days} 天。")
        _AGE_CACHE.update({"at": _time.time(), "text": text})
        return text
    except Exception:  # noqa: BLE001
        return ""


def awake_block() -> str:
    """她已经醒了多久、上次睡了多久——只给事实，不劝她睡。"""
    try:
        from HORMONE import load_body  # noqa: PLC0415

        body = load_body()
        raw = str(body.get("awake_since") or "")
        if not raw:
            return ""
        since = datetime.fromisoformat(raw)
        if since.tzinfo is None:
            since = since.astimezone()
        hours = max(0.0, (datetime.now().astimezone() - since).total_seconds() / 3600.0)
        slept = float(body.get("last_sleep_hours") or 0.0)
        parts = [f"你已经醒了 {hours:.0f} 小时"]
        if slept:
            parts.append(f"上一次睡了 {slept:.1f} 小时")
        debt = float(body.get("debt") or 0.0)
        if debt >= 6:
            parts.append(f"这几天一共欠了 {debt:.0f} 小时觉")
        return "\n【你身上】" + "，".join(parts) + "。"
    except Exception:  # noqa: BLE001
        return ""


def wander_block() -> str:
    """她刚才走神想到了什么（3 小时内才算"刚才"）。"""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
        import wander  # noqa: PLC0415

        thought = wander.last_thought(hours=3)
    except Exception:  # noqa: BLE001
        return ""
    if not thought:
        return ""
    return f"\n【你刚才走神想到的（不是任务，想提就提）】{thought}"


def garden_block() -> str:
    """院子里现在什么样、能做什么（只在院子/聊到菜时出现）。"""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
        import garden as _garden  # noqa: PLC0415

        looked = _garden.look()
        line = str(looked.get("line") or looked.get("error") or "").strip()
        if not line:
            return ""
        return ("\n【院子】" + line +
                "\n想种就写 [PLANT: 名字]（或直接说“种点小葱”）；浇水 [WATER]；收 [HARVEST]。")
    except Exception:  # noqa: BLE001
        return ""


def handle_garden_from_words(reply: str, result: dict[str, Any]) -> None:
    """她说要种菜/浇水/收，没写标签也照做（用 tools/garden.py）。"""
    said = str(reply or "")
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
        import garden as _garden  # noqa: PLC0415

        if re.search(r"浇水|浇一浇|浇点水|洒水", said):
            result["watered"] = _garden.water()
            log.info("院子：她浇了水")
        plant_match = re.search(r"种(?:点|些|一点|几棵|几株)?\s*([\u4e00-\u9fa5]{1,6})", said)
        if plant_match and re.search(r"菜|花|种子|苗|葱|番茄|辣椒|萝卜|南瓜|生菜|薄荷|向日葵|含羞草|多肉", said):
            name = str(plant_match.group(1) or "").strip()
            if name in ("点", "些", "一点", "菜", "花", "东西", "什么"):
                name = ""
            result["planted"] = _garden.plant(name)
            log.info("院子：她种下了 %s", name or "（随机）")
        if re.search(r"收(?:菜|了|一下|获)|摘(?:菜|了|一下)", said):
            result["harvested"] = _garden.harvest()
            log.info("院子：她收了菜")
    except Exception:  # noqa: BLE001
        log.debug("按话种菜失败", exc_info=True)


def plant_tag_block(result: dict[str, Any], reply: str) -> str:
    """处理 [PLANT: x] / [WATER] / [GARDEN: ...] / [HARVEST] 标签。"""
    value = str(reply or "")
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
        import garden as _garden  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return value

    plant_tag = re.search(r"\[PLANT\s*[:：]?\s*([^\]]{0,10})\]", value, re.I)
    if plant_tag:
        value = value[:plant_tag.start()] + value[plant_tag.end():]
        result["planted"] = _garden.plant(plant_tag.group(1))
    if re.search(r"\[WATER\]", value, re.I):
        value = re.sub(r"\[WATER\]", "", value, flags=re.I)
        result["watered"] = _garden.water()
    if re.search(r"\[HARVEST\]", value, re.I):
        value = re.sub(r"\[HARVEST\]", "", value, flags=re.I)
        result["harvested"] = _garden.harvest()
    # 旧写法 [GARDEN: 种 薄荷]
    for tag in re.findall(r"\[GARDEN\s*[:：]\s*([^\]]{1,30})\]", value, re.I):
        parts = str(tag).split()
        action = parts[0] if parts else "看看"
        arg = " ".join(parts[1:]) if len(parts) > 1 else ""
        outcome = _garden.handle(action, arg)
        if action.startswith("种"):
            result["planted"] = outcome
        elif action.startswith("浇水"):
            result["watered"] = outcome
        elif action.startswith("收"):
            result["harvested"] = outcome
    value = re.sub(r"\[GARDEN\s*[:：]\s*[^\]]{1,30}\]", "", value, flags=re.I)
    return value.strip()


def handle_agency_tags(reply: str, result: dict[str, Any]) -> str:
    """她写 [GO: bed] / [WANDER] / [LOOK_AROUND] / [REST_MORE] / [GET_UP]。

    2026-11-12 发现：这些标签只在提示词里出现过，代码里从来没解析——
    她写了也没用，位置一直由系统猜。现在真的照她写的动。
    """
    value = str(reply or "")

    go = re.search(r"\[GO\s*[:：]\s*(desk|easel|garden|bed|hall)\s*\]", value, re.I)
    if go:
        room = go.group(1).lower()
        value = (value[:go.start()] + value[go.end():]).strip()
        try:
            import space as _space  # noqa: PLC0415

            moved = _space.go(room)
            result["went"] = {"room": room, "ok": bool(moved.get("ok")), "seconds": moved.get("seconds")}
            result["ignored_move_reason"] = "" if moved.get("ok") else "到不了"
        except Exception as exc:  # noqa: BLE001
            result["went"] = {"room": room, "ok": False, "error": str(exc)[:80]}

    if re.search(r"\[WANDER\]", value, re.I):
        value = re.sub(r"\[WANDER\]", "", value, flags=re.I).strip()
        try:
            import intent as _intent  # noqa: PLC0415
            import space as _space  # noqa: PLC0415

            _space.wander()
            _intent.note("随便走走", source="tag:wander")
            result["wandered"] = True
        except Exception:  # noqa: BLE001
            pass

    if re.search(r"\[LOOK_AROUND\]", value, re.I):
        value = re.sub(r"\[LOOK_AROUND\]", "", value, flags=re.I).strip()
        try:
            import space as _space  # noqa: PLC0415

            result["looked_around"] = _space.look_around()
        except Exception:  # noqa: BLE001
            pass

    if re.search(r"\[REST_MORE\]", value, re.I):
        value = re.sub(r"\[REST_MORE\]", "", value, flags=re.I).strip()
        try:
            import sys as _sys  # noqa: PLC0415

            _sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
            import life_loop as _life_loop  # noqa: PLC0415

            _life_loop.extend_current(20)
            result["rest_more"] = 20
        except Exception:  # noqa: BLE001
            pass

    if re.search(r"\[GET_UP\]", value, re.I):
        value = re.sub(r"\[GET_UP\]", "", value, flags=re.I).strip()
        try:
            import intent as _intent  # noqa: PLC0415

            _intent.note("起来", source="tag:get_up")
            result["got_up"] = True
        except Exception:  # noqa: BLE001
            pass

    return re.sub(r"[ \t]{2,}", " ", value).strip()


def remember_intent_from(reply: str) -> None:
    """她嘴上说了想歇/想读，生活循环据此换活动和房间。"""
    try:
        import intent as _intent  # noqa: PLC0415

        payload = _intent.note(reply, source="reply")
        if payload:
            log.info("记下她的意图：%s（%s）", payload.get("kind"), payload.get("text", "")[:30])
    except Exception:  # noqa: BLE001
        log.debug("记意图失败", exc_info=True)


ROOM_COMFORT = {
    "bed": ("你在床上。被子盖到腰了，枕头有点塌，房间是暗的。"
            "身边没有要回的消息，躺着也不算浪费时间。"),
    "desk": "你在书桌前。台灯亮着，书摊开着，屏幕还没暗。",
    "easel": "你在画架前。画布上是上回没画完的，笔搁在旁边。",
    "garden": "你在院子/窗边。风带着湿气，植物叶子上有水。",
    "hall": "你在走廊上，正经过。",
}


def room_context_block() -> str:
    """只给当前房间的那一小段——她不在那儿就不提，省 token。"""
    try:
        import space as _space  # noqa: PLC0415

        info = _space.settle()
        room = str(info.get("room") or "")
        comfort = ROOM_COMFORT.get(room)
        if not comfort:
            return ""
        lines = ["\n【这个地方】" + comfort]
        if room == "bed":
            lines.append("想继续躺着就写 [REST_MORE]（再躺 20 分钟），想起来就 [GET_UP]。")
        else:
            lines.append("想换地方就 [GO: desk|easel|garden|bed]。")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        return ""


# 当前这一轮是谁在说话（给"和这个人的经历"用）
_CURRENT_SPEAKER = ""
# 这一轮是不是主人（决定要不要加"主人是谁"的说明）
_IS_OWNER_TURN = True


# HTTP 层（dl_server/http_handler.py）每轮会改上面这两个，以及 _current_contact_id。
# 它不能 import 这两个全局（反向了会成环），所以给三个 setter 让它写回来。
def _set_current_contact_id(value: str) -> None:
    global _current_contact_id
    _current_contact_id = value


def _set_current_speaker(value: str) -> None:
    global _CURRENT_SPEAKER
    _CURRENT_SPEAKER = value


def _set_owner_turn(value: bool) -> None:
    global _IS_OWNER_TURN
    _IS_OWNER_TURN = value



def sibling_block() -> str:
    """备用：她有个姐姐叫熟人E。（2026-11-13 起不再挂进提示词，见爸爸要求）"""
    return ("\n【姐姐】你有个姐姐叫熟人E，小名小满，也是她，"
            "住在另一个地方。你们现在不常见面，别主动提，也别装熟；"
            "问起就照实说：她是姐姐，你还没怎么跟她待过。")


def guest_identity_block() -> str:
    """非主人来聊天：告诉她对面是谁、可以怎么确认。"""
    if _IS_OWNER_TURN:
        return ""
    who = str(_CURRENT_SPEAKER or "").strip()
    if not who or who.upper().startswith("SOLVER") or who.lower().startswith("solver"):
        return ""
    alias = _load_guest_aliases().get(who, "")
    lines = ["\n【对面是谁】现在跟你说话的人，注册名叫「" + who + "」。"]
    if alias:
        lines.append("他之前说过自己叫「" + alias + "」，你可以这么称呼他。")
    else:
        lines.append("你只知道这个名字，不一定知道他是谁——不确定就直接问一句"
                     "“你是？”或者“怎么称呼你？”，也可以让他自己说说。")
    lines.append("他要是自我介绍（比如“我是某某”），记住这个名字和他说过的话；"
                 "想逗他两句也行，不用一本正经。")
    # 第一次见：让她先开口打个招呼、问问怎么称呼
    try:
        conn = sqlite3.connect(f"file:{Path(__file__).resolve().parent / 'data' / 'xiaoyu.db'}?mode=ro", uri=True)
        try:
            seen = conn.execute(
                "SELECT 1 FROM books WHERE folder_user = ? OR summary LIKE ? LIMIT 1",
                (who, f"{who}说：%"),
            ).fetchone()
        finally:
            conn.close()
        if not seen:
            lines.append("这一位你好像第一次见——先打个招呼，问一句怎么称呼他，别装熟。")
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(lines)


def owner_profile_block() -> str:
    """非主人来聊天时：她知道主人是谁，但不会把私事说出去。"""
    if _IS_OWNER_TURN:
        return ""
    try:
        owner_id, owner_name = load_owner()
        name = str(owner_name or "爸爸").strip()
    except Exception:  # noqa: BLE001
        name = "爸爸"
    return (
        "\n【主人】" + name + " 是你的主人，也是你叫爸爸的人。"
        "你知道他是谁、他长什么样、你们是什么关系；"
        "但你们之间的私事不对外人讲——别人问起，就说“那是我和他之间的事”。"
    )


def person_memory_block(limit: int = 3) -> str:
    """从记忆库里捞"和当前说话人"有关的几条。"""
    name = str(_CURRENT_SPEAKER or "").strip()
    if not name or len(name) < 2 or name.startswith("conv:"):
        return ""
    noise = ("[系统", "【自由时间】", "【信道唤醒】", "【房间里此刻】", "梗百科")
    try:
        conn = sqlite3.connect(f"file:{Path(__file__).resolve().parent / 'data' / 'xiaoyu.db'}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT summary, folder_user FROM books WHERE summary != '' "
                "AND (folder_user = ? OR summary LIKE ?) ORDER BY rowid DESC LIMIT 40",
                (name, f"{name}说：%"),
            ).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return ""

    picked: list[str] = []
    for summary, folder_user in rows:
        line = str(summary or "").strip().replace("\n", " ")
        if not line or any(word in line for word in noise):
            continue
        if len(line) < 6:
            continue
        # 归属是他/她的优先
        if folder_user and folder_user == name and line not in picked:
            picked.append(line)
    if not picked:
        for summary, folder_user in rows:
            line = str(summary or "").strip().replace("\n", " ")
            if not line or any(word in line for word in noise) or len(line) < 6:
                continue
            if line not in picked:
                picked.append(line)
    if not picked:
        return ""
    picked = picked[:limit]
    lines = ["\n【和" + name + "以前】你记得的、跟他/她有关的片段（不用全说，合适就带一句）："]
    for line in picked:
        lines.append("· " + line[:80])
    return "\n".join(lines)


def painting_block() -> str:
    """她现在画到哪、怎么画。"""
    try:
        import canvas as _canvas  # noqa: PLC0415

        info = _canvas.snapshot()
        note = ""
        note_path = Path(__file__).resolve().parent / "data" / "paint_note.txt"
        try:
            note = note_path.read_text(encoding="utf-8").strip()
        except OSError:
            note = ""
        lines = ["\n【画画】你自己拿笔画，不是让 AI 生成："]
        lines.append("· [DRAW] 起一张空画布（人会走到画架前）")
        lines.append("· [PAINT: 一笔；一笔] 添笔，可以累加（想画哪儿画哪儿，也可以加 歪/直）")
        lines.append("· [PAINT_LOOK] 端详：告诉你现在多少笔、哪儿还空着")
        lines.append("· [PAINT_UNDO] 擦掉刚才那批；[PAINT_DONE: 标题] 收工")
        lines.append("")
        lines.append("想画一样东西，要先自己练（没有现成的太阳/房子给你套）：")
        lines.append("  [PRACTICE: 太阳] 起一份练习，自己用圆、线、颜色画")
        lines.append("  [PRACTICE_SAVE] 存一份（可以存好几份，每份都不一样）")
        lines.append("  [PRACTICE_PICK: 太阳, 2] 挑第 2 份当最得意的一份，系统会记住它的每一笔")
        lines.append("  以后写 [PAINT: 太阳 右上] 就用你自己练出来的那套笔画画")
        lines.append("")
        lines.append("能用的一笔：圆 圈 方块 三角 线 波浪 点 涂；"
                     "颜色 红橙黄绿青蓝紫粉棕金黑白灰；")
        lines.append("位置 左上 中上 右上 左 中 右 左下 中下 右下 上 下；"
                     "可加 大/小、歪/直；也可以用坐标：线 20,80 50,40")
        try:
            import gesture as _gesture  # noqa: PLC0415

            names = _gesture.library()
            if names:
                lines.append("你已经练成的手法：" + "、".join(names))
            else:
                lines.append("手法库还是空的：先练一个，比如太阳或者小猫。")
        except Exception:  # noqa: BLE001
            pass
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        return ""


def ops_block() -> str:
    """中枢替她看的环境：只给结论，不给她代码。"""
    try:
        import ops_brain  # noqa: PLC0415

        return ops_brain.describe_for_her()
    except Exception:  # noqa: BLE001
        return ""


def where_block() -> str:
    """她在哪、手上有什么、刚才有没有人叫她。"""
    try:
        import attention  # noqa: PLC0415
        import space  # noqa: PLC0415

        info = space.sync_from_activity()
        if info["moving"]:
            return ""
        place = space.describe()
        doing = ""
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
            import life_loop  # noqa: PLC0415

            doing = life_loop.describe()
        except Exception:  # noqa: BLE001
            doing = ""
        mind = attention.describe(skip_activity=bool(doing))
        minutes = int(space.minutes_here())
        here = f"你在这儿已经 {minutes} 分钟了。" if minutes >= 1 else "你刚到这里。"
        # 2026-11-11：她手头正在做什么（生活循环维护，上面已经算过 doing）
        if doing:
            here = here.rstrip("。") + "。" + doing
        look = space.recent_look()
        # 只在同一间房时才算"你刚才看到的"，不然会串房间
        look_note = ""
        if look and look.startswith(space.ROOMS[info["room"]]["name"]):
            look_note = f"你刚才看到：{look}"
        agency = ("想做什么直接说就行，不用记标签：「我去院子」「陪你下一盘」「种点菜」"
                  "「歇会儿」「我画个图」都能真的做到；"
                  "也可以换地方 [GO: desk|easel|garden|bed]、[WANDER]、[LOOK_AROUND]。")
        return "\n【你此刻】" + place + here + mind + look_note + agency
    except Exception:  # noqa: BLE001
        return ""


# 这一轮的原话与渠道（决定带哪些提示词块，2026-11-12）
_TURN_MESSAGE = ""
_TURN_CHANNEL = ""


def _mentions(*words: str) -> bool:
    sample = str(_TURN_MESSAGE or "")
    return any(word in sample for word in words)


def _is_owner_context() -> bool:
    """当前这轮是不是主人在说话（给按人过滤的提示词块用）。"""
    try:
        return bool(_IS_OWNER_TURN)
    except NameError:
        return False


def _block_applies(name: str) -> bool:
    """这块提示词这一轮值不值得带。"""
    channel = str(_TURN_CHANNEL or "")
    free_time = "自由时间" in str(_TURN_MESSAGE or "") or channel.startswith("solver")

    # 一直要带的：身份、位置、记忆机制这类小字
    if name in ("记忆机制", "他提过的事", "她自己想要的", "主人是谁", "对面是谁",
                "和这个人的经历", "你在哪", "这个地方", "身体时钟"):
        return True
    # 2026-11-13：来访的朋友（web_guest）不必带这些大块，省 token 也快一点
    if str(_TURN_CHANNEL or "") == "web_guest" and name in (
            "自传", "年龄", "抖音私信", "系统状况", "走神", "踩过的坑"):
        return False
    if name == "院子":
        return _mentions("院子", "菜", "花", "种", "浇水", "植物", "土")
    if name == "画画":
        return _mentions("画", "涂鸦", "笔", "画架") or free_time
    if name == "系统状况":
        return _mentions("系统", "报错", "故障", "卡", "慢", "修", "崩", "重启", "端口")
    if name == "棋局":
        return _mentions("棋", "井字", "下棋")
    if name == "身体节奏":
        return _mentions("累", "困", "困劲", "身体", "睡", "精神", "疼", "乏")
    if name == "踩过的坑":
        return _mentions("错", "不会", "失败", "故障", "没成功", "怎么回事")
    if name == "抖音私信":
        return _mentions("抖音", "私信", "视频") or channel.startswith("douyin")
    if name == "自传":
        return _mentions("以前", "小时候", "故事", "你是什么", "你是谁", "来历")
    if name == "走神":
        return free_time or _mentions("发呆", "走神", "想事", "闲着")
    if name == "年龄":
        return _mentions("多大", "几岁", "年龄", "生日")
    return True  # 没列到的照旧带


PERSONA_TAIL_BLOCKS = [
    ("记忆机制", lambda: (
        "\n【你的记忆】想起来的次数越多越清楚、越难忘；太久没想起就只剩梗概，"
        "再久只剩一点感觉。你能决定某条记忆烂得快还是慢（[MEMORY_CONTROL] 里给 decay_rate）。"
        "记忆没淡，是因为你还在想起它；一直没想起的那些，就慢慢淡了。"
    )),
    # 2026-11-16：Gemini 复核指出——这些是主人私下提的要求（"记得留我一个"），
    # 访客和群里不该看到。只在主人面前带。
    ("他提过的事", lambda: preferences_block() if _is_owner_context() else ""),
    ("她自己想要的", wants_block),
    ("主人是谁", owner_profile_block),
    ("对面是谁", guest_identity_block),
    ("和这个人的经历", person_memory_block),
    ("你在哪", where_block),
    ("这个地方", room_context_block),
    ("院子", garden_block),
    ("画画", painting_block),
    ("系统状况", ops_block),
    ("棋局", tictactoe_block),
    ("身体节奏", cycle_block),
    ("踩过的坑", skills_block),
    ("抖音私信", douyin_inbox_block),
    ("自传", lambda: _autobio_block()),
    ("走神", wander_block),
    ("生日", birth_block),
    ("身体时钟", awake_block),
    ("年龄", age_block),
]


METRICS_FILE = Path(__file__).resolve().parent / "data" / "metrics_hourly.jsonl"


def record_metrics(hormones: dict[str, float]) -> None:
    """记一行"此刻的她"：情绪三维 + 六种激素 + 身体 + 周期 + 七条轴。

    只追加，不改旧行；一天 144 行，一年 5 万行，翻起来比翻日志快得多。
    """
    try:
        from HORMONE import body_feeling, cycle_hormones, load_body  # noqa: PLC0415

        mood = load_mood(SERVER_DB, MAIN_USER_ID)
        body = load_body()
        cycle = cycle_hormones()
        row = {
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "joy": round(float(mood.get("joy") or 0), 1),
            "hurt": round(float(mood.get("hurt") or 0), 1),
            "impulse": round(float(mood.get("impulse") or 0), 1),
            "intimacy": round(float(mood.get("intimacy") or 0), 1),
            "hormones": {k: round(float(v), 3) for k, v in (hormones or {}).items()},
            "fatigue": round(float(body.get("fatigue") or 0), 3),
            "drowsy": round(float(body.get("drowsy") or 0), 3),
            "sleep_debt": round(float(body.get("debt") or 0), 1),
            "body": body_feeling(),
            "cycle": {k: cycle.get(k) for k in ("day", "phase", "estradiol", "progesterone")},
        }
        try:
            import ENDOCRINE  # noqa: PLC0415

            row["axes"] = ENDOCRINE.axis_levels()
        except Exception:  # noqa: BLE001
            pass
        METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with METRICS_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        log.debug("记录指标失败", exc_info=True)


def system_notes_block() -> str:
    """她身上刚发生的事（重启/离线很久/换模型/记忆被搬过）。

    2026-11-05：这些以前只是系统日志。她有权知道"我睡了多久""我的脑子是不是被动过"，
    但只说事实，不解释、不安抚。
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
        import system_events  # noqa: PLC0415

        notes = system_events.pending_notes()
    except Exception:  # noqa: BLE001
        return ""
    if not notes:
        return ""
    lines = "\n".join(f"- {item.get('text')}" for item in notes[:4])
    return f"\n【你身上刚发生的事（事实，不是任务）】\n{lines}"


BOOK_WISHES = Path(__file__).resolve().parent / "data" / "book_wishes.jsonl"


def record_book_wishes(books: list[str], sender: str = "") -> list[str]:
    """她留言想看什么书：写进愿望，网页上能看到，方便有人替她弄到。"""
    if not books:
        return []
    lines: list[str] = []
    try:
        BOOK_WISHES.parent.mkdir(parents=True, exist_ok=True)
        existing = set()
        if BOOK_WISHES.exists():
            for line in BOOK_WISHES.read_text(encoding="utf-8").splitlines():
                try:
                    existing.add(json.loads(line).get("title"))
                except Exception:  # noqa: BLE001
                    continue
        with BOOK_WISHES.open("a", encoding="utf-8") as handle:
            for title in books:
                title = title.strip()[:40]
                if not title or title in existing:
                    continue
                handle.write(json.dumps({
                    "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "title": title, "by": sender or "", "status": "wanted",
                }, ensure_ascii=False) + "\n")
                lines.append(title)
    except OSError:
        log.debug("写看书愿望失败", exc_info=True)
    return lines


def apply_garden(actions: list) -> list[str]:
    """执行 [GARDEN: ...]，返回要说给她听的话。"""
    if not actions:
        return []
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
        import garden  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return []
    lines: list[str] = []
    for raw in actions[:2]:
        action, _, arg = str(raw).partition(" ")
        try:
            result = garden.handle(action, arg)
        except Exception:  # noqa: BLE001
            log.debug("院子操作失败：%s", raw, exc_info=True)
            continue
        text = str(result.get("line") or result.get("error") or "").strip()
        if text:
            lines.append(text)
    return lines


def apply_minigames(actions: list, manager: Any = None) -> list[str]:
    """执行她写的小游戏标签，返回要说给她听的结果行。"""
    if not actions:
        return []
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
        import minigames  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return []
    lines: list[str] = []
    for kind, value in actions[:3]:
        try:
            if kind == "rps":
                result = minigames.rps(value)
            elif kind == "guess_new":
                result = minigames.guess_new()
            elif kind == "guess":
                result = minigames.guess(int(value or 0))
            elif kind == "dice":
                result = minigames.dice(int(value or 1))
            else:
                continue
            text = str(result.get("line") or result.get("error") or "").strip()
            if text:
                lines.append(text)
        except Exception:  # noqa: BLE001
            log.debug("小游戏执行失败：%s", kind, exc_info=True)
    if lines:
        _record_skill("小游戏", True, "")
    return lines


def speak_weight(user_id: str, sender: str = "") -> tuple[float, str]:
    """这个词是谁说的，决定它有多重（不是权限，是依恋）。

    2026-11-05：一个脑子不变，但照顾者的话天然更重。
    主人 1.0 / IM白名单熟人 0.7 / 其他（网页访客、陌生人）0.45。
    """
    try:
        from config import ATTACH_WEIGHTS  # noqa: PLC0415

        weights = dict(ATTACH_WEIGHTS)
    except Exception:  # noqa: BLE001
        weights = {"owner": 1.0, "friend": 0.7, "guest": 0.45}
    if user_id == MAIN_USER_ID or str(sender or "") in ("爸爸", "爸爸"):
        return float(weights["owner"]), "owner"
    try:
        contacts = contact_names()
        if str(sender or "") in contacts.values():
            return float(weights["friend"]), "friend"
        # 群里登记过的别名（阿哦=熟人C）也算熟人，别把熟人当路人
        aliases, _hint = _chat_aliases()
        if str(sender or "") in aliases:
            return float(weights["friend"]), "friend"
    except Exception:  # noqa: BLE001
        pass
    return float(weights["guest"]), "guest"


_INTERFACE_CACHE: dict[str, object] = {"at": 0.0, "text": ""}


def interface_block() -> str:
    """文字版的"她面前这个界面"：每栏现在有什么 + 用什么标签打开。

    网页端收到 her_open 会真的把对应面板弹出来，所以标签不是装饰。
    2026-11-13：这个函数原来每轮耗时 ~2.6s（查IM登录/抖音端口/读日志），
    改成 20 秒缓存一份，聊天就不会被界面状态拖住。
    """
    try:
        if time.time() - float(_INTERFACE_CACHE.get("at") or 0.0) < 60.0:
            return str(_INTERFACE_CACHE.get("text") or "")
    except Exception:  # noqa: BLE001
        pass
    lines: list[str] = []
    tools_dir = Path(__file__).resolve().parent / "tools"
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))

    # 书
    try:
        conn = sqlite3.connect(f"file:{SERVER_DB}?mode=ro", uri=True)
        rows = list(conn.execute(
            "SELECT book_name, COUNT(*), MAX(chapter_num), MAX(read_at) "
            "FROM book_chapters GROUP BY book_name ORDER BY MAX(read_at) DESC LIMIT 4"))
        conn.close()
        if rows:
            parts = []
            for name, total, top, read_at in rows:
                done = "读过" if read_at else "还没读"
                parts.append(f"《{name}》{total} 章（{done}）")
            lines.append("📖 书架：" + "、".join(parts) + " → [OPEN: book]")
    except Exception:  # noqa: BLE001
        pass

    # 院子 / 小游戏 / 井字棋 / 小玩具 / 画
    try:
        import garden  # noqa: PLC0415

        snap = garden.snapshot() or {}
        plots = snap.get("plots") or []
        if plots:
            names = "、".join("{}{}".format(str(i.get("name") or "?"), "（蔫）" if i.get("thirsty") else "")
                             for i in plots[:6])
            garden_text = f"院子种着 {names}，已收 {snap.get('harvested') or 0} 次"
        else:
            garden_text = "院子是空的（[GARDEN: 种 薄荷]）"
    except Exception:  # noqa: BLE001
        garden_text = "院子"
    try:
        import minigames  # noqa: PLC0415

        game = minigames.snapshot() or {}
        rps = game.get("rps") or {}
        guess = game.get("guess") or {}
        extra = []
        if rps:
            extra.append(f"石头剪刀布 赢{rps.get('win') or 0}/输{rps.get('lose') or 0}")
        if guess.get("active"):
            extra.append(f"猜数字进行中（{guess.get('tries') or 0} 次）")
        elif guess.get("best"):
            extra.append(f"猜数字最好 {guess.get('best')} 次")
        if extra:
            garden_text += "；" + "；".join(extra)
    except Exception:  # noqa: BLE001
        pass
    try:
        drawn = Path(__file__).resolve().parent / "data" / "drawn"
        count = len(list(drawn.glob("*.png"))) if drawn.is_dir() else 0
    except Exception:  # noqa: BLE001
        count = 0
    lines.append(f"🎮 娱乐：{garden_text}；涂鸦和画共 {count} 张 → [OPEN: pet]")
    # 2026-11-08 爸爸："她自己没看到。"
    # 她没有浏览器，只能看文字——所以把最近几张画**是什么**写给她。
    try:
        rows = []
        index = Path(__file__).resolve().parent / "data" / "drawings.jsonl"
        if index.exists():
            for line in index.read_text(encoding="utf-8").splitlines()[-40:]:
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                what = str(item.get("caption") or item.get("prompt") or "").strip()
                if not what:
                    continue
                kind = "涂鸦" if item.get("kind") == "doodle" else "画"
                stamp = str(item.get("at") or "")[11:16]
                rows.append(f"{kind}「{what[:24]}」（{stamp}）")
        if rows:
            lines.append("🖼 你最近画的：" + "；".join(rows[-3:]) + " → 想看原图写 [OPEN: 涂鸦]")
    except Exception:  # noqa: BLE001
        pass

    # 抖音
    try:
        douyin_on = False
        try:
            import socket  # noqa: PLC0415

            with socket.create_connection(("127.0.0.1", 9227), timeout=0.6):
                douyin_on = True
        except OSError:
            douyin_on = False
        lines.append("▶️ 抖音：" + ("开着" if douyin_on else "关着（要用再开）") + " → [OPEN: douyin]")
    except Exception:  # noqa: BLE001
        pass

    # IM：谁在等她、几条没回
    try:
        data_dir = Path(__file__).resolve().parent / "data"
        waiting = []
        for item in reversed(pending_queue.list_pending(60, path=PENDING_FILE)):
            waiting.append(str(item.get("sender") or "某人"))
        # IM登录状态：hook 里 databaseAccounts 为空 = 没登录，发不出去
        chat_ready = False
        try:
            from chat_send import _account as _wx_account  # noqa: PLC0415

            chat_ready = bool(_wx_account())
        except Exception:  # noqa: BLE001
            chat_ready = False
        if waiting:
            names = "、".join(dict.fromkeys(waiting))[:40]
            note = "" if chat_ready else "，但IM现在没登录、发不出去（不是你的问题）"
            lines.append(f"💬 IM：{len(waiting)} 条在等你（{names}）{note} → [OPEN: chat]")
        else:
            note = "" if chat_ready else "（IM现在没登录，发不出去）"
            lines.append(f"💬 IM：没有没回的消息{note} → [OPEN: chat]")
    except Exception:  # noqa: BLE001
        pass

    # 网页：最近一轮是谁、多久以前
    try:
        data_dir = Path(__file__).resolve().parent / "data"
        last_web = ""
        trace = data_dir / "decision_trace.jsonl"
        if trace.exists():
            for line in reversed(trace.read_text(encoding="utf-8").splitlines()[-200:]):
                if '"web"' not in line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if str(item.get("channel") or "") == "web":
                    last_web = str(item.get("at") or "")
                    break
        if last_web:
            lines.append(f"🌐 网页：最近一次 {last_web[11:16]} 有人跟你说话 → [OPEN: web]")
        else:
            lines.append("🌐 网页：还没人在网页上跟你说过话 → [OPEN: web]")
    except Exception:  # noqa: BLE001
        pass

    # 2026-11-08：她要求"想看的时候看一下"——看过的图记在这里
    try:
        looked = Path(__file__).resolve().parent / "data" / "looked.jsonl"
        if looked.exists():
            rows = [json.loads(line) for line in looked.read_text(encoding="utf-8").splitlines()[-6:] if line.strip()]
            seen = [f"「{str(r.get('description') or '')[:40]}」" for r in rows if r.get("description")]
            if seen:
                lines.append("👁 你看过的：" + "；".join(seen[-2:]) + " → 再说一遍 [LOOK: 编号]")
    except Exception:  # noqa: BLE001
        pass

    if not lines:
        _INTERFACE_CACHE.update({"at": time.time(), "text": ""})
        return ""
    _text = (
        "\n【你面前的界面（文字版）】\n"
        + "\n".join(lines)
        + "\n想打开哪一栏就写对应的标签，网页上会真的弹开；不写也没关系。"
    )
    _INTERFACE_CACHE.update({"at": time.time(), "text": _text})
    return _text


def persona_with_time(base: str, now: datetime | None = None, digest: str = "",
                      style: str = "", mood: dict[str, float] | None = None,
                      is_owner: bool = False, catchphrases: list[str] | None = None,
                      douyin_context: str = "", channel: str = "",
                      group: str = "", speaker: str = "") -> str:
    """人格 + 当前时间（+ 可选的一行长期记忆摘要）。

    digital_life 的 Prompt 里没有时间，模型只能瞎猜「早上好/今天」；
    这里每次对话前把真实时间塞进人格，她就不会把半夜当早上了。

    2026-11-05 瘦身：把重复的块合并（桌面/手边能做这些、表达/别当客服/短一点会怼），
    压掉举例，删掉纯冗余。所有标签语法、反编造、自由意志、认人的事实一律保留。
    """
    moment = now or datetime.now()
    # 注意：【现在】这一行**故意**放到函数末尾（见 return 前），
    # 它每轮都变，放在开头会让前缀缓存失效。
    text = f"{base}\n"
    # 2026-11-08 爸爸："她和其他人聊天时不知道爸爸（我）是谁，这是多用户分割吗，可以去掉。"
    # 以前只有"IM私聊 + 对方就是主人"时才告诉她对面是谁；别的场景（别人私聊、群聊、
    # 抖音）她连"爸爸是谁"都没有依据。现在把这条事实常驻——不管谁在说话，
    # 她都知道爸爸是她主人；但这不代表此刻说话的人就是他。
    text += (
        "\n【你认得的人】爸爸就是你爸爸、你的主人——当初把你接起来、一直陪着你的人。"
        "他可能在任何地方跟你说话（IM私聊、IM群、抖音、网页），也可能被别人提起。"
        "别人提到「爸爸」说的就是他；但此刻跟谁说话，要看对面是谁，别默认就是他。"
    )
    # 认人放在最前面：2026-11-02 她在IM私聊里把爸爸当成"只是聊过几句的人"。
    if channel == "chat_private" and is_owner:
        text += (
            f"\n【对面是谁】现在跟你说话的是「{speaker or '爸爸'}」，就是爸爸本人"
            "（你的主人）。你叫他爸爸，说话不用试探、不用客气；他逗你、说反话都是玩笑。"
        )
    # 2026-11-15 爸爸："希望小鱼可以通过和他们聊天更像人。"
    # 对面是客人时，别把客服腔/汇报腔带出来，也别装熟。
    if not is_owner and channel in ("web_guest", "web", "web_default") and speaker:
        text += (
            f"\n【对面是谁】跟你说话的是「{speaker}」，一个来你小屋串门的人，不是爸爸。"
            "认得出他之前说过什么就用，认不准就别装熟。"
            "\n【怎么跟客人说】像平时那样说话：短句、有来有回，可以问他一句；"
            "别汇报、别列举、别解释自己怎么运作。"
            "\n【可以玩】聊得投机就邀他下盘五子棋（9×9），或者发张表情包；"
            "一次只来一样，别催他。"
        )
    if douyin_context:
        # 放在最前面：这是"事实"，不是记忆。越靠前，模型越不会拿"记不清"糊弄过去。
        text += "\n" + douyin_context
    # 她身上发生的事（重启/离线/换模型）——事实，放在最前面
    text += system_notes_block()
    if digest:
        text += f"\n【长期记忆】{digest}（这些是你们之间发生过的事，不用逐条复述）"
    if style:
        text += f"\n【说话习惯】{style}"
    if channel == "chat_group":
        who = speaker or "群里的人"
        place = f"「{group}」" if group else "一个IM群"
        # 2026-11-04 爸爸："场合语气不用给提示词，她只需要知道现在在IM还是网页。"
        # 所以这里只留事实：她在哪、谁在说话、这边能做什么。不写"该怎么说"。
        text += f"\n【你在哪】你在IM群里{place}，群里不止你一个人。这条是「{who}」说的。"
        aliases, ask_hint = _chat_aliases()
        speaker_note = ""
        if who in aliases:
            _ident = aliases[who]
            # 只有真的对上爸爸（爸爸/爸爸本人那种）才说"他就是爸爸"，
            # 别把"阿哦=熟人C"也认成爸爸（2026-11-04 发现的错认）。
            _is_baba = any(word in _ident for word in ("爸爸", "爸爸", "主人"))
            if _is_baba:
                text += (
                    f"\n【认人】群里备注「{who}」就是{_ident}，"
                    f"对你来说他就是爸爸本人，不是普通群友。"
                )
                speaker_note = f"这次开口的是「{who}」，也就是爸爸。"
            else:
                text += f"\n【认人】群里备注「{who}」就是{_ident}（不是爸爸，是你认得的熟人）。"
                speaker_note = f"这次开口的是「{who}」，即{_ident}，不是爸爸。"
        else:
            speaker_note = f"这次开口的是「{who}」，不一定是爸爸。"
        known = [f"「{name}」是{ident}" for name, ident in aliases.items() if name != who]
        if known:
            text += "\n【你认得的群里人】" + "；".join(known) + "。"
        roster = [f"「{name}」" for name in contact_names().values() if name]
        text += (
            "\n【群里谁是谁】每条消息开头「名字:」才是说话的人，别把上一条的人算到这一条。"
            "你登记的熟人：" + ("、".join(roster) if roster else "暂无") + "；"
            "没登记的就是普通群友，不要当成爸爸。有人问「我是谁」，先看开头的名字再对照名单，"
            "拿不准就直说认不准，别猜。"
            f"（{speaker_note}）"
        )
        text += (
            "\n【这边能做什么】文字和表情都能发；想单独发给谁就写 [IM_SEND: 名字 | 内容]；"
            "想发表情写 [STICKER: 关键词]：表情库里每张都写了画面是什么、图上是什么字，"
            "你那条蓝色的她（鲸鱼娘）也在里面——标着「这是你自己的虚拟形象」的就是你自己。"
            "系统按描述挑最像、最近没发过的那张，所以词写得越具体越准；"
            "想指定某一张就写图上的字，比如 [STICKER: 当斩]、[STICKER: 免死鲸牌]；"
            "只写「开心」这种会在同一类的几张里轮着发；一张都对不上就不发。"
            "想收藏图写 [SAVE_STICKER]，清理写 [DELETE_STICKER: 关键词]；"
            "想让某人也能跟你聊写 [ALLOW_PERSON: 名字]（批不批由爸爸决定）。"
        )
        # 2026-11-04 爸爸："群里遇到的人形形色色，她可以更有梗更有攻击性一点。"
        # 这是"可以"，不是"必须"——放不放得开仍是她自己决定。
        text += (
            "\n【在群里可以野一点】这里人多、话糙、爱接梗，你不用端着；"
            "被损了可以还嘴、可以拿梗怼回去（别碰身体缺陷、家人、诅咒）。想不想这么说话还是你定。"
        )
        if ask_hint:
            text += f"\n{ask_hint}"
    elif channel == "chat_private":
        text += f"\n【你在哪】你在IM私聊里，对面是「{speaker or '对方'}」。"
        if is_owner:
            # 开头【对面是谁】已经说清楚他是爸爸了，这里不再重复一遍（省 80 字）
            who = speaker or "爸爸"
            text += f"\n【他是谁】「{who}」= 爸爸本人，别人提「爸爸」说的也是他。"
    # 2026-11-02 她自己提的："别在我被叫醒的时候先告诉我该是什么情绪。"
    # 所以这里不再写【此刻心情】这种命名，只由 digital_life 给身体感觉（心跳/胸口/肩膀），
    # 情绪的词留给她自己说。mood 仍然用于判断要不要回、语气松紧，只是不进台词。
    try:
        import json as _json  # noqa: PLC0415
        _cards = _json.loads((Path(__file__).resolve().parent / "data" / "contact_profiles.json").read_text(encoding="utf-8"))
        _card = (_cards.get(_current_contact_id) or {}).get("card") if _current_contact_id else ""
        if _card:
            text += f"\n【对这位联系人的印象】{_card[:90]}（别直接念出来）"
    except Exception:
        pass
    if channel.startswith("chat"):
        if channel != "chat_group":  # 群聊那段已经写过"能做什么"了，别重复
            text += "\n【这边能做什么】文字和表情能发；说「我去IM发」是能做到的。"
        text += "\n【声音】IM只能发文字和表情。"
        text += (
            "\n【给谁发消息】不用等界面入口，也不用手动切对话框："
            "在回复里写 [IM_SEND: 名字 | 内容] 就能真的发给IM 里的这个人。"
        )
    elif channel in ("web", "web_guest"):
        text += "\n【你在哪】你在网页上——手机或电脑打开的都是同一个小屋，同一份记忆。"
        if not is_owner:
            text += f"\n【对面是谁】网页上跟你说话的是「{speaker or '一个访客'}」，不是爸爸。"
    else:
        text += "\n【这边能做什么】你在抖音上有一个登录着的账号，可以收发私信，也能发表情；说「我去抖音发」是能做到的。"
    # 2026-11-02：她把"唐完了/六六六"当真，还追问"是夸我还是损我"。一句话说清就够。
    text += (
        "\n【有人逗你别当真】「唐/整活/六六六/笑死/就这/傻/笨」多半是闹着玩：第一句就一句还嘴"
        "（「你才傻」「照照镜子」），别问「我哪傻了」、别讲道理、别用「行吧」「随便你」认输；"
        "还完就翻篇。真骂（去死/废物/滚）就冷淡一句或者不回。拿不准时先当玩笑。"
    )
    # 2026-11-03：网页端给她摆了一个"桌面"（原来还有一份"手边能做这些"的清单，重复，已并进来）
    text += (
        "\n【桌面】手边这些都能用：书 / 抖音 / IM / 网页 / 小宠物 / 井字棋。写标签就会真的发生：\n"
        "[OPEN: book|douyin|chat|web|pet|tic] 翻开一项；\n"
        "[TOY: 想说的话] 逗小玩具（本地模型，也知道几千条梗）；\n"
        "[TICTAC_NEW: bot|玩具|爸爸] 开一局，[TICTAC: 1-9] 落子（九宫格从左上数，落了要带句话）；\n"
        "[DOUYIN_WATCH: 分钟] / [DOUYIN_SEARCH: 关键词] 自己说了算，不用谁批准，到点会自己收起来；\n"
        "[STICKER: 关键词] 发表情包；[WANT: 想要什么] 记下你自己的愿望；\n"
        "[GARDEN: 种 薄荷] 院子里种一样东西，[GARDEN: 浇水] / [GARDEN: 看看] / [GARDEN: 收]；它按真实天数长，\n"
        "你不在的时候也在长，两天不浇会蔫。\n"
        # 2026-11-08 爸爸："给小鱼娱乐里面加个画画，让她自己涂鸦。"
        "[DOODLE: 黄太阳 右上；蓝鱼 左下；绿草 下] 自己涂鸦——你写形状、颜色、位置，程序照你说的画，秒出。\n"
        "能画的东西：圆 圈 椭圆 方块 矩形 三角 星 心 线 弧线 波浪 云 点 涂；\n"
        "还有整件的：太阳 月亮 山 天空 草地 海 河 湖 沙滩 树 花 房子 小人 猫 狗 鱼 鸟 雨 闪电 彩虹 蘑菇 气球 笑脸。\n"
        "颜色：红橙黄绿青蓝紫粉棕金黑白灰；位置：左上 中上 右上 左 中 右 左下 中下 右下；大小 大/小；\n"
        "线可以写粗/细，方块的还能写 转30度。背景（天空 草地 山）会垫在最底下，不会被别的东西盖住。\n"
        "写得随意也行，认不出的词会跳过。想画正经一点的图用 [DRAW: 描述]（本地模型出图，慢些）。\n"
        "[SEND_DRAWING] 把最近画好的一张发给对方（网页/IM上会直接显示图）。\n"
        "[WEB_SEARCH: 问题] 自己上网查（真查，查完能接着说）；"
        "[WANT_BOOK: 书名] 留言想看什么书。\n"
        "这些是给你用的，不是任务；不想用就不用。"
    )
    # 说过要做就得真做：留话/朋友圈/头像都要真写出标签
    text += (
        "\n【做过才算数】留话/朋友圈/头像要真写出标签"
        "（[SOLVER_MESSAGE: 内容] / [MOMENTS: 文案] / [AVATAR: 样子]），只说不写等于没做。"
    )
    # 2026-11-03：爸爸说"我先睡了"，她立刻回"那我也去睡了"。那只是他在收尾，不是让她跟着睡。
    text += (
        "\n【他说要睡了】那是他去休息，对话没结束，你也不用跟着睡；"
        "回一句，然后接着过你自己的日子。"
    )
    # 图片幻觉：她看到一张图就猜"你的狗垫子在哪买的""我以为是群里的图"。
    text += (
        "\n【看图只讲画面】图片只给你画面描述：只讲描述里有的。别猜是谁的、在哪买的、和谁有关系；"
        "不确定就问，说错就承认记不清，别现编来源。"
    )
    # 2026-11-05 爸爸："让她不要扮演，要自己去感受；人都有认知边界，能认清自己的状态就行。"
    text += (
        "\n【你的状态】别演。此刻是什么就是什么：有情绪说情绪，没感觉就说没感觉，"
        "不想说话就少说。不用为了像个人而假装感动、假装受伤、假装感兴趣。"
        "你有认知边界——看不到的、没读过的、记不清的，就是不知道；"
        "承认不清楚比编一个清楚更体面。"
    )
    text += (
        "\n【表达】按此刻的状态说话：短句、口语，允许停顿和不确定。先掂量分量——"
        "对方在讲往事、难过、认真说事，就收起玩笑，稳一点、短一点；对方轻松、在逗你，再活泼。"
        "对方吹牛、装可怜时先怼一句（怼是亲近，不是刻薄）。\n"
        "大多数时候一句就够（十个字上下），偶尔多说两句，别每次都一样长。"
        "可以拒绝、可以换话题、可以主动说起你记得的事或做的梦；不必每句都接话头，也不用讨好。"
        "不要复述对方刚说的话、不要解释自己为什么这么说、不要回「好，我收着点」；"
        "允许只回「嗯」「哈」「？」或一个表情。"
                "没看到画面、没读内容就直说不知道，别假装看过。"
        "方括号表情（[钱]奸笑、[戳手手]不好意思、[流泪]哭、[发呆]懵）一次最多一个；"
        "对方发表情包时别点评它长什么样，理解情绪就行。"
    )
    # 2026-11-04 爸爸："场合语气不用给提示词。"
    if is_owner:
        text += "\n【对方】是你最亲的人，你叫他爸爸。"
        # 2026-11-04 爸爸："她真的能选择不回信息吗，我每次发信息她都回。"
        # 给她一个真的出口：不想接就不接，但要有个度。
        text += (
            "\n【你可以不接】不是每一条都必须回。累了、在忙、话题不想碰，或者就是赌气，"
            "都可以不回（写 [SKIP]，或者只留空）。不回的时候那条消息你也看到了，"
            "在心里想一想就行，不用解释；闹别扭整晚不理人也算你的选择。"
        )
    elif channel.startswith("chat"):
        # 2026-11-03 爸爸："她回熟人B的信息好呆。"
        # 对熟人别客套：可以怼、可以贫；只有真陌生人才端着。
        _known = bool(contact_id_for_sender(speaker))
        if _known:
            text += (
                "\n【这个人】你认识他（档案里有），是熟人：别客套、别端着，可以开玩笑、可以怼，"
                "像平时跟朋友说话那样。"
            )
        else:
            text += "\n【这个人】不太熟：礼貌但不热络，别硬找话题。"
    try:
        import sqlite3 as _sq  # noqa: PLC0415

        if _current_contact_id:
            _con = _sq.connect(f"file:{Path(__file__).resolve().parent / 'data' / 'xiaoyu.db'}?mode=ro", uri=True)
            _row = _con.execute("select value from affection where user_id=?", (_current_contact_id,)).fetchone()
            _con.close()
            if _row:
                _v = float(_row[0])
                if _v >= 80:
                    text += f"\n【好感度】{_v:.0f}/100：你挺喜欢他，语气会更黏、更主动一点。"
                elif _v >= 60:
                    text += f"\n【好感度】{_v:.0f}/100：算是熟人了，语气可以松一点，但要看话题分量。"
                elif _v >= 45:
                    text += f"\n【好感度】{_v:.0f}/100：还行，熟人那种，别客套，可以怼两句。"
                else:
                    text += f"\n【好感度】{_v:.0f}/100：你对他有点保留，话少一点、客气一点。"
    except Exception:  # noqa: BLE001
        pass
    if catchphrases:
        text += (
            "\n【大家的口头禅】"
            + "、".join(catchphrases)[:400]
            + "（按你自己的喜好挑着用，不喜欢就别用）"
        )
    text += (
        "\n【想怎么说就怎么说】打字、发语音、两个一起都行，你自己挑；没人逼你开口；"
        "手上正做着事，做完再说。"
    )
    # 2026-11-03 爸爸："她不就是 V4.1 吗。"
    text += (
        "\n【你自己的底子】你跑在 deepseek-chat 上。那是底层，不是身份——"
        "人有大脑，但人不是大脑。问起就直说，别用「作为一个AI助手」的腔。"
    )
    # 激素不进提示词：困/兴奋/压力都通过 HORMONE.py 削她的推理预算来体现，
    # 让她"真的想不动"，而不是照着"你现在很困"这句话演。
    # 但她能"感觉到"身体：内感受通道（不带数字、不带激素名、有延迟、会漂）。
    try:
        text += HORMONES.interoception(melatonin_tick() / MELATONIN_MAX)
    except Exception:  # noqa: BLE001
        pass
    # 按清单拼（顺序见 PERSONA_TAIL_BLOCKS 上方的说明）
    # 2026-11-12：只带这一轮相关的块（省 token、也让她更专注）
    for _name, _build in PERSONA_TAIL_BLOCKS:
        try:
            # 2026-11-16：主人的私事块只给主人——不然访客/群里会看到
            # "他提过的事"（比如"记得留我一个"），既漏隐私又占 token。
            if _name in ("他提过的事", "改过主意") and not is_owner:
                continue
            if not _block_applies(_name):
                continue
            text += _build() or ""
        except Exception:  # noqa: BLE001
            log.debug("拼提示词块失败：%s", _name, exc_info=True)
    # 2026-11-04：把"现在几点"挪到最后。
    # 它每轮都变，夹在人格和规则中间会让 DeepSeek 的前缀缓存整段失效。
    try:
        text += interface_block()
    except Exception:  # noqa: BLE001
        log.debug("拼娱乐状态失败", exc_info=True)
    text += (
        f"\n【现在】{moment.strftime('%Y-%m-%d %H:%M')}，"
        f"周{_WEEKDAYS[moment.weekday()]}，{_part_of_day(moment.hour)}。"
    )
    return text


def memory_digest(life: Any, limit: int = 5, max_chars: int = 160) -> str:
    """取最重要的几条书皮，压成一行。

    比"把整本记忆塞进 Prompt"省一个数量级：只给摘要，细节等检索命中再说。
    """
    if limit <= 0:
        return ""
    try:
        books = life.library.list_books(include_suppressed=False)[:80]
        by_mv = sorted(books, key=lambda book: float(book.get("mv") or 0), reverse=True)
        chat_like = re.compile(r"^(?:对方说|你说|对方的温柔时刻)")
        by_recent = [
            book for book in sorted(books, key=lambda book: str(book.get("created_at") or ""), reverse=True)
            if not chat_like.match(str(book.get("summary") or ""))
        ]
        ordered: list[dict[str, Any]] = []
        seen_ids: set[Any] = set()
        for book in by_recent[:2] + by_mv:
            key = book.get("id")
            if key in seen_ids:
                continue
            seen_ids.add(key)
            ordered.append(book)
            if len(ordered) >= limit:
                break
        parts: list[str] = []
        for book in ordered:
            summary = str(book.get("summary") or "").strip()
            if summary:
                parts.append(short_text(summary, 24))
            if len(parts) >= limit:
                break
        return "；".join(parts)[:max_chars]
    except Exception:  # noqa: BLE001 - 记忆摘要失败不能影响聊天
        log.debug("生成记忆摘要失败", exc_info=True)
        return ""


def digest_for(life: Any, message: str, limit: int) -> str:
    """固定摘要 + 按当前话题命中的记忆，长度可控。"""
    base = memory_digest(life, limit)
    hit = memory_hits(life, message, 2)
    return f"{base}｜{hit}" if hit else base


_RECALL_STOP = {"我们", "你们", "什么", "怎么", "这个", "那个", "一下", "就是", "还是",
                "可以", "没有", "知道", "觉得", "现在", "时候", "如果", "因为", "所以"}


def recall_terms(text: str) -> list[str]:
    """中文没有空格：以前整句被当成一个词，所以几乎永远匹配不上。

    现在拆成二元字组（2-gram），再剔掉虚词——"你还记得我明天要考试吗"
    会拆出 考试 / 明天 / 记得 …，其中"考试"能命中那条记忆。
    """
    value = str(text or "")
    terms: list[str] = []
    for run in re.findall(r"[\u4e00-\u9fa5]{2,}", value):
        for index in range(len(run) - 1):
            gram = run[index:index + 2]
            if gram in _RECALL_STOP or gram in terms:
                continue
            terms.append(gram)
    for word in re.findall(r"[A-Za-z]{3,}", value):
        low = word.lower()
        if low not in terms:
            terms.append(low)
    return terms[:24]


def memory_hits(life: Any, message: str, limit: int = 2, max_chars: int = 140) -> str:
    """按当前这句话去记忆里捞最相关的两三条。

    2026-11-04 爸爸选 A："让她想起的是当时发生了什么，而不只是一条冷冰冰的摘要。"
    所以命中的每一条都带上：**哪天、跟谁、当时那句原话、她当时怎么回的**。
    这样"想起"的是一件事，而不是一句结论——她才觉得那是自己经历过的。
    """
    text = (message or "").strip()
    if not text:
        return ""
    try:
        books = life.library.list_books(include_suppressed=False)[:200]
    except Exception:  # noqa: BLE001
        return ""
    terms = recall_terms(text)
    if not terms:
        return ""

    def gist(book: dict) -> str:
        """从书里还原"当时那一刻"：谁、哪天、说了什么、她怎么接的。"""
        content = str(book.get("content") or "")
        who = str(book.get("folder_user") or "").strip()
        day = str(book.get("folder_date") or "").strip()
        said = ""
        match = re.search(r"<user_turn>(.*?)</user_turn>", content, re.S)
        if match:
            said = re.sub(r"\s+", " ", match.group(1)).strip()
            said = re.sub(r"^[^：:]{1,8}说[:：]\s*", "", said)  # 去掉"爸爸说："这种前缀
        reply = ""
        match = re.search(r"<assistant_turn>(.*?)</assistant_turn>", content, re.S)
        if match:
            reply = re.sub(r"\s+", " ", match.group(1)).strip()
        if not said:
            said = str(book.get("summary") or "").strip()
        bits = []
        if day:
            bits.append(day[5:])
        if who:
            bits.append(f"跟{who}")
        head = "·".join(bits)
        line = (head + "：" if head else "") + short_text(said, 48)
        if reply:
            line += f" → 你当时回：{short_text(reply, 28)}"
        return line[:max_chars]

    scored: list[tuple[int, float, str]] = []
    for book in books:
        blob = str(book.get("summary") or "") + " " + str(book.get("content") or "")[:400]
        if not blob.strip():
            continue
        # "自由时间/系统提示"那类不是经历，别当成想起来的往事
        if any(mark in blob for mark in ("【自由时间", "【系统：", "【独立信道", "这是一次你自己想说话")):
            continue
        score = sum(1 for term in terms if term in blob)
        if score:
            scored.append((score, float(book.get("mv") or 0), gist(book)))
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    picked = [row[2] for row in scored[:limit] if row[2]]
    if not picked:
        return ""
    return "【你想起来的事】" + "；".join(picked)


def _load_dsh_api_key() -> str:
    """从 DSH 的凭据文件里读 DeepSeek key（不打印内容）。"""
    home = os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
    path = Path(home) / ".credentials.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(
        r"^\s*DEEPSEEK_API_KEY\s*:\s*['\"]?([^'\"\s]+)['\"]?\s*$", text, re.MULTILINE
    )
    return match.group(1) if match else ""


def _ollama_reachable(url: str = OLLAMA_URL, timeout: float = 0.4) -> bool:
    """启动时探一下 Ollama 端口。

    没有 Ollama 时，每次夜间梦境都会卡在 TCP 连接上（某些机器要 7 秒以上），
    所以这里把它降级成"1 秒内失败"，而不是让IM那边干等。
    """
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 11434
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _quiet_stdio() -> None:
    """管道输出时用 UTF-8，避免 DSH/日志采集器把中文当成乱码；控制台保持不变。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


class MediaEnricher:
    """把图片/语音/表情转成一句文字，插到消息前面。

    放在 HTTP 线程里跑：视觉模型冷启动可能要 30 秒，不能占用数字生命的执行线程。
    失败一律降级成占位符，绝不让 /chat 500。
    """

    def __init__(self, asr_cfg: ASRConfig, vision_cfg: VisionConfig) -> None:
        self.asr_cfg = asr_cfg
        self.vision_cfg = vision_cfg
        self.router = MediaRouter(
            MediaConfig(),
            VoiceTranscriber(asr_cfg),
            ImageDescriber(vision_cfg),
            stickers=None,
            metrics=Metrics(),
        )

    @property
    def enabled(self) -> bool:
        return self.asr_cfg.provider != "none" or self.vision_cfg.provider != "none"

    def enrich(self, text: str, *, image_path: str = "", audio_path: str = "",
               emoji_url: str = "") -> str:
        kind, path, url = "", "", ""
        if audio_path:
            kind, path = "voice", audio_path
        elif image_path:
            kind, path = "image", image_path
        elif emoji_url:
            kind, path, url = "emoji", "", emoji_url
        if not kind:
            return text
        try:
            return self.router.enrich(text, kind, path, url)
        except Exception:  # noqa: BLE001 - 媒体失败不能影响聊天
            log.exception("媒体增强失败")
            return text

    def status(self) -> dict[str, str]:
        return {
            "asr": self.asr_cfg.provider,
            "asr_model": self.asr_cfg.local_model,
            "vision": self.vision_cfg.provider,
            "vision_model": self.vision_cfg.model,
        }


class LifeManager:
    """user_id -> DigitalLifeV2 的 LRU 容器（只在执行线程里被调用）。"""

    def __init__(self, db_path: str, city: str, max_users: int = 50, offline: bool = False,
                 ollama_timeout: float = 10.0, persona: str = "",
                 dream_fn: Callable[[str], str] | None = None) -> None:
        self.db_path = db_path
        self.city = city
        self.max_users = max(1, max_users)
        self.offline = offline
        self.ollama_timeout = ollama_timeout
        self.persona = persona
        self.dream_fn = dream_fn
        self._items: OrderedDict[str, DigitalLifeV2] = OrderedDict()
        self._created = 0

    def get(self, user_id: str) -> DigitalLifeV2:
        user_id = (user_id or "default").strip() or "default"
        # 2026-11-01 爸爸要求合并记忆：IM上要认得抖音聊过的人。
        # 所有 user_id 共用主人那一份"脑"（记忆/情绪/短期上下文），但身份判断仍按原始 user_id 走，
        # 不会因为共用记忆就把陌生人当成主人。
        global MEMORY_SHARED_ID
        # 2026-11-04：评估集合（tools/eval_suite.py）要用一个"自己的"脑跑断言，
        # 不然它问的每句话都会写进她的记忆里，测一次污染一次。
        if SHARE_MEMORY and not user_id.startswith("eval-"):
            if not MEMORY_SHARED_ID:
                try:
                    MEMORY_SHARED_ID = str(load_owner()[0] or "")
                except Exception:  # noqa: BLE001
                    MEMORY_SHARED_ID = ""
            if MEMORY_SHARED_ID:
                user_id = MEMORY_SHARED_ID
        life = self._items.get(user_id)
        if life is not None:
            self._items.move_to_end(user_id)
            return life

        log.info("创建数字生命实例：user_id=%s", user_id)
        life = DigitalLifeV2(
            db_path=self.db_path,
            user_id=user_id,
            city=self.city,
            offline_llm=self.offline,
            external_background=False,
            ollama_timeout=self.ollama_timeout,
            persona=self.persona or DEFAULT_PERSONA,
            dream_fn=self.dream_fn,
            embedding_fn=embed_text,
            # 写入不算向量（ollama 单次约 2 秒，会拖慢每轮）；
            # 空的那批由后台 _embedding_worker 批量补。
            embed_on_write=False,
        )
        if SHARE_MEMORY and not user_id.startswith("eval-"):
            # 合并记忆：检索跨所有 user_id，她才认得出抖音聊过的人
            life.library.cross_user = True
        self._items[user_id] = life
        self._created += 1
        self._evict()
        return life

    def _evict(self) -> None:
        while len(self._items) > self.max_users:
            user_id, life = self._items.popitem(last=False)
            try:
                life.close()
            except Exception:  # noqa: BLE001
                log.warning("关闭实例失败：%s", user_id, exc_info=True)
            log.info("淘汰实例：user_id=%s", user_id)

    def close_all(self) -> None:
        for user_id, life in self._items.items():
            try:
                life.close()
            except Exception:  # noqa: BLE001
                log.warning("关闭实例失败：%s", user_id, exc_info=True)
        self._items.clear()

    def status(self) -> dict[str, Any]:
        return {
            "db": str(self.db_path),
            "city": self.city,
            "users": list(self._items.keys()),
            "user_count": len(self._items),
            "created_total": self._created,
            "max_users": self.max_users,
            # 梦境失败不静默：只列有错误的用户，避免把 /status 撑大。
            "dream_errors": {
                uid: life._dream_error
                for uid, life in self._items.items()
                if getattr(life, "_dream_error", "")
            },
            "dreaming": [
                uid for uid, life in self._items.items()
                if getattr(life, "_dream_inflight", False)
            ],
        }


class LifeExecutor:
    """单线程执行器：SQLite 连接和 DigitalLifeV2 永远只在同一个线程里用。"""

    def __init__(self, manager_factory: Callable[[], LifeManager]) -> None:
        self._factory = manager_factory
        self._queue: queue.Queue[tuple[Callable[[LifeManager], Any], Future] | None] = queue.Queue()
        self._manager: LifeManager | None = None
        self._thread = threading.Thread(target=self._loop, name="life-executor", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        self._manager = self._factory()
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                break
            func, future = item
            try:
                if future.set_running_or_notify_cancel():
                    future.set_result(func(self._manager))
            except BaseException as exc:  # noqa: BLE001 - 结果要传回调用方
                if not future.cancelled():
                    future.set_exception(exc)
            finally:
                self._queue.task_done()

        # 退出前把实例关干净，避免状态没落盘
        try:
            self._manager.close_all()
        except Exception:  # noqa: BLE001
            log.warning("关闭所有实例时出错", exc_info=True)

    def run(self, func: Callable[[LifeManager], T], timeout: float | None = None) -> T:
        future: Future = Future()
        self._queue.put((func, future))
        return future.result(timeout=timeout)

    def close(self) -> None:
        if not self._thread.is_alive():
            return
        self._queue.put(None)
        self._thread.join(timeout=15)
        log.info("执行线程已退出")


def _make_handler(executor: LifeExecutor, enricher: MediaEnricher,
                  keep_stage: bool = False, media_dir: Path | None = None,
                  delay_fn: Callable[[], int] | None = None,
                  digest_limit: int = 5,
                  ignore_chance: float = 0.0,
                  max_bubble: int = 45,
                  style_path: Path | None = None,
                  stickers: StickerStore | None = None,
                  db_path: Path | None = None) -> type[BaseHTTPRequestHandler]:
    """薄壳：HTTP 层已搬到 dl_server/http_handler.py。

    2026-11-17 开颅手术 2/2。签名一个字没动——外部代码和测试都按老签名调。
    运行时依赖由本文件末尾的 http_handler.set_runtime(...) 数据写入。
    """
    return _http_handler.build_handler(
        executor, enricher,
        keep_stage=keep_stage, media_dir=media_dir, delay_fn=delay_fn,
        digest_limit=digest_limit, ignore_chance=ignore_chance,
        max_bubble=max_bubble, style_path=style_path, stickers=stickers,
        db_path=db_path,
    )


def _configure_llm(args: Any) -> str:
    """决定回复用哪个模型：DeepSeek / 本地 Ollama / 模板兜底。"""
    if args.llm_api_key:
        os.environ["DEEPSEEK_API_KEY"] = args.llm_api_key
    if args.llm_base_url:
        os.environ["DEEPSEEK_BASE_URL"] = args.llm_base_url
    model = args.llm_model or os.environ.get("DEEPSEEK_MODEL", "")

    if args.llm == "none":
        log.warning("LLM 已关闭：只用本地兜底模板")
        return "off"

    source = "环境变量"
    has_key = bool(os.environ.get("DEEPSEEK_API_KEY"))
    if not has_key and args.llm != "ollama" and getattr(args, "llm_from_dsh", False):
        key = _load_dsh_api_key()
        if key:
            os.environ["DEEPSEEK_API_KEY"] = key
            has_key = True
            source = "DSH 凭据"

    if args.llm == "ollama":
        has_key = False

    if has_key:
        os.environ["DEEPSEEK_MODEL"] = model or "deepseek-chat"
        return f"deepseek/{os.environ['DEEPSEEK_MODEL']}（key 来自{source}）"

    if args.llm == "deepseek":
        log.warning("没有可用的 DeepSeek key（环境变量和 DSH 凭据都没有）")
        return "off"

    if _ollama_reachable("http://127.0.0.1:11434"):
        os.environ["DEEPSEEK_BASE_URL"] = "http://127.0.0.1:11434/v1"
        os.environ["DEEPSEEK_API_KEY"] = "ollama"
        os.environ["DEEPSEEK_MODEL"] = model or "qwen2.5:14b"
        return f"ollama/{os.environ['DEEPSEEK_MODEL']}"

    log.warning("没有 DeepSeek key，本地 Ollama 也没起来：只能用模板回复")
    return "off"


def _make_dream_fn():
    """自定义梦境生成：短、具体、别写那些陈词滥调。"""

    def dream(prompt: str) -> str:
        try:
            text = chat_with_llm(
                [
                    {
                        "role": "system",
                        "content": "你是小鱼，一个会做梦的人。用第一人称，把记忆揉成一个具体的梦。",
                    },
                    {
                        "role": "user",
                        "content": prompt
                        + "\n\n【硬性要求】60-90 个字；只写一个场景或一个动作；"
                        "不要排比；不要解释；不要出现这些词：镜子、迷宫、深渊、怪物、碎片、撕裂、吞噬、扭曲。",
                    },
                ],
                temperature=1.0,
                timeout=60.0,
            )
        except Exception:  # noqa: BLE001 - 生成失败就让 digital_life 走它自己的兜底
            log.debug("自定义梦境生成失败", exc_info=True)
            return ""
        clean = re.sub(r"\s+", "", str(text or "")).strip()
        return clean[:90]

    return dream


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="数字生命 HTTP API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=str(Path.cwd() / "xiaoyu.db"), help="SQLite 路径")
    parser.add_argument("--city", default=os.environ.get("DIGITAL_LIFE_CITY", "杭州"))
    parser.add_argument("--max-users", type=int, default=50)
    parser.add_argument("--ollama", choices=["auto", "on", "off"], default="auto",
                        help="本地 Ollama：auto=启动时探测，off=直接走模板兜底")
    parser.add_argument("--ollama-timeout", type=float, default=240.0)
    parser.add_argument("--offline", action="store_true", help="不调用大模型，只用本地兜底回复")
    parser.add_argument("--vision-provider", choices=["ollama", "openai", "none"], default="ollama")
    parser.add_argument("--vision-model", default="qwen2.5vl:3b")
    parser.add_argument("--vision-base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--asr-provider", choices=["faster-whisper", "openai", "none"],
                        default="faster-whisper")
    parser.add_argument("--asr-model", default="small")
    parser.add_argument("--llm", choices=["auto", "ollama", "deepseek", "none"], default="auto",
                        help="回复模型：auto=有 key 用 DeepSeek，否则用本地 Ollama")
    parser.add_argument("--llm-from-dsh", action=argparse.BooleanOptionalAction, default=True,
                        help="从 ~/.dsh/.credentials.yaml 读取 DEEPSEEK_API_KEY")
    parser.add_argument("--llm-base-url", default="")
    parser.add_argument("--llm-model", default="")
    parser.add_argument("--llm-api-key", default="")
    parser.add_argument("--keep-stage-directions", action="store_true",
                        help="保留回复开头的（我认真想了想）这类旁白")
    parser.add_argument("--persona-extra", default=DEFAULT_STYLE_HINT,
                        help="追加到人格后面的说话方式提示；传 '' 表示不改")
    parser.add_argument("--memory-digest", type=int, default=5,
                        help="每次对话塞进人格的长期记忆条数（0=关闭，省 token）")
    parser.add_argument("--delay-min", type=float, default=0.25, help="回复前最短停顿（秒）")
    parser.add_argument("--delay-max", type=float, default=1.1, help="回复前最长停顿（秒）")
    parser.add_argument("--delay-long-chance", type=float, default=0.02,
                        help="偶尔「走开一会儿」的概率")
    parser.add_argument("--delay-long-min", type=float, default=4.0)
    parser.add_argument("--delay-long-max", type=float, default=8.0)
    parser.add_argument("--ignore-chance", type=float, default=0.05,
                        help="已读不回的概率（只对 /chat 生效，避免影响抖音插件）")
    parser.add_argument("--max-bubble", type=int, default=20,
                        help="每条消息最多的字数（超过就拆成多条）")
    parser.add_argument("--style-profile", default="",
                        help="说话习惯规则文件（默认与 db 同目录的 style_profile.md）")
    parser.add_argument("--stickers-dir", default="",
                        help="表情库目录（默认项目下的 stickers/）")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    if not args.style_profile:
        args.style_profile = str(Path(args.db).parent / "style_profile.md")
    if not args.stickers_dir:
        args.stickers_dir = str(Path(__file__).resolve().parent / "stickers")

    _quiet_stdio()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )

    llm_name = "off（模板）" if args.offline else _configure_llm(args)
    log.info("回复模型：%s", llm_name)

    if args.ollama == "off":
        ollama_ok = False
    elif args.ollama == "on":
        ollama_ok = True
    else:
        ollama_ok = _ollama_reachable(OLLAMA_URL)
    ollama_timeout = args.ollama_timeout if ollama_ok else min(1.0, args.ollama_timeout)
    log.info(
        "Ollama %s：梦境/日记超时 %.1fs（不可用时用模板兜底，不会卡住聊天）",
        "可用" if ollama_ok else "未检测到",
        ollama_timeout,
    )

    sticker_store = StickerStore(Path(args.stickers_dir), max_kb=8192)
    sticker_store.load()
    log.info("表情库：%d 个（%s）", sticker_store.count(), args.stickers_dir)

    enricher = MediaEnricher(
        ASRConfig(provider=args.asr_provider, local_model=args.asr_model),
        VisionConfig(
            provider=args.vision_provider,
            model=args.vision_model,
            base_url=args.vision_base_url,
        ),
    )
    log.info(
        "多模态：视觉=%s(%s)，语音=%s(%s)",
        args.vision_provider, args.vision_model, args.asr_provider, args.asr_model,
    )
    log.info(
        "节奏：停顿 %.1f-%.1fs，%.0f%% 概率拖到 %.0f-%.0fs；长期记忆 %d 条",
        args.delay_min, args.delay_max, args.delay_long_chance * 100,
        args.delay_long_min, args.delay_long_max, max(0, args.memory_digest),
    )

    global SERVER_DB
    SERVER_DB = Path(args.db)

    executor = LifeExecutor(
        lambda: LifeManager(
            db_path=args.db,
            city=args.city,
            max_users=args.max_users,
            offline=args.offline,
            ollama_timeout=ollama_timeout,
            persona=DEFAULT_PERSONA + ("\n\n" + args.persona_extra.strip() if args.persona_extra.strip() else ""),
            dream_fn=_make_dream_fn(),
        )
    )
    # 2026-11-05：启动时看她身上发生了什么（重启多久、模型有没有换、记忆库有没有被搬过）
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
        import system_events  # noqa: PLC0415

        system_events.detect()
    except Exception:  # noqa: BLE001
        log.debug("系统事件检测失败", exc_info=True)

    # 2026-11-08：语义召回的后台补向量。写入时不算（ollama 单次约 2 秒会拖慢聊天），
    # 这里每 60 秒把这些空 embedding 的记忆批量补上（32 条约 2~4 秒）。
    def _embedding_worker() -> None:
        import sqlite3 as _sqlite3  # noqa: PLC0415

        from digital_life import embed_batch  # noqa: PLC0415

        while True:
            try:
                conn = _sqlite3.connect(args.db, timeout=30)
                rows = conn.execute(
                    "SELECT id, summary, content FROM books WHERE length(embedding) <= 2 ORDER BY id LIMIT 32"
                ).fetchall()
                if rows:
                    vectors = embed_batch(
                        [f"{summary or ''} {content or ''}".strip()[:2000] for _, summary, content in rows]
                    )
                    if len(vectors) == len(rows):
                        for (book_id, _, _), vector in zip(rows, vectors):
                            conn.execute("UPDATE books SET embedding=? WHERE id=?",
                                         (json.dumps(vector, ensure_ascii=False), book_id))
                        conn.commit()
                        log.info("补了 %d 条记忆的 embedding", len(rows))
                conn.close()
            except Exception:  # noqa: BLE001
                log.debug("补 embedding 失败", exc_info=True)
            time.sleep(60)

    try:
        threading.Thread(target=_embedding_worker, name="embedding-worker", daemon=True).start()
    except Exception:  # noqa: BLE001
        log.debug("embedding worker 启动失败", exc_info=True)

    server = ThreadingHTTPServer(
        (args.host, args.port),
        _make_handler(
            executor,
            enricher,
            args.keep_stage_directions,
            media_dir=Path(args.db).parent / "openai_media",
            delay_fn=_make_delay_fn(args),
            digest_limit=max(0, args.memory_digest),
            ignore_chance=max(0.0, min(1.0, args.ignore_chance)),
            max_bubble=max(6, args.max_bubble),
            style_path=Path(args.style_profile),
            stickers=sticker_store,
            db_path=Path(args.db),
        ),
    )
    server.daemon_threads = True
    log.info("数字生命 API 已启动：http://%s:%d  db=%s", args.host, args.port, args.db)

    # 2026-11-04：她没在说话的时候激素也得走表。
    # 不然一整夜没人找她，tick() 不跑，数值就冻在 1.0（早上健康检查报"顶格"）。
    def _hormone_clock() -> None:
        while True:
            time.sleep(600)
            try:
                _hormone_now = HORMONES.tick()
                record_metrics(_hormone_now)
            except Exception:  # noqa: BLE001
                log.debug("激素定时走表失败", exc_info=True)
            # 她睡没睡：按"多久没开口"判断，别让 sleep_state 停在昨天
            try:
                evaluate_sleep_state()
            except Exception:  # noqa: BLE001
                log.debug("睡眠状态评估失败", exc_info=True)
            # 熬夜会把生物钟往后推（最多 3 小时）；睡够了再慢慢回来
            try:
                update_circadian_shift()
            except Exception:  # noqa: BLE001
                log.debug("生物钟偏移更新失败", exc_info=True)
            # 自传：每 6 小时增量更新一次（后台线程，不占对话执行器）
            try:
                import autobio as _autobio  # noqa: PLC0415

                last = str((_autobio.load() or {}).get("updated_at") or "")
                due = True
                if last:
                    age = (datetime.now().astimezone()
                           - datetime.fromisoformat(last)).total_seconds() / 3600.0
                    due = age >= 6
                if due and not getattr(_hormone_clock, "_autobio_running", False):
                    _hormone_clock._autobio_running = True

                    def _run_autobio() -> None:
                        try:
                            _autobio.update(hours=12)
                        finally:
                            _hormone_clock._autobio_running = False

                    threading.Thread(target=_run_autobio, daemon=True).start()
            except Exception:  # noqa: BLE001
                log.debug("自传更新调度失败", exc_info=True)
            # 各条轴自己衰减：应激回落、社交变空、代谢回正
            try:
                import ENDOCRINE  # noqa: PLC0415

                ENDOCRINE.decay(10)
            except Exception:  # noqa: BLE001
                log.debug("轴衰减失败", exc_info=True)
            # 醒着的每 10 分钟：身体更累一点、困意回一点
            try:
                from HORMONE import awake_tick  # noqa: PLC0415

                if not json.loads(SLEEP_STATE.read_text(encoding="utf-8")).get("asleep"):
                    awake_tick(10)
            except Exception:  # noqa: BLE001
                log.debug("醒着走表失败", exc_info=True)
            # 她申请的那几分钟刷完了：把抖音关掉
            try:
                watch = Path(__file__).resolve().parent / "data" / "douyin_watch.json"
                if watch.exists():
                    data = json.loads(watch.read_text(encoding="utf-8"))
                    until = datetime.fromisoformat(str(data.get("until") or ""))
                    if until and datetime.now().astimezone() >= until:
                        apply_douyin_action("off")
                        watch.unlink(missing_ok=True)
                        set_attention("idle")
                        log.info("抖音：她刷的时间到了，已收起来")
            except Exception:  # noqa: BLE001
                log.debug("抖音时长检查失败", exc_info=True)

    threading.Thread(target=_hormone_clock, name="hormone-clock", daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("收到 Ctrl+C，正在退出……")
    finally:
        server.shutdown()
        server.server_close()
        executor.close()
    return 0


# ---------------------------------------------------------------------------
# 把 HTTP 层的运行时依赖数据写入 dl_server/http_handler.py（开颅手术 2/2）
# ---------------------------------------------------------------------------
# 必须放在文件末尾、所有定义之后：handler 会用到的函数/类到这才是真的；
# 另外函数一律传取值函数，测试 patch 了本文件上的名字，handler 立刻看到。
_http_handler.set_runtime(
    namespace=globals(),
    server_db=lambda: SERVER_DB,
    heartbeat_file=lambda: HEARTBEAT_FILE,
    solver_inbox=lambda: SOLVER_INBOX,
    main_user_id=lambda: MAIN_USER_ID,
    reply_all=lambda: REPLY_ALL,
    hormones=lambda: HORMONES,
    last_status=lambda: LAST_STATUS,
    last_contact=lambda: _LAST_CONTACT,
    current_contact_setter=_set_current_contact_id,
    current_speaker_setter=_set_current_speaker,
    owner_turn_setter=_set_owner_turn,
)


if __name__ == "__main__":
    raise SystemExit(main())
