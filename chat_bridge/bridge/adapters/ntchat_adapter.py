# -*- coding: utf-8 -*-
"""ntchat 适配器（IM 3.6.0.18）。

ntchat 是注入式 hook，只认 `IM.exe 3.6.0.18`（见 ntchat.wc.SUPPORT_VERSIONS）。
IM 4.x 是重写的新客户端（Weixin.exe / xchat），ntchat 完全注入不进去，
所以这里在启动阶段就把版本问题翻译成人话，而不是让用户对着 DLL 报错发呆。

消息结构（ntchat 0.1.x）：
    文本   data["msg"]          正文
    图片   data["image"]        大图本地路径（另有 image_thumb）
    语音   data["mp3_file"]     已转码的 mp3（所以不需要 silk 解码器）
    表情   data["emoji_url"]    IM CDN 地址（需要下载）
    公共   from_wxid / room_wxid / at_user_list / msgid
"""

from __future__ import annotations

import hashlib
import os
import time
import urllib.parse
from typing import Any, Callable

from ..config import Config
from ..logs import get_logger
from .base import AdapterError, IncomingMessage, SendResult, IMVersionError

log = get_logger("adapter.ntchat")

SUPPORTED_VERSION = "3.6.0.18"
_FILEHELPER_ALIASES = {"filehelper", "文件传输助手", "文件助手", "file transfer", "filetransfer"}
_WXID_KEYS = ("wxid", "username", "user_name", "userName", "userNameStr")
_NAME_KEYS = ("nickname", "nickName", "name", "remark", "displayName")


