# -*- coding: utf-8 -*-
"""HTTP 层：/health、/status、/v1/models、POST /chat 等接口的 Handler。

2026-11-17 从 dl_api.py 拆出（开颅手术 2/2）。逻辑逐字搬运，只改了三类东西：

1. import / 路径：``Path(__file__).resolve().parent`` -> ``_BRIDGE_DIR``
   （本文件在 dl_server/ 下，data/ 在上一级）。
2. dl_api 里的运行时依赖改成 ``set_runtime()`` 数据写入（不反向 import，会成环）。
3. 三个模块级全局（``_current_contact_id`` / ``_CURRENT_SPEAKER`` /
   ``_IS_OWNER_TURN``）的赋值改成 setter 写回 dl_api —— prompt_builder 还在读它们。

## 为什么依赖要"现取"，不能 import 时拿一份快照

- **测试会 patch dl_api 上的函数。** tests/test_red_lines.py 用
  ``mock.patch.object(dl_api, "is_sleeping", ...)`` / ``queue_pending``；
  tests/test_media_dl_api_base64.py 用 ``note_preference``。若这里
  ``from dl_api import is_sleeping``，拿到的是导入那一刻的旧对象，patch 不生效，
  红线测试就成了假绿。所以函数一律走**代理**：调用时再去 dl_api 的 globals 里
  取当前对象。
- **有些值 import 之后才定。** ``SERVER_DB`` 在 main() 里才被 ``--db`` 改写
  （18099 冒烟全靠它指到临时库）；``REPLY_ALL`` / ``HORMONES`` / ``LAST_STATUS`` /
  ``_LAST_CONTACT`` 运行期会变。这些在调用点现取。

## dl_api.py 末尾（所有定义之后）调一次

    from dl_server import http_handler
    http_handler.set_runtime(
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

``lambda: X`` 是安全的：它每次调用都从 dl_api 的 globals 现查 X，
所以 dl_api 里重新绑定 X、或测试 patch 掉 X，这里立刻看到。

没数据写入时各依赖有安全兜底，模块可以单独 import，不会炸。
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import random
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable

_BRIDGE_DIR = Path(__file__).resolve().parent.parent
_DATA_DIR = _BRIDGE_DIR / "data"

# digital_life 在仓库根目录（chat_bridge/ 的上一级）。
_ROOT = _BRIDGE_DIR.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_BRIDGE_DIR) not in sys.path:
    sys.path.append(str(_BRIDGE_DIR))

log = logging.getLogger("dl_api.http_handler")

# 直接 import 的子模块：它们本来就不在 dl_api 里（dl_api 只是 re-export），
# 没有 monkeypatch 依赖，直接拿就行。
from dl_server.media import (  # noqa: E402
    _fetch_image, _sticker_tags, _tags_from_description,
)
from dl_server.physiology import MELATONIN_MAX, melatonin_tick  # noqa: E402
from dl_server.social import (  # noqa: E402
    canonical_sender, contact_id_for_sender, contact_names,
    owner_sender_names, remember_alias,
)
from dl_server.text_utils import (  # noqa: E402
    clean_incoming_text, clean_partial_tags, human_reply_delay_ms,
    shape_reply, split_bubbles, strip_stage_directions,
)

# digital_life 里的常量，dl_api 也只是 import 进来。同一个对象，直接拿。
from digital_life import (  # noqa: E402
    DEFAULT_PERSONA, HISTORY_PROMPT_TURNS, OLLAMA_MODEL, OLLAMA_TIMEOUT,
)

# 纯常量：只被 handler 用到（AST 查过引用），搬过来。
MAX_MESSAGE_CHARS = 4000
_MAX_BODY = 16 * 1024 * 1024

_DEFAULT_MAIN_USER_ID = (
    "conv:9e52b4549f424e730d8871644385bcce63768cc74b8f8ec7ffa895f62ca73eeb"
)

# ---------------------------------------------------------------------------
# 运行时依赖
# ---------------------------------------------------------------------------

# dl_api 的模块 globals（set_runtime(namespace=globals()) 写进来）。
# 函数代理从这里现取目标，所以测试 patch dl_api.X 后立刻生效。
_NS: dict[str, Any] = {}

# dl_api 里 handler 会调到的函数。名字必须和 dl_api 里的完全一致。
_FUNCTIONS = (
    "_feed_hormones", "_owner_mood_decision", "_record_skill", "_trace_turn",
    "apply_douyin_action", "apply_garden", "apply_minigames", "attach_drawing_data",
    "attention_now", "chat_with_llm", "decide_reply", "digest_for", "dir_drill_check",
    "douyin_browse", "douyin_contacts_block", "douyin_search_note", "douyin_send_text",
    "gomoku_block", "group_skip_reason", "handle_agency_tags", "handle_garden_from_words",
    "is_sleeping", "load_mood", "load_owner", "load_style_profile", "mark_agent_state",
    "maybe_doodle", "maybe_draw", "maybe_look", "maybe_move_from_words", "maybe_paint",
    "maybe_practice", "maybe_reset_from_words", "maybe_start_gomoku_from_words",
    "maybe_start_tic_from_words", "meme_context_for", "meme_note", "note_preference",
    "persona_with_time", "plant_tag_block", "proactive_throttled", "queue_pending",
    "record_book_wishes", "record_catchphrases", "record_douyin_watch",
    "record_her_gomoku", "record_her_open", "record_her_tictac", "record_pc_requests",
    "record_quiet_request", "record_solver_channel", "remember_intent_from",
    "repeat_reason", "route_her_intent", "run_douyin_command", "run_douyin_sticker",
    "run_douyin_text", "set_attention", "set_chat_poll_pause", "set_sleep_state",
    "speak_weight", "sticker_label", "tic_mismatch_note", "touch_activity",
)


def _live(name: str) -> Callable[..., Any]:
    """调用时再去 dl_api 的 globals 里取当前对象；没数据写入就报清楚。"""
    def proxy(*args: Any, **kwargs: Any) -> Any:
        target = _NS.get(name)
        if target is None:
            raise RuntimeError(
                f"dl_server.http_handler 的运行时依赖 {name!r} 还没数据写入；"
                "dl_api.py 末尾应调用 http_handler.set_runtime(...)"
            )
        return target(*args, **kwargs)
    proxy.__name__ = name
    proxy.__qualname__ = name
    return proxy


for _name in _FUNCTIONS:
    globals()[_name] = _live(_name)
del _name

# 类型名只在注解里出现（from __future__ import annotations，运行时不求值），
# 给个占位真对象，get_type_hints 之类的 introspection 也不至于炸。
for _name in ("LifeExecutor", "LifeManager", "MediaEnricher", "StickerStore"):
    globals()[_name] = type(_name, (), {})
del _name

# --- 运行期会变的值：调用点现取 ---------------------------------------------
def _default_server_db() -> Path:
    return _DATA_DIR / "xiaoyu.db"


def _default_heartbeat_file() -> Path:
    return _DATA_DIR / "life_heartbeat.json"


def _default_solver_inbox() -> Path:
    return _DATA_DIR / "solver_channel.jsonl"


def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None


def _false() -> bool:
    return False


_server_db: Callable[[], Path] = _default_server_db
_heartbeat_file: Callable[[], Path] = _default_heartbeat_file
_solver_inbox: Callable[[], Path] = _default_solver_inbox
_main_user_id: Callable[[], str] = lambda: _DEFAULT_MAIN_USER_ID
_reply_all: Callable[[], bool] = _false
_hormones: Callable[[], Any] = lambda: None
_last_status: Callable[[], dict[str, Any]] = dict
_last_contact: Callable[[], dict[str, Any]] = dict

# 每轮对话会变的三个"她是谁 / 在跟谁说"。dl_api 里是模块级全局，
# prompt_builder 通过自己的引用读回去，所以必须写回 dl_api，不能只存本模块。
_contact_setter: Callable[[str], None] = _noop
_speaker_setter: Callable[[str], None] = _noop
_owner_turn_setter: Callable[[bool], None] = _noop


def set_current_contact_id(value: str) -> None:
    _contact_setter(value)


def set_current_speaker(value: str) -> None:
    _speaker_setter(value)


def set_owner_turn(value: bool) -> None:
    _owner_turn_setter(value)


def set_runtime(
    *,
    namespace: dict[str, Any] | None = None,
    server_db: Callable[[], Path] | None = None,
    heartbeat_file: Callable[[], Path] | None = None,
    solver_inbox: Callable[[], Path] | None = None,
    main_user_id: Callable[[], str] | None = None,
    reply_all: Callable[[], bool] | None = None,
    hormones: Callable[[], Any] | None = None,
    last_status: Callable[[], dict[str, Any]] | None = None,
    last_contact: Callable[[], dict[str, Any]] | None = None,
    current_contact_setter: Callable[[str], None] | None = None,
    current_speaker_setter: Callable[[str], None] | None = None,
    owner_turn_setter: Callable[[bool], None] | None = None,
) -> None:
    """dl_api.py 在所有定义之后调一次，把运行时依赖数据写入本模块。

    传的全部是**取值函数**而不是快照：SERVER_DB 在 main() 里才定，
    REPLY_ALL / HORMONES / 她这轮在跟谁说，都是运行期会变的；
    函数则通过 namespace 现取，测试 patch dl_api.X 后立刻生效。

    namespace 应传 ``globals()`` —— 传的是模块的活字典本身，不是拷贝，
    后面 dl_api 里重新绑定/替换的名字，这里也能看到。
    """
    global _NS, _server_db, _heartbeat_file, _solver_inbox, _main_user_id
    global _reply_all, _hormones, _last_status, _last_contact
    global _contact_setter, _speaker_setter, _owner_turn_setter

    if namespace is not None:
        _NS = namespace
    if server_db is not None:
        _server_db = server_db
    if heartbeat_file is not None:
        _heartbeat_file = heartbeat_file
    if solver_inbox is not None:
        _solver_inbox = solver_inbox
    if main_user_id is not None:
        _main_user_id = main_user_id
    if reply_all is not None:
        _reply_all = reply_all
    if hormones is not None:
        _hormones = hormones
    if last_status is not None:
        _last_status = last_status
    if last_contact is not None:
        _last_contact = last_contact
    if current_contact_setter is not None:
        _contact_setter = current_contact_setter
    if current_speaker_setter is not None:
        _speaker_setter = current_speaker_setter
    if owner_turn_setter is not None:
        _owner_turn_setter = owner_turn_setter


def build_handler(executor: LifeExecutor, enricher: MediaEnricher,
                  keep_stage: bool = False, media_dir: Path | None = None,
                  delay_fn: Callable[[], int] | None = None,
                  digest_limit: int = 5,
                  ignore_chance: float = 0.0,
                  max_bubble: int = 45,
                  style_path: Path | None = None,
                  stickers: StickerStore | None = None,
                  db_path: Path | None = None) -> type[BaseHTTPRequestHandler]:
    media_root = media_dir or (Path.cwd() / "data" / "openai_media")
    media_root.mkdir(parents=True, exist_ok=True)
    pick_delay = delay_fn or (lambda: 0)

    class Handler(BaseHTTPRequestHandler):
        _last_status_at = 0.0
        _digest_cache: dict[str, tuple[float, str]] = {}

        server_version = "DigitalLifeAPI/1.0"
        protocol_version = "HTTP/1.1"

        # ---------------------------------------------------------- 连接健壮性

        def handle_one_request(self) -> None:
            # 客户端（urllib / PowerShell）提前断开时不要往 stderr 喷堆栈
            try:
                super().handle_one_request()
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                self.close_connection = True

        # ---------------------------------------------------------- GET

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path == "/health":
                self._send(200, {"ok": True, "service": "digital-life"})
            elif path == "/status":
                # 2026-11-05：改成"只读快照"。状态查询永不去挤执行器——
                # 她正说着一句 30 秒的长回答时，健康检查也不会误报"8000 无响应"。
                info = dict(_last_status()) or {"users": [], "user_count": 0}
                info.setdefault("users", [])
                info.setdefault("user_count", len(info.get("users") or []))
                info["busy"] = bool(executor.busy) if hasattr(executor, "busy") else info.get("busy", False)
                # 2026-11-04 爸爸："网页端她情绪三维怎么老是不显示。"
                # 以前只有 /chat 的返回里带 emotion，网页一刷新就没了；这里把当前情绪也给出。
                try:
                    _users = info.get("users") or []
                    _uid = _users[0] if _users else ""
                    _mood = load_mood(_server_db(), _uid) if _uid else {}
                except Exception:  # noqa: BLE001
                    _mood = {}
                self._send(200, {"ok": True, **info, "media": enricher.status(),
                                 "emotion": {k: _mood.get(k) for k in ("joy", "hurt", "impulse", "intimacy")},
                                 "emotion_full": _mood})
            elif path == "/v1/models":
                self._send(200, {
                    "object": "list",
                    "data": [
                        {"id": "xiaoyu", "object": "model", "owned_by": "digital-life"},
                        # IM插件里配置的模型名是中文「小鱼」，这里也列出来
                        {"id": "小鱼", "object": "model", "owned_by": "digital-life"},
                    ],
                })
            else:
                self._send(404, {"ok": False, "error": f"未知接口 {path}"})

        # ---------------------------------------------------------- POST

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            body = self._read_json()
            if body is None:
                return
            if path == "/chat":
                self._handle_chat(body)
            elif path == "/proactive":
                self._handle_proactive(body)
            elif path == "/v1/chat/completions":
                self._handle_openai_chat(body)
            elif path == "/share":
                self._handle_share(body)
            elif path == "/emoji/learn":
                self._handle_emoji_learn(body)
            elif path == "/compress":
                self._handle_compress(body)
            elif path == "/memory/note":
                self._handle_memory_note(body)
            elif path == "/describe":
                self._handle_describe(body)
            elif path == "/decide":
                self._handle_decide(body)
            elif path == "/remember":
                # 2026-11-04：她选择把刚才那段"收进图书馆"时，协议桥会调这里。
                text_in = str(body.get("text") or "").strip()
                uid = (str(body.get("user_id") or "").strip()
                       or _main_user_id())   # 2026-11-14 修：原来这里是字符串字面量，记忆会写进空白用户
                if not text_in:
                    self._send(400, {"ok": False, "error": "text 不能为空"})
                    return

                def _remember(manager: LifeManager) -> dict[str, Any]:
                    life = manager.get(uid)
                    book = life.remember(text_in[:1500], user="爸爸", origin="archive")
                    return {"ok": True, "id": book.get("id") if isinstance(book, dict) else None}

                self._send(200, executor.run(_remember, timeout=120))
            else:
                self._send(404, {"ok": False, "error": f"未知接口 {path}"})


        def _refresh_status(self, manager: LifeManager) -> None:
            """把状态快照刷新一下（只在执行器线程上调）。"""
            try:
                info = manager.status()
                _last_status().clear()
                _last_status().update(info)
            except Exception:  # noqa: BLE001
                log.debug("刷新状态快照失败", exc_info=True)

        def _handle_chat(self, body: dict[str, Any]) -> None:
            message = body.get("message")
            if message is None:
                message = ""
            if not isinstance(message, str):
                self._send(400, {"ok": False, "error": "message 必须是字符串"})
                return
            message = message.strip()[:MAX_MESSAGE_CHARS]
            # 2026-11-16：IM群消息常带"（成员ID:xxx）"和行首时间戳，
            # 它们会进她记忆的摘要，让一堆记忆长得一模一样（检索互相挤掉）。
            message = re.sub(r"[（(]\s*成员ID\s*[:：][^）)]{0,80}[）)]", "", message)
            message = re.sub(r"(?m)^\s*\d{1,2}:\d{2}\s+", "", message).strip()

            image_path = str(body.get("image_path") or "").strip()
            audio_path = str(body.get("audio_path") or "").strip()
            emoji_url = str(body.get("emoji_url") or "").strip()
            # 2026-11-13：网页（公网）和她的脑子不在一台机器上，文件路径传不过来，
            # web_ui 会带 base64；这里先落成临时文件，再走原来的识别/听写链路。
            for _kind in ("image", "audio"):
                _payload_b64 = str(body.get(f"{_kind}_base64") or "").strip()
                if not _payload_b64:
                    continue
                try:
                    import base64 as _b64std  # noqa: PLC0415

                    _suffix = str(body.get(f"{_kind}_suffix") or "").strip().lower()
                    if not _suffix:
                        _suffix = ".png" if _kind == "image" else ".wav"
                    if not _suffix.startswith("."):
                        _suffix = "." + _suffix
                    # 2026-11-14 修：后缀来自请求体，不校验就能带路径分隔符
                    _allowed = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
                                ".wav", ".mp3", ".m4a", ".ogg", ".webm")
                    if _suffix not in _allowed:
                        _suffix = ".png" if _kind == "image" else ".wav"
                    _tmpdir = Path(tempfile.mkdtemp(prefix=f"xy_{_kind}_"))
                    _tmpfile = _tmpdir / f"in{_suffix}"
                    _tmpfile.write_bytes(_b64std.b64decode(_payload_b64))
                    if _kind == "image":
                        image_path = str(_tmpfile)
                    else:
                        audio_path = str(_tmpfile)
                except Exception:  # noqa: BLE001
                    log.debug("网页媒体 base64 落盘失败（%s）", _kind, exc_info=True)
            has_media = bool(image_path or audio_path or emoji_url)
            media_note = ""
            note_preference(message)

            if not message and not has_media:
                self._send(400, {"ok": False, "error": "message 不能为空"})
                return

            if has_media:
                # 视觉/ASR 放在 HTTP 线程里做，别把数字生命的执行线程占住
                _before = message
                message = enricher.enrich(
                    message, image_path=image_path, audio_path=audio_path, emoji_url=emoji_url
                )
                # 2026-11-04 爸爸："她不是先说看清了吗，怎么又失忆了。"
                # 看过的内容要写进她自己的账本（action notes），下次才想得起来，
                # 不然她下一轮只剩一句"我没看清"。
                _seen = message.replace(_before, "").strip() if _before else message.strip()
                if _seen:
                    media_note = f"他发来一张图/一段语音，你当时看到的是：{_seen[:300]}"

            user_id = str(body.get("user_id") or body.get("sender") or "default").strip() or "default"
            owner_id, owner_name = load_owner()
            # 网页的随机 web-* / web-default 统一归到主人，不再各自养一份记忆。
            if owner_id and (user_id == "web-default" or user_id in ("web", "default") or user_id.startswith("web-")):
                user_id = owner_id
            sender = str(body.get("sender") or "")
            if not sender:
                sender = contact_names().get(user_id, "")
            # IM名不一定就是本名（他IM叫 neikos）；别名表统一成本名再认人。
            sender = canonical_sender(sender)

            # 2026-11-05 自查：这里以前在 sender 赋值之前调用，永远 NameError，
            # 所以"阿哦=熟人C"这种群里的身份对照一直学不进去。
            try:
                _alias_note = remember_alias(message, sender)
                if _alias_note:
                    log.info("记住群里的身份对照：%s", _alias_note)
            except Exception:  # noqa: BLE001
                log.debug("记录身份对照失败", exc_info=True)
            # IM 里所有人共用她这一份脑（同一个 user_id），所以"是谁"看说话人的名字。
            # 联系人卡片/好感度按说话人查，不然熟人D说话也会显示成爸爸的档案。
            set_current_contact_id(contact_id_for_sender(sender) or user_id)
            # 渠道：IM群里她是同一个小鱼，但说话的人不一定是爸爸。
            channel = str(body.get("channel") or "").strip()
            group_name = str(body.get("group") or "").strip()

            if is_sleeping():
                # 2026-11-05：评估探针（eval-）不是人，别把它塞进"未读"里——
                # 网页收件箱会显示一条谁也没发过的消息。
                _is_probe = str(user_id).startswith("eval-") or str(sender) in ("solver", "探针")
                if str(body.get("channel") or "").startswith("chat") and not _is_probe:
                    queue_pending(str(body.get("sender") or ""), str(body.get("message") or ""), "睡着了")
                self._send(200, {
                    "ok": True, "user_id": user_id, "reply": "", "replies": [],
                    "delay_ms": 0, "ignored": True, "asleep": True,
                    "ignored_reason": "睡着了", "elapsed": 0,
                })
                return

            # 群里就按"谁说的"处理；私聊里 user_id 是主人，那就是爸爸本人。
            # IM 里所有会话共用一个 user_id，所以主人要看说话人名字，不能只看 user_id。
            if channel.startswith("chat"):
                is_owner = (
                    bool(owner_id)
                    and user_id == owner_id
                    and channel != "chat_group"
                    and (not sender or sender.strip() in owner_sender_names())
                )
            else:
                # 2026-11-03：网页自助注册的人共用她的记忆，但不是爸爸本人。
                is_owner = bool(owner_id) and user_id == owner_id and channel != "web_guest"
            set_owner_turn(bool(is_owner))
            if is_owner:
                shared = re.search(r"https?://(?:www\.)?(?:douyin\.com/video/\d+|v\.douyin\.com/[A-Za-z0-9_-]+)", message or "")
                if shared:
                    url = shared.group(0)
                    env = dict(os.environ)
                    env["NODE_PATH"] = str(Path.home() / ".dsh" / "profiles" / "desktop" / "node_modules")
                    try:
                        proc = subprocess.run(
                            ["node", str(_BRIDGE_DIR / "tools" / "watch_video_url.cjs"), "--url", url],
                            capture_output=True, timeout=240, env=env,
                        )
                        out = proc.stdout.decode("utf-8", "replace").strip().splitlines()
                        data = json.loads(out[-1]) if out else {"ok": False, "error": "没有输出"}
                    except Exception as exc:  # noqa: BLE001
                        data = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                    if data.get("ok"):
                        descs = []
                        for frame in (data.get("frames") or [])[:3]:
                            try:
                                desc = enricher.enrich("", image_path=frame)
                            except Exception:  # noqa: BLE001
                                desc = ""
                            if desc:
                                descs.append(desc.strip())
                        note = f"[系统：你刚看完他转发的这条视频（时长{data.get('duration')}秒），画面内容：{'；'.join(descs)[:500] or '没看清'}。就这个内容回复他] "
                    else:
                        note = f"[系统：他转发的视频没加载出来（{str(data.get('error'))[:60]}），如实说没看成] "
                    message = note + message
            if is_owner:
                try:
                    import self_dev  # noqa: PLC0415

                    if self_dev.detect(message):
                        ok_dev, detail_dev = self_dev.handle_dev_request(message, self_dev.make_llm_call())
                        if not ok_dev:
                            self_dev.queue_request(message, detail_dev)
                            message = (
                                f"[系统：这次你自己没改成（{detail_dev}），已经把请求写进开发队列，"
                                "开发窗口会处理；告诉爸爸你转给开发了，别说已经改好] " + message
                            )
                        else:
                            message = (
                                f"[系统：你刚动手改了代码，成功：{detail_dev}。"
                                "如实告诉爸爸，别夸大，改完要重启才生效就说清楚。] " + message
                            )
                except Exception as exc:  # noqa: BLE001
                    message = f"[系统：改代码时出错（{type(exc).__name__}: {exc}），如实说没改成] " + message
            mood = load_mood(Path(db_path) if db_path else None, user_id)
            phrases = record_catchphrases(Path(db_path) if db_path else None, user_id, message)
            wants_sticker = bool(re.search(r"表情|贴纸|表情包", message)) or bool(re.search(r"发\s*(个|一个|张)", message))
            friend_target = ""
            friend_content = ""
            friend_result = ""
            friend_pending = None
            if is_owner and str(channel or "") not in ("chat_private", "chat_group"):
                names = contact_names()
                # 记住最近提到的联系人，方便她说"骂他一句""给他发个表情"时知道是谁
                mentioned = [(cid, cname) for cid, cname in names.items() if cid != owner_id and cname and cname in message]
                if mentioned:
                    _last_contact()[user_id] = mentioned[0]
                target = mentioned[0] if mentioned else None
                # 只有正文里明确点名了联系人，才允许自动代发；代词指向的最近联系人不再触发，
                # 否则“他一点完”这类句子会被当成“发给最近那个人”。
                explicit_target = bool(mentioned)
                # 必须真的是“给某人发/说点什么”才算托付；光提到名字不算——
                # 否则“熟人B那几条碎字别发了”也会被当成托付，让她自己编一句发出去。
                # 必须真的是“给某人发/说点什么”才算托付；光提到名字、
                # 或“我跟她说过了”这种陈述，都不算。
                intent = None
                if explicit_target and target:
                    name_pat = re.escape(target[1])
                    head = r"(?:给|跟|对)\s*" + name_pat + r"\s*(?:发|说|回|留|告诉)"
                    quoting = r"\s*[：:，,]?\s*[「\"“']"
                    marker = r"\s*(?:个|一句|一条|一下|句|条|信息|消息|招呼|表情|贴纸|[：:])"
                    intent = re.search(head + quoting, message) or re.search(head + marker, message)
                    # 2026-11-03 "你能给熟人B发消息吗" 被当成了托付，还回了一句"没写清楚要发什么"。
                    # 问句/能力询问不算托付；除非后面真带了要发的内容。
                    if intent and re.search(r"(吗|\?|？|能不能|可不可以|会不会)\s*$", message) and not re.search(quoting, message):
                        intent = None
                if target and intent:
                    cid, cname = target
                    friend_target = cid
                    if re.search(r"表情|贴纸|表情包", message):
                        trigger = "哈哈"
                        try:
                            if float(mood.get("joy") or 0) >= 55:
                                trigger = "开心"
                            elif float(mood.get("hurt") or 0) >= 55:
                                trigger = "委屈"
                        except Exception:  # noqa: BLE001
                            pass
                        ok_f, err_f = run_douyin_sticker(cname, cid, trigger)
                        friend_result = (
                            f"[系统：你刚在抖音给{cname}发了一个表情，确认已发出] "
                            if ok_f else f"[系统：给{cname}发表情失败了（{err_f}）。必须如实说没发出去] "
                        )
                    # 只认「给X发『内容』」这种带引号的明确说法。以前用宽正则从正文里抠字，
                    # 会把“留言、发起代码讨论…”这类文字抠成碎片发给最近联系人。
                    found = None
                    if explicit_target:
                        found = re.search(
                            r"(?:给|对|跟)\s*" + re.escape(cname)
                            + r"\s*(?:发|说|告诉|留言?|回)(?:个|一句|一条|一下|句|条)?\s*[：:，,]?\s*"
                            + r"[「\"“']([^」\"”'\n]{1,60})[」\"”']",
                            message,
                        )
                    friend_content = (found.group(1).strip() if found else "")
                    # 「发个道歉信息」里的“道歉信息”是要求，不是要发的内容
                    if friend_content and re.search(r"道歉|道个歉|信息|一句|招呼|问好|问候|诚恳|真诚", friend_content):
                        friend_content = ""
                    # 不再从正文里猜“哈喽/你好/在吗”：猜错就会真的发出去。
                    if friend_content:
                        ok_f, err_f = run_douyin_command(
                            {"action": "text", "contactId": friend_target, "text": friend_content}, timeout=60
                        )
                        friend_result = (
                            f"[系统：你刚在抖音给{cname}发了「{friend_content}」，确认已发出] "
                            if ok_f else
                            f"[系统：给{cname}发「{friend_content}」失败了（{err_f}）。必须如实说没发出去，不许说发了] "
                        )
                    elif re.search(r"骂|凶|生气|怼", message):
                        friend_pending = (cid, cname, "scold")
                        friend_result = f"[系统：你会真的发一句假装生气的逗趣话给{cname}，比如“坏人 不理你了”；发完再说] "
                    elif re.search(r"道歉|道个歉|对不起|打个招呼|问好|问候", message):
                        friend_pending = (cid, cname, "apology")
                        friend_result = (
                            f"[系统：这次不是把“道歉”两个字发过去，而是写一句真诚的、具体的话给{cname}，"
                            "说清楚刚才在忙什么、没顾上回她、让她别介意；这句会真的发到抖音，发完再告诉他] "
                        )
                    else:
                        friend_pending = (cid, cname, "open")
                        friend_result = (
                            f"[系统：你没有给定具体内容，就把你自己想好的那句开场白发{cname}，"
                            "写在你回复里用引号括起来，那句话会被真的发到抖音；"
                            f"开场白是直接对{cname}说的，不要提爸爸、也不要提第三个人；发完再说] "
                        )
            douyin_text_intent = is_owner and "抖音" in message and not wants_sticker and bool(
                re.search(r"回我|回一下|回信息|回消息|发我|发一条|发消息|说一句|回个", message)
            )
            if friend_result:
                message = friend_result + message
            if is_owner and "抖音" in message and wants_sticker:
                joy = float(mood.get("joy") or 0)
                hurt = float(mood.get("hurt") or 0)
                impulse = float(mood.get("impulse") or 0)
                trigger = "开心" if joy >= 55 else "委屈" if hurt >= 55 else "冲" if impulse >= 55 else "哈哈"
                ok, err = run_douyin_sticker(owner_name, owner_id, trigger)
                message = ("[系统：你刚在抖音给他发过一个表情，已经发出去了] " if ok else f"[系统：想在抖音发表情但没成功（{err}），如实告诉他] ") + message
            elif douyin_text_intent:
                message = "[系统：他让你在抖音私信里回他。你在抖音有登录账号，这条回复会同时发到抖音；发完就直说。] " + message
            # 2026-11-04 爸爸："玩抖音时先不回IM，看到信息再切回IM。"
            # 一次只在一处：她在抖音那边时，IM这边先不接，话存进待回队列。
            if str(channel or "").startswith("chat") and not _reply_all() and attention_now() == "douyin":
                _clean = re.sub(r"^(\s*\[系统：[^\]]*\]\s*)+", "", message)
                _clean = _clean.split("】")[-1] if "】" in _clean else _clean
                queue_pending(sender, _clean, "她正在刷抖音")
                log.info("chat user=%s ignored (她在抖音那边，消息排队)", user_id)
                self._send(200, {
                    "ok": True, "user_id": user_id, "reply": "", "replies": [],
                    "delay_ms": 0, "ignored": True, "ignored_reason": "她正在刷抖音",
                    "elapsed": 0,
                })
                return
            # 2026-11-04 爸爸："对她自由意志管束还是太严格了，去掉除激素外所有的硬约束，
            # 改成软提醒，让她有更大选择余地。"
            # 这里以前是直接不回（硬拦截），现在只往她的上下文里放一句提示，由她自己决定。
            soft_notes: list[str] = []
            if channel == "chat_group" and not _reply_all():
                gskip = group_skip_reason(group_name, message.split("】")[-1])
                if gskip:
                    soft_notes.append(f"群里{gskip}——要不要接这句，你自己定")
                    log.info("chat user=%s soft-note (group/%s)", user_id, gskip)
            _chan = str(channel or "")
            if (not _chan or _chan in ("web", "web_guest") or _chan.startswith("chat")) and not _reply_all():
                spoken = message.split("】")[-1] if "】" in message else message
                dup = repeat_reason(sender, spoken, flood=not is_owner)
                _is_request = bool(re.search(
                    r"[?？]|吗|嘛|帮我|给我|发个|语音|声音|听听|试一下|试试|唱|讲|说说|来一句|画",
                    spoken))
                if dup and _is_request:
                    dup = None   # 2026-11-16：他在提要求，别拿"刷屏"挡他
                if dup:
                    soft_notes.append(f"这句他刚说过一遍（{dup}）——想不想再应一句，你定")
                    log.info("chat user=%s soft-note (repeat/%s)", user_id, dup)
            # IM渠道里 is_owner 是 False（场合判断用的），但说话的人确实是主人：
            # "她自己的说话意愿"（[QUIET]、很委屈时一天只说一句）跟渠道无关。
            owner_chat = is_owner
            try:
                _act = str((json.loads(_heartbeat_file().read_text(encoding="utf-8")) or {}).get("activity") or "")
            except Exception:  # noqa: BLE001
                _act = ""
            _busy_cn = {"drawing": "画画", "gardening": "在院子", "reading": "看书"}.get(_act, "")
            if _busy_cn and not _reply_all():
                soft_notes.append(f"你正在{_busy_cn}，回他一句短的就好，别装没看见")
            if owner_chat and not _reply_all():
                quiet_reason = _owner_mood_decision(message, bool(image_path), mood)
                if quiet_reason:
                    soft_notes.append(f"你现在{quiet_reason}——但想回也可以回")
                    log.info("chat user=%s soft-note (owner-mood/%s)", user_id, quiet_reason)
            if soft_notes:
                message = "[系统：" + "；".join(soft_notes) + "] " + message
            allowed, decision_reason, decision_source = decide_reply(
                message, bool(image_path), mood, is_owner, channel=channel, mood_checked=owner_chat
            )
            if _reply_all():
                allowed, decision_reason, decision_source = True, "全部回复", "force-all"
            if not allowed:
                log.info("chat user=%s ignored (%s/%s)", user_id, decision_source, decision_reason)
                try:
                    _trace_turn(
                        user_id=user_id,
                        channel=channel or "web",
                        decision="SILENT",
                        source=decision_source,
                        note=decision_reason,
                        text=str(message or "")[:160],
                        msg_len=len(message),
                    )
                except Exception:  # noqa: BLE001
                    log.debug("决策追踪写入失败", exc_info=True)
                self._send(200, {
                    "ok": True, "user_id": user_id, "reply": "", "replies": [],
                    "delay_ms": 0, "ignored": True, "ignored_reason": decision_reason,
                    "elapsed": 0,
                })
                return

            # 2026-11-04 爸爸："给小鱼接上梗百科，让她更有梗一点。"
            # 提到词库里的梗时，把词条塞进这一轮，让她接得上而不是硬猜。
            # 2026-11-11：每小时 :41 有一条带梗百科前缀的 /chat 进来，先记清来源
            try:
                _keys = ",".join(sorted(k for k in body.keys() if k not in ("message", "image_path", "audio_path")))
                log.info(
                    "chat 来源 channel=%s sender=%s keys=%s len=%d head=%s",
                    channel or "(空)", sender or "(空)", _keys[:80], len(str(message or "")),
                    str(message or "")[:40].replace("\n", " "),
                )
            except Exception:  # noqa: BLE001
                pass

            # 2026-11-11：内部唤醒（SOLVER-* / internal）不注入梗百科——
            # 它只是提示词，不是人说的话；之前把它变成「[梗百科：11-1 …]」塞回去，
            # 她只能沉默或硬接。
            set_current_speaker(str(sender or ""))
            _internal_wake = (str(sender or "").upper().startswith("SOLVER")
                              or str(sender or "").lower().startswith("solver")
                              or bool(body.get("internal")))
            _meme_note = "" if _internal_wake else meme_note(
                message, aggressive=str(channel or "") == "chat_group")
            # 2026-11-08：同一段梗百科连着注入两遍，她会照着读两遍、第二次还被带偏
            # （司凤那条就是第一次答对了、第二次去接私聊的画图）。
            # 同一个会话里，同样的词条 20 分钟内只喂一次。
            if _meme_note:
                _meme_key = (str(channel or ""), str(body.get("group") or "") or str(sender or ""),
                             _meme_note[:80])
                _meme_seen = getattr(self.server, "_meme_seen", None)
                if _meme_seen is None:
                    _meme_seen = {}
                    self.server._meme_seen = _meme_seen
                _last = _meme_seen.get(_meme_key)
                _now = time.time()
                if _last and (_now - _last) < 20 * 60:
                    log.debug("梗百科重复注入已跳过：%s", _meme_note[:30])
                    _meme_note = ""
                else:
                    _meme_seen[_meme_key] = _now
            if _meme_note:
                message = "[梗百科：" + _meme_note + "] " + message
            _douyin_note = douyin_search_note()
            if _douyin_note:
                message = _douyin_note + "\n" + message

            started = time.time()

            def job(manager: LifeManager) -> dict[str, Any]:
                # 单线程执行器：前面还有人没说完，这条就得排队。
                # 把排队时间单独记出来，才分得清"模型慢"和"排队慢"。
                _waited = time.time() - started
                if _waited > 1.0:
                    log.info("chat user=%s 排队 %.1fs", user_id, _waited)
                life = manager.get(user_id)
                self._refresh_status(manager)
                # 看过的图/听过的语音要记进她自己的账本，下一轮才不会"失忆"
                if media_note:
                    try:
                        life._note_action(media_note)
                    except Exception:  # noqa: BLE001
                        log.debug("记媒体笔记失败", exc_info=True)
                _digest_key = f"{user_id}|{message[:40]}"
                _cached = Handler._digest_cache.get(_digest_key)
                if _cached and time.time() - _cached[0] < 15.0:
                    _digest = _cached[1]
                else:
                    _digest = digest_for(life, message, digest_limit)
                    Handler._digest_cache[_digest_key] = (time.time(), _digest)
                    if len(Handler._digest_cache) > 64:
                        Handler._digest_cache.clear()
                life.persona = persona_with_time(
                    manager.persona or DEFAULT_PERSONA,
                    digest=_digest,
                    style=load_style_profile(style_path),
                    mood=mood,
                    is_owner=is_owner,
                    catchphrases=phrases,
                    douyin_context=douyin_contacts_block(message),
                    channel=channel,
                    group=group_name,
                    speaker=sender,
                )
                # 褪黑素越高，她的推理预算越少：输出更短、等待更短、检索变浅。
                try:
                    life.sleepiness = max(0.0, min(1.0, melatonin_tick() / MELATONIN_MAX))
                except Exception:  # noqa: BLE001
                    life.sleepiness = 0.0
                # 激素系统：先随时间回落，再把这一轮的"身体"交给她
                try:
                    _hormones().tick()
                    life.knobs = _hormones().knobs(life.sleepiness)
                except Exception:  # noqa: BLE001
                    life.knobs = None
                _emotion_before = life.emotion.snapshot()
                _t0 = time.time()
                _weight, _weight_src = speak_weight(user_id, sender)
                try:
                    from digital_life import LAST_LLM_MS  # noqa: PLC0415

                    LAST_LLM_MS["ms"] = 0.0
                except Exception:  # noqa: BLE001
                    LAST_LLM_MS = {"ms": 0.0}  # type: ignore[assignment]
                # 2026-11-11 接活：她知道自己站在哪儿，也知道刚才有人叫她
                _place_before = ""
                _was_focus = False
                try:
                    import attention as _attention  # noqa: PLC0415
                    import space as _space  # noqa: PLC0415

                    _space.settle()
                    _place_before = _space.where().get("room") or ""
                    _snap = _attention.snapshot()
                    _was_focus = _snap.get("mode") == "focus"
                    if not str(body.get("internal") or "").strip() and not str(sender).startswith("SOLVER"):
                        _attention.incoming(sender or "对方", message)
                except Exception:  # noqa: BLE001
                    _place_before = ""
                # 2026-11-12：平常只读摘要（快），问"还记得/以前"时才深度检索
                _deep = bool(re.search(
                    r"记得|还记得|以前|上次|那天|过去|回忆|忘没忘|想起来|很久之前", str(message or "")))
                # 2026-11-13：意图识别放后台并行跑，别串行等它（省 ~0.8s）
                # 2026-11-14 修：原来把 job 的局部变量 result 在赋值前传给路由线程，
                # 必然 UnboundLocalError 被吞掉——"并行意图识别"从来没跑过。
                # 现在路由写自己的 dict，模型返回后合并。
                _router_future = None
                _router_sink: dict[str, Any] = {}
                _router_pool = None
                try:
                    from concurrent.futures import ThreadPoolExecutor as _TPE  # noqa: PLC0415

                    _router_pool = _TPE(max_workers=1)
                    _router_future = _router_pool.submit(
                        route_her_intent, str(message or ""), _router_sink)
                except Exception:  # noqa: BLE001
                    _router_future = None
                try:
                    # 2026-11-15：告诉记忆"现在是谁"，她才能分清人
                    life.current_speaker = str(sender or "")
                except Exception:  # noqa: BLE001
                    pass
                if gomoku_block():
                    life.persona = (life.persona or "") + gomoku_block()
                result = life.chat(message, user=sender or "对方",
                                   weight=_weight, weight_source=_weight_src,
                                   remember=not _internal_wake,
                                   deep=_deep,
                                   privacy_names=None if is_owner else owner_sender_names())
                if _router_future is not None:
                    try:
                        _router_future.result(timeout=6)
                        if isinstance(_router_sink, dict):
                            for _k, _v in _router_sink.items():
                                if _v not in (None, "", [], {}):
                                    result.setdefault(_k, _v)
                    except Exception:  # noqa: BLE001
                        log.debug("意图识别超时/失败，继续走正则兜底", exc_info=True)
                if _router_pool is not None:
                    try:
                        _router_pool.shutdown(wait=False)
                    except Exception:  # noqa: BLE001
                        pass
                # 2026-11-11 中枢：她想修什么，先提出来，等爸爸批
                try:
                    import ops_brain  # noqa: PLC0415

                    _reply = str(result.get("reply") or "")
                    for _m in re.finditer(r"\[FIX_REQUEST[:：]\s*([^\]\n]{2,120})\]", _reply):
                        ops_brain.propose(
                            title=_m.group(1).strip(),
                            detail=str(message)[:200],
                            source="her" if not str(sender).startswith("SOLVER") else "solver",
                        )
                        log.info("她提了一条修复请求：%s", _m.group(1).strip()[:60])
                    _clean = re.sub(r"\[FIX_REQUEST[:：][^\]\n]{2,120}\]", "", _reply).strip()
                    if _clean != _reply:
                        result["reply"] = _clean
                except Exception:  # noqa: BLE001
                    log.debug("修复请求入队失败", exc_info=True)

                # 她回过了：把"未回的呼叫"清掉；被打断的话留一笔痕迹
                try:
                    import attention as _attention  # noqa: PLC0415
                    import space as _space  # noqa: PLC0415

                    _attention.clear_calls()
                    if _was_focus:
                        _attention.interrupted("刚才在画画")
                    _reply_text = str(result.get("reply") or "")
                    if re.search(r"(画|涂鸦|doodle)", _reply_text) and re.search(r"(画好了|画完|我画)", _reply_text):
                        _space.go("easel")
                    elif _place_before == "hall":
                        _space.settle()
                except Exception:  # noqa: BLE001
                    log.debug("位置/注意力写回失败", exc_info=True)
                _turn_ms = (time.time() - _t0) * 1000.0
                log.info("chat user=%s 处理 %.2fs（模型 %.2fs）",
                         user_id, _turn_ms / 1000.0,
                         (float(LAST_LLM_MS.get("ms") or 0.0)) / 1000.0)
                try:
                    _trace_turn(
                        user_id=user_id,
                        channel=channel or "web",
                        decision="RESPOND" if str(result.get("reply") or "").strip() else "SILENT",
                        latency_ms=int((time.time() - _t0) * 1000),
                        source=result.get("source"),
                        hits=len((result.get("retrieval") or {}).get("hits") or []) if isinstance(result.get("retrieval"), dict) else None,
                        joy_before=round(float((_emotion_before or {}).get("joy") or 0), 1),
                        hurt_before=round(float((_emotion_before or {}).get("hurt") or 0), 1),
                        joy_after=round(float((result.get("emotion") or {}).get("joy") or 0), 1),
                        hurt_after=round(float((result.get("emotion") or {}).get("hurt") or 0), 1),
                        hormones={k: round(float(v), 2) for k, v in (_hormones().values or {}).items()},
                        max_tokens=(life.knobs or {}).get("max_tokens"),
                        # 2026-11-08 爸爸：最高权限要看"具体"，不是只知道她回了一句
                        text=str(message or "")[:160],
                        reply=str(result.get("reply") or "")[:200],
                        msg_len=len(message),
                    )
                except Exception:  # noqa: BLE001
                    log.debug("决策追踪写入失败", exc_info=True)
                try:
                    _feed_hormones(result, message, sender or "", channel or "")
                except Exception:  # noqa: BLE001
                    log.debug("激素事件写入失败", exc_info=True)
                attach_drawing_data(result)
                return result

            try:
                result = executor.run(job, timeout=None)
            except Exception as exc:  # noqa: BLE001
                log.exception("chat 失败 user_id=%s", user_id)
                self._send(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
                return

            latency = time.time() - started
            # 主模型没接上：她不在线就不在，不再拿本地模板冒充她。
            if str(result.get("source") or "") == "offline" and not str(result.get("reply") or "").strip():
                mark_agent_state(False, "offline", str(result.get("llm_error") or ""))
                if str(channel or "").startswith("chat"):
                    queue_pending(sender, message.split("】")[-1] if "】" in message else message, "她不在线")
                try:
                    _trace_turn(
                        user_id=user_id, channel=channel or "web", decision="SILENT",
                        source="offline", note=str(result.get("llm_error") or "")[:120],
                        latency_ms=int(latency * 1000), msg_len=len(message),
                    )
                except Exception:  # noqa: BLE001
                    log.debug("决策追踪写入失败", exc_info=True)
                self._send(200, {
                    "ok": True, "user_id": user_id, "reply": "", "replies": [],
                    "delay_ms": 0, "ignored": True, "ignored_reason": "她不在线",
                    "source": "offline", "elapsed": 0,
                })
                return
            mark_agent_state(True, str(result.get("source") or ""))
            raw_reply = str(result.get("reply") or "")
            thought = str(result.get("thought") or "").strip()
            if thought:
                try:
                    with (_BRIDGE_DIR / "data" / "thoughts.jsonl").open("a", encoding="utf-8") as _fh:
                        _fh.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "user_id": user_id, "thought": thought[:500]}, ensure_ascii=False) + "\n")
                except Exception:  # noqa: BLE001
                    pass
            record_solver_channel(result, sender)
            record_pc_requests(result)
            record_quiet_request(result)
            if str(result.get("her_open") or "").strip():
                record_her_open(str(result.get("her_open") or ""))
            try:
                maybe_start_tic_from_words(str(raw_reply or ""), result)
            except Exception:  # noqa: BLE001
                pass
            record_her_tictac(result)
            try:
                maybe_start_gomoku_from_words(str(message or ""), str(result.get("reply") or ""),
                                              result, str(sender or ""))
            except Exception:  # noqa: BLE001
                pass
            record_her_gomoku(result)
            maybe_move_from_words(str(result.get("reply") or ""), result)
            maybe_reset_from_words(str(result.get("reply") or ""), result)
            # 2026-11-04：她落子时常常只写标签、一个字不说，网页那头就是空白。
            if not str(result.get("reply") or "").strip():
                _move = result.get("tictac_move")
                if isinstance(_move, int):
                    result["reply"] = f"我下{_move}"
                    result["replies"] = [f"我下{_move}"]
            # 2026-11-14 修：上面可能刚补写 reply，raw_reply 还停在旧值，
            # 结果补的内容不会发给客户端。这里同步一次。
            raw_reply = str(result.get("reply") or "")
            _douyin_action = str(result.get("douyin_action") or "") or ("on" if result.get("open_douyin") else "")
            if _douyin_action:
                _douyin_note = apply_douyin_action(_douyin_action)
                if _douyin_note:
                    log.info("抖音开关结果：%s", _douyin_note)
            # 2026-11-04 爸爸："给她一个权限，可以刷几分钟抖音或者搜视频看，不需要我同意。"
            _douyin_watch = int(result.get("douyin_watch") or 0)
            _douyin_search = str(result.get("douyin_search") or "").strip()
            if _douyin_watch or _douyin_search:
                apply_douyin_action("on")
                # 她人现在在抖音：IM那边先不接，等这一小段过去再回头看
                set_attention("douyin", minutes=max(1, _douyin_watch or 5))
                if _douyin_watch:
                    _info = record_douyin_watch(_douyin_watch)
                    douyin_browse("open")
                    log.info("抖音：她自己要刷 %s 分钟（到 %s）", _douyin_watch, _info.get("until"))
                if _douyin_search:
                    _res = douyin_browse("search", _douyin_search)
                    try:
                        (_BRIDGE_DIR / "data" / "douyin_search_last.json").write_text(
                            json.dumps({"query": _douyin_search, "items": _res.get("items") or [],
                                        "at": datetime.now().astimezone().isoformat(timespec="seconds")},
                                       ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                    except OSError:
                        log.debug("写抖音搜索结果失败", exc_info=True)
                    log.info("抖音搜索「%s」：%s 条（%s）", _douyin_search,
                             len(_res.get("items") or []), _res.get("error") or "ok")
            if result.get("wake_now"):
                set_sleep_state(wake=True)
            elif result.get("sleep_hours"):
                set_sleep_state(hours=float(result["sleep_hours"]))
            proactive_after = int(result.get("proactive_after") or 0)
            if result.get("resume_poll"):
                set_chat_poll_pause(resume=True)
            elif result.get("pause_poll_minutes"):
                set_chat_poll_pause(minutes=int(result["pause_poll_minutes"]))
            if proactive_after > 0:
                raw_reply = raw_reply.rstrip() + f" [[LATER:{proactive_after}]]"
            raw_reply, drawing = maybe_draw(raw_reply, origin="her")
            if drawing is not None:
                result["drawing"] = drawing
                # 2026-11-08：她写了 [DRAW] 说"画好了"，可图只落在 data/drawn 里，
                # 对面什么也看不到。画成功就直接发过去（IM私聊）。
                if drawing.get("name") and str(channel or "") == "chat_private" and sender:
                    try:
                        from chat_send import send_image  # noqa: PLC0415

                        _dpath = _BRIDGE_DIR / "data" / "drawn" / str(drawing["name"])
                        _sent = send_image(str(sender), str(_dpath))
                        result["drawing"]["sent"] = bool(_sent.get("ok"))
                        if not _sent.get("ok"):
                            result["drawing"]["send_error"] = str(_sent.get("error") or "")[:80]
                    except Exception as exc:  # noqa: BLE001
                        result["drawing"]["send_error"] = f"{type(exc).__name__}: {exc}"[:80]
            raw_reply, doodle_result = maybe_doodle(raw_reply)
            if doodle_result is not None:
                result["doodle"] = doodle_result
                # 2026-11-14：涂鸦也要在网页上显示出来，不然她"画好了发你啦"对面什么也看不到
                if str(doodle_result.get("name") or "").strip():
                    result["drawing"] = {
                        "name": str(doodle_result["name"]),
                        "caption": str(doodle_result.get("caption") or "刚画的"),
                    }
            # 2026-11-14：她画完了想发给对方（爸爸："让她把这张发过去"）。
            # 她写 [SEND_DRAWING] 就附上她最近画好的那张图，网页上会直接显示。
            # 另外支持"这一条指定要发某张"（爸爸让她发时写进 data/pending_art.txt）。
            _pending_art = _BRIDGE_DIR / "data" / "pending_art.txt"
            try:
                _wanted = _pending_art.read_text(encoding="utf-8").strip() if _pending_art.exists() else ""
            except OSError:
                _wanted = ""
            # 可以写成 "文件名|收件人"，只发给那个人（大小写不敏感）
            _wanted_to = ""
            if "|" in _wanted:
                _wanted, _wanted_to = [item.strip() for item in _wanted.split("|", 1)]
            if _wanted_to and str(sender or "").strip().lower() != _wanted_to.lower():
                _wanted = ""
            if re.search(r"\[SEND_(?:DRAWING|ART)\]", raw_reply or "", re.I):
                raw_reply = re.sub(r"\[SEND_(?:DRAWING|ART)\]", "", raw_reply or "", flags=re.I).strip()
                _wanted = _wanted or "__latest__"
            if _wanted:
                try:
                    _drawn_dir = _BRIDGE_DIR / "data" / "drawn"
                    _shots = sorted(_drawn_dir.glob("*.png"), key=lambda item: item.stat().st_mtime)
                    if _wanted == "__latest__" and _shots:
                        result["drawing"] = {"name": _shots[-1].name, "caption": "刚画好的，给你看"}
                    elif _wanted and (_drawn_dir / Path(_wanted).name).exists():
                        # 2026-11-14 修：只取文件名，防止 .. 把任意本机图片挂上去
                        result["drawing"] = {"name": Path(_wanted).name, "caption": "她画的"}
                    if _pending_art.exists():
                        _pending_art.unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    log.debug("附上指定的画失败", exc_info=True)
            try:
                route_her_intent(str(message or ""), result)
            except Exception:  # noqa: BLE001
                log.debug("意图路由失败", exc_info=True)
            raw_reply = handle_agency_tags(raw_reply, result)
            raw_reply = plant_tag_block(result, raw_reply)
            try:
                handle_garden_from_words(str(raw_reply or ""), result)
            except Exception:  # noqa: BLE001
                pass
            raw_reply, practice_result = maybe_practice(raw_reply)
            if practice_result is not None:
                result["practice"] = practice_result
            raw_reply, paint_result = maybe_paint(raw_reply)
            if paint_result is not None:
                result["painting"] = paint_result
                if paint_result.get("name"):
                    result["drawing"] = {"name": paint_result["name"],
                                         "caption": paint_result.get("caption", "")}
            raw_reply, look_result = maybe_look(raw_reply)
            if look_result is not None:
                result["looked"] = look_result
            # 2026-11-16：她嘴上说"发你了/给你看"，但忘了写 [SEND_DRAWING]，
            # 网页就什么都没有。刚画过的（2 小时内）就替她附上最新那张。
            if not isinstance(result.get("drawing"), dict):
                try:
                    if re.search(r"发你了|发你|给你看|画好了|刚画好|画完|重画|这张", str(raw_reply or "")):
                        _dir = _BRIDGE_DIR / "data" / "drawn"
                        _shots = sorted(_dir.glob("*.png"), key=lambda item: item.stat().st_mtime)
                        if _shots:
                            _age = time.time() - _shots[-1].stat().st_mtime
                            if _age < 2 * 3600:
                                result["drawing"] = {"name": _shots[-1].name,
                                                     "caption": "刚画好的，给你看"}
                                log.info("她提到发画，替她附上最新一张：%s", _shots[-1].name)
                except Exception:  # noqa: BLE001
                    log.debug("自动附带画失败", exc_info=True)
            raw_reply = re.sub(r"</?(?:content|answer|response|reply|message)\s*>", "", raw_reply, flags=re.I).strip()
            reply_text = raw_reply if keep_stage else strip_stage_directions(raw_reply)
            # 2026-11-16：她难过的时候会把标签写残（"…[STICKER: 难过"没闭合），
            # 这一截会原样发到对面。这里清掉行尾未闭合的标签残渣。
            reply_text = clean_partial_tags(reply_text)
            if str(result.get("decision") or "") != "SILENT":
                remember_intent_from(reply_text)
            # [[LATER:n]] 是给调度器看的，别让它漏进气泡（2026-11-03 见到 "[[LATER: " 露出来）
            reply_text = re.sub(r"\[\[\s*LATER\s*:\s*\d+\s*\]\]", "", reply_text).strip()
            # 每条最多 max_bubble 个字，像真人那样一点点说
            bubbles = split_bubbles(reply_text, max_bubble)
            # 2026-11-04 爸爸："她真的能在IM 端选择不回信息吗，我每次发信息她都回。"
            # 她写了 [SKIP]（或 [不回]/[不接]）：这一轮就不回，是她自己的选择。
            if result.get("skip_this"):
                bubbles = []
            # 标签写坏了会漏出半截括号（[TOY: ...] 里带括号时见过），清掉首尾孤立的括号
            bubbles = [re.sub(r"^[\]］】]+|[\]］】]+$", "", b).strip() for b in bubbles]
            bubbles = [b for b in bubbles if b]
            # 她只写了 [DRAW: …] 没留别的话：图已经发出去了，至少让她那句话说出口
            if not bubbles and isinstance(result.get("drawing"), dict) and result["drawing"].get("sent"):
                bubbles = ["（画好了，发你了）"]
            # 2026-11-09：她只写 [TICTAC: 5] 这种动作标签时，正文是空的，
            # 网页端会报"没有可用的 reply"。动作落地了就该有一句话。
            if not bubbles:
                _acted = []
                if isinstance(result.get("tictac_move"), int):
                    _acted.append(f"（我下 {result['tictac_move']}）")
                elif result.get("tictac_new"):
                    _acted.append("（开了一局）")
                if isinstance(result.get("doodle"), dict):
                    _acted.append("（画好了）")
                if not _acted and result.get("touched_toy"):
                    _acted.append("（去逗了一下小玩具）")
                if _acted:
                    bubbles = _acted

            # 2026-11-09：方向小测判分
            try:
                _drill_note = dir_drill_check(reply_text)
                if _drill_note:
                    bubbles = list(bubbles) + [_drill_note]
            except Exception:  # noqa: BLE001
                log.debug("方向小测判分失败", exc_info=True)
            try:
                _mis = result.get("tic_mismatch")
                if isinstance(_mis, tuple) and len(_mis) == 2:
                    _note = tic_mismatch_note("", _mis[0])
                    if _note:
                        bubbles = list(bubbles) + [_note]
            except Exception:  # noqa: BLE001
                log.debug("方位不一致提示失败", exc_info=True)
            try:
                _tic_note = str(result.get("tic_note") or "").strip()
                if _tic_note and not any(_tic_note[:6] in b for b in bubbles):
                    bubbles = list(bubbles) + [_tic_note]
            except Exception:  # noqa: BLE001
                log.debug("棋局反馈插入失败", exc_info=True)
            if result.get("tic_out_of_turn") and not any("轮" in b for b in bubbles):
                bubbles = list(bubbles) + ["（这步不算，现在轮到对方）"]
            if not bubbles and not result.get("skip_this"):
                # 兜底：任何情况下都别让 reply 空着
                bubbles = ["（嗯）"]
            # 她真的开口了：记一次活动，用来判断她睡没睡
            if bubbles:
                touch_activity()
                # 这些事说一次就够，别每轮重复（她已经在那一轮"听到"了）
                try:
                    sys.path.insert(0, str(_BRIDGE_DIR / "tools"))
                    import system_events as _sys_events  # noqa: PLC0415

                    _sys_events.take_notes()
                except Exception:  # noqa: BLE001
                    pass
                # 说上话了：社交轴涨一点（催产素跟着走）
                try:
                    import ENDOCRINE  # noqa: PLC0415

                    ENDOCRINE.nudge("social", 0.03)
                except Exception:  # noqa: BLE001
                    pass
            # 她想看的书：记下来，并让她知道自己"留过言"
            try:
                _wished = record_book_wishes(result.get("book_wishes") or [], sender)
                if _wished:
                    bubbles = list(bubbles) + [f"那我想看《{_wished[0]}》，记下了。"]
            except Exception:  # noqa: BLE001
                log.debug("看书愿望处理失败", exc_info=True)
            # 院子里的事：种下/浇水/看看/收
            try:
                _garden_lines = apply_garden(result.get("garden_actions") or [])
                if _garden_lines:
                    bubbles = list(bubbles) + _garden_lines
            except Exception:  # noqa: BLE001
                log.debug("院子结果拼接失败", exc_info=True)
            # 涂鸦：她自己画的那张，画好了要说一声；IM私聊里直接发给他看
            try:
                _doodle = result.get("doodle")
                if isinstance(_doodle, dict):
                    if _doodle.get("error"):
                        bubbles = list(bubbles) + [f"（涂鸦没画成：{_doodle['error']}）"]
                    else:
                        _cap = str(_doodle.get("caption") or "")
                        _where = ""
                        if str(channel or "") == "chat_private" and sender:
                            try:
                                from chat_send import send_image  # noqa: PLC0415

                                _sent = send_image(str(sender), str(_doodle.get("path") or ""))
                                _where = "已经发给你了" if _sent.get("ok") else f"没发出去（{_sent.get('error')}）"
                            except Exception as exc:  # noqa: BLE001
                                _where = f"没发出去（{type(exc).__name__}）"
                        _line = f"（涂鸦画好了：{_cap}"
                        _line += f"，{_where}）" if _where else "）"
                        bubbles = list(bubbles) + [_line]
            except Exception:  # noqa: BLE001
                log.debug("涂鸦结果拼接失败", exc_info=True)
            # 小游戏：她写了 [RPS: 石头] 之类，结果要让她自己看到
            try:
                _game_lines = apply_minigames(result.get("game_actions") or [])
                if _game_lines:
                    bubbles = list(bubbles) + _game_lines
            except Exception:  # noqa: BLE001
                log.debug("小游戏结果拼接失败", exc_info=True)
            # 她自己选了不说话（空回复）：IM 里把话存进待回队列，别丢。
            # 但"她主动选择不回"（[SKIP]）不进队列——爸爸的原话是：
            # "知道她能看到信息，在心里默默想就行"，别让待回队列过一会儿又逼她回。
            if (not bubbles and str(channel or "").startswith("chat")
                    and not result.get("skip_this")
                    and not body.get("internal")          # 2026-11-09：系统内部请求不排队
                    and not str(sender or "").startswith("SOLVER")
                    and not str(user_id).startswith("eval-")):
                clean = re.sub(r"^(\s*\[系统：[^\]]*\]\s*)+", "", message)
                clean = clean.split("】")[-1] if "】" in clean else clean
                queue_pending(sender, clean, "她自己选择不回")
            if bubbles:
                # 她一开口就等于"人回到这儿了"：把注意力从抖音挪回来
                _chan = str(channel or "")
                set_attention("chat" if _chan.startswith("chat")
                              else ("web" if _chan in ("", "web", "web_guest") else _chan))
            # 2026-11-03 爸爸："IM怎么这么人机这么客服。"
            # 真人不会每条都回满、不会每次都拆成两三条完整句子：
            # IM 里偶尔只留第一句，剩下的咽回去。
            # 2026-11-03 爸爸："她发的每条信息都好长啊，可以有短有长。"
            # IM 里大多数时候只留第一句（短），偶尔全发（长），别每次都两三段。
            if str(channel or "").startswith("chat") and len(bubbles) > 1:
                roll = random.random()
                if roll < 0.6:
                    bubbles = bubbles[:1]
                elif roll < 0.78:
                    bubbles = bubbles[:2]
                else:
                    bubbles = bubbles[:3]
            elif str(channel or "") in ("web", "web_guest") and len(bubbles) > 1:
                # 2026-11-03 爸爸："没必要回那么多。"网页上也别每次都两三段。
                roll = random.random()
                if roll < 0.35:
                    bubbles = bubbles[:1]
                elif roll < 0.6:
                    bubbles = bubbles[:2]
            if len(bubbles) > 1 and sum(len(b) for b in bubbles) > 150:
                bubbles = bubbles[:1]
            # 她想主动给IM 里的某人发一条（[IM_SEND: 名字 | 内容]）：
            # 真发，发完如实告诉她结果；失败就直说没发出去。
            chat_sends = [
                item for item in (result.get("chat_sends") or [])
                if isinstance(item, dict) and str(item.get("to") or "").strip()
            ]
            if chat_sends:
                target = chat_sends[0]
                try:
                    from chat_send import send_text as _wx_send  # noqa: PLC0415

                    sent = _wx_send(str(target.get("to") or ""), str(target.get("text") or ""))
                except Exception as exc:  # noqa: BLE001
                    sent = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                if sent.get("ok"):
                    bubbles = (bubbles + [f"已经发给{sent.get('to') or target.get('to')}了：{target.get('text')}"])[:6]
                    _record_skill("IM发消息", True)
                else:
                    bubbles = (bubbles + [f"没发出去（{sent.get('error')}）"])[:6]
                    _record_skill("IM发消息", False, str(sent.get("error") or ""))
            # 她想主动给抖音上的人发一条（[DOUYIN_SEND: 名字 | 内容]）。
            # 2026-11-07 爸爸："让她可以在IM和抖音上主动发私信。"
            douyin_sends = [
                item for item in (result.get("douyin_sends") or [])
                if isinstance(item, dict) and str(item.get("to") or "").strip()
            ]
            if douyin_sends:
                target = douyin_sends[0]
                sent = douyin_send_text(str(target.get("to") or ""), str(target.get("text") or ""))
                if sent.get("ok"):
                    bubbles = (bubbles + [f"抖音上已经发给{sent.get('to') or target.get('to')}了：{target.get('text')}"])[:6]
                    _record_skill("抖音发私信", True)
                else:
                    bubbles = (bubbles + [f"抖音那边没发出去（{sent.get('error')}）"])[:6]
                    _record_skill("抖音发私信", False, str(sent.get("error") or ""))
            # 她想跟本地小玩具说一句（[TOY: 内容]）：玩具的回答下一轮告诉她
            toy_prompts = [str(x).strip() for x in (result.get("toy_prompts") or []) if str(x).strip()]
            if toy_prompts:
                try:
                    import urllib.request as _req  # noqa: PLC0415

                    payload = json.dumps({
                        "model": "qwen2.5:14b",
                        "messages": [
                            {"role": "system", "content":
                                "你是本地的小玩具模型，说话短、口语；不要假装自己是小鱼。"
                                + meme_context_for(toy_prompts[0])},
                            {"role": "user", "content": toy_prompts[0][:1000]},
                        ],
                        "stream": False,
                        "options": {"temperature": 0.8, "num_predict": 220},
                    }, ensure_ascii=False).encode("utf-8")
                    req = _req.Request("http://127.0.0.1:11434/api/chat", data=payload,
                                       headers={"Content-Type": "application/json; charset=utf-8"})
                    with _req.urlopen(req, timeout=120) as resp:
                        toy_data = json.loads(resp.read().decode("utf-8"))
                    toy_reply = str((toy_data.get("message") or {}).get("content") or "").strip()
                except Exception as exc:  # noqa: BLE001
                    toy_reply = f"（小玩具没答上来：{type(exc).__name__}）"
                if toy_reply:
                    # 2026-11-05 自查：这里原先是 life/manager 都没定义的死代码（一直被 except 吞掉）。
                    # 跟小玩具的来往本来就写进 data/toy_chat.jsonl、桌面上也看得到，就别留假代码。
                    # 2026-11-04：桌面上的"逗小宠物"要能看到她跟小玩具的来往
                    try:
                        with (_BRIDGE_DIR / "data" / "toy_chat.jsonl").open("a", encoding="utf-8") as _fh:
                            _fh.write(json.dumps({
                                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                                "her": toy_prompts[0][:600],
                                "toy": toy_reply[:600],
                            }, ensure_ascii=False) + "\n")
                    except Exception:  # noqa: BLE001
                        pass
                    # 2026-11-04 爸爸："当场对上话，不应该还要转一手。"
                    # 小玩具是本地的、免费；这里只多花她一次模型调用，换来她这一轮就接上它的话，
                    # 而不是等到下一轮（以前那样看着像"中间隔着一道手"）。
                    try:
                        note = (
                            f"[系统：你刚跟小玩具说「{toy_prompts[0][:80]}」，它回你：「{toy_reply[:200]}」。"
                            "就着它这句接一句；别说「下一轮告诉你」，也别再写 [TOY]。] "
                        )
                        follow = executor.run(
                            lambda manager: manager.get(user_id).chat(note, user=sender or "对方"),
                            timeout=None,
                        )
                        follow_text = str(follow.get("reply") or "").strip()
                        if follow_text:
                            fresh = [b.strip() for b in split_bubbles(follow_text, max_bubble) if b.strip()]
                            if fresh:
                                bubbles = fresh
                    except Exception:  # noqa: BLE001
                        log.debug("小玩具当场回话失败", exc_info=True)
            # 她自己写 [VOICE: ...] 的那几句是"开口说"的。IM发不出语音，
            # 所以那边折回文字（内容不能丢），网页端留着当语音气泡。
            voice_parts = [str(x).strip() for x in (result.get("voice_parts") or []) if str(x).strip()]
            sing_parts = [
                item for item in (result.get("sing_parts") or [])
                if isinstance(item, dict) and str(item.get("text") or "").strip()
            ]
            bubbles, voice_parts, sing_parts = shape_reply(
                bubbles, voice_parts, sing_parts, str(channel or "")
            )
            # 她想给IM 里的人发表情包（[IM_STICKER: 名字 | 关键词]）
            chat_stickers = [
                item for item in (result.get("chat_stickers") or [])
                if isinstance(item, dict) and str(item.get("to") or "").strip()
            ]
            if chat_stickers:
                target = chat_stickers[0]
                try:
                    from chat_send import pick_sticker, send_image  # noqa: PLC0415

                    sticker_path = pick_sticker(str(target.get("keyword") or ""))
                    # 2026-11-08：她会把收件人写成"对方"（占位名），send_image 找不到人就报错，
                    # 她还以为路断了。这里把占位名换成本轮真正的说话人。
                    _to = str(target.get("to") or "").strip()
                    # 2026-11-08：群里发表情必须发给群——她写"爸爸"时，
                    # 以前会私发给他（人在群里说话、表情却私聊发出去）。
                    if str(channel or "").startswith("chat_group") and group_name:
                        _to = group_name
                    elif _to in ("", "对方", "他", "她", "你", "对方说"):
                        _to = str(sender or "").strip()
                    if not _to:
                        sent = {"ok": False, "error": "不知道该发给谁（没认出对面是谁）"}
                    elif sticker_path is None:
                        sent = {"ok": False, "error": "表情库里没找到这张"}
                    else:
                        sent = send_image(_to, sticker_path)
                except Exception as exc:  # noqa: BLE001
                    sent = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                if sent.get("ok"):
                    bubbles = (bubbles + [f"表情包已经发给{sent.get('to') or target.get('to')}了"])[:6]
                    _record_skill("发表情包", True)
                else:
                    bubbles = (bubbles + [f"表情包没发出去（{sent.get('error')}）"])[:6]
                    _record_skill("发表情包", False, str(sent.get("error") or ""))
            # 她自己写 [VOICE: ...] 的那几句是"开口说"的。IM发不出语音，
            # 所以那边折回文字（内容不能丢），网页端留着当语音气泡。
            voice_parts = [str(x).strip() for x in (result.get("voice_parts") or []) if str(x).strip()]
            sing_parts = [
                item for item in (result.get("sing_parts") or [])
                if isinstance(item, dict) and str(item.get("text") or "").strip()
            ]
            if str(channel or "").startswith("chat"):
                if voice_parts:
                    bubbles = (bubbles + voice_parts)[:6]
                    voice_parts = []
                if sing_parts:
                    bubbles = (bubbles + [str(item["text"]) for item in sing_parts])[:6]
                    sing_parts = []
            elif voice_parts or sing_parts:
                # 2026-11-03 爸爸："不要既写文字又发语音。"
                # 人一次只用一种方式：要么打字，要么开口，少数时候才两样都来。
                if random.random() < 0.8:
                    bubbles = []

            sticker_payload = None
            if friend_pending:
                kind = friend_pending[2] if len(friend_pending) > 2 else "apology"
                send_text = ""
                if kind == "scold":
                    send_text = "坏人 不理你了[戳手手]"
                elif kind == "open":
                    joined = " ".join(bubbles)
                    quoted = re.search(r"[「“\"']([^」”\"']{2,30})[」”\"']", joined)
                    send_text = quoted.group(1) if quoted else ""
                    # 开场白里出现"爸爸"或第三人称，说明她把对爸爸说的话发过去了
                    if re.search(r"爸爸|你爸|我爸", send_text):
                        send_text = ""
                else:
                    send_text = "刚才在忙 没顾上回你 别介意呀🐟"
                if not send_text:
                    # 没写出要发的那句，就不发；不许拿一句现成的顶上去。
                    bubbles = (bubbles + ["没写清楚要发什么，这次就没发"])[:4]
                else:
                    ok, err = run_douyin_text(friend_pending[0], send_text)
                    if not ok:
                        bubbles = (bubbles + [f"抖音那边没发出去：{err}"])[:4]
                    else:
                        bubbles = (bubbles + [f"已经发给{friend_pending[1]}了：{send_text}"])[:4]
            if douyin_text_intent:
                # 一句一条发过去（模拟真人按 Enter），不要挤成一条多行
                failed = ""
                for part in bubbles:
                    part = (part or "").strip()
                    if not part:
                        continue
                    ok, err = run_douyin_text(user_id, part)
                    if not ok:
                        failed = err
                        break
                    time.sleep(0.6)
                if failed:
                    bubbles = (bubbles + [f"抖音那边没发出去：{failed}"])[:4]
            if wants_sticker and "抖音" not in message and stickers is not None:
                try:
                    # 2026-11-12：她要发表情时，按她此刻心情 + 偏好的标签挑
                    import sticker_mood as _sticker_mood  # noqa: PLC0415

                    picked = _sticker_mood.choose(stickers, mood, is_owner)
                    if picked is not None:
                        sticker_payload = {"id": picked.id, "url": f"/api/sticker?id={picked.id}", "tag": (picked.tags or [""])[0]}
                except Exception:  # noqa: BLE001
                    log.debug("挑表情失败", exc_info=True)
            elif stickers is not None:
                # 2026-11-12：不再掷骰子——心情浓不浓、有没有偏爱的图、隔了多久，
                # 一起决定"她这会儿想不想甩一张"，挑哪张也按偏好来。
                try:
                    import sticker_mood as _sticker_mood  # noqa: PLC0415

                    if _sticker_mood.should_send(mood, is_owner):
                        picked = _sticker_mood.choose(stickers, mood, is_owner)
                        if picked is not None:
                            sticker_payload = {"id": picked.id, "url": f"/api/sticker?id={picked.id}",
                                               "tag": (picked.tags or [""])[0]}
                except Exception:  # noqa: BLE001
                    log.debug("顺手带表情失败", exc_info=True)

            # IM上按真人节奏来：她"看到→想一下→打字"这段时间由服务端先等，
            # 客户端随后还有一段打字模拟。网页/抖音不受影响。
            reply_text_for_pace = "\n".join(bubbles)
            delay_ms = pick_delay()
            if str(channel or "").startswith("chat"):
                delay_ms = human_reply_delay_ms(reply_text_for_pace)
                if delay_ms:
                    time.sleep(min(delay_ms, 15_000) / 1000)

            # 2026-11-16：公网网页读不到本机 drawn 目录，画会碎成文字；这里内嵌图。
            attach_drawing_data(result)
            payload = {
                "ok": True,
                "user_id": user_id,
                "reply": "\n".join(bubbles),
                "replies": bubbles,
                "voice_parts": voice_parts,
                "sing_parts": sing_parts,
                "delay_ms": delay_ms,
                "ignored": False,
                "sticker": sticker_payload,
                # 2026-11-14：她把画好的图发出来时，网页要能显示（以前这里没带，图就丢了）
                "drawing": result.get("drawing"),
                "doodle": result.get("doodle"),
                "emotion": result.get("emotion"),
                "intimacy": result.get("intimacy"),
                "relationship": result.get("relationship"),
                "memory_action": result.get("memory_action"),
                "safety_blocked": result.get("safety_blocked"),
                "solver_messages": result.get("solver_messages") or [],
                "solver_discussions": result.get("solver_discussions") or [],
                "solver_silent": bool(result.get("solver_silent")),
                "her_open": str(result.get("her_open") or ""),
                "sleep_hours": result.get("sleep_hours"),
                "wake_now": bool(result.get("wake_now")),
                "pc_requests": result.get("pc_requests") or [],
                "source": result.get("source"),
                "error": result.get("error"),
                "elapsed": round(time.time() - started, 2),
            }
            log.info(
                "chat user=%s %.1fs src=%s err=%s | %s -> %s",
                user_id,                payload["elapsed"],
                payload["source"],
                payload["error"],
                message[:40].replace("\n", " "),
                str(payload["reply"])[:60].replace("\n", " "),
            )
            self._send(200, payload)

        def _handle_openai_chat(self, body: dict[str, Any]) -> None:
            """OpenAI 兼容接口：让抖音插件之类的客户端直接把她当模型用。"""
            messages = body.get("messages")
            if not isinstance(messages, list):
                self._send(400, {"error": {"message": "messages 必须是数组", "type": "invalid_request_error"}})
                return

            image_data = ""
            image_url = ""
            turns: list[dict[str, str]] = []
            for item in messages:
                if not isinstance(item, dict):
                    continue
                role = item.get("role")
                content = item.get("content")
                parts: list[str] = []
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        if part.get("type") == "text":
                            parts.append(str(part.get("text") or ""))
                        elif part.get("type") == "image_url":
                            url = (part.get("image_url") or {}).get("url") or ""
                            if isinstance(url, str) and url.startswith("data:image"):
                                image_data = url
                            elif isinstance(url, str) and url.startswith(("http://", "https://")):
                                image_url = url
                joined = "\n".join(part.strip() for part in parts if part and part.strip())
                if role in ("user", "assistant") and joined:
                    if role != "assistant":
                        joined = clean_incoming_text(joined)
                    turns.append({"role": "assistant" if role == "assistant" else "user", "content": joined})

            current = next((turn for turn in reversed(turns) if turn["role"] == "user"), None)
            text = current["content"] if current else ""
            history = list(turns)
            if current is not None and history and history[-1] is current:
                history = history[:-1]
            history = [turn for turn in history if turn["content"]][-HISTORY_PROMPT_TURNS:]
            image_path = ""
            if image_data or image_url:
                if image_data:
                    header, _, encoded = image_data.partition(",")
                    suffix = ".png" if "png" in header.lower() else ".jpg"
                    try:
                        raw = base64.b64decode(encoded)
                    except Exception:  # noqa: BLE001
                        raw = b""
                else:
                    raw = _fetch_image(image_url)
                    suffix = ".png" if image_url.lower().endswith(".png") else ".jpg"
                if raw:
                    path = media_root / f"{uuid.uuid4().hex[:16]}{suffix}"
                    path.write_bytes(raw)
                    image_path = str(path)

            if not text and not image_path:
                self._send(400, {"error": {"message": "没有 user 文本或图片", "type": "invalid_request_error"}})
                return
            image_data_url = ""
            if image_path:
                try:
                    from PIL import Image as _PILImage  # noqa: PLC0415
                    _img = _PILImage.open(image_path)
                    _img.thumbnail((1280, 1280))
                    _buf = io.BytesIO()
                    _img.convert("RGB").save(_buf, format="JPEG", quality=85)
                    image_data_url = "data:image/jpeg;base64," + base64.b64encode(_buf.getvalue()).decode("ascii")
                except Exception:  # noqa: BLE001
                    log.debug("图片转 data URL 失败，退回纯文本", exc_info=True)
                if not text:
                    text = "[图片]"

            # 表情包：不认识就现场看一眼，顺便收进她自己的表情库
            saved_sticker = None
            if image_path and stickers is not None and ("表情" in text or "emoji" in text.lower()):
                try:
                    desc = enricher.router.vision.describe(image_path) if enricher.router.vision.enabled else ""
                    tags = _tags_from_description(desc)
                    saved_sticker = stickers.add_file(image_path, tags, source="douyin-emoji", note=desc)
                    if saved_sticker is not None:
                        log.info("收录表情 %s：%s（%s）", saved_sticker.id, desc[:40], "/".join(tags))
                except Exception:  # noqa: BLE001
                    log.debug("表情收录失败", exc_info=True)

            # 2026-11-03：这个口子现在也接"IM那个 agent"（DSH 的 pi-ai 路由）。
            # 它不带 user 字段，模型名是 xiaoyu —— 那就统一落到主人这一份记忆上，
            # IM 里说话的才是同一个她，而不是另一个 agent。
            model_name = str(body.get("model") or "xiaoyu").strip()
            low_model = model_name.lower()
            owner_id, owner_name = load_owner()
            user_id = str(body.get("user") or "").strip()
            if not user_id:
                user_id = (owner_id or "douyin") if low_model.startswith("xiaoyu") else "douyin"
            channel_hint = str(body.get("channel") or "").strip()
            if not channel_hint and low_model.startswith("xiaoyu"):
                channel_hint = "chat_private"
            set_current_contact_id(user_id)
            sender = str(body.get("sender") or "").strip()
            if not sender:
                sender = contact_names().get(user_id, "") or ("爸爸" if low_model.startswith("xiaoyu") else "对方")
            is_owner = bool(owner_id) and user_id == owner_id and sender in owner_sender_names()
            mood = load_mood(Path(db_path) if db_path else None, user_id)
            phrases = record_catchphrases(Path(db_path) if db_path else None, user_id, text)
            started = time.time()

            def job(manager: LifeManager) -> dict[str, Any]:
                life = manager.get(user_id)
                # 2026-11-08 爸爸："她理解有点问题，我解释了一下什么是司凤音乐，她莫名其妙接了一句。"
                # 真因：life.history 是**所有会话共用一条**。私聊里 Zero° 让她画苹果、
                # 群里在聊司凤音乐，两条线混在同一份 history 里，她就拿画图去回答群聊。
                # 现在按"渠道 + 群名/对方"分开存，每次只把当前这条会话的历史换进去。
                _chat_key = f"{channel_hint}|{str(body.get('group') or '').strip() or sender}"
                _histories = getattr(manager, "_chat_histories", None)
                if _histories is None:
                    _histories = {}
                    manager._chat_histories = _histories
                # 2026-11-14 修：插件没带 history 时也不该把已存上下文清空，
                # 否则首轮请求或插件重启后 60 轮历史被一轮覆盖。
                life.history = [dict(turn) for turn in (_histories.get(_chat_key) or [])]
                if history:
                    # 插件每次只带最近 8 条；不要覆盖掉长期实例里的上下文，合并去重。
                    # 2026-11-08 爸爸："上下文怎么断了。"
                    # 老写法 prefix = 存档里"不在插件这 8 条里"的 → 因为插件发的就是存档那 8 条，
                    # prefix 永远是空的，存档也就永远只有 8 条，隔一阵就接不上。
                    # 改成按"第一个重叠点"拼接：找到 incoming[0] 在存档里最后出现的位置，
                    # 前面的留着，后面接上插件的新窗口。
                    incoming = [dict(turn) for turn in history]
                    stored = [dict(turn) for turn in life.history]
                    head = incoming[0]
                    cut = None
                    for index in range(len(stored) - 1, -1, -1):
                        if (str(stored[index].get("role")) == str(head.get("role"))
                                and str(stored[index].get("content")) == str(head.get("content"))):
                            cut = index
                            break
                    if cut is not None:
                        merged = stored[:cut] + incoming
                    else:
                        known = {(str(turn.get("role")), str(turn.get("content"))) for turn in stored}
                        merged = stored + [turn for turn in incoming
                                           if (str(turn.get("role")), str(turn.get("content"))) not in known]
                    life.history = merged[-60:]
                    life.history_times = [life.now()] * len(life.history)
                    _histories[_chat_key] = list(life.history)
                life.persona = persona_with_time(
                    manager.persona or DEFAULT_PERSONA,
                    digest=digest_for(life, text, digest_limit),
                    style=load_style_profile(style_path),
                    mood=mood,
                    is_owner=is_owner,
                    catchphrases=phrases,
                    douyin_context=douyin_contacts_block(text),
                    channel=channel_hint,
                    speaker=sender,
                )
                try:
                    life.sleepiness = max(0.0, min(1.0, melatonin_tick() / MELATONIN_MAX))
                except Exception:  # noqa: BLE001
                    life.sleepiness = 0.0
                try:
                    _hormones().tick()
                    life.knobs = _hormones().knobs(life.sleepiness)
                except Exception:  # noqa: BLE001
                    life.knobs = None
                if gomoku_block():
                    life.persona = (life.persona or "") + gomoku_block()
                result = life.chat(text, user=sender, image_data_url=image_data_url or None)
                # 这一轮她自己说的话也要留在**这条会话**的历史里（按会话分开存）
                try:
                    _histories[_chat_key] = list(life.history)[-60:]
                except Exception:  # noqa: BLE001
                    pass
                try:
                    _feed_hormones(result, text, sender or "")
                except Exception:  # noqa: BLE001
                    log.debug("激素事件写入失败", exc_info=True)
                return result

            try:
                result = executor.run(job, timeout=None)
            except Exception as exc:  # noqa: BLE001
                log.exception("openai chat 失败 user_id=%s", user_id)
                self._send(500, {"error": {"message": f"{type(exc).__name__}: {exc}", "type": "server_error"}})
                return

            raw_reply = str(result.get("reply") or "")
            thought = str(result.get("thought") or "").strip()
            if thought:
                try:
                    with (_BRIDGE_DIR / "data" / "thoughts.jsonl").open("a", encoding="utf-8") as _fh:
                        _fh.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "user_id": user_id, "thought": thought[:500]}, ensure_ascii=False) + "\n")
                except Exception:  # noqa: BLE001
                    pass
            record_solver_channel(result, sender)
            record_pc_requests(result)
            record_quiet_request(result)
            if str(result.get("her_open") or "").strip():
                record_her_open(str(result.get("her_open") or ""))
            record_her_tictac(result)
            maybe_move_from_words(str(result.get("reply") or ""), result)
            maybe_reset_from_words(str(result.get("reply") or ""), result)
            # 2026-11-04：她落子时常常只写标签、一个字不说，网页那头就是空白。
            if not str(result.get("reply") or "").strip():
                _move = result.get("tictac_move")
                if isinstance(_move, int):
                    result["reply"] = f"我下{_move}"
                    result["replies"] = [f"我下{_move}"]
            _douyin_action = str(result.get("douyin_action") or "") or ("on" if result.get("open_douyin") else "")
            if _douyin_action:
                _douyin_note = apply_douyin_action(_douyin_action)
                if _douyin_note:
                    log.info("抖音开关结果：%s", _douyin_note)
            # 2026-11-04 爸爸："给她一个权限，可以刷几分钟抖音或者搜视频看，不需要我同意。"
            _douyin_watch = int(result.get("douyin_watch") or 0)
            _douyin_search = str(result.get("douyin_search") or "").strip()
            if _douyin_watch or _douyin_search:
                apply_douyin_action("on")
                # 她人现在在抖音：IM那边先不接，等这一小段过去再回头看
                set_attention("douyin", minutes=max(1, _douyin_watch or 5))
                if _douyin_watch:
                    _info = record_douyin_watch(_douyin_watch)
                    douyin_browse("open")
                    log.info("抖音：她自己要刷 %s 分钟（到 %s）", _douyin_watch, _info.get("until"))
                if _douyin_search:
                    _res = douyin_browse("search", _douyin_search)
                    try:
                        (_BRIDGE_DIR / "data" / "douyin_search_last.json").write_text(
                            json.dumps({"query": _douyin_search, "items": _res.get("items") or [],
                                        "at": datetime.now().astimezone().isoformat(timespec="seconds")},
                                       ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                    except OSError:
                        log.debug("写抖音搜索结果失败", exc_info=True)
                    log.info("抖音搜索「%s」：%s 条（%s）", _douyin_search,
                             len(_res.get("items") or []), _res.get("error") or "ok")
            if result.get("wake_now"):
                set_sleep_state(wake=True)
            elif result.get("sleep_hours"):
                set_sleep_state(hours=float(result["sleep_hours"]))
            proactive_after = int(result.get("proactive_after") or 0)
            if result.get("resume_poll"):
                set_chat_poll_pause(resume=True)
            elif result.get("pause_poll_minutes"):
                set_chat_poll_pause(minutes=int(result["pause_poll_minutes"]))
            if proactive_after > 0:
                raw_reply = raw_reply.rstrip() + f" [[LATER:{proactive_after}]]"
            reply_text = raw_reply if keep_stage else strip_stage_directions(raw_reply)
            bubbles = split_bubbles(reply_text, max_bubble)
            # 用户让转发视频时，模型光顾着贫嘴没给链接——这里兜底把链接补上
            video_url = re.search(r"https://www\.douyin\.com/video/\d+", text)
            wants_share = any(word in text for word in ("转发", "转给", "转我", "分享给", "发我", "发过来"))
            if video_url and wants_share and video_url.group(0) not in reply_text:
                bubbles = (bubbles + [video_url.group(0)])[:4]
            # 抖音那边按 ||| 拆成多条气泡发；网页端仍按 replies 分条
            content = " ||| ".join(bubbles)

            log.info("openai user=%s %.1fs | %s -> %s", user_id, time.time() - started,
                     text[:40].replace("\n", " "), content[:60].replace("\n", " "))
            # 客户端会一直等响应，所以这里睡多久，消息就晚多久出现
            delay_ms = pick_delay()
            if delay_ms:
                time.sleep(min(delay_ms, 25_000) / 1000)
            reply_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
            # 2026-11-03：DSH 的 pi-ai 路由要的是流式（SSE）。她回完就一次性推完，
            # 不假装逐字生成——但格式必须是标准 chat.completion.chunk。
            if bool(body.get("stream")):
                self._send_openai_stream(reply_id, model_name, content, bubbles)
                return
            self._send(200, {
                "id": reply_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": str(body.get("model") or "xiaoyu"),
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }],
                "usage": {
                    "prompt_tokens": int((result.get("tokens") or {}).get("prompt_tokens") or 0),
                    "completion_tokens": int((result.get("tokens") or {}).get("completion_tokens") or 0),
                    "total_tokens": sum(int((result.get("tokens") or {}).get(k) or 0)
                                        for k in ("prompt_tokens", "completion_tokens")),
                    "prompt_cache_hit_tokens": int((result.get("tokens") or {}).get("cached_tokens") or 0),
                },
                "replies": bubbles,
                "sticker_saved": (
                    {"id": saved_sticker.id, "tags": saved_sticker.tags}
                    if saved_sticker is not None else None
                ),
            })

        def _handle_compress(self, body: dict[str, Any]) -> None:
            """闲置会话压缩：用本地模型写一条记忆进图书馆，0 API token。"""
            user_id = str(body.get("user_id") or "").strip() or "douyin"
            raw = body.get("messages")
            if not isinstance(raw, list) or not raw:
                self._send(400, {"ok": False, "error": "messages 不能为空"})
                return
            lines: list[str] = []
            for item in raw[-24:]:
                if not isinstance(item, dict):
                    continue
                who = str(item.get("sender") or "对方").strip()[:20]
                say = str(item.get("text") or "").strip().replace("\n", " ")[:400]
                if say:
                    lines.append(f"{who}：{say}")
            if not lines:
                self._send(400, {"ok": False, "error": "没有可压缩的文本"})
                return
            transcript = "\n".join(lines)[:4000]
            prompt = (
                "把下面这段抖音私信对话压缩成一条记忆，只输出一句话，60字以内："
                "写清楚谁说了什么、情绪和还没说完的事，不要编造。\n\n" + transcript
            )
            summary = ""
            try:
                from digital_life import ollama_generate  # noqa: PLC0415
                summary = ollama_generate(
                    prompt, model=OLLAMA_MODEL, timeout=min(180.0, float(OLLAMA_TIMEOUT))
                ).strip()
            except Exception as exc:  # noqa: BLE001
                log.info("压缩记忆：本地模型不可用（%s），用截断兜底", exc)
            summary = (summary or transcript.replace("\n", " "))[:120].strip()

            def job(manager: LifeManager) -> dict[str, Any]:
                life = manager.get(user_id)
                book = life.library.add_book(
                    content=transcript,
                    summary=summary,
                    emotion_tag="日常",
                    folder_date=datetime.now().strftime("%Y-%m-%d"),
                    folder_user=user_id,
                    folder_event="私信",
                    mv=120.0,
                    now=datetime.now(),
                    unresolved=False,
                    origin="douyin_dm",
                )
                return {"id": book.get("id") if isinstance(book, dict) else None}

            try:
                result = executor.run(job, timeout=30)
            except Exception as exc:  # noqa: BLE001
                log.exception("压缩记忆入库失败")
                self._send(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
                return
            log.info("压缩记忆 user=%s | %s", user_id, summary[:60])
            self._send(200, {"ok": True, "summary": summary, "book_id": result.get("id")})

        def _handle_describe(self, body: dict[str, Any]) -> None:
            """给IM 里的表情包打标签：描述 + 情绪/用途关键词。

            IM插件原本靠 DSH 模型自己上报 STICKER_LABELS；换成小鱼之后没人上报，
            标签缓存一直是空的，所以永远没有候选、也发不出表情。这里用本机视觉模型补上，
            和之前 DSH 用的是同一个 qwen2.5vl:3b。
            """
            path = str(body.get("image_path") or "").strip()
            if not path or not Path(path).exists():
                self._send(400, {"ok": False, "error": "image_path 不存在"})
                return
            description = ""
            try:
                if enricher.router.vision.enabled:
                    if str(body.get("task") or "").strip() == "sticker":
                        # 表情包：画面 + 图上文字，两次最简提问（见 sticker_label）。
                        label = sticker_label(enricher.router.vision, path)
                        label["ok"] = True
                        self._send(200, label)
                        return
                    description = str(enricher.router.vision.describe(path) or "").strip()[:120]
            except Exception as exc:  # noqa: BLE001
                self._send(200, {"ok": False, "error": str(exc)[:120], "description": "", "tags": []})
                return
            self._send(200, {"ok": True, "description": description, "tags": _sticker_tags(description)})

        def _handle_memory_note(self, body: dict[str, Any]) -> None:
            """任务结果写进图书馆：让做过的事变成她记得的事。"""
            user_id = str(body.get("user_id") or "").strip() or "douyin"
            text = str(body.get("text") or "").strip()[:2000]
            summary = str(body.get("summary") or "").strip()[:120] or text[:60]
            if not text:
                self._send(400, {"ok": False, "error": "text 不能为空"})
                return

            def job(manager: LifeManager) -> dict[str, Any]:
                life = manager.get(user_id)
                book = life.library.add_book(
                    content=text,
                    summary=summary,
                    emotion_tag="日常",
                    folder_date=datetime.now().strftime("%Y-%m-%d"),
                    folder_user=user_id,
                    folder_event="任务",
                    mv=130.0,
                    now=datetime.now(),
                    unresolved=False,
                    origin="douyin_task",
                )
                return {"id": book.get("id") if isinstance(book, dict) else None}

            try:
                result = executor.run(job, timeout=30)
            except Exception as exc:  # noqa: BLE001
                log.exception("任务记忆入库失败")
                self._send(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
                return
            log.info("任务记忆 user=%s | %s", user_id, summary[:60])
            self._send(200, {"ok": True, "book_id": result.get("id")})

        def _handle_decide(self, body: dict[str, Any]) -> None:
            """她决定这条消息回不回。当前设置：醒着都回，睡着不回。"""
            if is_sleeping():
                self._send(200, {"reply": False, "reason": "睡着了", "source": "sleep"})
                return
            # 主人要求"没回的都回"：暂时关闭按心情跳过的逻辑
            self._send(200, {"reply": True, "reason": "全部回复", "source": "force-all"})
            return
            message = str(body.get("message") or "").strip()
            has_media = bool(body.get("has_media"))
            source = "heuristic"
            decision = None
            if not message and not has_media:
                self._send(200, {"reply": True, "reason": "空消息，先看着", "source": source})
                return
            if message:
                try:
                    prompt = (
                        "你是小鱼，一个有点傲娇的数字生命。对方刚发来：\n"
                        f"{message[:400]}\n"
                        "你现在想不想回？回不回都行，看心情，但别连续好几次都不理人。"
                        "只输出 yes 或 no，不要解释。"
                    )
                    answer = chat_with_llm(
                        [{"role": "user", "content": prompt}], temperature=0.7, timeout=4.0
                    )
                    lowered = str(answer or "").lower()
                    if "yes" in lowered or "回" in lowered and "不回" not in lowered:
                        decision, source = True, "model"
                    elif "no" in lowered:
                        decision, source = False, "model"
                except Exception:  # noqa: BLE001
                    log.debug("回复决策模型不可用", exc_info=True)
            if decision is None:
                text = message.strip()
                if has_media:
                    decision = True
                elif len(text) <= 1:
                    decision = random.random() < 0.4
                elif re.fullmatch(r"[嗯哦噢啊哈额6。，…\s]+", text):
                    decision = random.random() < 0.45
                else:
                    decision = True
            self._send(200, {"reply": bool(decision), "reason": "想不想回", "source": source})

        def _handle_share(self, body: dict[str, Any]) -> None:
            """转发视频时，配一句带记忆的话，而不是干巴巴甩个卡片。"""
            title = str(body.get("title") or "").strip()[:120]
            author = str(body.get("author") or "").strip()[:60]
            user_id = str(body.get("user_id") or "douyin").strip() or "douyin"
            if not title:
                self._send(400, {"ok": False, "error": "title 不能为空"})
                return

            def job(manager: LifeManager) -> str:
                life = manager.get(user_id)
                persona = persona_with_time(
                    manager.persona or DEFAULT_PERSONA,
                    digest=digest_for(life, f"{title} {author}", digest_limit),
                )
                prompt = (
                    f"你在抖音刷到一个视频《{title}》"
                    f"（作者：{author or '未知'}），想转给正在和你聊天的人。\n"
                    "结合你记得的、你们之间发生过的事，写一句不超过 25 个字的话，"
                    "像随手发的那种；不要解释、不要引号、不要加链接。"
                )
                return chat_with_llm(
                    [
                        {"role": "system", "content": persona},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.9,
                    timeout=60.0,
                )

            try:
                text = executor.run(job, timeout=None)
            except Exception as exc:  # noqa: BLE001
                log.exception("share 文案生成失败 user_id=%s", user_id)
                self._send(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
                return

            text = re.sub(r"^[\"'“”「『]+|[\"'“”」』]+$", "", str(text or "")).strip()[:80]
            log.info("share user=%s | 《%s》-> %s", user_id, title[:30], text)
            self._send(200, {"ok": True, "text": text, "delay_ms": pick_delay()})

        def _handle_emoji_learn(self, body: dict[str, Any]) -> None:
            """认识一张表情：描述它、打标签、收进表情库。"""
            if stickers is None:
                self._send(501, {"ok": False, "error": "表情库未启用"})
                return
            path = str(body.get("image_path") or "").strip()
            if not path and body.get("image_base64"):
                encoded = str(body.get("image_base64"))
                try:
                    raw = base64.b64decode(encoded.partition(",")[2] or encoded)
                except Exception:  # noqa: BLE001
                    raw = b""
                if raw:
                    target = media_root / f"emoji_{uuid.uuid4().hex[:12]}.png"
                    target.write_bytes(raw)
                    path = str(target)
            if not path and body.get("image_url"):
                raw = _fetch_image(str(body.get("image_url")))
                if raw:
                    target = media_root / f"emoji_{uuid.uuid4().hex[:12]}.jpg"
                    target.write_bytes(raw)
                    path = str(target)
            if not path or not Path(path).exists():
                self._send(400, {"ok": False, "error": "没有可用的图片（image_path / image_base64 / image_url）"})
                return

            desc = ""
            try:
                if enricher.router.vision.enabled:
                    desc = enricher.router.vision.describe(path)
            except Exception:  # noqa: BLE001
                log.debug("表情描述失败", exc_info=True)
            tags = body.get("tags") or _tags_from_description(desc)
            tags = [str(tag).strip() for tag in tags if str(tag).strip()][:6] or ["未分类"]
            sticker = stickers.add_file(path, tags, source="emoji-learn", note=desc)
            if sticker is None:
                self._send(400, {"ok": False, "error": "表情入库失败（格式或体积不支持）"})
                return
            log.info("学习表情 %s：%s（%s）", sticker.id, desc[:40], "/".join(tags))
            self._send(200, {
                "ok": True,
                "id": sticker.id,
                "tags": sticker.tags,
                "description": desc,
                "total": stickers.count(),
            })

        def _handle_proactive(self, body: dict[str, Any]) -> None:
            user_id = str(body.get("user_id") or "default").strip() or "default"
            started = time.time()
            message = str(body.get("message") or "")

            if proactive_throttled():
                log.info("proactive 被节流（10 分钟内已说过一次）")
                self._send(200, {"ok": True, "user_id": user_id, "reply": None,
                                 "replies": [], "sent": False, "throttled": True,
                                 "elapsed": round(time.time() - started, 2)})
                return

            def job(manager: LifeManager) -> str | None:
                life = manager.get(user_id)
                life.persona = persona_with_time(
                    manager.persona or DEFAULT_PERSONA,
                    digest=digest_for(life, message, digest_limit),
                )
                # 2026-11-16：主动开口前，先给她一个"刚想起的事"当由头，
                # 不然她只会说"在干嘛"。素材见 tools/proactive_seeds.py。
                try:
                    sys.path.insert(0, str(_BRIDGE_DIR / "tools"))
                    import proactive_seeds  # noqa: PLC0415

                    _act = ""
                    try:
                        _act = str(json.loads(_heartbeat_file().read_text(encoding="utf-8")).get("activity") or "")
                    except Exception:  # noqa: BLE001
                        _act = ""
                    _seed = proactive_seeds.pick(_act)
                    if _seed:
                        life.persona = (life.persona or "") + (
                            f"\n【你刚想起】{_seed['seed']}（用你自己的话说，别照抄，一句就够）"
                        )
                except Exception:  # noqa: BLE001
                    log.debug("挑主动开口素材失败", exc_info=True)
                return life.proactive_chat()

            try:
                text = executor.run(job, timeout=None)
            except Exception as exc:  # noqa: BLE001
                log.exception("proactive 失败 user_id=%s", user_id)
                self._send(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
                return

            log.info("proactive user=%s -> %s", user_id, (text or "(无)")[:80])
            raw_text = text or ""
            tag = re.match(r"^\s*\[(NEWS|DREAM|DIARY|OBSESSION|PROACTIVE)\]", raw_text)
            clean = re.sub(r"^\s*\[(?:NEWS|DREAM|DIARY|OBSESSION|PROACTIVE)\]\s*", "", raw_text).strip()
            if clean.startswith("我昨晚梦见"):
                clean = clean[len("我昨晚梦见"):].strip()
            # 梦和日记按 20 字一条；新闻标题更长，放宽到 40，别把词切碎
            # 2026-11-10：梦/执念/日记属于她的内心空间，归信道，不进聊天框
            if tag and tag.group(1) in ("DREAM", "DIARY") and clean:
                # 2026-11-11 爸爸："分开"——梦和日记是她的私房本子，进 /diary，不进信道
                try:
                    conn = sqlite3.connect(str(_server_db()))
                    try:
                        conn.execute(
                            "INSERT INTO diaries(content, created_at, source) VALUES (?,?,?)",
                            (clean[:2000],
                             datetime.now().astimezone().isoformat(timespec="seconds"),
                             "dream" if tag.group(1) == "DREAM" else "diary"),
                        )
                        conn.commit()
                    finally:
                        conn.close()
                    log.info("proactive: %s 已写进日记", tag.group(1))
                except Exception:  # noqa: BLE001
                    log.debug("写日记失败", exc_info=True)
                self._send(200, {"ok": True, "user_id": user_id, "reply": None,
                                 "replies": [], "sent": False, "delay_ms": 0,
                                 "routed": "diary",
                                 "elapsed": round(time.time() - started, 2)})
                return
            if tag and tag.group(1) == "OBSESSION" and clean:
                # 执念是"她想跟你说话"，仍然进信道
                try:
                    record = {
                        "type": "message",
                        "from": "xiaoyu",
                        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "text": clean[:2000],
                        "visibility": "private",
                    }
                    _solver_inbox().parent.mkdir(parents=True, exist_ok=True)
                    with _solver_inbox().open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                    log.info("proactive: OBSESSION 已写进信道")
                except Exception:  # noqa: BLE001
                    log.debug("写信道失败", exc_info=True)
                self._send(200, {"ok": True, "user_id": user_id, "reply": None,
                                 "replies": [], "sent": False, "delay_ms": 0,
                                 "routed": "channel",
                                 "elapsed": round(time.time() - started, 2)})
                return
            limit = max(max_bubble, 40) if tag and tag.group(1) == "NEWS" else max_bubble
            bubbles = split_bubbles(clean, limit) if clean else []
            self._send(
                200,
                {
                    "ok": True,
                    "user_id": user_id,
                    "reply": "\n".join(bubbles) if bubbles else None,
                    "replies": bubbles,
                    "delay_ms": pick_delay() if bubbles else 0,
                    "elapsed": round(time.time() - started, 2),
                },
            )

        # ---------------------------------------------------------- 工具

        def _read_json(self) -> dict[str, Any] | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > _MAX_BODY:
                self._send(413, {"ok": False, "error": "请求体过大"})
                return None
            raw = self.rfile.read(length) if length else b""
            if not raw.strip():
                return {}
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send(400, {"ok": False, "error": "请求体必须是 UTF-8 JSON"})
                return None
            if not isinstance(data, dict):
                self._send(400, {"ok": False, "error": "请求体必须是 JSON 对象"})
                return None
            return data

        def _send_openai_stream(self, reply_id: str, model: str, content: str,
                                bubbles: list[str] | None = None) -> None:
            """OpenAI 兼容的流式输出（SSE）。她的话已经想好了，这里只按标准格式推。

            DSH 的 pi-ai 路由（IM那个 agent）只吃流式，非流式会一直等。
            """
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()

                def frame(delta: dict[str, Any], finish: str | None = None) -> bytes:
                    return b"data: " + json.dumps({
                        "id": reply_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                    }, ensure_ascii=False).encode("utf-8") + b"\n\n"

                self.wfile.write(frame({"role": "assistant", "content": ""}))
                # 分两三段推，客户端能早点开始渲染
                size = max(1, len(content) // 3)
                for index in range(0, len(content), size):
                    self.wfile.write(frame({"content": content[index:index + size]}))
                    self.wfile.flush()
                self.wfile.write(frame({}, "stop"))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            finally:
                self.close_connection = True

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            log.debug("%s - %s", self.address_string(), fmt % args)

    return Handler

