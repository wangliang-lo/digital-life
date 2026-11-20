#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""IM ⇄ 数字生命 中间件入口。

    python run_bridge.py run --config config.json      # 启动
    python run_bridge.py doctor --config config.json   # 环境自检（强烈建议先跑）
    python run_bridge.py status --config config.json   # 查看运行状态
    python run_bridge.py send "文件传输助手" "你好"     # 手动发一条
    python run_bridge.py proactive --config config.json  # 手动触发一次主动聊天
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import socket
import struct
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from bridge.adapters import AdapterError, IMVersionError  # noqa: E402
from bridge.app import BridgeApp  # noqa: E402
from bridge.client import DigitalLifeClient  # noqa: E402
from bridge.config import Config, ConfigError  # noqa: E402
from bridge.logs import get_logger, setup_logging  # noqa: E402
from bridge.stickers import StickerStore  # noqa: E402

NTCHAT_IM_VERSION = "3.6.0.18"
OK, WARN, FAIL, INFO = "[ OK ]", "[WARN]", "[FAIL]", "[INFO]"


# =============================================================================
# run
# =============================================================================

def cmd_run(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    if args.adapter:
        cfg.chat.adapter = args.adapter
    if args.log_level:
        cfg.logging.level = args.log_level.upper()

    log_path = cfg.log_path()
    if log_path:
        cfg.logging.file = str(log_path)
    logger = setup_logging(cfg.logging)
    for warning in cfg.warnings:
        logger.warning(warning)

    app = BridgeApp(cfg)
    try:
        app.start()
    except IMVersionError as exc:
        logger.error("%s", exc)
        logger.error("→ 运行 `python run_bridge.py doctor` 查看版本详情")
        return 2
    except AdapterError as exc:
        logger.error("适配器启动失败：%s", exc)
        return 2
    except ConfigError as exc:
        logger.error("配置错误：%s", exc)
        return 2

    if app.control:
        logger.info("控制接口：%s", app.control.url)
        logger.info("  GET  %s/status", app.control.url)
        logger.info('  POST %s/send   {"target":"文件传输助手","text":"你好"}', app.control.url)
        logger.info("  POST %s/proactive", app.control.url)

    try:
        while not app.stop_event.wait(0.5):
            pass
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C")
    finally:
        app.stop()
    return 0


# =============================================================================
# doctor
# =============================================================================

def _check_python() -> tuple[bool, str]:
    version = sys.version_info
    bits = struct.calcsize("P") * 8
    detail = f"Python {version.major}.{version.minor}.{version.micro} ({bits} 位)  {sys.executable}"
    ok = (version.major, version.minor) in ((3, 10), (3, 11))
    if not ok:
        detail += "  ← ntchat 只有 cp310/cp311 的 wheel"
    return ok, detail


def _check_ntchat() -> tuple[bool, str]:
    try:
        import ntchat  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return False, f"未安装或导入失败：{type(exc).__name__}: {exc}"

    version = getattr(ntchat, "__version__", "?")
    detail = f"ntchat {version}"
    try:
        installed = ntchat.get_install_chat_version()
    except Exception as exc:  # noqa: BLE001
        installed = f"读取失败({type(exc).__name__})"
    detail += f"，检测到IM版本 {installed}"
    return installed == NTCHAT_IM_VERSION, detail


def _check_chat_install() -> tuple[bool, str]:
    candidates = [
        Path(r"C:\Program Files (x86)\Tencent\IM\IM.exe"),
        Path(r"C:\Program Files\Tencent\IM\IM.exe"),
    ]
    found = [str(path) for path in candidates if path.exists()]
    if found:
        return True, "找到IM 3.x：" + "；".join(found)

    v4 = [
        Path(r"C:\Program Files\Tencent\weixin\Weixin.exe"),
        Path(r"C:\Program Files\Tencent\IM\Weixin.exe"),
    ]
    found_v4 = [str(path) for path in v4 if path.exists()]
    if found_v4:
        return False, "只找到IM 4.x：" + "；".join(found_v4) + "  ← ntchat 不支持"
    return False, "没在常见路径找到IM 3.6.0.18"


def _check_registry() -> tuple[bool, str]:
    try:
        import winreg  # noqa: PLC0415
    except ImportError:
        return False, "非 Windows，跳过"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Tencent\IM") as key:
            version, _ = winreg.QueryValueEx(key, "Version")
            try:
                install, _ = winreg.QueryValueEx(key, "InstallPath")
            except OSError:
                install = "?"
    except OSError:
        return False, r"HKCU\SOFTWARE\Tencent\IM 不存在（没装 3.x，或装在别的用户下）"
    return version == 0x63060012, f"Version=0x{int(version):08X}  InstallPath={install}"


def _check_api(cfg: Config) -> tuple[bool, str]:
    ok, detail = DigitalLifeClient(cfg.api).ping()
    return ok, f"{cfg.api.base_url}  {detail}"


def _check_asr(cfg: Config) -> tuple[bool, str]:
    if cfg.asr.provider == "none":
        return True, "已关闭：语音只回占位符"
    if cfg.asr.provider == "openai":
        return bool(cfg.asr.api_key), f"远端 ASR {cfg.asr.base_url}（key {'已配置' if cfg.asr.api_key else '缺失'}）"
    try:
        import faster_whisper  # noqa: PLC0415
    except ImportError:
        return False, "未安装 faster-whisper（pip install faster-whisper）"
    version = getattr(faster_whisper, "__version__", "?")
    return True, f"faster-whisper {version}，本地模型 {cfg.asr.local_model}（首次使用会下载）"


def _check_vision(cfg: Config) -> tuple[bool, str]:
    if cfg.vision.provider == "none":
        return True, "已关闭：图片只回占位符"
    if cfg.vision.provider == "openai":
        return bool(cfg.vision.api_key), f"远端视觉 {cfg.vision.model}（key {'已配置' if cfg.vision.api_key else '缺失'}）"

    url = cfg.vision.base_url.rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return False, f"连不上 Ollama（{cfg.vision.base_url}）：{exc}"

    names = [str(item.get("name", "")) for item in data.get("models", [])]
    wanted = cfg.vision.model
    base = wanted.split(":")[0]
    hit = [name for name in names if name == wanted or name.split(":")[0] == base]
    if hit:
        return True, f"Ollama {wanted} 已就绪"
    return False, f"Ollama 里没有 {wanted}；现有：{', '.join(names[:5]) or '（空）'}"


def _check_version_spoof(cfg: Config) -> tuple[bool, str]:
    if not cfg.chat.version_spoof_enabled:
        return True, "未开启（弹「版本过低」时用 patch-version 或打开它）"
    try:
        import pymem  # noqa: F401,PLC0415
    except ImportError:
        return False, "已开启，但没装 pymem：pip install pymem"
    return True, f"已开启，目标版本 {cfg.chat.version_spoof_target}"


def _check_stickers(cfg: Config) -> tuple[bool, str]:
    if not cfg.stickers.enabled:
        return True, "已关闭"
    root = cfg.resolve_dir(cfg.stickers.dir)
    if not root.exists():
        return True, f"还没有表情（目录未创建）：{root}"
    store = StickerStore(root, max_kb=cfg.stickers.max_kb)
    store.load()
    tags = store.tag_counts()
    summary = "、".join(f"{tag}×{n}" for tag, n in sorted(tags.items(), key=lambda kv: -kv[1])[:5])
    return True, f"{store.count()} 个表情（{summary or '空'}）{root}"


def _check_control_port(cfg: Config) -> tuple[bool, str]:
    host, port = cfg.control.host, cfg.control.port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        if sock.connect_ex(("127.0.0.1" if host in ("localhost", "::1") else host, port)) == 0:
            return True, f"{host}:{port} 已被占用（可能中间件已经在运行）"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1" if host in ("localhost", "::1") else host, port))
        return True, f"{host}:{port} 可用"
    except OSError as exc:
        return False, f"{host}:{port} 无法绑定：{exc}"


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    print("=" * 78)
    print("IM ⇄ 数字生命 中间件 · 环境自检")
    print("=" * 78)

    checks: list[tuple[str, bool, str, str]] = [
        ("Python", *_check_python(), "ntchat 只提供 cp310/cp311 wheel，请用 3.10/3.11 64 位"),
        ("ntchat", *_check_ntchat(), "pip install ntchat==0.1.20"),
        ("IM安装", *_check_chat_install(), "ntchat 只支持 IM 3.6.0.18"),
        ("注册表", *_check_registry(), "装 3.6.0.18 后注册表应为 0x63060012"),
        ("版本伪装", *_check_version_spoof(cfg), "pip install pymem；IM打开后 python run_bridge.py patch-version"),
        ("数字生命 API", *_check_api(cfg), "先启动 python dl_api.py"),
        ("语音识别", *_check_asr(cfg), "pip install faster-whisper，或把 asr.provider 设成 none"),
        ("图片理解", *_check_vision(cfg), f"ollama pull {cfg.vision.model}"),
        ("表情包", *_check_stickers(cfg), "把 GIF 放进 stickers/files 或聊天里发一张"),
        ("控制端口", *_check_control_port(cfg), "改 control.port"),
    ]

    hard_fail = False
    for name, ok, detail, hint in checks:
        marker = OK if ok else WARN
        print(f"{marker} {name:<12} {detail}")
        if not ok:
            print(f"       └─ 建议：{hint}")
            if name in ("Python", "ntchat", "IM安装"):
                hard_fail = True

    print("-" * 78)
    if hard_fail:
        print(
            "结论：当前环境无法直接运行 ntchat 适配器。\n"
            "  IM 4.2.0.7 是重写后的新客户端（Weixin.exe / xchat），ntchat 注入不进去。\n"
            "  二选一：\n"
            "    A. 保留 ntchat：加装IM 3.6.0.18 + Python 3.10/3.11 64 位（推荐，hook 稳定）\n"
            "    B. 留在IM 4.x：改用 UI 自动化方案（wxauto），需要额外适配器，稳定性差一档\n"
            "  详见 README.md「IM版本」一节。"
        )
        return 1

    print("结论：环境看起来没问题，可以 `python run_bridge.py run --config config.json`。")
    return 0


# =============================================================================
# 控制口小工具
# =============================================================================

def _control_request(cfg: Config, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    url = f"http://127.0.0.1:{cfg.control.port}{path}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json; charset=utf-8")
    if cfg.control.token:
        request.add_header("X-Bridge-Token", cfg.control.token)
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")
        raise SystemExit(f"控制接口返回 HTTP {exc.code}：{body}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"连不上控制接口 {url}：{exc.reason}\n中间件是不是没启动？"
        ) from exc


def cmd_status(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    print(json.dumps(_control_request(cfg, "GET", "/status"), ensure_ascii=False, indent=2))
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    result = _control_request(cfg, "POST", "/send", {"target": args.target, "text": args.text})
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def cmd_proactive(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    result = _control_request(cfg, "POST", "/proactive", {})
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("sent") else 1


def cmd_patch_version(args: argparse.Namespace) -> int:
    from bridge.version_patch import VersionPatcher  # noqa: PLC0415

    try:
        patcher = VersionPatcher(source=args.source, target=args.target)
    except ValueError as exc:
        print(f"版本号不合法：{exc}", file=sys.stderr)
        return 2

    print(f"正在给 IM.exe 打补丁：{args.source} -> {args.target} ……")
    result = patcher.patch(timeout=args.timeout)
    print(json.dumps(dataclasses.asdict(result), ensure_ascii=False, indent=2))
    if result.ok:
        print("\n现在回到IM窗口扫码登录；登录成功后启动中间件：")
        print("  python run_bridge.py run --config config.json")
        return 0
    print("\n没成功。常见原因：")
    print("  * IM还没打开（先启动 3.6.0.18，停在扫码界面）")
    print("  * 装的根本不是 3.6.0.18（偏移量对不上）")
    print("  * 没装 pymem：pip install pymem")
    return 1


def cmd_stickers(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    store = StickerStore(cfg.resolve_dir(cfg.stickers.dir), max_kb=cfg.stickers.max_kb)
    store.load()

    if args.action == "list":
        items = store.list()
        if not items:
            print(f"表情包是空的（{store.root}）")
            return 0
        for sticker in items:
            print(f"{sticker.id}  {'/'.join(sticker.tags):<18} 用了{sticker.uses}次  {sticker.file}")
        print(f"\n共 {len(items)} 个，目录 {store.root}")
        return 0

    if args.action == "add":
        tags = [tag for tag in (args.tags or "未分类").split(",") if tag.strip()]
        sticker = store.add_file(args.path, tags, source="cli")
        if sticker is None:
            print("导入失败：文件不存在、格式不支持或体积过大")
            return 1
        print(f"已导入 {sticker.id}（{'/'.join(sticker.tags)}）")
        return 0

    if args.action == "import":
        tags = [tag for tag in (args.tags or "").split(",") if tag.strip()]
        count = store.import_dir(args.path, tags)
        print(f"从 {args.path} 导入了 {count} 个表情，现在共有 {store.count()} 个")
        return 0

    if args.action == "remove":
        ok = store.remove(args.sticker_id)
        print("已删除" if ok else f"没找到：{args.sticker_id}")
        return 0 if ok else 1

    print(f"未知操作：{args.action}")
    return 1


# =============================================================================
# main
# =============================================================================

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=None, help="配置文件路径（默认 config.json）")
    common.add_argument("--log-level", default=None)

    parser = argparse.ArgumentParser(description="IM ⇄ 数字生命 中间件")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", parents=[common], help="启动中间件")
    run.add_argument("--adapter", choices=["ntchat", "mock"], default=None)
    run.set_defaults(func=cmd_run)

    doctor = sub.add_parser("doctor", parents=[common], help="环境自检")
    doctor.set_defaults(func=cmd_doctor)

    status = sub.add_parser("status", parents=[common], help="查看状态")
    status.set_defaults(func=cmd_status)

    send = sub.add_parser("send", parents=[common], help="手动发送一条消息")
    send.add_argument("target", help="wxid / 群名 / 文件传输助手")
    send.add_argument("text")
    send.set_defaults(func=cmd_send)

    pro = sub.add_parser("proactive", parents=[common], help="手动触发一次主动聊天")
    pro.set_defaults(func=cmd_proactive)

    patch = sub.add_parser("patch-version", help="绕过「当前IM版本过低」（改内存版本号）")
    patch.add_argument("--source", default="3.6.0.18")
    patch.add_argument("--target", default="3.9.12.15")
    patch.add_argument("--timeout", type=float, default=15.0)
    patch.set_defaults(func=cmd_patch_version)

    stickers = sub.add_parser("stickers", help="管理表情包")
    sticker_sub = stickers.add_subparsers(dest="action", required=True)
    s_list = sticker_sub.add_parser("list", parents=[common], help="列出现有表情")
    s_list.set_defaults(func=cmd_stickers)
    s_add = sticker_sub.add_parser("add", parents=[common], help="添加一个文件")
    s_add.add_argument("path")
    s_add.add_argument("--tags", default="", help="逗号分隔，例如 开心,可爱")
    s_add.set_defaults(func=cmd_stickers)
    s_import = sticker_sub.add_parser("import", parents=[common], help="导入整个目录")
    s_import.add_argument("path")
    s_import.add_argument("--tags", default="")
    s_import.set_defaults(func=cmd_stickers)
    s_remove = sticker_sub.add_parser("remove", parents=[common], help="按 id 删除")
    s_remove.add_argument("sticker_id")
    s_remove.set_defaults(func=cmd_stickers)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1
    if getattr(args, "config", None) is None and Path("config.json").exists():
        args.config = "config.json"
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except AdapterError as exc:
        print(f"适配器错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
