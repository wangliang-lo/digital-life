# -*- coding: utf-8 -*-
"""bridge/config.py 的单元测试：加载、默认值、类型校验、未知字段。

只使用 tmp_path 下的临时配置，不读取 data/ 里的真实配置，不启动服务。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from chat_bridge.bridge.config import (  # noqa: E402
    ApiConfig,
    Config,
    ConfigError,
    MediaConfig,
    VisionConfig,
)

# 最小配置：只写 api.base_url，其余字段全部走默认值。
# 2026-11-14：曾因 dataclass(slots=True) 下取类属性当默认值而崩（member_descriptor），已修。
MINIMAL = {"api": {"base_url": "http://127.0.0.1:8000"}}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """清掉会覆盖配置的环境变量，避免本机环境影响结果。"""
    for name in ("DIGITAL_LIFE_API", "WX_BRIDGE_TOKEN", "WX_BRIDGE_LOG_LEVEL"):
        monkeypatch.delenv(name, raising=False)


def write_config(tmp_path: Path, payload) -> Path:
    path = tmp_path / "config.json"
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# --------------------------------------------------------------- 1. 默认值

def test_missing_fields_fall_back_to_defaults(tmp_path):
    cfg = Config.load(write_config(tmp_path, MINIMAL))

    assert cfg.warnings == []
    assert cfg.path == tmp_path / "config.json"

    # api
    assert cfg.api.base_url == "http://127.0.0.1:8000"
    assert cfg.api.chat_path == "/chat"
    assert cfg.api.timeout == 120.0
    assert cfg.api.retries == 1
    assert cfg.api.chat_url == "http://127.0.0.1:8000/chat"

    # chat
    assert cfg.chat.adapter == "ntchat"
    assert cfg.chat.wake_words == ("她",)
    assert cfg.chat.max_reply_chars == 1200
    assert cfg.chat.max_queue == 50
    assert cfg.chat.user_id_strategy == "sender"
    assert cfg.chat.allowed_rooms == ()

    # proactive（quiet_hours 内部存成当天分钟数）
    assert cfg.proactive.enabled is True
    assert cfg.proactive.interval_minutes == 45.0
    assert cfg.proactive.quiet_start == 23 * 60 + 30
    assert cfg.proactive.quiet_end == 8 * 60
    assert cfg.proactive.in_quiet_hours() in (True, False)  # 不抛错即可

    # 其余 section
    assert cfg.control.host == "127.0.0.1"
    assert cfg.control.port == 8765
    assert cfg.logging.level == "INFO"
    assert cfg.logging.max_bytes == 5 * 1024 * 1024
    assert cfg.logging.console is True
    assert cfg.media.workers == 2
    assert cfg.media.cache_dir == "media_cache"
    assert cfg.asr.provider == "faster-whisper"
    assert cfg.asr.language == "zh"
    assert cfg.asr.timeout == 120.0
    assert cfg.vision.model == "gemma3:4b"
    assert cfg.vision.max_side == 1280
    assert cfg.stickers.chance == 0.25
    assert cfg.stickers.max_kb == 2048


def test_explicit_values_override_defaults(tmp_path):
    cfg = Config.load(
        write_config(
            tmp_path,
            {
                "api": {"base_url": "http://10.0.0.5:9000/", "timeout": 30, "retries": 3},
                "control": {"port": 9999},
                "chat": {"adapter": "  NTCHAT ", "wake_words": ["鱼", " 小助手 ", ""]},
                "proactive": {"quiet_hours": ["22:00", "07:30"]},
                "logging": {"level": "debug"},
            },
        )
    )

    assert cfg.api.base_url == "http://10.0.0.5:9000/"
    assert cfg.api.chat_url == "http://10.0.0.5:9000/chat"
    assert cfg.api.timeout == 30.0
    assert cfg.api.retries == 3
    assert cfg.control.port == 9999
    assert cfg.chat.adapter == "ntchat"  # __post_init__ 去空格+小写
    assert cfg.chat.wake_words == ("鱼", "小助手")  # 空白项被丢掉
    assert cfg.proactive.quiet_start == 22 * 60
    assert cfg.proactive.quiet_end == 7 * 60 + 30
    assert cfg.logging.level == "DEBUG"


# --------------------------------------------------------------- 2. 路径不存在

def test_missing_file_raises_config_error(tmp_path):
    missing = tmp_path / "nope.json"
    with pytest.raises(ConfigError) as excinfo:
        Config.load(missing)
    assert "nope.json" in str(excinfo.value)


# --------------------------------------------------------------- 3. 非法 JSON

def test_invalid_json_raises_config_error(tmp_path):
    path = write_config(tmp_path, '{"api": {"base_url": "http://x", }')  # 尾部逗号
    with pytest.raises(ConfigError):
        Config.load(path)


def test_root_must_be_object(tmp_path):
    path = write_config(tmp_path, [1, 2, 3])
    with pytest.raises(ConfigError):
        Config.load(path)


# --------------------------------------------------------------- 4. 类型错误

@pytest.mark.parametrize(
    "payload, field_hint",
    [
        ({"control": {"port": "abc"}}, "control.port"),
        ({"control": {"port": 70000}}, "control.port"),
        ({"api": {"timeout": "很快"}}, "api.timeout"),
        ({"api": {"base_url": 123}}, "api.base_url"),
        ({"chat": {"max_reply_chars": "很多"}}, "chat.max_reply_chars"),
        ({"chat": {"wake_words": ["ok", 1]}}, "chat.wake_words"),
        ({"chat": {"user_id_strategy": "random"}}, "chat.user_id_strategy"),
        ({"logging": {"level": "LOUD"}}, "logging.level"),
        ({"stickers": {"max_kb": 10}}, "stickers.max_kb"),
        ({"control": {"host": "0.0.0.0"}}, "control.host"),
    ],
)
def test_type_and_range_errors_name_the_field(tmp_path, payload, field_hint):
    payload = {"api": {"base_url": "http://127.0.0.1:8000"}, **payload}
    with pytest.raises(ConfigError) as excinfo:
        Config.load(write_config(tmp_path, payload))
    assert field_hint in str(excinfo.value)


def test_api_base_url_must_be_http(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        Config.load(write_config(tmp_path, {"api": {"base_url": "ftp://example.com"}}))
    assert "api.base_url" in str(excinfo.value)


# --------------------------------------------------------------- 5. 未知字段

def test_unknown_field_warns_but_loads(tmp_path):
    cfg = Config.load(
        write_config(
            tmp_path,
            {
                "api": {"base_url": "http://127.0.0.1:8000"},
                "chat": {"bogus_key": 1, "max_queue": 10},
            },
        )
    )

    assert cfg.chat.max_queue == 10
    assert len(cfg.warnings) == 1
    assert "chat.bogus_key" in cfg.warnings[0]


def test_unknown_fields_collected_across_sections(tmp_path):
    cfg = Config.load(
        write_config(
            tmp_path,
            {"api": {"base_url": "http://127.0.0.1:8000"}, "control": {"prot": 1234}},
        )
    )
    assert any("control.prot" in w for w in cfg.warnings)


def test_known_field_is_not_a_warning(tmp_path):
    cfg = Config.load(write_config(tmp_path, {"api": {"base_url": "http://127.0.0.1:8000", "token": "t"}}))
    assert cfg.warnings == []


# --------------------------------------------------------------- 已知缺陷

def test_empty_config_uses_defaults(tmp_path):
    cfg = Config.load(write_config(tmp_path, {}))
    assert cfg.api.base_url == "http://127.0.0.1:8000"
    assert cfg.control.port == 8765


def test_string_defaults_are_real_strings(tmp_path):
    cfg = Config.load(write_config(tmp_path, MINIMAL))
    assert isinstance(cfg.media.placeholder_emoji, str)
    assert isinstance(cfg.vision.prompt, str)
