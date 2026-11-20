#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""她 · 网页版。

    python web_ui.py                 # http://127.0.0.1:8899
    python web_ui.py --port 9000 --api http://127.0.0.1:8000

它不重复实现任何逻辑：消息转给 `dl_api` 的 /chat、/proactive，
表情包读的是同一个 `stickers/` 目录。所以网页里聊出来的记忆，
和以后IM版是同一份（同一个 user_id）。

只用标准库 + 项目里的 bridge 包，不引入前后端框架。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import hmac
import html as html_lib
import json
import mimetypes
import os
import random
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from http.cookies import SimpleCookie
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TOOLS_DIR = str(ROOT / "tools")
if _TOOLS_DIR not in sys.path:
    sys.path.append(_TOOLS_DIR)
import pending_queue  # noqa: E402
from bridge.client import ChatReply, DigitalLifeClient, DigitalLifeError  # noqa: E402
from bridge.config import ApiConfig  # noqa: E402
from bridge.logs import get_logger  # noqa: E402
from bridge.stickers import StickerStore  # noqa: E402
from bridge.worker import mood_tag  # noqa: E402

# 她自己的 user_id：放到环境变量里，别散落在代码中（2026-11-05 打包前整理）
MAIN_USER_ID = os.environ.get("XIAOYU_MAIN_USER") or "conv:9e52b4549f424e730d8871644385bcce63768cc74b8f8ec7ffa895f62ca73eeb"

log = get_logger("web")

DATA_DIR = ROOT / "data"
DB_FILE = DATA_DIR / "xiaoyu.db"

# 她自己发不出去、只能托人办的事：发朋友圈 / 换头像。
# 由 digital_life 的 [MOMENTS: ...] / [AVATAR: ...] 写进来，网页端给爸爸看。
OWNER_REQUESTS = DATA_DIR / "owner_requests.jsonl"
# 最高权限：能看桌面所有面板、能改她的东西
PRIVILEGED_KEYS = ("FATHER", "SOLVER")


def is_owner_view(session: dict[str, Any] | None) -> bool:
    """是不是"主人自己的视角"：最高权限，或者指向主库的通道视图。"""
    if is_privileged(session):
        return True
    # 2026-11-15 修：所有账号的 api_user_id 都是 MAIN_USER_ID，原来等于"谁登录谁是主人"。
    # 现在只认最高权限账号；普通访客不算主人视角。
    try:
        if str((session or {}).get("surface") or "") in ("chat", "douyin"):
            return True
        return False
    except Exception:  # noqa: BLE001
        return False


def is_privileged(session: dict[str, Any] | None) -> bool:
    return str((session or {}).get("key") or "") in PRIVILEGED_KEYS


WISH_LABEL = {"moments": "想发朋友圈", "avatar": "想换头像"}

# 她的声音（tools/tts_server.py，Breeze TTS 2）。没起服务时网页只是不出声，不影响聊天。
TTS_URL = os.environ.get("XIAOYU_TTS_URL", "http://127.0.0.1:7861")
TTS_TIMEOUT = float(os.environ.get("XIAOYU_TTS_TIMEOUT", "240"))

# 异步 RVC：Breeze 先出声，worker 再把声音换成她的音色（CPU，13 秒/句）
RVC_CONFIG = ROOT / "data" / "voice" / "rvc_config.json"
RVC_JOBS = ROOT / "data" / "voice" / "jobs"
_RVC_CACHE: dict[str, Any] = {"mtime": -1.0, "data": {}}


def rvc_config() -> dict[str, Any]:
    """data/voice/rvc_config.json 改了立刻生效（按 mtime 缓存）。"""
    try:
        mtime = RVC_CONFIG.stat().st_mtime
        if _RVC_CACHE["mtime"] != mtime:
            _RVC_CACHE["data"] = json.loads(RVC_CONFIG.read_text(encoding="utf-8")) or {}
            _RVC_CACHE["mtime"] = mtime
    except Exception:  # noqa: BLE001
        return {}
    return dict(_RVC_CACHE["data"])


def owner_requests() -> list[dict[str, Any]]:
    """读出她的心愿和爸爸的销账记录；只返回还没处理的。"""
    if not OWNER_REQUESTS.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in OWNER_REQUESTS.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            records.append(item)
    done = {
        str(item.get("id")): str(item.get("status") or "")
        for item in records
        if item.get("type") == "wish_decision"
    }
    pending = []
    for item in records:
        if item.get("type") not in WISH_LABEL:
            continue
        status = done.get(str(item.get("id")))
        if status in ("done", "dismissed"):
            continue
        pending.append({**item, "status": status or "pending"})
    pending.reverse()
    return pending


def activity_payload() -> dict:
    """她此刻在干嘛——按她的真实状态来，不按"聊过天"脑补。

    优先级：不在线 > 睡觉 > 安静休息 > 玩IM > 看书 > 刷抖音 > 刚聊完 > 闲着。
    """
    now = datetime.now().astimezone()

    def ago_text(moment: datetime | None) -> str:
        if moment is None:
            return ""
        mins = max(0.0, (now - moment.astimezone()).total_seconds() / 60.0)
        if mins < 1:
            return "刚刚"
        if mins < 60:
            return f"{int(mins)} 分钟前"
        return f"{int(mins / 60)} 小时前"

    def load(name: str) -> dict:
        try:
            return json.loads((DATA_DIR / name).read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            return {}

    # 1) 主模型没接上 = 不在线（10 分钟内的失败才算）
    agent = load("agent_state.json")
    if agent and not agent.get("ok", True):
        try:
            when = datetime.fromisoformat(str(agent.get("at") or ""))
            if (now - when.astimezone()).total_seconds() < 600:
                return {
                    "asleep": False,
                    "offline": True,
                    "text": "不在线",
                    "detail": "主模型没接上，她这会儿说不了话",
                }
        except Exception:  # noqa: BLE001
            pass

    # 2) 睡觉（比安静更硬，先判）
    # 睡觉
    sleep_state = load("sleep_state.json")
    if sleep_state.get("asleep"):
        tail = ""
        try:
            until = datetime.fromisoformat(str(sleep_state.get("until") or ""))
            left = (until - now).total_seconds() / 3600.0
            tail = f" · 还有约 {left:.1f} 小时醒" if left > 0 else " · 快醒了"
        except Exception:  # noqa: BLE001
            tail = ""
        return {"asleep": True, "text": "在睡觉" + tail, "detail": "睡下后只在整理记忆和做梦"}


    talk = load("talk_state.json")
    quiet_until = str(talk.get("quiet_until") or "")
    if quiet_until:
        try:
            until = datetime.fromisoformat(quiet_until)
            if until.tzinfo is None:
                until = until.astimezone()  # 文件里是不带时区的本地时间
            if until > now:
                return {
                    "asleep": False,
                    "quiet": True,
                    "text": f"在安静休息 · 到 {until.strftime('%H:%M')}",
                    "detail": str(talk.get("quiet_reason") or "她自己按的") + "（她自己按的，不是卡住）",
                }
        except Exception:  # noqa: BLE001
            pass

    # 4) 在玩IM（最近 6 分钟有IM收发）
    try:
        social = json.loads(SOCIAL_MESSAGES.read_text(encoding="utf-8"))
        last_msg = max(
            (float(item.get("timestamp") or 0) for items in (social.get("chats") or {}).values() for item in items),
            default=0.0,
        )
        if last_msg and now.timestamp() - last_msg < 360:
            when = f"（{ago_text(datetime.fromtimestamp(last_msg, tz=now.tzinfo))}）"
            return {"asleep": False, "text": "在玩IM" + when, "detail": "刚在IM 里说话"}
    except Exception:  # noqa: BLE001
        pass

    # 5) 在看书（最近 30 分钟有阅读记录）
    try:
        con = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        row = con.execute(
            "select substr(summary,1,60) s, last_accessed from books "
            "where folder_event = '阅读' order by last_accessed desc limit 1"
        ).fetchone()
        con.close()
        if row and row["last_accessed"]:
            when = datetime.fromisoformat(str(row["last_accessed"]))
            if (now - when.astimezone()).total_seconds() < 1800:
                text = "在看《小王子》" if "小王子" in str(row["s"]) else "在看书"
                return {"asleep": False, "text": text, "detail": f"{ago_text(when)}翻过书"}
    except Exception:  # noqa: BLE001
        pass

    # 6) 在刷抖音
    douyin = load("douyin_state.json")
    if douyin.get("enabled"):
        return {"asleep": False, "text": "在刷抖音", "detail": "抖音开着"}

    # 7) 刚聊完
    try:
        con = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        row = con.execute("select created_at from traces order by id desc limit 1").fetchone()
        con.close()
        if row:
            when = datetime.fromisoformat(str(row["created_at"]))
            mins = (now - when.astimezone()).total_seconds() / 60.0
            if mins <= 4:
                return {"asleep": False, "text": "刚和你聊完 · 在发呆", "detail": "几分钟后可能就去翻书或睡了"}
            if mins <= 30:
                return {"asleep": False, "text": f"在线 · {ago_text(when)}说过话", "detail": ""}
    except Exception:  # noqa: BLE001
        pass

    return {"asleep": False, "text": "在线 · 闲着", "detail": ""}

STATIC_DIR = ROOT / "web"
UPLOAD_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".webm", ".mp3", ".wav", ".m4a"}
MAX_UPLOAD = 20 * 1024 * 1024
MAX_BODY = 1024 * 1024

AUTH_SECRET_FILE = ROOT / "data" / "web_auth_secret"
AUTH_COOKIE = "xy_session"
AUTH_MAX_AGE = 7 * 24 * 3600


def _main_user_id() -> str:
    try:
        policy = Path.home() / ".dsh" / "profiles" / "desktop" / "node_modules" / "dsh-douyin-browser-local" / "state" / "chat-policy.json"
        data = json.loads(policy.read_text(encoding="utf-8"))
        first = (data.get("allowlist") or [{}])[0]
        owner = str(first.get("id") or "").strip()
        if owner:
            return owner
    except Exception:  # noqa: BLE001
        pass
    return "MAIN_USER_ID"


MAIN_USER_ID = _main_user_id()

AUTH_KEYS: dict[str, dict[str, str]] = {
    "FATHER": {"user_id": "father", "api_user_id": MAIN_USER_ID, "label": "FATHER / 主库 / 最高权限"},
    "SOLVER": {"user_id": "solver", "api_user_id": MAIN_USER_ID, "label": "SOLVER / 主库 / DeepSeek 调试"},
    # 网页是主入口：用这两个 key 进来，就按IM/抖音的场合跟她说话（同一个脑子）
    "IM": {
        "user_id": "chat-viewer",
        "api_user_id": MAIN_USER_ID,
        "label": "IM / IM通道 / 同一个脑子",
        "surface": "chat",
    },
    "TIKTOK": {
        "user_id": "tiktok-viewer",
        "api_user_id": MAIN_USER_ID,
        "label": "TIKTOK / 抖音通道 / 同一个脑子",
        "surface": "douyin",
    },
    # 2026-11-13 爸爸：给熟人E一个网页账号（密钥 YUMAN）。
    # 她登录后说话人是"熟人E"，不是主人；记忆按这个名字单独放。
    "YUMAN": {
        "user_id": "熟人E",
        "api_user_id": MAIN_USER_ID,
        "label": "YUMAN / 熟人E",
        "guest": True,
    },
}
AUTH_ALIASES = {"DOUYIN": "TIKTOK", "WX": "IM"}

# 自助注册：名字 + 名字确认 → 一个可用的 user_id（每个名字一份独立记忆）
USERS_FILE = ROOT / "data" / "web_users.json"
# 2026-11-15：公网自助注册改成"要有邀请码"（没设邀请码就关掉注册），
# 乱猜密钥不会再凭空造出一个能用的账号。
PUBLIC_USERS_FILE = ROOT / "data" / "web_users_public.json"
# 2026-11-15 爸爸选的方案 B：按 IP 环境放行，只有白名单里的 IP 能访问网页
ALLOW_IPS_FILE = ROOT / "data" / "allowed_ips.txt"
BLOCKED_LOG = ROOT / "data" / "web_blocked.jsonl"