def _pick(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


class NtchatAdapter:
    name = "ntchat"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._ntchat: Any = None
        self._wx: Any = None
        self._self_wxid: str = ""
        self._on_message: Callable[[IncomingMessage], None] | None = None
        self._rooms_cache: tuple[float, list[dict[str, Any]]] | None = None
        self._contacts_cache: tuple[float, list[dict[str, Any]]] | None = None
        self._media_types: dict[int, str] = {}

    # ------------------------------------------------------------------ 生命周期

    def start(self, on_message: Callable[[IncomingMessage], None]) -> None:
        # NTCHAT_LOG 必须在 import ntchat 之前设置，否则它自己的日志会刷屏
        os.environ.setdefault("NTCHAT_LOG", "ERROR")
        try:
            import ntchat  # noqa: PLC0415 - 延迟导入，没装 ntchat 时也能跑 doctor
        except ImportError as exc:  # pragma: no cover - 依赖环境
            raise AdapterError(
                "没有安装 ntchat。请用 Python 3.10/3.11（64 位）执行：pip install ntchat==0.1.20\n"
                f"当前解释器：{os.sys.executable} {os.sys.version.split()[0]}"
            ) from exc

        self._ntchat = ntchat
        self._on_message = on_message

        exe_path = (self.cfg.chat.chat_exe_path or "").strip()
        if exe_path:
            log.info("使用自定义IM路径：%s", exe_path)
            ntchat.set_chat_exe_path(chat_exe_path=exe_path, chat_version=SUPPORTED_VERSION)

        self._log_installed_version(ntchat)

        self._wx = ntchat.IM()
        self._register_handlers(ntchat)

        log.info("正在注入IM（smart=True）……")
        try:
            opened = self._wx.open(smart=True)
        except Exception as exc:  # noqa: BLE001
            raise self._translate_open_error(exc) from exc
        if not opened:
            raise AdapterError(
                "IM注入失败：open() 返回 False。请确认IM 3.6.0.18 已安装并能正常登录，"
                "必要时在配置里指定 chat.chat_exe_path。"
            )

        # 「当前IM版本过低」是服务端拒绝，先伪装版本再等你扫码
        if self.cfg.chat.version_spoof_enabled:
            self._apply_version_spoof()

        log.info("等待IM登录（最多 %.0fs）……", self.cfg.chat.login_timeout)
        self._wx.wait_login(timeout=self.cfg.chat.login_timeout)

        info = self._safe_call(self._wx.get_login_info) or {}
        self._self_wxid = str(info.get("wxid") or "").strip()
        if not self._self_wxid:
            raise AdapterError(
                "等待登录超时：还没有扫码登录。\n"
                "  1) 如果IM弹「当前IM版本过低」，说明服务端在拒绝 3.6.0.18：\n"
                "     - IM已经打开时执行：python run_bridge.py patch-version\n"
                "     - 或在配置里打开 chat.version_spoof_enabled=true\n"
                "  2) 如果只是没扫码：登录后重启中间件（或调大 chat.login_timeout）"
            )
        log.info("登录成功：%s（%s）", info.get("nickname") or "?", self._self_wxid)

    def _apply_version_spoof(self) -> None:
        """把IM内存里的 3.6.0.18 改成 3.9.x，骗过服务端的版本检查。"""
        try:
            from ..version_patch import VersionPatcher  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            log.warning("版本伪装模块加载失败", exc_info=True)
            return

        target = self.cfg.chat.version_spoof_target or "3.9.12.15"
        result = VersionPatcher(target=target).patch(timeout=20.0)
        if result.ok:
            log.info("版本伪装成功：%s", result.detail)
        else:
            log.warning("版本伪装失败：%s", result.detail)

    def _register_handlers(self, ntchat: Any) -> None:
        """注册文本 + 图片 + 语音 + 表情四类回调。"""
        wanted = [
            ("MT_RECV_TEXT_MSG", "text"),
            ("MT_RECV_PICTURE_MSG", "image"),
            ("MT_RECV_VOICE_MSG", "voice"),
            ("MT_RECV_EMOJI_MSG", "emoji"),
        ]
        types: list[int] = []
        for name, kind in wanted:
            value = getattr(ntchat, name, None)
            if isinstance(value, int) and value not in types:
                types.append(value)
                self._media_types[value] = kind
        if not types:
            raise AdapterError("ntchat 里找不到任何消息类型常量，版本可能不对")

        @self._wx.msg_register(types)
        def _handler(chat_instance: Any, message: dict[str, Any]) -> None:  # noqa: ANN401
            try:
                self._handle_message(message)
            except Exception:  # noqa: BLE001 - 回调里绝不能抛出去
                log.exception("处理IM消息回调异常")

        log.info("已注册消息类型：%s", ", ".join(str(t) for t in types))

    def stop(self) -> None:
        ntchat = self._ntchat
        self._wx = None
        if ntchat is None:
            return
        try:
            ntchat.exit_()
        except Exception:  # noqa: BLE001
            log.debug("ntchat.exit_() 异常（通常无所谓）", exc_info=True)

    @property
    def self_wxid(self) -> str:
        return self._self_wxid

    def is_alive(self) -> bool:
        wx = self._wx
        return bool(wx is not None and getattr(wx, "login_status", False))

    # ------------------------------------------------------------------ 收消息

    def _handle_message(self, message: dict[str, Any]) -> None:
        data = message.get("data") if isinstance(message, dict) else None
        if not isinstance(data, dict):
            return

        msg_type = message.get("type")
        kind = self._detect_kind(msg_type, data)

        from_wxid = str(data.get("from_wxid") or "").strip()
        room_wxid = str(data.get("room_wxid") or "").strip()
        content = ""
        media_path = ""
        media_url = ""

        if kind == "text":
            raw = data.get("msg")
            if raw is None:
                raw = data.get("content")
            content = "" if raw is None else str(raw)
        elif kind == "image":
            media_path = str(data.get("image") or data.get("image_thumb") or "").strip()
        elif kind == "voice":
            media_path = str(data.get("mp3_file") or data.get("voice") or data.get("file") or "").strip()
        elif kind == "emoji":
            media_url = str(data.get("emoji_url") or "").strip()
            if media_url:
                media_url = urllib.parse.unquote(media_url)

        at_list = data.get("at_user_list") or []
        if isinstance(at_list, str):
            at_list = [at_list]
        at_wxid = [str(item).strip() for item in at_list if str(item).strip()]

        msg_id = str(
            data.get("msgid") or data.get("msg_id") or data.get("msgsvrid") or data.get("newmsgid") or ""
        ).strip()

        timestamp = data.get("timestamp") or data.get("create_time") or 0
        try:
            timestamp = float(timestamp)
        except (TypeError, ValueError):
            timestamp = 0.0
        if timestamp > 10_000_000_000:  # 毫秒
            timestamp /= 1000.0
        if timestamp <= 0:
            timestamp = time.time()

        if not msg_id:
            raw = f"{room_wxid}|{from_wxid}|{content}|{media_path}|{media_url}|{int(timestamp // 2)}"
            msg_id = hashlib.md5(raw.encode("utf-8", "ignore")).hexdigest()

        incoming = IncomingMessage(
            msg_id=msg_id,
            from_wxid=from_wxid,
            content=content,
            room_wxid=room_wxid,
            at_wxid=at_wxid,
            is_self=bool(self._self_wxid and from_wxid == self._self_wxid),
            timestamp=timestamp,
            media_kind="" if kind == "text" else kind,
            media_path=media_path,
            media_url=media_url,
        )
        log.debug(
            "收到 %s：room=%s from=%s at=%s path=%s msg=%s",
            kind,
            room_wxid or "-",
            from_wxid,
            at_wxid,
            media_path or media_url or "-",
            content[:60].replace("\n", " "),
        )
        if self._on_message:
            self._on_message(incoming)

    def _detect_kind(self, msg_type: Any, data: dict[str, Any]) -> str:
        """先按常量判断，常量对不上时按字段兜底。"""
        if isinstance(msg_type, int) and msg_type in self._media_types:
            kind = self._media_types[msg_type]
            if kind == "text" and not (data.get("msg") or data.get("content")):
                pass
            else:
                return kind
        if data.get("emoji_url"):
            return "emoji"
        if data.get("mp3_file"):
            return "voice"
        if data.get("image"):
            return "image"
        return "text"

    # ------------------------------------------------------------------ 发消息

    def send_text(self, to_wxid: str, text: str) -> SendResult:
        wx = self._wx
        if wx is None:
            return SendResult(False, "IM实例不存在")
        try:
            response = wx.send_text(to_wxid=to_wxid, content=text)
        except Exception as exc:  # noqa: BLE001
            return SendResult(False, f"{type(exc).__name__}: {exc}")
        return self._interpret(response, to_wxid)

    def send_room_at(self, room_wxid: str, text: str, at_wxid: list[str]) -> SendResult:
        wx = self._wx
        if wx is None:
            return SendResult(False, "IM实例不存在")
        if not at_wxid:
            return self.send_text(room_wxid, text)

        # {$@} 是 ntchat 的占位符，数量必须和 at_list 一致，发送时会被替换成 "@昵称"
        content = "{$@} " * len(at_wxid) + text
        try:
            response = wx.send_room_at_msg(to_wxid=room_wxid, content=content, at_list=at_wxid)
        except Exception as exc:  # noqa: BLE001
            return SendResult(False, f"{type(exc).__name__}: {exc}")
        return self._interpret(response, room_wxid)

    def send_image(self, to_wxid: str, file_path: str) -> SendResult:
        wx = self._wx
        if wx is None:
            return SendResult(False, "IM实例不存在")
        try:
            response = wx.send_image(to_wxid=to_wxid, file_path=file_path)
        except Exception as exc:  # noqa: BLE001
            return SendResult(False, f"{type(exc).__name__}: {exc}")
        return self._interpret(response, to_wxid)

    def send_gif(self, to_wxid: str, file_path: str) -> SendResult:
        wx = self._wx
        if wx is None:
            return SendResult(False, "IM实例不存在")
        try:
            response = wx.send_gif(to_wxid=to_wxid, file=file_path)
        except Exception as exc:  # noqa: BLE001
            # 某些版本没有 send_gif，退回普通图片发送
            log.warning("send_gif 失败（%s），改用 send_image", exc)
            try:
                response = wx.send_image(to_wxid=to_wxid, file_path=file_path)
            except Exception as inner:  # noqa: BLE001
                return SendResult(False, f"{type(inner).__name__}: {inner}")
        return self._interpret(response, to_wxid)

    @staticmethod
    def _interpret(response: Any, target: str) -> SendResult:
        """ntchat 发送成功时可能返回 dict，也可能返回 None（异步）。"""
        if isinstance(response, dict):
            for key in ("error", "errmsg"):
                value = response.get(key)
                if value and str(value).strip():
                    return SendResult(False, f"{key}={value}")
            code = response.get("code")
            if code not in (None, 0, "0", 200, "200"):
                return SendResult(False, f"code={code} resp={str(response)[:120]}")
            return SendResult(True, str(response)[:120])
        if response is None:
            # 没有回包不等于失败：ntchat 的发送是半异步的
            log.debug("发送 %s 没有回包（按成功处理）", target)
            return SendResult(True, "no-response")
        return SendResult(True, str(response)[:120])

    # ------------------------------------------------------------------ 目标解析

    def resolve_target(self, target: str) -> tuple[str, str] | None:
        target = (target or "").strip()
        if not target:
            return None
        if target.casefold() in _FILEHELPER_ALIASES:
            return ("filehelper", "private")
        if target.endswith("@chatroom"):
            return (target, "group")
        if target.startswith("wxid_"):
            return (target, "private")

        for room in self._rooms():
            if target in (_pick(room, _NAME_KEYS), _pick(room, _WXID_KEYS)):
                wxid = _pick(room, _WXID_KEYS)
                return (wxid, "group") if wxid else None

        for contact in self._contacts():
            if target in (_pick(contact, _NAME_KEYS), _pick(contact, _WXID_KEYS)):
                wxid = _pick(contact, _WXID_KEYS)
                return (wxid, "private") if wxid else None
        return None

    def _rooms(self) -> list[dict[str, Any]]:
        cached = self._rooms_cache
        if cached and time.time() - cached[0] < 60:
            return cached[1]
        rooms = self._safe_call(self._wx.get_rooms) if self._wx else []
        rooms = [item for item in (rooms or []) if isinstance(item, dict)]
        self._rooms_cache = (time.time(), rooms)
        return rooms

    def _contacts(self) -> list[dict[str, Any]]:
        cached = self._contacts_cache
        if cached and time.time() - cached[0] < 60:
            return cached[1]
        contacts = self._safe_call(self._wx.get_contacts) if self._wx else []
        contacts = [item for item in (contacts or []) if isinstance(item, dict)]
        self._contacts_cache = (time.time(), contacts)
        return contacts

    # ------------------------------------------------------------------ 工具

    @staticmethod
    def _safe_call(func: Callable[[], Any]) -> Any:
        try:
            return func()
        except Exception:  # noqa: BLE001
            log.debug("调用 %s 失败", getattr(func, "__name__", func), exc_info=True)
            return None

    @staticmethod
    def _log_installed_version(ntchat: Any) -> None:
        try:
            version = ntchat.get_install_chat_version()
            log.info("本机IM版本：%s（ntchat 支持：%s）", version, SUPPORTED_VERSION)
            if version and version != SUPPORTED_VERSION:
                log.warning(
                    "IM版本 %s 与 ntchat 不匹配，注入很可能失败。需要 3.6.0.18。",
                    version,
                )
        except Exception:  # noqa: BLE001
            log.debug("读取IM版本失败", exc_info=True)

    def _translate_open_error(self, exc: Exception) -> AdapterError:
        name = type(exc).__name__
        text = str(exc)
        level = log.warning if "Version" in name else log.error
        level("注入失败：%s: %s", name, text)
        if "Version" in name or "版本" in text:
            return IMVersionError(
                f"IM版本不匹配（{name}）。ntchat 只支持 {SUPPORTED_VERSION}，"
                "请卸载 4.x 后安装 3.6.0.18，或在配置里指定 chat.chat_exe_path。"
            )
        return AdapterError(
            f"注入IM失败（{name}: {text}）。常见原因：IM未安装 3.6.0.18 / 被安全软件拦截 / 已登录实例冲突。"
        )
