# -*- coding: utf-8 -*-
"""配置加载与校验。

设计原则：
    * 只用标准库（json / dataclasses）；
    * 缺字段就用默认值，写错类型直接报 ConfigError，别让程序带着半截配置跑；
    * 未知字段收集成 warnings，方便用户发现拼写错误。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """配置文件不合法。"""


# ---------------------------------------------------------------- 基础类型转换

def _as_str(value: Any, where: str, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ConfigError(f"{where} 需要字符串，得到 {type(value).__name__}")
    return value


def _as_bool(value: Any, where: str, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on", "是"):
            return True
        if lowered in ("0", "false", "no", "off", "否"):
            return False
    raise ConfigError(f"{where} 需要布尔值，得到 {value!r}")


def _as_float(value: Any, where: str, default: float, minimum: float | None = None) -> float:
    if value is None or value == "":
        return default
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{where} 需要数字，得到 {value!r}") from exc
    if minimum is not None and number < minimum:
        raise ConfigError(f"{where} 不能小于 {minimum}，得到 {number}")
    return number


def _as_int(value: Any, where: str, default: int, minimum: int | None = None) -> int:
    return int(_as_float(value, where, float(default), None if minimum is None else float(minimum)))


def _as_str_tuple(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        result: list[str] = []
        for index, item in enumerate(value):
            if not isinstance(item, str):
                raise ConfigError(f"{where}[{index}] 需要字符串，得到 {type(item).__name__}")
            if item.strip():
                result.append(item.strip())
        return tuple(result)
    raise ConfigError(f"{where} 需要字符串或字符串数组")


def _as_hhmm(value: Any, where: str) -> float:
    """把 "23:30" 转成当天分钟数，方便比较跨零点的安静时段。"""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        hour, minute = text.split(":")
        total = int(hour) * 60 + int(minute)
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"{where} 需要 HH:MM 格式，得到 {value!r}") from exc
    if not 0 <= total < 24 * 60:
        raise ConfigError(f"{where} 超出范围：{value!r}")
    return float(total)


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"配置项 {name} 需要是对象，得到 {type(value).__name__}")
    return value


# ---------------------------------------------------------------- 配置数据类

@dataclass(slots=True)
class ApiConfig:
    """数字生命 HTTP API。"""

    base_url: str = "http://127.0.0.1:8000"
    chat_path: str = "/chat"
    proactive_path: str = "/proactive"
    status_path: str = "/status"
    timeout: float = 120.0
    retries: int = 1
    token: str = ""

    @property
    def chat_url(self) -> str:
        return self.base_url.rstrip("/") + "/" + self.chat_path.lstrip("/")

    @property
    def proactive_url(self) -> str:
        return self.base_url.rstrip("/") + "/" + self.proactive_path.lstrip("/")

    @property
    def status_url(self) -> str:
        return self.base_url.rstrip("/") + "/" + self.status_path.lstrip("/")


@dataclass(slots=True)
class IMConfig:
    """IM行为与过滤规则。"""

    adapter: str = "ntchat"
    chat_exe_path: str = ""
    login_timeout: float = 180.0
    version_spoof_enabled: bool = False
    version_spoof_target: str = "3.9.12.15"

    wake_words: tuple[str, ...] = ("她",)
    private_enabled: bool = True
    group_enabled: bool = True
    group_reply_at: bool = True
    strip_leading_at: bool = True

    allowed_rooms: tuple[str, ...] = ()
    allowed_wxids: tuple[str, ...] = ()
    blocked_wxids: tuple[str, ...] = ()
    owner_wxids: tuple[str, ...] = ()      # 只有这些人能用 /表情 指令

    user_id_strategy: str = "sender"   # sender | room | fixed
    fixed_user_id: str = "default"

    min_reply_interval: float = 2.0        # 同一个会话两次回复的最小间隔（秒）
    max_reply_chars: int = 1200
    split_long_reply: bool = True
    send_gap: float = 0.8
    fallback_on_error: str = ""            # API 挂了要不要回一句；空字符串=不回
    max_input_chars: int = 1000
    max_queue: int = 50
    dedupe_ttl: float = 300.0
    echo_guard_seconds: float = 60.0

    def __post_init__(self) -> None:
        self.adapter = self.adapter.strip().lower() or "ntchat"
        if self.user_id_strategy not in ("sender", "room", "fixed"):
            raise ConfigError("chat.user_id_strategy 只能是 sender / room / fixed")
        if self.max_queue < 1:
            raise ConfigError("chat.max_queue 必须 >= 1")
        if self.max_reply_chars < 50:
            raise ConfigError("chat.max_reply_chars 不能小于 50")


@dataclass(slots=True)
class ProactiveConfig:
    """主动聊天。"""

    enabled: bool = True
    interval_minutes: float = 45.0
    jitter: float = 0.25
    quiet_start: float = 23 * 60 + 30
    quiet_end: float = 8 * 60
    target: str = "文件传输助手"
    user_id: str = "default"
    strip_tags: bool = True
    max_chars: int = 800
    run_on_start: bool = False
    min_gap_minutes: float = 20.0

    def __post_init__(self) -> None:
        if self.interval_minutes < 1:
            raise ConfigError("proactive.interval_minutes 不能小于 1")
        if not 0 <= self.jitter < 1:
            raise ConfigError("proactive.jitter 需要在 [0, 1) 之间")

    def in_quiet_hours(self, now: datetime | None = None) -> bool:
        moment = now or datetime.now()
        current = moment.hour * 60 + moment.minute
        start, end = self.quiet_start, self.quiet_end
        if start == end:
            return False
        if start < end:
            return start <= current < end
        return current >= start or current < end


@dataclass(slots=True)
class ControlConfig:
    """给 DSH / 本机脚本调用的控制 API。"""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765
    token: str = ""

    def __post_init__(self) -> None:
        if not 0 <= self.port <= 65535:
            raise ConfigError("control.port 必须在 0-65535 之间")
        if self.host not in ("127.0.0.1", "localhost", "::1"):
            # 控制接口能发消息、能关进程，绝不默认暴露到局域网
            raise ConfigError("control.host 只允许 127.0.0.1 / localhost / ::1")


@dataclass(slots=True)
class LoggingConfig:
    level: str = "INFO"
    file: str = "logs/bridge.log"
    max_bytes: int = 5 * 1024 * 1024
    backup_count: int = 3
    console: bool = True


@dataclass(slots=True)
class ASRConfig:
    """语音转文字。ntchat 已经把语音转成 mp3，所以不需要 silk 解码器。"""

    provider: str = "faster-whisper"     # faster-whisper | openai | none
    local_model: str = "small"           # tiny / base / small / medium / large-v3
    device: str = "cpu"                  # cpu | cuda
    compute_type: str = "int8"           # int8 | float16 | int8_float16
    download_root: str = "C:/aihuman/models/whisper"   # 已预下载 medium
    language: str = "zh"
    timeout: float = 120.0

    # openai 兼容的远端 ASR（/audio/transcriptions）
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    api_model: str = "whisper-1"

    def __post_init__(self) -> None:
        self.provider = self.provider.strip().lower() or "none"
        if self.provider not in ("faster-whisper", "openai", "none"):
            raise ConfigError("asr.provider 只能是 faster-whisper / openai / none")
        if self.provider == "openai" and not self.base_url.startswith(("http://", "https://")):
            raise ConfigError("asr.base_url 必须以 http:// 或 https:// 开头")


@dataclass(slots=True)
class VisionConfig:
    """图片理解（给图片生成一句中文描述）。"""

    provider: str = "ollama"             # ollama | openai | none
    model: str = "gemma3:4b"
    base_url: str = "http://127.0.0.1:11434"
    api_key: str = ""
    prompt: str = "用一句中文描述这张图片，40 字以内，只说你看到的东西，不要解释"
    timeout: float = 60.0
    max_image_mb: float = 8.0
    max_side: int = 1280              # 送模型前先缩到这个边长（有 Pillow 时生效）

    def __post_init__(self) -> None:
        self.provider = self.provider.strip().lower() or "none"
        if self.provider not in ("ollama", "openai", "none"):
            raise ConfigError("vision.provider 只能是 ollama / openai / none")
        if not self.base_url.startswith(("http://", "https://")):
            raise ConfigError("vision.base_url 必须以 http:// 或 https:// 开头")


@dataclass(slots=True)
class MediaConfig:
    """媒体处理总开关。"""

    enabled: bool = True
    workers: int = 2
    voice_enabled: bool = True
    image_enabled: bool = True
    emoji_enabled: bool = True
    cache_dir: str = "media_cache"
    placeholder_voice: str = "[对方发来一条语音，你没听清]"
    placeholder_image: str = "[对方发来一张图片，你没看清]"
    placeholder_emoji: str = "[对方发来一个表情]"

    def __post_init__(self) -> None:
        self.workers = max(1, min(8, int(self.workers)))


@dataclass(slots=True)
class StickerConfig:
    """表情包：自动收集 + 按情绪使用。"""

    enabled: bool = True
    dir: str = "stickers"
    auto_collect: bool = True
    collect_emoji: bool = True
    collect_gif: bool = True
    chance: float = 0.25             # 每次回复后发表情的概率（有匹配表情且开启时）
    mood_enabled: bool = True
    max_kb: int = 2048
    commands_enabled: bool = True

    def __post_init__(self) -> None:
        if not 0.0 <= self.chance <= 1.0:
            raise ConfigError("stickers.chance 必须在 0-1 之间")
        if self.max_kb < 64:
            raise ConfigError("stickers.max_kb 不能小于 64")


@dataclass(slots=True)
class Config:
    api: ApiConfig = field(default_factory=ApiConfig)
    chat: IMConfig = field(default_factory=IMConfig)
    proactive: ProactiveConfig = field(default_factory=ProactiveConfig)
    control: ControlConfig = field(default_factory=ControlConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    media: MediaConfig = field(default_factory=MediaConfig)
    asr: ASRConfig = field(default_factory=ASRConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    stickers: StickerConfig = field(default_factory=StickerConfig)
    path: Path | None = None
    warnings: list[str] = field(default_factory=list)

    # ------------------------------------------------------------ 加载

    @classmethod
    def load(cls, path: str | Path | None) -> "Config":
        raw: dict[str, Any] = {}
        config_path: Path | None = None
        if path:
            config_path = Path(path).expanduser()
            if not config_path.exists():
                raise ConfigError(f"配置文件不存在：{config_path}")
            try:
                text = config_path.read_text(encoding="utf-8-sig")
            except OSError as exc:
                raise ConfigError(f"读取配置文件失败：{exc}") from exc
            try:
                raw = json.loads(text) if text.strip() else {}
            except json.JSONDecodeError as exc:
                raise ConfigError(f"配置文件不是合法 JSON：第 {exc.lineno} 行 {exc.msg}") from exc
            if not isinstance(raw, dict):
                raise ConfigError("配置文件根节点必须是 JSON 对象")

        warnings: list[str] = []
        config = cls(
            api=cls._parse_api(_section(raw, "api"), warnings),
            chat=cls._parse_chat(_section(raw, "chat"), warnings),
            proactive=cls._parse_proactive(_section(raw, "proactive"), warnings),
            control=cls._parse_control(_section(raw, "control"), warnings),
            logging=cls._parse_logging(_section(raw, "logging"), warnings),
            media=cls._parse_media(_section(raw, "media"), warnings),
            asr=cls._parse_asr(_section(raw, "asr"), warnings),
            vision=cls._parse_vision(_section(raw, "vision"), warnings),
            stickers=cls._parse_stickers(_section(raw, "stickers"), warnings),
            path=config_path,
            warnings=warnings,
        )

        env_url = os.environ.get("DIGITAL_LIFE_API", "").strip()
        if env_url:
            config.api.base_url = env_url.rstrip("/")
        env_token = os.environ.get("WX_BRIDGE_TOKEN", "").strip()
        if env_token:
            config.control.token = env_token
        env_level = os.environ.get("WX_BRIDGE_LOG_LEVEL", "").strip().upper()
        if env_level:
            config.logging.level = env_level
        return config

    @staticmethod
    def _check_unknown(section: dict[str, Any], known: set[str], name: str, warnings: list[str]) -> None:
        for key in section:
            if key not in known:
                warnings.append(f"未知配置项 {name}.{key}（已忽略）")

    @classmethod
    def _parse_api(cls, data: dict[str, Any], warnings: list[str]) -> ApiConfig:
        cls._check_unknown(
            data,
            {"base_url", "chat_path", "proactive_path", "status_path", "timeout", "retries", "token"},
            "api",
            warnings,
        )
        cfg = ApiConfig(
            base_url=_as_str(data.get("base_url"), "api.base_url", "http://127.0.0.1:8000"),
            chat_path=_as_str(data.get("chat_path"), "api.chat_path", "/chat"),
            proactive_path=_as_str(data.get("proactive_path"), "api.proactive_path", "/proactive"),
            status_path=_as_str(data.get("status_path"), "api.status_path", "/status"),
            timeout=_as_float(data.get("timeout"), "api.timeout", 120.0, 1.0),
            retries=_as_int(data.get("retries"), "api.retries", 1, 0),
            token=_as_str(data.get("token"), "api.token", ""),
        )
        if not cfg.base_url.startswith(("http://", "https://")):
            raise ConfigError("api.base_url 必须以 http:// 或 https:// 开头")
        return cfg

    @classmethod
    def _parse_chat(cls, data: dict[str, Any], warnings: list[str]) -> IMConfig:
        known = {
            "adapter", "chat_exe_path", "login_timeout", "wake_words",
            "version_spoof_enabled", "version_spoof_target",
            "private_enabled", "group_enabled", "group_reply_at", "strip_leading_at",
            "allowed_rooms", "allowed_wxids", "blocked_wxids", "owner_wxids",
            "user_id_strategy", "fixed_user_id",
            "min_reply_interval", "max_reply_chars", "split_long_reply", "send_gap",
            "fallback_on_error", "max_input_chars", "max_queue", "dedupe_ttl",
            "echo_guard_seconds",
        }
        cls._check_unknown(data, known, "chat", warnings)
        return IMConfig(
            adapter=_as_str(data.get("adapter"), "chat.adapter", "ntchat"),
            chat_exe_path=_as_str(data.get("chat_exe_path"), "chat.chat_exe_path", ""),
            login_timeout=_as_float(data.get("login_timeout"), "chat.login_timeout", 180.0, 5.0),
            version_spoof_enabled=_as_bool(
                data.get("version_spoof_enabled"), "chat.version_spoof_enabled", False
            ),
            version_spoof_target=_as_str(
                data.get("version_spoof_target"), "chat.version_spoof_target", "3.9.12.15"
            ),
            wake_words=_as_str_tuple(data.get("wake_words", ("她",)), "chat.wake_words"),
            private_enabled=_as_bool(data.get("private_enabled"), "chat.private_enabled", True),
            group_enabled=_as_bool(data.get("group_enabled"), "chat.group_enabled", True),
            group_reply_at=_as_bool(data.get("group_reply_at"), "chat.group_reply_at", True),
            strip_leading_at=_as_bool(data.get("strip_leading_at"), "chat.strip_leading_at", True),
            allowed_rooms=_as_str_tuple(data.get("allowed_rooms"), "chat.allowed_rooms"),
            allowed_wxids=_as_str_tuple(data.get("allowed_wxids"), "chat.allowed_wxids"),
            blocked_wxids=_as_str_tuple(data.get("blocked_wxids"), "chat.blocked_wxids"),
            owner_wxids=_as_str_tuple(data.get("owner_wxids"), "chat.owner_wxids"),
            user_id_strategy=_as_str(data.get("user_id_strategy"), "chat.user_id_strategy", "sender"),
            fixed_user_id=_as_str(data.get("fixed_user_id"), "chat.fixed_user_id", "default"),
            min_reply_interval=_as_float(data.get("min_reply_interval"), "chat.min_reply_interval", 2.0, 0.0),
            max_reply_chars=_as_int(data.get("max_reply_chars"), "chat.max_reply_chars", 1200, 50),
            split_long_reply=_as_bool(data.get("split_long_reply"), "chat.split_long_reply", True),
            send_gap=_as_float(data.get("send_gap"), "chat.send_gap", 0.8, 0.0),
            fallback_on_error=_as_str(data.get("fallback_on_error"), "chat.fallback_on_error", ""),
            max_input_chars=_as_int(data.get("max_input_chars"), "chat.max_input_chars", 1000, 10),
            max_queue=_as_int(data.get("max_queue"), "chat.max_queue", 50, 1),
            dedupe_ttl=_as_float(data.get("dedupe_ttl"), "chat.dedupe_ttl", 300.0, 1.0),
            echo_guard_seconds=_as_float(data.get("echo_guard_seconds"), "chat.echo_guard_seconds", 60.0, 0.0),
        )

    @classmethod
    def _parse_proactive(cls, data: dict[str, Any], warnings: list[str]) -> ProactiveConfig:
        known = {
            "enabled", "interval_minutes", "jitter", "quiet_hours", "target",
            "user_id", "strip_tags", "max_chars", "run_on_start", "min_gap_minutes",
        }
        cls._check_unknown(data, known, "proactive", warnings)
        quiet = data.get("quiet_hours")
        quiet_start, quiet_end = 23 * 60 + 30, 8 * 60
        if isinstance(quiet, (list, tuple)) and len(quiet) == 2:
            quiet_start = _as_hhmm(quiet[0], "proactive.quiet_hours[0]")
            quiet_end = _as_hhmm(quiet[1], "proactive.quiet_hours[1]")
        elif quiet not in (None, "", []):
            raise ConfigError('proactive.quiet_hours 需要形如 ["23:30", "08:00"] 的数组')
        return ProactiveConfig(
            enabled=_as_bool(data.get("enabled"), "proactive.enabled", True),
            interval_minutes=_as_float(data.get("interval_minutes"), "proactive.interval_minutes", 45.0, 1.0),
            jitter=_as_float(data.get("jitter"), "proactive.jitter", 0.25, 0.0),
            quiet_start=quiet_start,
            quiet_end=quiet_end,
            target=_as_str(data.get("target"), "proactive.target", "文件传输助手"),
            user_id=_as_str(data.get("user_id"), "proactive.user_id", "default"),
            strip_tags=_as_bool(data.get("strip_tags"), "proactive.strip_tags", True),
            max_chars=_as_int(data.get("max_chars"), "proactive.max_chars", 800, 10),
            run_on_start=_as_bool(data.get("run_on_start"), "proactive.run_on_start", False),
            min_gap_minutes=_as_float(data.get("min_gap_minutes"), "proactive.min_gap_minutes", 20.0, 0.0),
        )

    @classmethod
    def _parse_control(cls, data: dict[str, Any], warnings: list[str]) -> ControlConfig:
        cls._check_unknown(data, {"enabled", "host", "port", "token"}, "control", warnings)
        return ControlConfig(
            enabled=_as_bool(data.get("enabled"), "control.enabled", True),
            host=_as_str(data.get("host"), "control.host", "127.0.0.1"),
            port=_as_int(data.get("port"), "control.port", 8765, 0),
            token=_as_str(data.get("token"), "control.token", ""),
        )

    @classmethod
    def _parse_logging(cls, data: dict[str, Any], warnings: list[str]) -> LoggingConfig:
        cls._check_unknown(data, {"level", "file", "max_bytes", "backup_count", "console"}, "logging", warnings)
        level = _as_str(data.get("level"), "logging.level", "INFO").upper()
        if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ConfigError(f"logging.level 不合法：{level}")
        return LoggingConfig(
            level=level,
            file=_as_str(data.get("file"), "logging.file", "logs/bridge.log"),
            max_bytes=_as_int(data.get("max_bytes"), "logging.max_bytes", 5 * 1024 * 1024, 0),
            backup_count=_as_int(data.get("backup_count"), "logging.backup_count", 3, 0),
            console=_as_bool(data.get("console"), "logging.console", True),
        )

    # ------------------------------------------------------------ 多模态

    @classmethod
    def _parse_media(cls, data: dict[str, Any], warnings: list[str]) -> MediaConfig:
        known = {
            "enabled", "workers", "voice_enabled", "image_enabled", "emoji_enabled",
            "cache_dir", "placeholder_voice", "placeholder_image", "placeholder_emoji",
        }
        cls._check_unknown(data, known, "media", warnings)
        return MediaConfig(
            enabled=_as_bool(data.get("enabled"), "media.enabled", True),
            workers=_as_int(data.get("workers"), "media.workers", 2, 1),
            voice_enabled=_as_bool(data.get("voice_enabled"), "media.voice_enabled", True),
            image_enabled=_as_bool(data.get("image_enabled"), "media.image_enabled", True),
            emoji_enabled=_as_bool(data.get("emoji_enabled"), "media.emoji_enabled", True),
            cache_dir=_as_str(data.get("cache_dir"), "media.cache_dir", "media_cache"),
            placeholder_voice=_as_str(data.get("placeholder_voice"), "media.placeholder_voice",
                                      "[对方发来一条语音，你没听清]"),
            placeholder_image=_as_str(data.get("placeholder_image"), "media.placeholder_image",
                                      "[对方发来一张图片，你没看清]"),
            placeholder_emoji=_as_str(data.get("placeholder_emoji"), "media.placeholder_emoji",
                                      "[对方发来一个表情]"),
        )

    @classmethod
    def _parse_asr(cls, data: dict[str, Any], warnings: list[str]) -> ASRConfig:
        known = {
            "provider", "local_model", "device", "compute_type", "download_root",
            "language", "timeout", "base_url", "api_key", "api_model",
        }
        cls._check_unknown(data, known, "asr", warnings)
        return ASRConfig(
            provider=_as_str(data.get("provider"), "asr.provider", "faster-whisper"),
            local_model=_as_str(data.get("local_model"), "asr.local_model", "small"),
            device=_as_str(data.get("device"), "asr.device", "cpu"),
            compute_type=_as_str(data.get("compute_type"), "asr.compute_type", "int8"),
            download_root=_as_str(data.get("download_root"), "asr.download_root", ""),
            language=_as_str(data.get("language"), "asr.language", "zh"),
            timeout=_as_float(data.get("timeout"), "asr.timeout", 120.0, 5.0),
            base_url=_as_str(data.get("base_url"), "asr.base_url", "https://api.openai.com/v1"),
            api_key=_as_str(data.get("api_key"), "asr.api_key", ""),
            api_model=_as_str(data.get("api_model"), "asr.api_model", "whisper-1"),
        )

    @classmethod
    def _parse_vision(cls, data: dict[str, Any], warnings: list[str]) -> VisionConfig:
        known = {"provider", "model", "base_url", "api_key", "prompt", "timeout", "max_image_mb", "max_side"}
        cls._check_unknown(data, known, "vision", warnings)
        return VisionConfig(
            provider=_as_str(data.get("provider"), "vision.provider", "ollama"),
            model=_as_str(data.get("model"), "vision.model", "gemma3:4b"),
            base_url=_as_str(data.get("base_url"), "vision.base_url", "http://127.0.0.1:11434"),
            api_key=_as_str(data.get("api_key"), "vision.api_key", ""),
            prompt=_as_str(data.get("prompt"), "vision.prompt",
                "用一句中文描述这张图片，40 字以内，只说你看到的东西，不要解释"),
            timeout=_as_float(data.get("timeout"), "vision.timeout", 60.0, 5.0),
            max_image_mb=_as_float(data.get("max_image_mb"), "vision.max_image_mb", 8.0, 0.1),
            max_side=_as_int(data.get("max_side"), "vision.max_side", 1280, 0),
        )

    @classmethod
    def _parse_stickers(cls, data: dict[str, Any], warnings: list[str]) -> StickerConfig:
        known = {
            "enabled", "dir", "auto_collect", "collect_emoji", "collect_gif",
            "chance", "mood_enabled", "max_kb", "commands_enabled",
        }
        cls._check_unknown(data, known, "stickers", warnings)
        return StickerConfig(
            enabled=_as_bool(data.get("enabled"), "stickers.enabled", True),
            dir=_as_str(data.get("dir"), "stickers.dir", "stickers"),
            auto_collect=_as_bool(data.get("auto_collect"), "stickers.auto_collect", True),
            collect_emoji=_as_bool(data.get("collect_emoji"), "stickers.collect_emoji", True),
            collect_gif=_as_bool(data.get("collect_gif"), "stickers.collect_gif", True),
            chance=_as_float(data.get("chance"), "stickers.chance", 0.25, 0.0),
            mood_enabled=_as_bool(data.get("mood_enabled"), "stickers.mood_enabled", True),
            max_kb=_as_int(data.get("max_kb"), "stickers.max_kb", 2048, 64),
            commands_enabled=_as_bool(data.get("commands_enabled"), "stickers.commands_enabled", True),
        )

    # ------------------------------------------------------------ 其他

    def log_path(self) -> Path | None:
        if not self.logging.file:
            return None
        path = Path(self.logging.file)
        if not path.is_absolute():
            base = self.path.parent if self.path else Path.cwd()
            path = base / path
        return path

    def to_dict(self) -> dict[str, Any]:
        return {
            "api": {
                "base_url": self.api.base_url,
                "chat_path": self.api.chat_path,
                "proactive_path": self.api.proactive_path,
                "timeout": self.api.timeout,
                "retries": self.api.retries,
            },
            "chat": {
                "adapter": self.chat.adapter,
                "wake_words": list(self.chat.wake_words),
                "private_enabled": self.chat.private_enabled,
                "group_enabled": self.chat.group_enabled,
                "group_reply_at": self.chat.group_reply_at,
                "user_id_strategy": self.chat.user_id_strategy,
            },
            "proactive": {
                "enabled": self.proactive.enabled,
                "interval_minutes": self.proactive.interval_minutes,
                "target": self.proactive.target,
            },
            "control": {
                "enabled": self.control.enabled,
                "host": self.control.host,
                "port": self.control.port,
                "token_required": bool(self.control.token),
            },
            "media": {
                "enabled": self.media.enabled,
                "asr": self.asr.provider,
                "vision": self.vision.provider,
                "vision_model": self.vision.model,
                "stickers": self.stickers.enabled,
            },
            "path": str(self.path) if self.path else None,
        }

    def resolve_dir(self, value: str) -> Path:
        """相对路径按配置文件所在目录解析，保证从任何 cwd 启动都一样。"""
        path = Path(value).expanduser()
        if path.is_absolute():
            return path
        base = self.path.parent if self.path else Path.cwd()
        return (base / path).resolve()