def allowed_ip_list() -> list[str]:
    try:
        raw = ALLOW_IPS_FILE.read_text(encoding="utf-8-sig")
    except OSError:
        return []
    return [ln.strip() for ln in raw.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def ip_allowed(ip: str) -> bool:
    """白名单为空 = 全部放行（避免把自己锁在外面）。"""
    entries = allowed_ip_list()
    if not entries:
        return True
    import ipaddress as _ip
    try:
        addr = _ip.ip_address(ip)
    except ValueError:
        return False
    for entry in entries:
        try:
            if "/" in entry:
                if addr in _ip.ip_network(entry, strict=False):
                    return True
            elif addr == _ip.ip_address(entry):
                return True
        except ValueError:
            continue
    return False


INVITE_FILE = ROOT / "data" / "web_invite.txt"


def web_invite_code() -> str:
    import os as _os
    code = str(_os.environ.get("XIAOYU_WEB_INVITE") or "").strip()
    if code:
        return code
    try:
        return INVITE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def load_public_users() -> dict[str, dict]:
    try:
        import json as _json
        data = _json.loads(PUBLIC_USERS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def sync_public_users() -> dict:
    """从公网服务器拉一份注册名单（只读缓存；失败不影响页面）。"""
    try:
        tool = ROOT / "tools" / "sync_web_users.py"
        proc = subprocess.run(
            [sys.executable, str(tool)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25,
        )
        return {"ok": proc.returncode == 0, "out": (proc.stdout or proc.stderr or "").strip()[-300:]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "out": f"{type(exc).__name__}: {exc}"[:200]}



MAX_WEB_USERS = 200


def load_web_users() -> dict[str, dict]:
    try:
        data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    users = data.get("users") if isinstance(data, dict) else {}
    return {str(k): v for k, v in (users or {}).items() if isinstance(v, dict)}


def save_web_users(users: dict[str, dict]) -> None:
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = USERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "users": users}, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(USERS_FILE)


def find_web_user(key: str) -> tuple[str, dict] | None:
    """按登录名找注册用户（不区分大小写）。"""
    target = str(key or "").strip().lower()
    if not target:
        return None
    for name, info in load_web_users().items():
        if name.lower() == target:
            return name, info
    return None


# 网页通道模式：输入 "爸爸: 在吗" → 说话人是爸爸，内容是"在吗"。
SURFACE_PREFIX = re.compile(r"^\s*([\u4e00-\u9fa5A-Za-z0-9_\-]{1,16})\s*[:：]\s*(.+)$", re.S)


def parse_surface_input(text: str, default_sender: str = "爸爸") -> tuple[str, str]:
    """把"名字: 内容"拆成 (说话人, 内容)；没写名字就用默认的。"""
    value = str(text or "").strip()
    match = SURFACE_PREFIX.match(value)
    if match:
        sender = match.group(1).strip()
        body = match.group(2).strip()
        # "http://..." 这种不是"名字:"，别把人名识别成 http
        if not body.startswith("//") and "." not in sender:
            return sender, body
    return default_sender, value


def surface_channel(surface: str) -> str:
    return {"chat": "chat_private", "douyin": "douyin"}.get(surface, "")


def _auth_secret() -> bytes:
    AUTH_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        value = AUTH_SECRET_FILE.read_text(encoding="ascii").strip()
        if value:
            return bytes.fromhex(value)
    except (OSError, ValueError):
        pass
    value = secrets.token_hex(32)
    try:
        AUTH_SECRET_FILE.write_text(value, encoding="ascii")
    except OSError:
        pass
    return bytes.fromhex(value)


AUTH_SECRET = _auth_secret()
SOLVER_INBOX = ROOT / "data" / "solver_channel.jsonl"
PC_REQUESTS = ROOT / "data" / "pc_requests.jsonl"
# IM桥接缓冲的消息（也是网页 IM 通道的"收件箱"）
SOCIAL_MESSAGES = Path(r"C:\Users\Lenovo\Desktop\aihuman\data\workspace\state\social-messages.json")


# 2026-11-15：网页有人进来时，给她一个提示，看她想不想先说一句。
ARRIVALS_FILE = DATA_DIR / "web_arrivals.jsonl"
GREETINGS_FILE = DATA_DIR / "web_greetings.json"


def _load_greetings() -> dict:
    try:
        data = json.loads(GREETINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


_GREET_LOCK = threading.Lock()


def _save_greetings(data: dict) -> None:
    try:
        GREETINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        # 2026-11-16：原子写 + 加锁，避免并发时把整个缓存写坏
        tmp = GREETINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(GREETINGS_FILE)
    except Exception:  # noqa: BLE001
        log.warning("写问候缓存失败", exc_info=True)


# 2026-11-15：登录失败按 IP 限速
_LOGIN_FAILS: dict[str, list[float]] = {}
_ARRIVE_LAST: dict[str, float] = {}


class WebApp:
    def __init__(self, api_url: str, sticker_dir: Path, upload_dir: Path,
                 sticker_chance: float = 0.15, db_path: Path | None = None) -> None:
        # 2026-11-03：--api 可以写多个地址（逗号分隔），按顺序挑第一个活着的。
        # 云端用这个把"手机打开的网页"接到本机那一份她身上：
        #   --api http://127.0.0.1:18000,http://127.0.0.1:8000
        # 18000 是反向隧道（本机 dl_api），隧道断了才退回云端自己的 8000。
        self.api_candidates = [part.strip().rstrip("/") for part in str(api_url).split(",") if part.strip()]
        if not self.api_candidates:
            self.api_candidates = ["http://127.0.0.1:8000"]
        self.api_url = self._pick_api(self.api_candidates[0])
        self.client = DigitalLifeClient(ApiConfig(base_url=self.api_url, timeout=180.0, retries=0))
        self.stickers = StickerStore(sticker_dir, max_kb=8192)
        self.stickers.load()
        self.upload_dir = upload_dir
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.chance = sticker_chance
        self.db_path = db_path
        self.rng = random.Random()
        self._lock = threading.Lock()
        self._last_sticker_at = 0.0
        self._started = time.time()

    def _api_alive(self, url: str, timeout: float = 2.0) -> bool:
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(url + "/status", timeout=timeout) as resp:
                return 200 <= int(getattr(resp, "status", 200)) < 300
        except Exception:  # noqa: BLE001
            return False

    def desk_mood(self) -> dict[str, Any]:
        """她此刻的情绪三维：joy / hurt / impulse（网页刷新时也要有）。

        2026-11-04 爸爸："网页端她情绪三维怎么老是不显示。"
        以前只有聊天返回里带 emotion，页面一刷新就空了；而且 dl_api 挂了就更没有。
        这里直接读库里的 meta（和 dl_api 用的是同一份数据），不依赖它在不在。
        """
        db_path = ROOT / "data" / "xiaoyu.db"
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                row = con.execute(
                    "select value from meta where user_id=? and key='emotion'", (MAIN_USER_ID,)
                ).fetchone()
                if not row:
                    row = con.execute(
                        "select user_id, value from meta where key='emotion' order by rowid desc limit 1"
                    ).fetchone()
                    row = (row[1],) if row else None
                intimacy = con.execute(
                    "select value from meta where user_id=? and key='intimacy'", (MAIN_USER_ID,)
                ).fetchone()
            finally:
                con.close()
        except Exception:  # noqa: BLE001
            return {"ok": False, "emotion": {}, "error": "读不到情绪"}
        try:
            mood = json.loads(row[0]) if row and row[0] else {}
        except Exception:  # noqa: BLE001
            mood = {}
        if intimacy and intimacy[0]:
            try:
                mood["intimacy"] = float(intimacy[0])
            except (TypeError, ValueError):
                pass
        emotion = {k: mood.get(k) for k in ("joy", "hurt", "impulse", "intimacy")}
        return {"ok": bool(emotion.get("joy") is not None), "emotion": emotion, "full": mood}

    def _pick_api(self, fallback: str) -> str:
        for url in self.api_candidates:
            if self._api_alive(url, timeout=1.5):
                return url
        return fallback

    def _switch_api(self, url: str) -> None:
        if url == self.api_url:
            return
        self.api_url = url
        self.client = DigitalLifeClient(ApiConfig(base_url=url, timeout=180.0, retries=0))
        log.info("网页端切换 API：%s", url)

    # ------------------------------------------------------------------ 聊天

    def arrive(self, user_id: str, name: str, channel: str = "web_guest",
               session_key: str = "") -> dict[str, Any]:
        """有人打开网页了：记一笔，再问她要不要先说一句。"""
        # 2026-11-15：同一会话 10 分钟内只触发一次，防止有人刷接口烧 token
        key = session_key or user_id
        now_ts = time.time()
        last = _ARRIVE_LAST.get(key, 0.0)
        if now_ts - last < 600:
            return {"ok": True, "greeted": False, "skipped": "cooldown"}
        _ARRIVE_LAST[key] = now_ts
        try:
            with ARRIVALS_FILE.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "user_id": user_id, "name": name, "channel": channel,
                }, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            log.debug("记录到达失败", exc_info=True)

        prompt = (
            f"【有人来了】{name} 刚打开网页，还没说话。你想先说一句就先说，"
            "不想说就只回 [SILENT]。不要解释，不要报流程。"
        )
        greeting = ""
        try:
            reply = self.client.chat(
                prompt,
                user_id=user_id,
                sender_name=name,
                channel=channel,
                internal=True,
            )
            greeting = str(reply.text or "").strip()
        except Exception as exc:  # noqa: BLE001
            log.info("到达提示没走通：%s", exc)
        if greeting and "[SILENT]" not in greeting.upper():
            greetings = _load_greetings()
            greetings[session_key or user_id] = {
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "name": name,
                "text": greeting[:300],
            }
            _save_greetings(greetings)
            return {"ok": True, "greeted": True}
        return {"ok": True, "greeted": False}

    def take_greeting(self, user_id: str) -> str:
        """取走她给这个人的问候（只给一次）。"""
        greetings = _load_greetings()
        item = greetings.pop(user_id, None)
        if item is None:
            return ""
        _save_greetings(greetings)
        return str(item.get("text") or "")

    def chat(self, message: str, user_id: str, image_path: str = "", sender_name: str = "你",
             channel: str = "", group: str = "", audio_path: str = "",
             image_base64: str = "", audio_base64: str = "",
             image_suffix: str = "", audio_suffix: str = "", internal: bool = False) -> dict[str, Any]:
        def _send() -> ChatReply:
            return self.client.chat(
                message,
                user_id=user_id,
                sender_name=sender_name,
                image_path=image_path,
                audio_path=audio_path,
                channel=channel,
                group=group,
                image_base64=image_base64,
                audio_base64=audio_base64,
                image_suffix=image_suffix,
                audio_suffix=audio_suffix,
                internal=internal,
            )

        try:
            reply: ChatReply = _send()
        except DigitalLifeError:
            # 主地址掉线（隧道断了之类）：换下一个活着的地址再试一次。
            switched = False
            for url in self.api_candidates:
                if url != self.api_url and self._api_alive(url, timeout=1.5):
                    self._switch_api(url)
                    switched = True
                    break
            if not switched:
                raise
            reply = _send()
        raw = reply.raw or {}
        replies = raw.get("replies") if isinstance(raw.get("replies"), list) else []
        raw_text = str(reply.text or "")
        wants_sticker = bool(re.search(r"\[STICKER(?::[^\]]{0,40})?\]", raw_text, re.I))
        clean_text = re.sub(r"\[STICKER(?::[^\]]{0,40})?\]", "", raw_text, flags=re.I)
        clean_text = re.sub(r"\[\[LATER:\d+\]\]", "", clean_text).strip()
        clean_replies = [
            re.sub(r"\[STICKER(?::[^\]]{0,40})?\]", "", re.sub(r"\[\[LATER:\d+\]\]", "", str(item)), flags=re.I).strip()
            for item in replies if str(item).strip()
        ]
        sticker = self._pick_sticker(reply.emotion, message, force=wants_sticker)
        voice_parts = [
            str(item).strip() for item in (raw.get("voice_parts") or []) if str(item).strip()
        ]
        sing_parts = [
            item for item in (raw.get("sing_parts") or [])
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ]
        return {
            "ok": True,
            "reply": clean_text,
            "replies": [item for item in clean_replies if item] or [clean_text],
            "voice_parts": voice_parts,
            "sing_parts": sing_parts,
            "delay_ms": int(raw.get("delay_ms") or 0),
            "ignored": bool(raw.get("ignored")),
            "ignored_reason": str(raw.get("ignored_reason") or ""),
            "emotion": reply.emotion,
            "intimacy": reply.intimacy,
            "source": raw.get("source"),
            "memory_action": raw.get("memory_action"),
            "safety_blocked": raw.get("safety_blocked"),
            "sticker": sticker,
            "her_open": str(raw.get("her_open") or ""),
            # 2026-11-16 修：网页聊天原来把 drawing/doodle 丢在这一层，
            # 所以她画好的图到页面就只剩一句文字。
            "drawing": raw.get("drawing"),
            "doodle": raw.get("doodle"),
        }

    def proactive(self, user_id: str) -> dict[str, Any]:
        data = self.client.proactive_data(user_id=user_id)
        text = str(data.get("reply") or "")
        replies = data.get("replies") if isinstance(data.get("replies"), list) else []
        wants_sticker = bool(re.search(r"\[STICKER(?::[^\]]{0,40})?\]", text, re.I))
        text = re.sub(r"\[STICKER(?::[^\]]{0,40})?\]", "", text, flags=re.I).strip()
        sticker = self._pick_sticker({}, text, force=wants_sticker)
        return {
            "ok": True,
            "reply": text,
            "replies": [str(item) for item in replies if str(item).strip()],
            "sent": bool(text),
            "delay_ms": int(data.get("delay_ms") or 0),
            "sticker": sticker,
        }

    # ------------------------------------------------------------------ 表情

    def _pick_sticker(self, emotion: dict[str, float], text: str, force: bool = False) -> dict[str, Any] | None:
        if self.stickers.count() == 0:
            return None
        forced = force or any(word in text for word in ("发个表情", "发张表情", "表情包", "发个图"))
        now = time.time()
        # 只有她明确想发时才发；不随机补图。
        if not forced:
            return None
        tag = mood_tag(emotion) if emotion else "日常"
        sticker = self.stickers.pick([tag])
        if sticker is None:
            return None
        self.stickers.mark_used(sticker.id)
        self._last_sticker_at = now
        return {"id": sticker.id, "tag": tag, "url": f"/api/sticker?id={sticker.id}"}

    def sticker_file(self, sticker_id: str) -> Path | None:
        sticker = self.stickers.get(sticker_id)
        if sticker is None:
            return None
        path = self.stickers.resolve(sticker)
        return path if path.exists() else None

    # ------------------------------------------------------------------ 状态

    def health(self) -> dict[str, Any]:
        api_ok, detail = self.client.ping(timeout=2.0)
        if not api_ok:
            # 主地址挂了（比如隧道断了）：换下一个活着的地址。
            for url in self.api_candidates:
                if url != self.api_url and self._api_alive(url, timeout=1.5):
                    self._switch_api(url)
                    api_ok, detail = self.client.ping(timeout=2.0)
                    break
        media: dict[str, Any] = {}
        if api_ok:
            try:
                with urllib.request.urlopen(self.api_url + "/status", timeout=3) as response:
                    status = json.loads(response.read().decode("utf-8"))
                    media = status.get("media", {})
            except Exception:  # noqa: BLE001
                media = {}
        _sleep: dict[str, Any] = {}
        try:
            _sleep = json.loads((DATA_DIR / "sleep_state.json").read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            _sleep = {}
        return {
            "ok": True,
            "asleep": bool(_sleep.get("asleep")),
            "sleep_until": str(_sleep.get("until") or ""),
            "api": api_ok,
            "api_detail": detail,
            "media": media,
            "stickers": self.stickers.count(),
            "uptime": round(time.time() - self._started, 1),
        }


    # --------------------------------------------------------------- 她的桌面
    # 2026-11-03 爸爸："把网页端给小鱼一个适配，让她可以选择：打开书看书、
    # 打开抖音、打开IM、打开网页端回复信息。"
    # 这四处不是给访客的功能菜单，是她自己能翻开的东西：页面上摆着状态，
    # 她自己写 [OPEN: book] 之类的标签就能打开，爸爸也可以点开看同一份内容。

    def _read_json(self, path: Path, default: Any) -> Any:
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return default

    def _read_jsonl(self, path: Path, limit: int = 200) -> list[dict]:
        try:
            lines = Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()[-limit:]
        except Exception:  # noqa: BLE001
            return []
        out = []
        for line in lines:
            try:
                item = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(item, dict):
                out.append(item)
        return out

    def _book_rows(self, sql: str, params: tuple = ()) -> list[tuple]:
        if not self.db_path or not Path(self.db_path).exists():
            return []
        try:
            con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=3)
        except Exception:  # noqa: BLE001
            return []
        try:
            return con.execute(sql, params).fetchall()
        except Exception:  # noqa: BLE001
            return []
        finally:
            con.close()

    def desk_book(self) -> dict[str, Any]:
        info: dict[str, Any] = {"title": "", "chapter": 0, "read_at": "", "chapters": 0, "next": 0, "recent": []}
        rows = self._book_rows(
            "select book_name, chapter_num, read_at from book_chapters "
            "where read_at is not null and read_at <> '' order by read_at desc limit 1"
        )
        if rows:
            title, chapter, read_at = rows[0][0], int(rows[0][1] or 0), str(rows[0][2] or "")
            top = self._book_rows("select max(chapter_num) from book_chapters where book_name = ?", (title,))
            total = int((top[0][0] if top else 0) or 0)
            info.update({
                "title": title,
                "chapter": chapter,
                "read_at": read_at,
                "chapters": total,
                "next": chapter + 1 if chapter < total else 0,
            })
        recent = self._book_rows(
            "select summary, folder_date from books where folder_event = '阅读' order by id desc limit 3"
        )
        info["recent"] = [{"summary": str(r[0] or "")[:140], "date": str(r[1] or "")} for r in recent]
        info["wishes"] = self.desk_book_wishes()
        return info

    def desk_book_open(self, chapter: int | None = None) -> dict[str, Any]:
        """翻开书：返回章节列表和当前这章（她读到哪、下一章是什么）。"""
        info = self.desk_book()
        title = info.get("title") or ""
        if not title:
            return {"ok": False, "error": "还没开始读哪本"}
        want = chapter or int(info.get("chapter") or 0)
        rows = self._book_rows(
            "select chapter_num, read_at, content, summary from book_chapters where book_name = ? "
            "and chapter_num in (?, ?)",
            (title, want, want + 1),
        )
        by_num = {int(r[0]): r for r in rows}
        picked = by_num.get(want) or by_num.get(want + 1)
        text, summary = "", ""
        if picked:
            text = str(picked[2] or "")
            summary = str(picked[3] or "")
        return {
            "ok": True,
            "title": title,
            "chapter": int(picked[0]) if picked else want,
            "chapters": info.get("chapters") or 0,
            "next": info.get("next") or 0,
            "read_at": str(picked[1] or "") if picked else "",
            "summary": summary[:400],
            "text": text[:6000],
        }

    def desk_douyin(self) -> dict[str, Any]:
        state = self._read_json(ROOT / "data" / "douyin_state.json", {})
        attention = self._read_json(ROOT / "data" / "attention.json", {})
        watch = self._read_json(ROOT / "data" / "douyin_watch.json", {})
        place = str(attention.get("place") or "")
        # 2026-11-04 爸爸："网页端显示在刷抖音，抖音开着但她没有刷。"
        # "enabled" 只是开关状态；她人到底在不在抖音，要看 attention。
        watching = bool(state.get("enabled")) and place == "douyin"
        return {
            "enabled": bool(state.get("enabled")),
            "reason": str(state.get("reason") or ""),
            "at": str(state.get("at") or ""),
            "watching": watching,
            "attention": place,
            "watch_until": str(watch.get("until") or ""),
        }

    def desk_chat(self) -> dict[str, Any]:
        # 2026-11-09：IM没登录时，"未回"不是她的问题，面板要说清楚
        backend_ok = False
        try:
            from chat_send import _account as _wx_account  # noqa: PLC0415

            backend_ok = bool(_wx_account())
        except Exception:  # noqa: BLE001
            backend_ok = False
        waiting = []
        for item in reversed(pending_queue.list_pending(100, path=ROOT / "data" / "pending_messages.jsonl")):
            waiting.append({
                "at": str(item.get("at") or ""),
                "sender": str(item.get("sender") or ""),
                "text": str(item.get("text") or "")[:300],
                "reason": str(item.get("reason") or ""),
            })
        recent = []
        data = self._read_json(SOCIAL_MESSAGES, {})
        chats = data.get("chats") if isinstance(data, dict) else {}
        flat: list[dict[str, Any]] = []
        for items in (chats or {}).values():
            if not isinstance(items, list):
                continue
            for item in items[-4:]:
                if not isinstance(item, dict):
                    continue
                flat.append({
                    "ts": float(item.get("timestamp") or 0),
                    "name": str(item.get("sender_name") or ""),
                    "text": str(item.get("text") or "")[:160],
                    "outgoing": bool(item.get("outgoing")),
                })
        flat.sort(key=lambda x: x["ts"])
        recent = flat[-6:]
        return {"backend_ok": backend_ok, "waiting": waiting[-6:],
                "waiting_count": len(waiting), "recent": recent}

    def desk_games(self) -> dict[str, Any]:
        """她玩离线小游戏的战绩（石头剪刀布 / 猜数字 / 骰子）。"""
        try:
            sys.path.insert(0, str(ROOT / "tools"))
            import minigames  # noqa: PLC0415

            data = minigames.snapshot()
        except Exception:  # noqa: BLE001
            data = {}
        # 2026-11-05：院子也归到"娱乐"里
        try:
            sys.path.insert(0, str(ROOT / "tools"))
            import garden  # noqa: PLC0415

            data["garden"] = garden.snapshot()
        except Exception:  # noqa: BLE001
            data["garden"] = {}
        return data

    def desk_book_wishes(self) -> list[str]:
        """她留言想看、但还没有的书。"""
        rows = self._read_jsonl(ROOT / "data" / "book_wishes.jsonl", 60)
        return [str(item.get("title") or "") for item in rows if item.get("title")][-6:]

    def desk_pet(self) -> dict[str, Any]:
        """逗小宠物：她跟本地小玩具最近的来往。"""
        rows = self._read_jsonl(ROOT / "data" / "toy_chat.jsonl", 40)
        recent = [{"at": str(r.get("at") or ""), "her": str(r.get("her") or "")[:300],
                   "toy": str(r.get("toy") or "")[:300]} for r in rows[-6:]]
        return {"count": len(rows), "recent": recent, "last_at": recent[-1]["at"] if recent else ""}

    def desk_doodles(self) -> list[dict[str, Any]]:
        """她自己涂的鸦：data/doodles.jsonl（没有记录就退回扫目录）。"""
        # 2026-11-09：她的"画"和"涂鸦"记在两个文件里，画廊要合起来看，
        # 否则她画完问"看到了吗"，页面上一张都没有。
        rows = self._read_jsonl(ROOT / "data" / "drawings.jsonl", 200)
        rows += self._read_jsonl(ROOT / "data" / "doodles.jsonl", 200)
        rows.sort(key=lambda item: str(item.get("at") or ""))
        items: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        for row in rows:
            name = str(row.get("name") or "")
            if not re.fullmatch(r"[0-9A-Za-z_\-]+\.png", name):
                continue
            if name in seen_names:      # 同一个文件在两个索引里都出现过，只留一份
                continue
            seen_names.add(name)
            items.append({"name": name,
                          "caption": str(row.get("caption") or row.get("prompt") or "")[:120],
                          "spec": str(row.get("spec") or row.get("prompt") or "")[:200],
                          "kind": str(row.get("kind") or ""),
                          "at": str(row.get("at") or "")})
        if not items:
            drawn = ROOT / "data" / "drawn"
            if drawn.is_dir():
                for path in sorted(drawn.glob("*.png"), key=lambda p: p.stat().st_mtime)[-24:]:
                    if path.name.startswith("doodle-"):
                        items.append({"name": path.name, "caption": "", "spec": "",
                                      "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(path.stat().st_mtime))})
        return list(reversed(items))[:24]

    def desk_tic(self) -> dict[str, Any]:
        """井字棋：当前这局。"""
        try:
            sys.path.insert(0, str(ROOT / "tools"))
            from tictactoe import describe, load  # noqa: PLC0415

            state = load()
            return {
                "board": state.get("board") or [""] * 9,
                "turn": state.get("turn") or "her",
                "opponent": state.get("opponent") or "bot",
                "result": state.get("result") or "",
                "text": describe(state),
            }
        except Exception:  # noqa: BLE001
            return {"board": [""] * 9, "turn": "her", "opponent": "bot", "result": "", "text": ""}

    def desk_web(self) -> dict[str, Any]:
        """网页这边：谁在，最近一轮是什么时候。"""
        turns = []
        for item in self._read_jsonl(ROOT / "data" / "decision_trace.jsonl", 400):
            if str(item.get("channel") or "") != "web":
                continue
            turns.append({
                "at": str(item.get("at") or ""),
                "decision": str(item.get("decision") or ""),
                "source": str(item.get("source") or ""),
                # 2026-11-08：最高权限要看具体内容，不是只知道"她回了一句"
                "text": str(item.get("text") or "")[:160],
                "reply": str(item.get("reply") or "")[:200],
                "note": str(item.get("note") or "")[:60],
            })
        accounts = len(load_web_users())
        return {
            "recent": turns[-6:],
            "accounts": accounts,
            "last_at": turns[-1]["at"] if turns else "",
        }

    def desk_her_open(self) -> str:
        rows = self._read_jsonl(ROOT / "data" / "her_desk.jsonl", 20)
        for item in reversed(rows):
            kind = str(item.get("kind") or "")
            if kind:
                return kind
        return ""

    def desk_summary(self) -> dict[str, Any]:
        return {
            "ok": True,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "book": self.desk_book(),
            "douyin": self.desk_douyin(),
            "chat": self.desk_chat(),
            "web": self.desk_web(),
            "pet": self.desk_pet(),
            "tic": self.desk_tic(),
            "games": self.desk_games(),
            "doodles": self.desk_doodles(),
            "wishes": self.desk_book_wishes(),
            "her_open": self.desk_her_open(),
        }

    def her_desk_open(self, kind: str, chapter: int | None = None) -> dict[str, Any]:
        kind = str(kind or "").strip().lower()
        if kind == "book":
            return {"ok": True, "kind": kind, "book": self.desk_book_open(chapter)}
        if kind == "douyin":
            return {"ok": True, "kind": kind, "douyin": self.desk_douyin()}
        if kind == "chat":
            return {"ok": True, "kind": kind, "chat": self.desk_chat()}
        if kind == "web":
            return {"ok": True, "kind": kind, "web": self.desk_web()}
        if kind in ("pet", "doodle", "draw", "fun"):
            # 2026-11-08：她写 [OPEN: 涂鸦] 会归一成 pet，但前端只有"娱乐"这一栏的渲染。
            # 直接返回娱乐面板需要的那一份（含 doodles，否则涂鸦区是空的）。
            return {"ok": True, "kind": "fun",
                    "douyin": self.desk_douyin(), "pet": self.desk_pet(),
                    "tic": self.desk_tic(), "games": self.desk_games(),
                    "wishes": self.desk_book_wishes(),
                    "doodles": self.desk_doodles()}
        if kind == "tic":
            return {"ok": True, "kind": kind, "tic": self.desk_tic()}
        if kind == "fun":
            # 2026-11-05：抖音 / 小玩具 / 井字棋 / 小游戏 合并成一个"娱乐"入口
            return {"ok": True, "kind": kind,
                    "douyin": self.desk_douyin(), "pet": self.desk_pet(),
                    "tic": self.desk_tic(), "games": self.desk_games(),
                    "wishes": self.desk_book_wishes(),
                    "doodles": self.desk_doodles()}
        return {"ok": False, "error": f"不认识的入口：{kind}"}

    def _wake_for_tic(self) -> None:
        """爸爸落子后叫她看棋盘。失败不影响返回。"""
        import threading  # noqa: PLC0415
        import urllib.request  # noqa: PLC0415

        def _run() -> None:
            try:
                body = json.dumps({
                    "message": "[系统：爸爸刚在井字棋落了一子，现在轮到你。看一眼棋盘再走。]",
                    "user_id": MAIN_USER_ID,
                    "sender": "SOLVER-GAME",
                    "internal": True,
                }, ensure_ascii=False).encode("utf-8")
                request = urllib.request.Request(
                    self.api_url.rstrip("/") + "/chat", data=body,
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(request, timeout=120) as response:
                    response.read()
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=_run, daemon=True).start()

    def her_tic_move(self, action: str, position: int | None = None, opponent: str = "bot") -> dict[str, Any]:
        """她开的棋局：轮到她/爸爸时落子。只有"她"能开局，这里只处理落子。"""
        try:
            sys.path.insert(0, str(ROOT / "tools"))
            from tictactoe import apply_move, describe, load, new_game  # noqa: PLC0415

            state = load()
            if action == "new":
                state = new_game(opponent)
            elif action == "move" and position:
                side = "her" if state.get("turn") == "her" else "other"
                state = apply_move(int(position), side)
                # 2026-11-09：爸爸下完就该她走了，别等她被动等人说话
                if side == "other":
                    self._wake_for_tic()
            return {"ok": True, "tic": self.desk_tic(), "state": state, "text": describe(state)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def her_gomoku(self, action: str, row: int | None = None, col: int | None = None,
                   opponent: str = "") -> dict[str, Any]:
        """五子棋（9×9）：开新局 / 落子。轮到谁由棋盘状态决定。"""
        try:
            sys.path.insert(0, str(ROOT / "tools"))
            import gomoku  # noqa: PLC0415

            state = gomoku.load()
            note = ""
            if action == "new":
                state = gomoku.new_game(opponent or "小玩具")
                note = "开了新局"
            elif action == "move" and row is not None and col is not None:
                if state.get("result"):
                    return {"ok": False, "error": "这局已经结束了", "gomoku": state,
                            "text": gomoku.describe(state)}
                # 网页上的人执「对方」，机器人替她落
                ok, note = gomoku.other_move(state, int(row) - 1, int(col) - 1)
                if not ok:
                    return {"ok": False, "error": note, "gomoku": state, "text": gomoku.describe(state)}
            elif state.get("result") is None:
                pass
            state = gomoku.load()
            return {"ok": True, "gomoku": state, "note": note, "text": gomoku.describe(state)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def douyin_switch(self, action: str) -> dict[str, Any]:
        """开关抖音：开=拉起同步轮询，关=停浏览器和轮询。"""
        script = ROOT / "tools" / "douyin_control.py"
        if not script.exists():
            return {"ok": False, "error": "找不到 douyin_control.py"}
        flag = "--on" if str(action).lower() == "on" else "--off"
        try:
            proc = subprocess.run(
                [sys.executable, str(script), flag],
                cwd=str(ROOT), capture_output=True, timeout=60,
            )
            out = proc.stdout.decode("utf-8", "replace").strip().splitlines()
            data = json.loads(out[-1]) if out else {}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": bool(data.get("ok")), **data, "douyin": self.desk_douyin()}


def _static_cache_header(suffix: str) -> str:
    """静态资源缓存策略：大媒体长缓存，页面/接口不缓存。"""
    if suffix.lower() in (".mp4", ".webm", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".moc3", ".wav", ".mp3"):
        return "public, max-age=86400"
    return "no-store"


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")


def _make_handler(app: WebApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "XiaoyuWeb/1.0"
        protocol_version = "HTTP/1.1"

        def handle_one_request(self) -> None:
            try:
                super().handle_one_request()
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                self.close_connection = True

        # ---------------------------------------------------------- 登录

        def _make_token(self, account: dict[str, str], key: str) -> str:
            payload = {
                "v": 1,
                "key": key,
                "user_id": account["user_id"],
                "api_user_id": account["api_user_id"],
                "label": account["label"],
                "surface": account.get("surface", ""),
                "guest": bool(account.get("guest")),
                "iat": int(time.time()),
            }
            raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
            signature = hmac.new(AUTH_SECRET, encoded.encode("ascii"), hashlib.sha256).hexdigest()
            return f"{encoded}.{signature}"

        def _ip_gate(self) -> bool:
            """IP 白名单：不在名单里直接 403，并记一笔方便加白。"""
            ip = str(self.client_address[0] if self.client_address else "")
            if ip_allowed(ip):
                return True
            try:
                with BLOCKED_LOG.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({
                        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "ip": ip,
                        "path": self.path[:120],
                    }, ensure_ascii=False) + "\n")
            except Exception:  # noqa: BLE001
                pass
            body = (
                "<!doctype html><meta charset=\"utf-8\">"
                "<body style=\"background:#0d1117;color:#c9d1d9;font-family:system-ui;padding:40px\">"
                f"<h3>访问被拒绝</h3><p>你的 IP：<code>{html_lib.escape(ip)}</code></p>"
                "<p>这个环境不在允许名单里。要开放的话，把它加进 "
                "<code>data/allowed_ips.txt</code>，下一请求立即生效。</p></body>"
            )
            raw = body.encode("utf-8")
            self.send_response(403)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return False

        def _session(self) -> dict[str, Any] | None:
            try:
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                morsel = cookie.get(AUTH_COOKIE)
                if morsel is None:
                    return None
                encoded, signature = morsel.value.rsplit(".", 1)
                expected = hmac.new(AUTH_SECRET, encoded.encode("ascii"), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(signature, expected):
                    return None
                padding = "=" * (-len(encoded) % 4)
                payload = json.loads(base64.urlsafe_b64decode(encoded + padding).decode("utf-8"))
                if not isinstance(payload, dict) or not payload.get("api_user_id"):
                    return None
                if time.time() - float(payload.get("iat") or 0) > AUTH_MAX_AGE:
                    return None
                return payload
            except Exception:  # noqa: BLE001
                return None

        def _redirect(self, location: str, cookie: str | None = None) -> None:
            self.send_response(302)
            self.send_header("Location", location)
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _serve_register(self, error: bool = False, taken: bool = False, ip_limit: bool = False) -> None:
            message = ""
            if taken:
                message = "[ERROR] 这个名字已经有人用了，换一个"
            elif ip_limit:
                message = "[ERROR] 这个网络已经注册过一个账号了"
            elif error:
                message = "[ERROR] 两次输入不一致，或者名字不合法（2-16 个字）"
            html = f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>[REGISTER] NEURAL LINK</title>
<style>
:root{{--bg:#0d0d0d;--cyan:#35f6ff;--green:#8dffb0;--dim:#5b6b70;}}
*{{box-sizing:border-box;font-family:"Cascadia Mono","JetBrains Mono",Consolas,monospace;}}
body{{margin:0;background:var(--bg);color:var(--green);min-height:100vh;display:flex;align-items:center;justify-content:center;}}
.terminal{{width:min(620px,92vw);border:1px solid rgba(53,246,255,.28);background:rgba(7,12,14,.82);padding:34px 30px;box-shadow:0 0 40px rgba(53,246,255,.08);}}
.title{{color:var(--cyan);font-size:15px;line-height:1.7;text-shadow:0 0 10px rgba(53,246,255,.45);}}
.sub{{color:var(--dim);font-size:12px;margin-top:8px;line-height:1.8;}}
form{{margin-top:22px;}}
label{{display:block;color:var(--dim);font-size:12px;margin:14px 0 6px;}}
input{{width:100%;background:#05090a;color:var(--green);border:1px solid rgba(53,246,255,.45);padding:12px 14px;font-size:15px;outline:none;}}
input:focus{{border-color:var(--cyan);box-shadow:0 0 18px rgba(53,246,255,.22);}}
button{{margin-top:16px;width:100%;background:transparent;color:var(--cyan);border:1px solid var(--cyan);padding:11px;font-size:13px;letter-spacing:.14em;cursor:pointer;}}
button:hover{{background:rgba(53,246,255,.1);}}
.error{{color:#ff5b6e;font-size:13px;margin-top:12px;}}
a{{color:var(--cyan);font-size:12px;}}
</style></head><body><div class="terminal">
<div class="title">[REGISTER] 创建一个属于你的链接</div>
<div class="sub">输入一个名字，再输入一次确认。这个名字以后就是你的登录密钥。<br>所有入口都是同一个她，共用一份记忆。</div>
<form method="post" action="/register">
<label>名字（2-16 个字）</label>
<input name="name" maxlength="16" autocomplete="off" autofocus placeholder="比如：爸爸">
<label>再输一次</label>
<input name="confirm" maxlength="16" autocomplete="off" placeholder="再输一次同样的名字">
<label>邀请码（找主人要）</label>
<input name="invite" maxlength="64" autocomplete="off" placeholder="没有邀请码不能注册">
<button type="submit">CREATE LINK</button>
</form>
<div class="error">{message}</div>
<div style="margin-top:18px"><a href="/login">← 已经有密钥，去登录</a></div>
</div></body></html>"""
            data = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _handle_register(self) -> None:
            import hashlib as _hashlib  # noqa: PLC0415
            import re as _re  # noqa: PLC0415

            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            raw = self.rfile.read(min(length, 8192)).decode("utf-8", errors="replace") if length else ""
            form = parse_qs(raw)
            name = str((form.get("name") or [""])[0]).strip()
            confirm = str((form.get("confirm") or [""])[0]).strip()
            invite = str((form.get("invite") or [""])[0]).strip()
            # 2026-11-15：没有邀请码就别开注册；填错也不开。
            expected = web_invite_code()
            if not expected:
                self._redirect("/login?error=1")
                return
            if not hmac.compare_digest(invite, expected):
                self._redirect("/register?error=1")
                return
            if name != confirm or not (2 <= len(name) <= 16) or _re.search(r"[<>/\\\x00-\x1f]", name):
                self._redirect("/register?error=1")
                return
            # 2026-11-15 修：注册名不能冒充固定密钥（否则邀请码 = 最高权限）
            _reserved = set(AUTH_KEYS) | set(AUTH_ALIASES) | set(PRIVILEGED_KEYS)
            if name.upper() in {str(item).upper() for item in _reserved}:
                self._redirect("/register?taken=1")
                return
            ip = str(self.client_address[0] if self.client_address else "")
            users = load_web_users()
            if any(k.lower() == name.lower() for k in users):
                self._redirect("/register?taken=1")
                return
            # 2026-11-03 爸爸："让每个 IP 只能注册一个账号。"
            if ip and any(str(info.get("ip") or "") == ip for info in users.values()):
                self._redirect("/register?ip=1")
                return
            if len(users) >= MAX_WEB_USERS:
                self._redirect("/register?error=1")
                return
            user_id = "conv:web-" + _hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]
            users[name] = {
                "user_id": user_id,
                "name": name,
                "ip": ip,
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            save_web_users(users)
            account = {"user_id": name, "api_user_id": MAIN_USER_ID, "label": f"{name} / 自助注册", "guest": True}
            token = self._make_token(account, name)
            cookie = f"{AUTH_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={AUTH_MAX_AGE}"
            log.info("网页注册：%s -> 同一个她（%s）", name, MAIN_USER_ID)
            self._redirect("/", cookie)

        def _login_page(self, error: bool = False) -> None:
            message = "[ERROR] 密钥无效，拒绝访问" if error else ""
            body = f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>[SECURITY] NEURAL LINK</title>
<style>
:root{{--bg:#0d0d0d;--cyan:#35f6ff;--green:#8dffb0;--dim:#5b6b70;}}
*{{box-sizing:border-box;font-family:"Cascadia Mono","JetBrains Mono",Consolas,monospace;}}
body{{margin:0;background:var(--bg);color:var(--green);min-height:100vh;display:flex;
align-items:center;justify-content:center;}}
.terminal{{width:min(620px,92vw);border:1px solid rgba(53,246,255,.28);
background:rgba(7,12,14,.82);padding:34px 30px;box-shadow:0 0 40px rgba(53,246,255,.08);}}
.title{{color:var(--cyan);font-size:15px;letter-spacing:.04em;line-height:1.7;
text-shadow:0 0 10px rgba(53,246,255,.45);}}
.sub{{color:var(--dim);font-size:12px;margin-top:8px;}}
form{{margin-top:24px;}}
input{{width:100%;background:#05090a;color:var(--green);border:1px solid rgba(53,246,255,.45);
padding:13px 14px;font-size:15px;letter-spacing:.14em;outline:none;}}
input:focus{{border-color:var(--cyan);box-shadow:0 0 18px rgba(53,246,255,.22);}}
button{{margin-top:12px;width:100%;background:transparent;color:var(--cyan);
border:1px solid var(--cyan);padding:11px;font-size:13px;letter-spacing:.14em;cursor:pointer;}}
button:hover{{background:rgba(53,246,255,.1);box-shadow:0 0 18px rgba(53,246,255,.22);}}
.error{{color:#ff5b6e;font-size:13px;margin-top:12px;}}
#boot{{display:none;white-space:pre-wrap;font-size:13px;line-height:1.9;color:var(--green);}}
.cursor{{color:var(--cyan);animation:blink 1s steps(2,start) infinite;}}
@keyframes blink{{50%{{opacity:0;}}}}
@media (max-width:480px){{
  .terminal{{padding:24px 18px;width:94vw;}}
  input{{font-size:16px;padding:12px;}}
  .title{{font-size:14px;}}
}}
</style></head>
<body><div class="terminal">
<div class="title">[SECURITY] 请输入访问密钥以建立神经链接：</div>
<div class="sub">// unauthorized access will be logged //</div>
<div id="boot"></div>
<form id="loginForm" method="post" action="/login">
<input id="key" name="key" type="password" autocomplete="off" autofocus placeholder="ENTER ACCESS KEY">
<button type="submit">ESTABLISH NEURAL LINK</button>
</form>
<div class="error">{message}</div>
<div style="margin-top:18px"><a href="/register" style="color:#35f6ff;font-size:12px;text-decoration:none">没有密钥？注册一个 →</a></div>
</div>
<script>
const form=document.getElementById('loginForm'), key=document.getElementById('key'), boot=document.getElementById('boot');
form.addEventListener('submit',event=>{{
  if(!key.value.trim()){{event.preventDefault();return;}}
  event.preventDefault();
  const lines=['[SYSTEM] 正在建立安全连接...','[SCAN] 扫描未知实体生物特征...',
    '[AUTH] 正在验证密钥: '+key.value+'...','[LINK] 正在建立神经链接...'];
  form.style.display='none'; boot.style.display='block';
  let i=0;
  const timer=setInterval(()=>{{
    if(i>=lines.length){{clearInterval(timer);setTimeout(()=>form.submit(),260);return;}}
    boot.textContent+=lines[i++]+'\\n';}},260);
}});
</script></body></html>"""
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _handle_login(self) -> None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            raw = self.rfile.read(min(length, 8192)).decode("utf-8", errors="replace") if length else ""
            form = parse_qs(raw)
            key = str((form.get("key") or [""])[0]).strip()
            key = AUTH_ALIASES.get(key.upper(), key.upper())
            account = AUTH_KEYS.get(key)
            if not account:
                # 自助注册的用户：名字就是登录密钥（不区分大小写）
                hit = find_web_user(key)
                if hit:
                    name, info = hit
                    key = name
                    account = {
                        "user_id": name,
                        "api_user_id": MAIN_USER_ID,
                        "label": f"{name} / 自助注册",
                        "guest": True,
                    }
            if not account:
                # 2026-11-15：同一 IP 10 分钟内错 5 次，先挡住，别让人慢慢猜 FATHER
                ip = str(self.client_address[0] if self.client_address else "")
                now = time.time()
                hits = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < 600]
                hits.append(now)
                _LOGIN_FAILS[ip] = hits[-20:]
                if len(hits) >= 5:
                    log.warning("登录失败过多：%s", ip)
                    self._redirect("/login?error=1&locked=1")
                    return
                self._redirect("/login?error=1")
                return
            token = self._make_token(account, key)
            cookie = f"{AUTH_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={AUTH_MAX_AGE}"
            log.info("网页登录：%s (%s)", key, account["user_id"])
            self._redirect("/", cookie)

        # ---------------------------------------------------------- 路由

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)

            if path == "/login":
                if self._session():
                    self._redirect("/")
                else:
                    self._login_page(error=query.get("error") == ["1"])
                return
            if path == "/register":
                if self._session():
                    self._redirect("/")
                else:
                    self._serve_register(error=query.get("error") == ["1"], taken=query.get("taken") == ["1"], ip_limit=query.get("ip") == ["1"])
                return
            if path == "/logout":
                self._redirect("/login", f"{AUTH_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
                return
            if path == "/room" or path.startswith("/room/"):
                session = self._session() or {}
                if not session:
                    # 2026-11-16 爸爸："登录成功了才能看到房间。"
            # 下面这些页面路由也必须登录后才能看（Gemini 复核发现）
                    self._redirect("/login")
                    return
                name = path[len("/room"):].lstrip("/") or "index.html"
                target = (STATIC_DIR / "room" / name).resolve()
                room_dir = (STATIC_DIR / "room").resolve()
                if room_dir not in target.parents and target != room_dir / "index.html":
                    self._json(404, {"ok": False, "error": "没有这个文件"})
                    return
                if not target.exists():
                    self._json(404, {"ok": False, "error": "没有这个文件"})
                    return
                ctype = {".html": "text/html; charset=utf-8", ".png": "image/png",
                         ".jpg": "image/jpeg", ".css": "text/css", ".js": "application/javascript"}.get(
                    target.suffix.lower(), "application/octet-stream")
                body = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", _static_cache_header(target.suffix))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/health":
                self._json(200, app.health())
                return
            if path == "/api/activity":
                if not self._session():
                    self._json(401, {"ok": False, "error": "未登录"})
                    return
                self._json(200, activity_payload())
                return
            if path in ("/her.png", "/her_sleep.png"):
                target = STATIC_DIR / path.lstrip("/")
                if not target.exists():
                    self._json(404, {"ok": False, "error": "还没有她的样子"})
                    return
                body = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/her.png":
                # 她的样子：头像和房间都用，谁都能看（不是什么隐私）
                target = STATIC_DIR / "her.png"
                if not target.exists():
                    self._json(404, {"ok": False, "error": "还没有她的样子"})
                    return
                body = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                self.wfile.write(body)
                return
            if path.startswith("/live2d/"):
                # 本地自用；模型许可 CC BY-NC-SA，不对外分发
                name = path[len("/live2d/"):].lstrip("/")
                base = (STATIC_DIR / "live2d").resolve()
                target = (base / name).resolve()
                if base not in target.parents:
                    self._json(404, {"ok": False, "error": "没有这个文件"})
                    return
                if not target.exists() or not target.is_file():
                    self._json(404, {"ok": False, "error": "没有这个文件"})
                    return
                ctype = {
                    ".json": "application/json; charset=utf-8",
                    ".js": "application/javascript; charset=utf-8",
                    ".png": "image/png",
                    ".moc3": "application/octet-stream",
                    ".md": "text/markdown; charset=utf-8",
                }.get(target.suffix.lower(), "application/octet-stream")
                body = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return

            if path == "/api/her/state":
                if not ip_allowed(str(self.client_address[0] if self.client_address else "")):
                    self._ip_gate()
                    return
                try:
                    import sys as _sys  # noqa: PLC0415

                    _sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
                    import her_state as _her_state  # noqa: PLC0415

                    data = _her_state.snapshot()
                    data["ok"] = True
                except Exception as exc:  # noqa: BLE001
                    data = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                self._json(200, data)
                return

            if path.startswith("/api/room/"):
                pass  # 位置与状态公开；内容抽屉（inspect）另有限制
                if path == "/api/room/state":
                    if not self._session():
                        self._json(401, {"ok": False, "error": "未登录"})
                        return
                    self._json(200, self._room_state())
                    return
                session = self._session() or {}
                if path == "/api/room/inspect" and not is_owner_view(session):
                    self._json(403, {"ok": False, "error": "抽屉里的东西只有主人能看"})
                    return
                if path == "/api/room/inspect":
                    slot = ""
                    try:
                        slot = str(parse_qs(urlparse(self.path).query).get("slot", [""])[0])
                    except Exception:  # noqa: BLE001
                        slot = ""
                    self._json(200, self._room_inspect(slot))
                    return
                self._json(404, {"ok": False, "error": "未知的房间接口"})
                return

            # 2026-11-16 爸爸："网页进去一开始应该是登录界面。"
            if path == "/" and not self._session():
                self._redirect("/login")
                return

            # 公网部署（有 chat.html）时，登录后 "/" 给房间入口
            if path == "/" and (STATIC_DIR / "chat.html").exists():
                page = STATIC_DIR / "landing.html"
                if page.exists():
                    body = page.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                    return

            if path == "/bg" or path.startswith("/bg/"):
                name = path[len("/bg"):].lstrip("/") or "list.json"
                base = (STATIC_DIR / "bg").resolve()
                target = (base / name).resolve()
                if base not in target.parents and target != base:
                    self._json(404, {"ok": False, "error": "没有这个文件"})
                    return
                if not target.exists() or not target.is_file():
                    self._json(404, {"ok": False, "error": "没有这个文件"})
                    return
                ctype = {".json": "application/json; charset=utf-8", ".mp4": "video/mp4",
                         ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
                         ".png": "image/png"}.get(target.suffix.lower(), "application/octet-stream")
                body = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                self.wfile.write(body)
                return

            if path == "/landing":
                if not self._session():
                    self._redirect("/login")
                    return
                page = STATIC_DIR / "landing.html"
                if not page.exists():
                    self._json(404, {"ok": False, "error": "还没有入口页"})
                    return
                body = page.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return

            if path == "/chat":
                if not self._session():
                    self._redirect("/login")
                    return
                page = STATIC_DIR / "chat.html"
                if not page.exists():
                    page = STATIC_DIR / "index.html"
                if not page.exists():
                    self._json(404, {"ok": False, "error": "没有聊天页"})
                    return
                body = page.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return

            if path == "/gomoku":
                self._serve_gomoku(self._session() or {})
                return

            if path == "/ops":
                if not ip_allowed(str(self.client_address[0] if self.client_address else "")):
                    self._ip_gate()
                    return
                session = self._session() or {}
                if not is_privileged(session):
                    self._json(403, {"ok": False, "error": "只有主人能看中枢"})
                    return
                page = STATIC_DIR / "ops.html"
                if not page.exists():
                    self._json(404, {"ok": False, "error": "还没有中枢页面"})
                    return
                body = page.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return

            if path.startswith("/api/ops/"):
                if not ip_allowed(str(self.client_address[0] if self.client_address else "")):
                    self._ip_gate()
                    return
                session = self._session() or {}
                if not is_privileged(session):
                    self._json(403, {"ok": False, "error": "只有主人能看中枢"})
                    return
                try:
                    import ops_brain  # noqa: PLC0415
                except Exception as exc:  # noqa: BLE001
                    self._json(500, {"ok": False, "error": f"中枢没装上：{exc}"})
                    return
                if path == "/api/ops/diagnose":
                    self._json(200, ops_brain.diagnose())
                    return
                if path == "/api/ops/requests":
                    status = ""
                    try:
                        status = str(parse_qs(urlparse(self.path).query).get("status", [""])[0])
                    except Exception:  # noqa: BLE001
                        status = ""
                    self._json(200, {"ok": True, "rows": ops_brain.list_requests(status),
                                     "pending": ops_brain.pending_count()})
                    return
                if path == "/api/ops/decide":
                    length = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(length) if length else b"{}"
                    try:
                        body = json.loads(raw.decode("utf-8") or "{}")
                    except json.JSONDecodeError:
                        self._json(400, {"ok": False, "error": "请求体不是 JSON"})
                        return
                    rid = str(body.get("id") or "")
                    approve = bool(body.get("approve"))
                    self._json(200, ops_brain.decide(rid, approve,
                                                     by=str(body.get("by") or "爸爸"),
                                                     note=str(body.get("note") or "")))
                    return
                self._json(404, {"ok": False, "error": "未知的中枢接口"})
                return

            if path == "/roomv" or path.startswith("/roomv/"):
                if not self._session():
                    self._redirect("/login")
                    return
                name = unquote(path[len("/roomv"):].lstrip("/") or "index.html")
                base = (STATIC_DIR / "roomv").resolve()
                target = (base / name).resolve()
                if base not in target.parents and target != base / "index.html":
                    self._json(404, {"ok": False, "error": "没有这个文件"})
                    return
                if not target.exists():
                    self._json(404, {"ok": False, "error": "没有这个文件"})
                    return
                suffix = target.suffix.lower()
                ctype = {".html": "text/html; charset=utf-8",
                         ".js": "application/javascript; charset=utf-8",
                         ".json": "application/json; charset=utf-8",
                         ".mp4": "video/mp4",
                         ".md": "text/markdown; charset=utf-8"}.get(suffix, "application/octet-stream")
                body = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", _static_cache_header(suffix))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/room3d" or path.startswith("/room3d/"):
                if not self._session():
                    self._redirect("/login")
                    return
                name = path[len("/room3d"):].lstrip("/") or "index.html"
                base = (STATIC_DIR / "room3d").resolve()
                target = (base / name).resolve()
                if base not in target.parents and target != base / "index.html":
                    self._json(404, {"ok": False, "error": "没有这个文件"})
                    return
                if not target.exists():
                    self._json(404, {"ok": False, "error": "没有这个文件"})
                    return
                ctype = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                         ".png": "image/png"}.get(target.suffix.lower(), "application/octet-stream")
                body = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/favicon.ico":
                self._empty(204)
                return

            session = self._session()
            if not session:
                if path.startswith("/api/"):
                    self._json(401, {"ok": False, "error": "未登录"})
                else:
                    self._redirect("/login")
                return

            if path == "/":
                self._serve_index(session)
            elif path == "/solver":
                self._serve_solver(session)
            elif path == "/accounts":
                if not ip_allowed(str(self.client_address[0] if self.client_address else "")):
                    self._ip_gate()
                    return
                self._serve_accounts(session)
            elif path == "/toy":
                self._serve_toy(session)
            elif path == "/diary":
                self._serve_diary()
            elif path == "/api/greet":
                # 2026-11-15：取走她给这个人的问候（只给一次，按会话名）
                _sess = self._session() or {}
                _name = str(_sess.get("user_id") or "web-default")
                self._json(200, {"ok": True, "text": app.take_greeting(_name)})
            elif path == "/api/sticker":
                self._serve_sticker(query)
            elif path == "/api/stickers":
                # 2026-11-13：网页端给用户挑表情包用（熟人E要能发表情）
                try:
                    items = []
                    for sticker in app.stickers.list()[:80]:
                        tags = [str(t) for t in (getattr(sticker, "tags", None) or [])]
                        items.append({
                            "id": str(getattr(sticker, "id", "")),
                            "tags": tags,
                            "note": str(getattr(sticker, "note", "") or ""),
                            "url": f"/api/sticker?id={getattr(sticker, 'id', '')}",
                        })
                    self._json(200, {"ok": True, "items": items})
                except Exception as exc:  # noqa: BLE001
                    self._json(500, {"ok": False, "error": str(exc)[:120]})
            elif path == "/api/voice":
                self._serve_voice(query)
            elif path == "/api/voice_stream":
                self._serve_voice_stream(query)
            elif path == "/api/sing":
                self._serve_voice(query, singing=True)
            elif path == "/api/rvc_enabled":
                cfg = rvc_config()
                self._json(200, {"ok": True, "enabled": bool(cfg.get("enabled"))})
            elif path == "/api/rvc_enqueue":
                self._serve_rvc_enqueue(query)
            elif path == "/api/rvc_status":
                self._serve_rvc_status(query)
            elif path == "/api/rvc_audio":
                self._serve_rvc_audio(query)
            elif path == "/api/inbox":
                self._serve_inbox(query, session)
            elif path == "/api/drawn":
                self._serve_drawn(query)
            elif path == "/api/doodles":
                self._json(200, {"ok": True, "items": app.desk_doodles()})
            elif path == "/api/her/desk":
                # 2026-11-08：桌面面板只给最高权限（书/IM/网页/娱乐）
                if not is_privileged(session):
                    self._json(403, {"ok": False, "error": "只有主人能看到她的桌面"})
                    return
                self._json(200, app.desk_summary())
            elif path == "/api/her/mood":
                self._json(200, app.desk_mood())
            elif path == "/api/her/open":
                if not is_privileged(session):
                    self._json(403, {"ok": False, "error": "只有主人能操作她的桌面"})
                    return
                kind = str((query.get("kind") or [""])[0])
                chapter_raw = str((query.get("chapter") or [""])[0]).strip()
                chapter = int(chapter_raw) if chapter_raw.isdigit() else None
                self._json(200, app.her_desk_open(kind, chapter))
            elif path.startswith("/media/"):
                self._serve_upload(path[len("/media/"):])
            else:
                self._json(404, {"ok": False, "error": f"未知接口 {path}"})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)

            if path == "/register":
                self._handle_register()
                return
            if path == "/login":
                self._handle_login()
                return
            if path == "/logout":
                self._redirect("/login", f"{AUTH_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
                return

            session = self._session()
            if not session:
                self._json(401, {"ok": False, "error": "未登录"})
                return

            if path == "/api/upload":
                self._handle_upload(query)
                return
            if path == "/solver/reply":
                self._handle_solver_reply()
                return
            if path == "/solver/pc":
                self._handle_pc_decision()
                return
            if path == "/wishes/decide":
                self._handle_wish_decision()
                return

            body = self._read_json()
            if body is None:
                return
            if path == "/api/ops/decide":
                if not is_privileged(session):
                    self._json(403, {"ok": False, "error": "只有主人能拍板"})
                    return
                try:
                    import ops_brain  # noqa: PLC0415

                    result = ops_brain.decide(
                        str(body.get("id") or ""),
                        bool(body.get("approve")),
                        by=str(body.get("by") or "爸爸"),
                        note=str(body.get("note") or ""),
                    )
                    self._json(200 if result.get("ok") else 400, result)
                except Exception as exc:  # noqa: BLE001
                    self._json(500, {"ok": False, "error": f"中枢出错：{exc}"})
                return
            try:
                if path == "/api/chat":
                    self._handle_chat(body, session)
                elif path == "/api/toy":
                    self._handle_toy(body)
                elif path == "/api/arrive":
                    user_id = str(session.get("api_user_id") or "web-default")
                    name = str(session.get("user_id") or "有人")
                    self._json(200, app.arrive(user_id, name,
                                               "web_guest" if session.get("guest") else "web",
                                               session_key=name))
                elif path == "/api/greet":
                    name = str(session.get("user_id") or "web-default")
                    text = app.take_greeting(name)
                    self._json(200, {"ok": True, "text": text})
                elif path == "/api/proactive":
                    user_id = str(session.get("api_user_id") or "web-default")
                    self._json(200, app.proactive(user_id))
                elif path == "/api/her/open":
                    kind = str(body.get("kind") or "").strip().lower()
                    chapter = body.get("chapter")
                    chapter = int(chapter) if isinstance(chapter, int) or str(chapter or "").isdigit() else None
                    self._json(200, app.her_desk_open(kind, chapter))
                elif path == "/api/her/douyin":
                    if not is_privileged(session):
                        self._json(403, {"ok": False, "error": "只有主人能操作她的桌面"})
                        return
                    action = str(body.get("action") or "status").strip().lower()
                    self._json(200, self._handle_douyin_switch(action, session))
                elif path == "/api/her/gomoku":
                    payload = body if isinstance(body, dict) else {}
                    action = str(payload.get("action") or "state").strip().lower()
                    row = payload.get("row")
                    col = payload.get("col")
                    opponent = str(payload.get("opponent") or "")
                    try:
                        row = int(row) if row is not None else None
                        col = int(col) if col is not None else None
                    except (TypeError, ValueError):
                        row = col = None
                    self._json(200, app.her_gomoku(action, row, col, opponent))

                elif path == "/api/her/tic":
                    if not is_privileged(session):
                        self._json(403, {"ok": False, "error": "只有主人能操作她的桌面"})
                        return
                    action = str(body.get("action") or "move").strip().lower()
                    pos_raw = str(body.get("pos") or "").strip()
                    pos = int(pos_raw) if pos_raw.isdigit() else None
                    opponent = str(body.get("opponent") or "bot").strip()
                    self._json(200, app.her_tic_move(action, pos, opponent))
                else:
                    self._json(404, {"ok": False, "error": f"未知接口 {path}"})
            except DigitalLifeError as exc:
                self._json(502, {"ok": False, "error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                log.exception("处理 %s 出错", path)
                self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

        # ---------------------------------------------------------- 业务

        def _handle_chat(self, body: dict[str, Any], session: dict[str, Any] | None = None) -> None:
            message = str(body.get("message") or "")[:4000]
            session = session or self._session() or {}
            user_id = str(session.get("api_user_id") or "web-default").strip() or "web-default"
            sender = str(session.get("user_id") or "visitor")
            image_path = str(body.get("image_path") or "").strip()
            audio_path = str(body.get("audio_path") or "").strip()
            surface = str(session.get("surface") or "")
            channel = surface_channel(surface)
            # 网页上聊天：主人（FATHER/SOLVER）是 web，自助注册的是 web_guest
            # ——2026-11-03 爸爸："全都算同一个她"，记忆共用，但别把陌生人当爸爸。
            if not channel:
                channel = "web_guest" if session.get("guest") else "web"
            group = ""
            conversation = body.get("conversation") if isinstance(body.get("conversation"), dict) else None
            if conversation:
                kind = str(conversation.get("kind") or "").strip()
                conv_name = str(conversation.get("name") or "").strip()
                conv_id = str(conversation.get("id") or "").strip()
                if kind == "group":
                    channel = "chat_group"
                    group = conv_name or conv_id
                elif kind == "private":
                    channel = "chat_private"
            if surface:
                default_sender = "爸爸" if surface == "chat" else "我"
                if conversation and conversation.get("kind") == "private" and conversation.get("name"):
                    default_sender = str(conversation["name"])
                sender, message = parse_surface_input(message, default_sender)

            if not message.strip() and not image_path and not audio_path:
                self._json(400, {"ok": False, "error": "说点什么吧"})
                return
            # 2026-11-15 修：路径能指向任意本机文件（例如 web_auth_secret），
            # 现在必须落在 upload_dir 里。
            _upload_root = app.upload_dir.resolve()
            for _label, _path in (("图片", image_path), ("语音", audio_path)):
                if not _path:
                    continue
                try:
                    _resolved = Path(_path).resolve()
                    _resolved.relative_to(_upload_root)
                except Exception:  # noqa: BLE001
                    self._json(400, {"ok": False, "error": f"{_label}路径不合法"})
                    return
                if not _resolved.exists():
                    self._json(400, {"ok": False, "error": f"{_label}不存在，可能上传失败"})
                    return

            # 2026-11-13：网页可能在公网服务器上，她的脑子在本机——路径对方读不到。
            # 小文件直接带 base64 过去；太大就只带路径（本机网页仍然可用）。
            import base64 as _b64  # noqa: PLC0415

            image_b64 = image_suffix = audio_b64 = audio_suffix = ""
            for _label, _path in (("image", image_path), ("audio", audio_path)):
                if not _path:
                    continue
                try:
                    _file = Path(_path)
                    if _file.stat().st_size > 4 * 1024 * 1024:
                        continue
                    _encoded = _b64.b64encode(_file.read_bytes()).decode("ascii")
                    _suffix = _file.suffix.lower()
                    if _label == "image":
                        image_b64, image_suffix = _encoded, _suffix
                    else:
                        audio_b64, audio_suffix = _encoded, _suffix
                except Exception:  # noqa: BLE001
                    log.debug("读取上传媒体失败：%s", _path, exc_info=True)

            started = time.time()
            result = app.chat(
                message, user_id, image_path, sender_name=sender, channel=channel, group=group,
                audio_path=audio_path,
                image_base64=image_b64, audio_base64=audio_b64,
                image_suffix=image_suffix, audio_suffix=audio_suffix,
            )
            result["elapsed"] = round(time.time() - started, 2)
            log.info(
                "chat user=%s %.1fs sticker=%s | %s -> %s",
                user_id,
                result["elapsed"],
                (result.get("sticker") or {}).get("tag"),
                (message or image_path)[:40],
                (result.get("reply") or "")[:50],
            )
            self._json(200, result)

        def _handle_douyin_switch(self, action: str, session: dict[str, Any] | None = None) -> dict[str, Any]:
            if action not in ("on", "off", "status"):
                return {"ok": False, "error": "action 只能是 on / off / status"}
            if action == "status":
                return {"ok": True, "douyin": app.desk_douyin()}
            key = str((session or {}).get("key") or "").upper()
            if key not in ("FATHER", "SOLVER"):
                return {"ok": False, "error": "开关抖音只给 FATHER / SOLVER"}
            return app.douyin_switch(action)

        def _handle_upload(self, query: dict[str, list[str]]) -> None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0:
                self._json(400, {"ok": False, "error": "空文件"})
                return
            if length > MAX_UPLOAD:
                self._json(413, {"ok": False, "error": "文件太大（上限 20MB）"})
                return

            raw_name = (query.get("name") or ["upload.bin"])[0]
            suffix = Path(raw_name).suffix.lower()
            if suffix not in UPLOAD_EXTS:
                self._json(400, {"ok": False, "error": f"不支持的类型：{suffix or '未知'}"})
                return

            data = self.rfile.read(length)
            name = f"{uuid.uuid4().hex[:16]}{suffix}"
            target = app.upload_dir / name
            target.write_bytes(data)
            log.info("上传 %s（%d KB）", name, len(data) // 1024)
            self._json(200, {"ok": True, "path": str(target), "url": f"/media/{name}"})

        # ---------------------------------------------------------- 她想做的事

        def _wish_banner(self, session: dict[str, Any], back: str = "/") -> str:
            """首页横幅：她想发朋友圈 / 想换头像，等爸爸一句话。"""
            if session.get("key") not in ("FATHER", "SOLVER"):
                return ""
            pending = owner_requests()
            if not pending:
                return ""
            cards = []
            for item in pending[:5]:
                kind = str(item.get("type"))
                label = WISH_LABEL.get(kind, "想做件事")
                text = html_lib.escape(str(item.get("text", ""))).replace("\n", "<br>")
                at = html_lib.escape(str(item.get("at", ""))[5:16].replace("T", " "))
                cards.append(
                    f"<div class='xy-wish-item'><div class='xy-wish-meta'>{label} · {at}</div>"
                    f"<p>{text}</p>"
                    "<form method='post' action='/wishes/decide'>"
                    f"<input type='hidden' name='id' value='{html_lib.escape(str(item.get('id', '')))}'>"
                    f"<input type='hidden' name='back' value='{html_lib.escape(back)}'>"
                    "<button name='action' value='done' type='submit'>处理完了</button>"
                    "<button name='action' value='dismiss' type='submit'>这次不弄</button>"
                    "</form></div>"
                )
            more = f"<div class='xy-wish-meta'>还有 {len(pending) - 5} 条</div>" if len(pending) > 5 else ""
            return (
                "<style>.xy-wish{max-width:900px;margin:10px auto 0;padding:10px 14px;"
                "background:#12191b;border:1px solid rgba(53,246,255,.35);border-radius:10px;"
                "color:#d9ffff;font:14px/1.6 'PingFang SC','Microsoft YaHei',sans-serif}"
                ".xy-wish h3{margin:0 0 6px;font-size:13px;color:#35f6ff;letter-spacing:.06em}"
                ".xy-wish-item{border-top:1px dashed rgba(53,246,255,.2);padding:8px 0}"
                ".xy-wish-meta{font-size:11px;color:#7b9096;margin-bottom:4px}"
                ".xy-wish p{margin:0 0 6px;white-space:normal}"
                ".xy-wish form{margin:0}.xy-wish button{margin-right:8px;background:transparent;"
                "color:#8dffb0;border:1px solid #35f6ff55;border-radius:6px;padding:4px 12px;"
                "font:inherit;cursor:pointer}</style>"
                f"<div class='xy-wish'><h3>她想做的事（等她开口，你点头）</h3>{''.join(cards)}{more}</div>"
            )

        def _handle_wish_decision(self) -> None:
            session = self._session()
            if not session or session.get("key") not in ("FATHER", "SOLVER"):
                self._redirect("/login")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            raw = self.rfile.read(min(length, 8192)).decode("utf-8", errors="replace") if length else ""
            form = parse_qs(raw)
            request_id = str((form.get("id") or [""])[0]).strip()
            action = str((form.get("action") or [""])[0]).strip()
            back = str((form.get("back") or ["/"])[0]).strip()
            if back not in ("/", "/solver"):
                back = "/"
            if request_id and action in ("done", "dismiss"):
                with OWNER_REQUESTS.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({
                        "type": "wish_decision",
                        "id": request_id,
                        "status": "done" if action == "done" else "dismissed",
                        "by": session.get("user_id"),
                        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    }, ensure_ascii=False) + "\n")
                log.info("她的心愿 %s 已标记 %s", request_id, action)
            self._redirect(back)

        # ---------------------------------------------------------- 静态文件

        def _serve_index(self, session: dict[str, Any] | None = None) -> None:
            index = STATIC_DIR / "index.html"
            if not index.exists():
                self._json(500, {"ok": False, "error": "web/index.html 不见了"})
                return
            content = index.read_bytes()
            session = session or self._session() or {}
            if session:
                payload = {
                    "user_id": session.get("user_id"),
                    "api_user_id": session.get("api_user_id"),
                    "label": session.get("label"),
                    "surface": session.get("surface") or "",
                    # 2026-11-08：普通注册用户只留信道，看不到桌面那几栏
                    "privileged": is_privileged(session),
                }
                injected = (
                    "<script>window.__XY_SESSION__="
                    + json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
                    + ";</script>"
                ).encode("utf-8")
                content = content.replace(b"</head>", injected + b"</head>", 1)
                banner = self._wish_banner(session, back="/").encode("utf-8")
                if banner:
                    content = content.replace(b"<body>", b"<body>" + banner, 1)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

        def _serve_diary(self) -> None:
            rows = []
            if app.db_path and Path(app.db_path).exists():
                con = sqlite3.connect(f"file:{app.db_path}?mode=ro", uri=True)
                try:
                    rows = con.execute(
                        "select content, created_at, source from diaries order by id desc limit 80"
                    ).fetchall()
                finally:
                    con.close()
            articles = []
            for content, created_at, source in rows:
                when = str(created_at)[:16].replace("T", " ")
                if source == "ollama":
                    when += " · 早期本地模型，可能有幻觉"
                text = html_lib.escape(str(content)).replace("\n", "<br>")
                articles.append(f'<article><div class="meta">{when}</div><p>{text}</p></article>')
            growth = ""
            if app.db_path:
                growth_path = Path(app.db_path).parent / "growth_diary.md"
                if growth_path.exists():
                    growth = html_lib.escape(growth_path.read_text(encoding="utf-8", errors="ignore")).replace("\n", "<br>")
            sections = []
            if growth:
                sections.append(f'<section class="growth"><h2>成长日记</h2><div>{growth}</div></section>')
            if articles:
                sections.append("<h2>早期日记</h2>\n" + "\n".join(articles))
            body = "\n".join(sections) or "<p class='empty'>还没有日记，等她今晚睡一觉。</p>"
            page = f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>小鱼的日记</title>
<style>
 body{{margin:0;background:#f6f2ea;color:#2b2b2b;font:16px/1.75 "PingFang SC","Microsoft YaHei",sans-serif}}
 header{{padding:28px 20px 12px}} h1{{margin:0;font-size:22px}}
 main{{max-width:720px;margin:0 auto;padding:0 20px 60px}}
 h2{{font-size:16px;margin:22px 0 8px;color:#6b5e4a}}
 article{{background:#fffdf8;border:1px solid #e8dfd0;border-radius:14px;padding:16px 18px;margin:14px 0;box-shadow:0 1px 2px rgba(0,0,0,.03)}}
 .growth{{background:#fffaf0;border:1px solid #e3d5bc;border-radius:14px;padding:14px 18px;margin:14px 0}}
 .meta{{font-size:12px;color:#9a8f7d;margin-bottom:6px}} p{{margin:0;white-space:normal}}
 .empty{{color:#9a8f7d}}
</style></head>
<body><header><h1>小鱼的日记</h1></header><main>{body}</main></body></html>"""
            data = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _solver_records(self) -> list[dict[str, Any]]:
            if not SOLVER_INBOX.exists():
                return []
            result = []
            for line in SOLVER_INBOX.read_text(encoding="utf-8", errors="ignore").splitlines():
                try:
                    item = json.loads(line)
                    if isinstance(item, dict):
                        result.append(item)
                except Exception:  # noqa: BLE001
                    continue
            return result

        def _room_state(self) -> dict[str, Any]:
            """她在哪、在做什么——房间页每 5 秒读一次。"""
            import attention  # noqa: PLC0415
            import space  # noqa: PLC0415

            info = space.sync_from_activity()
            snap = attention.snapshot()
            sleep = {}
            try:
                sleep = json.loads((DATA_DIR / "sleep_state.json").read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                sleep = {}
            book = {}
            try:
                book = self._book_progress()
            except Exception:  # noqa: BLE001
                book = {}
            hour = datetime.now().hour + datetime.now().minute / 60
            room = info["room"]
            name = info["name"]
            # 2026-11-12：她此刻的活动（生活循环维护），房间页用它决定躺还是坐
            activity = ""
            activity_label = ""
            try:
                life = json.loads((DATA_DIR / "life_state.json").read_text(encoding="utf-8")) or {}
                activity = str(life.get("activity") or "")
                activity_label = str(life.get("label") or "")
            except Exception:  # noqa: BLE001
                activity = ""

            if info["moving"]:
                doing = "正走过去%s，还有 %d 秒" % (
                    space.ROOMS.get(info["to"], {}).get("name", ""), round(info["arrive_in"]))
                badge = "走过去"
            elif sleep.get("asleep"):
                doing = "睡着了"
                badge = "睡觉"
            elif activity == "resting":
                doing = "在歇着，没做什么"
                badge = "歇会儿"
            elif snap["mode"] == "focus":
                doing = "%s，专注 %d 分钟" % (snap["target"] or "手上的事", round(snap["minutes"]))
                badge = "心无旁骛"
            elif book.get("title") and room == "desk":
                doing = "在读《%s》%s" % (book["title"], book.get("where", ""))
                badge = "读书"
            else:
                doing = space.ROOMS[room]["display"]
                badge = name
            calls = snap.get("calls") or []
            objects = {}
            try:
                book = self._book_progress()
                if book.get("title"):
                    objects["desk"] = {"book": book["title"], "chapter": book.get("chapter")}
            except Exception:  # noqa: BLE001
                pass
            try:
                doodles = app.desk_doodles()[:1]
                if doodles:
                    newest = doodles[0]
                    objects["easel"] = {
                        "name": str(newest.get("caption") or newest.get("name") or "一张画")[:30],
                        "url": "/api/drawn?name=" + str(newest.get("name") or ""),
                        "at": str(newest.get("at") or "")[11:16],
                    }
            except Exception:  # noqa: BLE001
                pass
            if sleep.get("asleep"):
                objects["bed"] = {"covered": True, "until": str(sleep.get("until") or "")[11:16]}
            mood = {}
            try:
                from dl_api import load_mood as _load_mood  # noqa: PLC0415

                mood = _load_mood(None, MAIN_USER_ID) or {}
            except Exception:  # noqa: BLE001
                mood = {}
            body = {}
            try:
                from HORMONE import load_body as _load_body  # noqa: PLC0415

                body = _load_body() or {}
            except Exception:  # noqa: BLE001
                body = {}
            return {
                "ok": True,
                "joy": round(float(mood.get("joy") or 0), 1),
                "hurt": round(float(mood.get("hurt") or 0), 1),
                "drowsy": round(float(body.get("drowsy") or 0), 3),
                "room": room,
                "room_name": name,
                "doing": doing,
                "badge": badge,
                "mode": "focus" if snap["mode"] == "focus" else ("sleep" if sleep.get("asleep") else snap["mode"]),
                "hour": round(hour, 2),
                "lamp": hour >= 18 or hour < 7,
                "calls": len(calls),
                "objects": objects,
                "asleep": bool(sleep.get("asleep")),
                "activity": activity,
                "activity_label": activity_label,
            }

        def _room_inspect(self, slot: str) -> dict[str, Any]:
            """点开家具：那儿现在有什么。"""
            import attention  # noqa: PLC0415
            import space  # noqa: PLC0415

            if slot == "bed":
                sleep = {}
                try:
                    sleep = json.loads((DATA_DIR / "sleep_state.json").read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    sleep = {}
                body = {}
                try:
                    sys.path.insert(0, str(ROOT))
                    from HORMONE import load_body  # noqa: PLC0415

                    body = load_body()
                except Exception:  # noqa: BLE001
                    body = {}
                if sleep.get("asleep"):
                    sub = "她在睡"
                    lines = [["睡下", str(sleep.get("since") or "")[11:16]],
                             ["预计醒", str(sleep.get("until") or "")[11:16]]]
                else:
                    sub = "她醒着"
                    lines = [["疲劳", "%.0f%%" % (float(body.get("fatigue") or 0) * 100)],
                             ["困意", "%.0f%%" % (float(body.get("drowsy") or 0) * 100)],
                             ["上次醒", str(body.get("awake_since") or "")[11:16]]]
                return {"title": "床", "sub": sub, "lines": lines}

            if slot == "desk":
                book = self._book_progress()
                lines = []
                if book.get("title"):
                    lines.append(["正在读", "《%s》" % book["title"]])
                    lines.append(["进度", book.get("where", "")])
                else:
                    lines.append(["手上", "没在读什么"])
                try:
                    snap = attention.snapshot()
                    if snap["mode"] == "focus":
                        lines.append(["正专注", "%s（%d 分钟）" % (snap["target"], round(snap["minutes"]))])
                except Exception:  # noqa: BLE001
                    pass
                return {"title": "电脑桌", "sub": "她在这儿说话、读书", "lines": lines}

            if slot == "shelf":
                lines = []
                try:
                    conn = sqlite3.connect("file:%s?mode=ro" % str(ROOT / "data" / "xiaoyu.db"), uri=True)
                    for name, total, done in conn.execute(
                            "select book_name, count(*), sum(read_at!='') from book_chapters "
                            "group by book_name order by min(id)"):
                        lines.append([str(name), "%d/%d 章" % (int(done or 0), int(total or 0))])
                    conn.close()
                except Exception:  # noqa: BLE001
                    lines = []
                return {"title": "书架", "sub": "读完的会自己收起来", "lines": lines}

            if slot in ("easel", "garden"):
                if slot == "easel":
                    try:
                        items = app.desk_doodles()[:5]
                    except Exception:  # noqa: BLE001
                        items = []
                    lines = [[str(i.get("caption") or i.get("name"))[:16],
                              str(i.get("at") or "")[5:16].replace("T", " ")] for i in items]
                    return {"title": "画画的角落", "sub": "她在这张地毯上画", "lines": lines or [["还没有画", "—"]]}
                try:
                    snap = app.desk_games().get("garden") or {}
                except Exception:  # noqa: BLE001
                    snap = {}
                plots = snap.get("plants") or snap.get("plots") or []
                lines = [[str(i.get("name") or "?"), "浇水" if i.get("thirsty") else "还好"] for i in plots[:6]]
                return {"title": "植物角", "sub": "院子就在这儿", "lines": lines or [["还空着", "—"]]}

            return {"title": slot, "sub": "这儿是她的房间", "lines": []}

        def _book_progress(self) -> dict[str, Any]:
            """她手上那本书读到哪了。"""
            try:
                conn = sqlite3.connect("file:%s?mode=ro" % str(ROOT / "data" / "xiaoyu.db"), uri=True)
                rows = list(conn.execute(
                    "select book_name, chapter_num, read_at from book_chapters "
                    "where read_at != '' order by read_at desc limit 1"))
                total = list(conn.execute(
                    "select count(*), sum(read_at != '') from book_chapters where book_name = ?",
                    (rows[0][0],) if rows else ("",)))
                conn.close()
            except Exception:  # noqa: BLE001
                return {}
            if not rows:
                return {}
            name, chapter = rows[0][0], int(rows[0][1])
            count = int(total[0][0] or 0) if total else 0
            done = int(total[0][1] or 0) if total else 0
            where = "第 %d 章（%d/%d）" % (chapter, done, count) if count else "第 %d 章" % chapter
            return {"title": name, "where": where, "chapter": chapter}

        def _serve_solver(self, session: dict[str, Any]) -> None:
            # 2026-11-08：普通注册用户也能看信道，但只能看，不能回、不能处理愿望
            if not session:
                self._json(403, {"ok": False, "error": "请先登录"})
                return
            readonly = not is_privileged(session)
            records = self._solver_records()
            messages = [item for item in records if item.get("type") == "message"]
            if readonly:
                # 普通用户只看她**自己愿意公开**的那些
                messages = [item for item in messages if str(item.get("visibility") or "") == "public"]
            threads: dict[str, list[dict[str, Any]]] = {}
            for item in records:
                if item.get("type") in ("discussion", "reply"):
                    thread = str(item.get("thread") or "default")
                    threads.setdefault(thread, []).append(item)

            pc_records = []
            if PC_REQUESTS.exists():
                for line in PC_REQUESTS.read_text(encoding="utf-8", errors="ignore").splitlines():
                    try:
                        item = json.loads(line)
                        if isinstance(item, dict):
                            pc_records.append(item)
                    except Exception:  # noqa: BLE001
                        continue
            pc_status = {
                str(item.get("id")): str(item.get("status") or "")
                for item in pc_records if item.get("type") == "pc_decision"
            }
            pc_requests = [item for item in pc_records if item.get("type") == "pc_request"]
            pc_html = "".join(
                "<article class='pc'><div class='meta'>电脑申请 · {at} · {status}</div>"
                "<p>{text}</p>"
                "<form method='post' action='/solver/pc'>"
                "<input type='hidden' name='id' value='{id}'>"
                "<button name='action' value='approve' type='submit'>批准</button> "
                "<button name='action' value='deny' type='submit'>拒绝</button></form></article>".format(
                    at=html_lib.escape(str(item.get("at", ""))),
                    status=html_lib.escape(pc_status.get(str(item.get("id")), "pending")),
                    text=html_lib.escape(str(item.get("text", ""))).replace("\n", "<br>"),
                    id=html_lib.escape(str(item.get("id", ""))),
                )
                for item in reversed(pc_requests[-20:])
            ) or "<p class='empty'>她还没有申请用电脑。</p>"

            open_requests = [item for item in records if item.get("type") == "open_douyin"]
            open_html = "".join(
                "<article class='pc'><div class='meta'>她想打开抖音 · {at}</div>"
                "<p>她在信道里请求打开抖音。请 SOLVER 手动启动专用浏览器和私信。</p></article>".format(
                    at=html_lib.escape(str(item.get("at", ""))),
                )
                for item in reversed(open_requests[-20:])
            ) or "<p class='empty'>她还没有请求打开抖音。</p>"

            # 2026-11-08 爸爸："只展示新留言，旧的全在'查看更多留言'里面。"
            # 做法：记一个"上次看到哪儿"的时间戳（solver_page_seen.txt）。
            # 比它新的 → 直接铺开；其余全部收进底部的「查看更多留言」（每条仍可点开看全文，
            # 免得旧的又占一屏，翻不到下面）。
            def _preview(text: str) -> str:
                flat = " ".join(str(text or "").split())
                return (flat[:44] + "…") if len(flat) > 44 else flat

            def _full(item: dict[str, Any]) -> str:
                return ("<div class='full'>{}</div>".format(
                    html_lib.escape(str(item.get("text", ""))).replace("\n", "<br>")))

            seen_path = ROOT / "data" / "solver_page_seen.txt"
            try:
                seen_at = seen_path.read_text(encoding="utf-8").strip()
            except OSError:
                seen_at = ""
            if not seen_at and messages:
                # 第一次打开：最近 5 条算"新"（水位取第 6 条的上一刻），其余进"查看更多"
                pivot = messages[-6] if len(messages) >= 6 else None
                seen_at = str(pivot.get("at") or "") if pivot else ""
            fresh = [item for item in messages if str(item.get("at") or "") > seen_at]
            older = [item for item in messages if str(item.get("at") or "") <= seen_at]

            message_html = "".join(
                "<article><div class='meta'>新留言 · {at}</div>{full}</article>".format(
                    at=html_lib.escape(str(item.get("at", ""))), full=_full(item))
                for item in reversed(fresh)
            ) or "<p class='empty'>没有新留言。</p>"

            if older:
                older_html = "".join(
                    "<details class='msg'><summary><span class='meta'>留言 · {at}</span>"
                    "<span class='preview'>{preview}</span></summary>{full}</details>".format(
                        at=html_lib.escape(str(item.get("at", ""))),
                        preview=html_lib.escape(_preview(item.get("text", ""))),
                        full=_full(item),
                    )
                    for item in reversed(older[-200:])
                )
                if len(older) > 200:
                    older_html += "<p class='empty more'>更早的 {} 条没显示。</p>".format(len(older) - 200)
                message_html += (
                    "<details class='morebox'><summary>查看更多留言（{n} 条）</summary>{body}</details>"
                ).format(n=len(older), body=older_html)

            # 看过了就推进水位（这页是"收件箱"语义）
            if messages:
                newest = str(messages[-1].get("at") or "")
                if newest and newest > seen_at:
                    try:
                        seen_path.parent.mkdir(parents=True, exist_ok=True)
                        seen_path.write_text(newest, encoding="utf-8")
                    except OSError:
                        pass

            def _wish_image(item: dict[str, Any]) -> str:
                """2026-11-08 爸爸："让信道想发朋友圈这里可以放图片。"

                她提朋友圈请求时，digital_life 会把她最近那张画挂在 image 字段上；
                这里只负责显示（点开看大图），原图还在 data/drawn/。
                """
                name = str(item.get("image") or "").strip()
                if not re.fullmatch(r"[0-9A-Za-z_\-]+\.png", name):
                    return ""
                url = "/api/drawn?name=" + name
                return (
                    "<a href='" + url + "' target='_blank' rel='noopener'>"
                    "<img class='wish-img' src='" + url + "' alt='她想配的图' loading='lazy'></a>"
                )

            def _wish_card(item: dict[str, Any]) -> str:
                """只读用户（普通注册）看不到处理按钮。"""
                form = ""
                if not readonly:
                    form = (
                        "<form method='post' action='/wishes/decide'>"
                        "<input type='hidden' name='id' value='{id}'>"
                        "<input type='hidden' name='back' value='/solver'>"
                        "<button name='action' value='done' type='submit'>处理完了</button> "
                        "<button name='action' value='dismiss' type='submit'>这次不弄</button></form>"
                    ).format(id=html_lib.escape(str(item.get("id", ""))))
                return (
                    "<article class='pc'><div class='meta'>{label} · {at}</div><p>{text}</p>{image}{form}"
                    "</article>"
                ).format(
                    label=WISH_LABEL.get(str(item.get("type")), "想做件事"),
                    at=html_lib.escape(str(item.get("at", ""))),
                    text=html_lib.escape(str(item.get("text", ""))).replace("\n", "<br>"),
                    image=_wish_image(item),
                    form=form,
                )

            if readonly:
                pc_html = "<p class='empty'>（这部分只有主人能看）</p>"
                open_html = ""
            wish_html = "".join(_wish_card(item) for item in owner_requests()[:20]) \
                or "<p class='empty'>她现在没有想发朋友圈、也没有想换头像。</p>"

            thread_html = []
            if readonly:
                pass
            for thread, items in list(threads.items())[-12:]:
                first = next((item for item in items if item.get("type") == "discussion"), items[0])
                replies = [item for item in items if item.get("type") == "reply"]
                replies_html = "".join(
                    "<div class='reply'><div class='meta'>solver · {at}</div>{text}</div>".format(
                        at=html_lib.escape(str(item.get("at", ""))),
                        text=html_lib.escape(str(item.get("text", ""))).replace("\n", "<br>"),
                    )
                    for item in replies
                ) or "<div class='empty'>还没有回复。</div>"
                thread_html.append(
                    "<article class='thread'><div class='meta'>她发起的代码讨论 · {at}</div>"
                    "<p>{text}</p>{replies}"
                    "<form method='post' action='/solver/reply'>"
                    "<input type='hidden' name='thread' value='{thread}'>"
                    "<textarea name='text' rows='3' placeholder='回复她…'></textarea>"
                    "<button type='submit'>发送回复</button></form></article>".format(
                        at=html_lib.escape(str(first.get("at", ""))),
                        text=html_lib.escape(str(first.get("text", ""))).replace("\n", "<br>"),
                        replies=replies_html,
                        thread=html_lib.escape(thread),
                    )
                )
            threads_html = "".join(thread_html) or "<p class='empty'>她还没有发起代码讨论。</p>"
            body = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"><title>她的信道 // SOLVER</title>
<style>
body{{margin:0;background:#0d0d0d;color:#d9ffff;font:15px/1.75 "Cascadia Mono",Consolas,monospace}}
header{{padding:24px 22px 10px;border-bottom:1px solid rgba(53,246,255,.2)}}
h1{{margin:0;color:#35f6ff;font-size:20px;letter-spacing:.08em}}
a{{color:#8dffb0}} main{{max-width:900px;margin:0 auto;padding:20px}}
h2{{color:#b26cff;font-size:15px;margin-top:28px;letter-spacing:.08em}}
article{{background:#0a1012;border:1px solid rgba(53,246,255,.24);border-radius:4px;padding:14px 16px;margin:12px 0}}
.thread{{border-color:rgba(178,108,255,.38)}} .meta{{color:#5b6b70;font-size:11px;margin-bottom:5px}}
p{{white-space:normal;margin:0 0 8px}} .reply{{border-left:2px solid #b26cff;padding:6px 10px;margin:8px 0;color:#eadcff}}
textarea{{width:100%;background:#05090a;color:#8dffb0;border:1px solid rgba(53,246,255,.32);padding:8px;font:inherit}}
button{{margin-top:8px;background:transparent;color:#35f6ff;border:1px solid #35f6ff;padding:7px 14px;font:inherit;cursor:pointer}}
.empty{{color:#5b6b70}}
details.msg{{background:#0a1012;border:1px solid rgba(53,246,255,.24);border-radius:4px;padding:10px 14px;margin:10px 0}}
details.msg summary{{cursor:pointer;display:flex;gap:10px;align-items:baseline;list-style:none}}
details.msg summary::-webkit-details-marker{{display:none}}
details.msg summary::before{{content:"▸";color:#35f6ff;flex:0 0 auto}}
details.msg[open] summary::before{{content:"▾"}}
details.msg .preview{{color:#d9ffff;opacity:.78;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
details.msg[open] .preview{{display:none}}
details.msg .full{{margin-top:8px}}
details.msg .more{{margin:8px 0 0}}
details.morebox{{border:1px solid rgba(53,246,255,.18);border-radius:4px;padding:10px 14px;margin:18px 0}}
details.morebox>summary{{cursor:pointer;color:#35f6ff;font-size:13px;letter-spacing:.04em}}
details.morebox[open]>summary{{margin-bottom:6px}}
.wish-img{{max-width:260px;max-height:260px;border:1px solid rgba(53,246,255,.28);border-radius:4px;
margin:6px 0 10px;display:block}}
/* 2026-11-08 手机版适配 */
@media (max-width: 640px) {{
  body{{font-size:14px;line-height:1.7}}
  header{{padding:16px 14px 8px}} h1{{font-size:17px}}
  main{{padding:14px 12px}}
  article{{padding:12px 12px;margin:10px 0}}
  details.msg{{padding:8px 10px}}
  details.msg summary{{flex-wrap:wrap;gap:6px}}
  details.msg .preview{{white-space:normal;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;
    overflow:hidden}}
  .wish-img,article img{{max-width:100%;height:auto;max-height:none}}
  button{{padding:9px 14px;font-size:14px}}
  textarea{{font-size:16px}}
}}
</style></head><body><header><h1>她的独立信道 // {html_lib.escape(str(session.get('key')))}</h1>
<div class="meta"><a href="/">返回聊天</a> · 只有她主动写下的内容会出现在这里</div></header>
<main><h2>她的留言</h2>{message_html}<h2>她想做的事（朋友圈 / 头像）</h2>{wish_html}<h2>她想打开抖音</h2>{open_html}<h2>她想用电脑</h2>{pc_html}<h2>她发起的代码讨论</h2>{threads_html}</main></body></html>"""
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _handle_solver_reply(self) -> None:
            session = self._session()
            if not session or session.get("key") not in ("SOLVER", "FATHER"):
                self._redirect("/login")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            raw = self.rfile.read(min(length, 16384)).decode("utf-8", errors="replace") if length else ""
            form = parse_qs(raw)
            thread = str((form.get("thread") or [""])[0]).strip()
            text = str((form.get("text") or [""])[0]).strip()
            if thread and text:
                with SOLVER_INBOX.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({
                        "type": "reply",
                        "thread": thread,
                        "from": "solver",
                        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "text": text[:4000],
                    }, ensure_ascii=False) + "\n")
            self._redirect("/solver")

        def _handle_pc_decision(self) -> None:
            session = self._session()
            if not session or session.get("key") not in ("SOLVER", "FATHER"):
                self._redirect("/login")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            raw = self.rfile.read(min(length, 8192)).decode("utf-8", errors="replace") if length else ""
            form = parse_qs(raw)
            request_id = str((form.get("id") or [""])[0]).strip()
            action = str((form.get("action") or [""])[0]).strip()
            if request_id and action in ("approve", "deny"):
                with PC_REQUESTS.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({
                        "type": "pc_decision",
                        "id": request_id,
                        "status": "approved" if action == "approve" else "denied",
                        "by": session.get("user_id"),
                        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    }, ensure_ascii=False) + "\n")
            self._redirect("/solver")

        def _tts_bytes(self, payload: dict[str, Any], singing: bool = False) -> bytes:
            """向 TTS 服务要一段 wav。失败抛异常。"""
            request = urllib.request.Request(
                TTS_URL.rstrip("/") + ("/sing" if singing else "/tts"),
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            with urllib.request.urlopen(request, timeout=TTS_TIMEOUT) as resp:
                return resp.read()

        def _serve_rvc_enqueue(self, query: dict[str, list[str]]) -> None:
            """先让 Breeze 出声（快），再把换成她音色的任务排进队列（异步）。"""
            cfg = rvc_config()
            if not cfg.get("enabled"):
                self._json(400, {"ok": False, "error": "RVC 没开"})
                return
            text = str((query.get("text") or [""])[0]).strip()[:160]
            if not text:
                self._json(400, {"ok": False, "error": "没有要说的字"})
                return
            payload: dict[str, Any] = {"text": text, "cfg_scale": 4.0, "seed": 42}
            instruction = str((query.get("instr") or [""])[0]).strip()[:300]
            if instruction:
                payload["instruction"] = instruction
            if str((query.get("sing") or [""])[0]).strip() in ("1", "true"):
                payload["melody"] = str((query.get("melody") or ["twinkle"])[0]).strip()[:20]
            mood = {}
            for key in ("joy", "hurt", "impulse"):
                raw = str((query.get(key) or [""])[0]).strip()
                if raw:
                    try:
                        mood[key] = float(raw)
                    except ValueError:
                        pass
            if mood:
                payload["mood"] = mood
            try:
                wav = self._tts_bytes(payload, singing=bool(payload.get("melody")))
            except Exception as exc:  # noqa: BLE001
                log.warning("语音服务不可用：%s", exc)
                self._json(503, {"ok": False, "error": f"她的声音服务没起来（{type(exc).__name__}）"})
                return
            RVC_JOBS.mkdir(parents=True, exist_ok=True)
            job_id = uuid.uuid4().hex[:12]
            input_path = RVC_JOBS / f"{job_id}.input.wav"
            input_path.write_bytes(wav)
            job = {
                "id": job_id,
                "input": str(input_path),
                "pitch": int(cfg.get("pitch") or 0),
                "model": str(cfg.get("model") or ""),
                "index": str(cfg.get("index") or ""),
                "created": time.time(),
            }
            (RVC_JOBS / f"{job_id}.json").write_text(
                json.dumps(job, ensure_ascii=False), encoding="utf-8"
            )
            self._json(200, {"ok": True, "job": job_id})

        def _serve_gomoku(self, session: dict[str, Any]) -> None:
            if not session:
                self._redirect("/login")
                return
            page = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>五子棋 · 小鱼</title>
<style>
body{background:#0b1418;color:#cfeeF5;font:15px/1.6 system-ui;margin:0;padding:16px}
h1{font-size:17px;color:#8fe9f5;margin:0 0 4px}
.hint{color:#7b9096;font-size:13px;margin-bottom:12px}
#board{display:grid;grid-template-columns:repeat(9,34px);gap:0;margin:0 auto;
  background:#c8a97a;padding:6px;border-radius:6px;box-shadow:0 6px 20px rgba(0,0,0,.4);width:max-content}
.cell{width:34px;height:34px;display:flex;align-items:center;justify-content:center;
  font-size:22px;cursor:pointer;color:#1c1c1c;position:relative}
.cell:after{content:"";position:absolute;inset:0;border:.5px solid rgba(60,40,20,.45)}
.cell:hover{background:rgba(255,255,255,.18)}
.cell.her{color:#12303a}.cell.other{color:#f5f7f2}
.bar{max-width:520px;margin:14px auto 0;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
button{padding:8px 16px;border-radius:10px;border:0;background:#35f6ff;color:#04262b;font-weight:600;cursor:pointer}
a{color:#8fe9f5}
#msg{color:#8dffb0;font-size:13px}
</style></head><body>
<h1>五子棋（9×9，五连赢）</h1>
<div class="hint">点格子落子。● 是小鱼，○ 是你。她下的时候这里会跟着变。</div>
<div id="board"></div>
<div class="bar"><button id="new">开新局</button><span id="msg"></span><a href="/">← 回小屋</a></div>
<script>
const board=document.getElementById('board'), msg=document.getElementById('msg');
let state=null;
function render(){
  if(!state) return;
  board.innerHTML='';
  for(let r=0;r<9;r++)for(let c=0;c<9;c++){
    const v=state.board[r*9+c];
    const d=document.createElement('div');
    d.className='cell'+(v==='her'?' her':(v==='other'?' other':''));
    d.textContent=v==='her'?'●':(v==='other'?'○':'');
    d.dataset.r=r+1; d.dataset.c=c+1;
    d.onclick=()=>move(r+1,c+1);
    board.appendChild(d);
  }
  msg.textContent = state.result ? ({her:'她赢了',other:'你赢了',draw:'和棋'}[state.result]||'') : '轮到你了';
}
async function api(payload){
  const res=await fetch('/api/her/gomoku',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  return res.json();
}
async function load(){ const d=await api({action:'state'}); state=d.gomoku||state; render(); }
async function move(r,c){
  const d=await api({action:'move',row:r,col:c});
  if(d.gomoku) state=d.gomoku;
  if(!d.ok) msg.textContent=d.error||'这步不行';
  render();
}
document.getElementById('new').onclick=async()=>{ const d=await api({action:'new'}); state=d.gomoku||state; render(); };
load();
setInterval(load, 4000);
</script></body></html>"""
            data = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _serve_accounts(self, session: dict[str, Any]) -> None:
            """所有自助注册的账号（只有 FATHER / SOLVER 能看）。"""
            if session.get("key") not in ("FATHER", "SOLVER"):
                self._json(403, {"ok": False, "error": "只有 FATHER / SOLVER 能看账号列表"})
                return
            users = load_web_users()
            # 2026-11-15：公网名单用本地缓存（tools/sync_web_users.py 同步），
            # 页面打开时不再现场 ssh，避免把请求卡死。
            _sync = {"ok": True, "out": "cached"}
            public_users = load_public_users()
            rows = []
            for name, info in sorted(users.items(), key=lambda kv: str(kv[1].get("created_at") or "")):
                rows.append(
                    "<tr><td>{}</td><td><code>{}</code></td><td>{}</td><td>{}</td></tr>".format(
                        html.escape(str(name)),
                        html.escape(str(info.get("user_id") or "")),
                        html.escape(str(info.get("ip") or "")),
                        html.escape(str(info.get("created_at") or "")),
                    )
                )
            body = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>账号 · 小鱼</title><style>
body{{background:#0b1418;color:#cfeeF5;font:14px/1.6 system-ui;padding:24px}}
table{{border-collapse:collapse;width:100%;max-width:900px}}
th,td{{border-bottom:1px solid #1d4b57;padding:8px;text-align:left;font-size:13px}}
th{{color:#8fe9f5}}code{{color:#8fe9f5}}a{{color:#8fe9f5}}
</style></head><body>
<h2>注册账号（{count} 个）</h2>
<p><a href="/">← 回聊天</a></p>
<table><tr><th>名字</th><th>user_id</th><th>注册 IP</th><th>时间</th></tr>{rows}</table>
<h2 style="margin-top:26px">公网注册（{pub_count} 个）</h2>
<p style="color:#7fb6c4">同步：{sync_state}</p>
<table><tr><th>名字</th><th>user_id</th><th>注册 IP</th><th>时间</th></tr>{pub_rows}</table>
</body></html>""".format(count=len(users), rows="".join(rows), pub_count=len(public_users),
                        sync_state="成功" if _sync.get("ok") else "失败（用上次缓存）",
                        pub_rows="".join(
                            "<tr><td>{}</td><td><code>{}</code></td><td>{}</td><td>{}</td></tr>".format(
                                html.escape(str(info.get("name") or name)),
                                html.escape(str(info.get("user_id") or "")),
                                html.escape(str(info.get("ip") or "")),
                                html.escape(str(info.get("created_at") or "")),
                            ) for name, info in sorted(public_users.items())
                        ))
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _serve_toy(self, session: dict[str, Any]) -> None:
            """小玩具：本地 qwen2.5:14b 的对话框（和主模型无关，挂了也能玩）。"""
            html = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>小玩具 · 本地模型</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{background:#0b1418;color:#cfeeF5;font:15px/1.6 system-ui;margin:0;padding:16px}
#log{max-width:720px;margin:0 auto 90px;display:flex;flex-direction:column;gap:10px}
.b{padding:8px 12px;border-radius:12px;max-width:80%;white-space:pre-wrap}
.me{align-self:flex-end;background:#1f5f46;color:#eafff3}.it{align-self:flex-start;background:#12323d}
form{position:fixed;bottom:0;left:0;right:0;display:flex;gap:8px;padding:12px;background:#0b1418;border-top:1px solid #1d4b57}
input{flex:1;padding:10px;border-radius:10px;border:1px solid #1d4b57;background:#0f2027;color:#cfeeF5}
button{padding:10px 18px;border-radius:10px;border:0;background:#35f6ff;color:#04262b;font-weight:600}
a{color:#8fe9f5}</style></head><body>
<div id="log"></div>
<form id="f"><input id="q" placeholder="跟小玩具说点什么…" autocomplete="off"><button>说</button></form>
<script>
const log=document.getElementById('log'), q=document.getElementById('q'), f=document.getElementById('f');
function add(cls,text){const d=document.createElement('div');d.className='b '+cls;d.textContent=text;log.appendChild(d);scrollTo(0,document.body.scrollHeight);}
add('it','我是本地的小玩具（qwen2.5:14b），不联网、不算她。随便聊。');
f.addEventListener('submit',async(e)=>{e.preventDefault();const text=q.value.trim();if(!text)return;q.value='';add('me',text);add('it','想想…');const pending=log.lastChild;
 try{const r=await fetch('/api/toy',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:text})});const d=await r.json();pending.textContent=d.reply||'（没说话）';}
 catch(err){pending.textContent='（小玩具坏了：'+err.message+'）';}});
</script></body></html>"""
            data = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _handle_toy(self, body: dict[str, Any]) -> None:
            import urllib.request as _req  # noqa: PLC0415

            text = str(body.get("message") or "").strip()[:2000]
            if not text:
                self._json(400, {"ok": False, "error": "说点什么"})
                return
            payload = {
                "model": "qwen2.5:14b",
                "messages": [
                    {"role": "system", "content": "你是一个本地的小玩具模型，陪人聊天，说话短、口语、别端着；不要假装自己是小鱼。"},
                    {"role": "user", "content": text},
                ],
                "stream": False,
                "options": {"temperature": 0.8, "num_predict": 300},
            }
            try:
                req = _req.Request(
                    "http://127.0.0.1:11434/api/chat",
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json; charset=utf-8"},
                )
                with _req.urlopen(req, timeout=120) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                reply = str((data.get("message") or {}).get("content") or "").strip()
            except Exception as exc:  # noqa: BLE001
                self._json(502, {"ok": False, "error": f"本地模型没起来：{type(exc).__name__}"})
                return
            self._json(200, {"ok": True, "reply": reply or "（它没说话）"})

        def _serve_inbox(self, query: dict[str, list[str]], session: dict[str, Any]) -> None:
            """IM 通道的收件箱：最近收到的IM消息（按时间倒序，最多 40 条）。"""
            # 2026-11-15 修：原来任何登录用户都能用 ?surface=chat 读主人的IM收件箱
            if not is_privileged(session):
                self._json(403, {"ok": False, "error": "只有主人能看IM收件箱"})
                return
            surface = str(session.get("surface") or (query.get("surface") or ["chat"])[0])
            if surface != "chat":
                self._json(200, {"ok": True, "surface": surface, "messages": [],
                                 "note": "抖音暂时没有本地缓冲，等她自己去抖音里看"})
                return
            try:
                limit = max(1, min(80, int((query.get("limit") or ["30"])[0])))
            except ValueError:
                limit = 30
            messages: list[dict[str, Any]] = []
            conversations: list[dict[str, Any]] = []
            try:
                data = json.loads(SOCIAL_MESSAGES.read_text(encoding="utf-8"))
                chats = data.get("chats") if isinstance(data, dict) else {}
                for key, items in (chats or {}).items():
                    if not str(key).startswith("chat:"):
                        continue
                    kind = "group" if ":group:" in key else "private"
                    conv_id = str(key).split(":")[-1]
                    latest = max((float(i.get("timestamp") or 0) for i in (items or [])), default=0.0)
                    name = ""
                    for item in reversed(items or []):
                        if item.get("conversation_name"):
                            name = str(item["conversation_name"])
                            break
                    conversations.append({
                        "key": str(key), "id": conv_id, "kind": kind,
                        "name": name or conv_id, "at": latest,
                        "count": len(items or []),
                    })
                    for item in items or []:
                        if not isinstance(item, dict) or not str(item.get("text") or "").strip():
                            continue
                        messages.append({
                            "conversation": str(item.get("conversation_name") or item.get("conversation_id") or ""),
                            "key": str(key),
                            "kind": kind,
                            "sender": str(item.get("sender_name") or ""),
                            "text": str(item.get("text") or "")[:400],
                            "at": float(item.get("timestamp") or 0),
                            "outgoing": bool(item.get("outgoing")),
                        })
            except Exception as exc:  # noqa: BLE001
                log.warning("读IM收件箱失败：%s", exc)
            messages.sort(key=lambda m: m["at"], reverse=True)
            conversations.sort(key=lambda c: c["at"], reverse=True)
            self._json(200, {
                "ok": True, "surface": surface,
                "messages": messages[:limit],
                "conversations": conversations[:20],
            })

        def _serve_rvc_status(self, query: dict[str, list[str]]) -> None:
            job_id = str((query.get("job") or [""])[0]).strip()
            if not job_id or not re.fullmatch(r"[0-9a-f]{6,32}", job_id):
                self._json(400, {"ok": False, "error": "job 不合法"})
                return
            status_file = RVC_JOBS / f"{job_id}.status.json"
            if not status_file.exists():
                if (RVC_JOBS / f"{job_id}.wav").exists():
                    self._json(200, {"ok": True, "state": "done", "error": ""})
                else:
                    self._json(200, {"ok": True, "state": "queued", "error": ""})
                return
            try:
                data = json.loads(status_file.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                self._json(200, {"ok": True, "state": "queued", "error": ""})
                return
            self._json(200, {
                "ok": True,
                "state": str(data.get("state") or "queued"),
                "error": str(data.get("error") or ""),
            })

        def _serve_rvc_audio(self, query: dict[str, list[str]]) -> None:
            job_id = str((query.get("job") or [""])[0]).strip()
            if not job_id or not re.fullmatch(r"[0-9a-f]{6,32}", job_id):
                self._json(400, {"ok": False, "error": "job 不合法"})
                return
            path = RVC_JOBS / f"{job_id}.wav"
            if not path.exists():
                self._json(404, {"ok": False, "error": "还没做好"})
                return
            self._send_file(path)

        def _serve_voice_stream(self, query) -> None:
            """把 tts_server 的 PCM 流原样转给浏览器（2026-11-13）。"""
            import urllib.parse  # noqa: PLC0415
            import urllib.request  # noqa: PLC0415

            params = {k: v[0] for k, v in query.items() if v}
            url = TTS_URL.rstrip("/") + "/tts_stream?" + urllib.parse.urlencode(params)
            try:
                upstream = urllib.request.urlopen(url, timeout=600)
            except Exception as exc:  # noqa: BLE001
                # 2026-11-13：公网 7861 是 SSH 反向隧道过来的，断了要能看出是哪一段
                log.warning("语音流服务不可用（%s）：%s", TTS_URL, exc, exc_info=True)
                self._json(503, {
                    "ok": False,
                    "error": f"声音服务没连上（隧道/TTS 未启动）：{type(exc).__name__}",
                })
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            for header in ("X-Sample-Rate", "X-Channels", "X-Format"):
                value = upstream.headers.get(header)
                if value:
                    self.send_header(header, value)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                while True:
                    chunk = upstream.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except Exception:  # noqa: BLE001
                pass
            finally:
                try:
                    upstream.close()
                except Exception:  # noqa: BLE001
                    pass

        def _serve_voice(self, query: dict[str, list[str]], singing: bool = False) -> None:
            """把她的一句话合成语音：她自己写 [VOICE] 时走这里；[SING] 走 /api/sing。"""
            text = str((query.get("text") or [""])[0]).strip()[:160]
            if not text:
                self._json(400, {"ok": False, "error": "没有要说的字"})
                return
            instruction = str((query.get("instr") or [""])[0]).strip()[:300]
            payload = {"text": text, "cfg_scale": 4.0, "seed": 42}
            if instruction:
                payload["instruction"] = instruction
            if singing:
                payload["melody"] = str((query.get("melody") or ["twinkle"])[0]).strip()[:20]
            mood = {}
            for key in ("joy", "hurt", "impulse"):
                raw = str((query.get(key) or [""])[0]).strip()
                if raw:
                    try:
                        mood[key] = float(raw)
                    except ValueError:
                        pass
            if mood:
                payload["mood"] = mood
            try:
                request = urllib.request.Request(
                    TTS_URL.rstrip("/") + ("/sing" if singing else "/tts"),
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json; charset=utf-8"},
                )
                with urllib.request.urlopen(request, timeout=TTS_TIMEOUT) as resp:
                    data = resp.read()
            except Exception as exc:  # noqa: BLE001
                log.warning("语音服务不可用（%s）：%s", TTS_URL, exc, exc_info=True)
                self._json(503, {
                    "ok": False,
                    "error": "声音服务没连上（隧道/TTS 未启动）",
                })
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(data)

        def _serve_sticker(self, query: dict[str, list[str]]) -> None:
            sticker_id = (query.get("id") or [""])[0]
            tag = (query.get("tag") or [""])[0]
            if sticker_id:
                path = app.sticker_file(sticker_id)
            else:
                sticker = app.stickers.pick([tag] if tag else [])
                path = app.stickers.resolve(sticker) if sticker else None
            if path is None or not path.exists():
                self._json(404, {"ok": False, "error": "没有这张表情"})
                return
            self._send_file(path)

        def _serve_drawn(self, query: dict[str, list[str]]) -> None:
            """她自己画的图：data/drawn/<名字>.png。"""
            name = (query.get("name") or [""])[0]
            if not re.fullmatch(r"[0-9A-Za-z_\-]+\.png", name or ""):
                self._json(400, {"ok": False, "error": "名字不对"})
                return
            path = Path(__file__).resolve().parent / "data" / "drawn" / name
            if not path.exists():
                self._json(404, {"ok": False, "error": "没有这张画"})
                return
            self._send_file(path)

        def _serve_upload(self, name: str) -> None:
            safe = Path(name).name
            path = app.upload_dir / safe
            if not path.exists():
                self._json(404, {"ok": False, "error": "文件不存在"})
                return
            self._send_file(path)

        def _send_file(self, path: Path) -> None:
            content = path.read_bytes()
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(content)

        # ---------------------------------------------------------- 工具

        def _read_json(self) -> dict[str, Any] | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > MAX_BODY:
                self._json(413, {"ok": False, "error": "请求体太大"})
                return None
            raw = self.rfile.read(length) if length else b""
            if not raw.strip():
                return {}
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(400, {"ok": False, "error": "请求体必须是 UTF-8 JSON"})
                return None
            if not isinstance(data, dict):
                self._json(400, {"ok": False, "error": "请求体必须是 JSON 对象"})
                return None
            return data

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            body = _json_bytes(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _empty(self, status: int) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            log.debug("%s - %s", self.address_string(), fmt % args)

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="她网页版")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--api", default="http://127.0.0.1:8000", help="dl_api 地址")
    parser.add_argument("--stickers", default=str(ROOT / "stickers"))
    parser.add_argument("--uploads", default=str(ROOT / "web_uploads"))
    parser.add_argument("--sticker-chance", type=float, default=0.15)
    parser.add_argument("--db", default=str(ROOT / "data" / "xiaoyu.db"), help="日记页读取的数据库")
    args = parser.parse_args(argv)

    app = WebApp(
        api_url=args.api,
        sticker_dir=Path(args.stickers),
        upload_dir=Path(args.uploads),
        sticker_chance=args.sticker_chance,
        db_path=Path(args.db),
    )

    server = ThreadingHTTPServer((args.host, args.port), _make_handler(app))
    server.daemon_threads = True
    print(f"她网页版已启动：http://{args.host}:{args.port}")
    print(f"  数字生命 API：{args.api}")
    print(f"  表情包：{app.stickers.count()} 个（{args.stickers}）")
    print("  Ctrl+C 退出")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在退出……")
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
