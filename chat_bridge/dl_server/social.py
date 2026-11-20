"""小鱼的"认人 / 别名"逻辑。

2026-11-16 从 dl_api.py 拆出，认人/别名这一块单独一个文件。
逐字搬运：除 import、以及 __file__ 指向 data/ 的路径修正外，逻辑一行没改。
注意本文件在 dl_server/ 下，所以 data/ 路径一律是 Path(__file__).resolve().parent.parent。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("dl_api.social")

# owner_sender_names() 要问"谁是她主人"，那份逻辑（读抖音许可名单 + MAIN_USER_ID 兜底）
# 仍在 dl_api.py。不反向 import dl_api（会循环），改成启动时注入。
_OWNER_LOADER: Callable[[], tuple[str, str]] | None = None


def set_runtime(load_owner: Callable[[], tuple[str, str]] | None = None) -> None:
    """dl_api.py 启动处调用一次：把它的 load_owner 注入进来。"""
    global _OWNER_LOADER
    _OWNER_LOADER = load_owner


def load_owner() -> tuple[str, str]:
    """没注入时的兜底：主人就是爸爸（和 dl_api.load_owner 的兜底一致）。"""
    if _OWNER_LOADER is not None:
        return _OWNER_LOADER()
    return "", "爸爸"

# IM群里的备注名 -> 她该怎么认这个人（爸爸=爸爸）。改了文件按 mtime 自动重载。
_ALIAS_CACHE: dict[str, Any] = {"mtime": -1.0, "aliases": {}, "hint": ""}


def _chat_aliases() -> tuple[dict[str, str], str]:
    path = Path(__file__).resolve().parent.parent / "data" / "chat_aliases.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}, ""
    if _ALIAS_CACHE["mtime"] != mtime:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            aliases = data.get("aliases") or {}
            _ALIAS_CACHE["aliases"] = {str(k): str(v) for k, v in aliases.items() if str(k).strip()}
            _ALIAS_CACHE["hint"] = str(data.get("ask_hint") or "").strip()
            _ALIAS_CACHE["mtime"] = mtime
        except Exception:  # noqa: BLE001
            log.warning("IM别名文件读不了，先跳过")
    return _ALIAS_CACHE["aliases"], _ALIAS_CACHE["hint"]


# 群里有人自报"我是谁"、"X 就是 Y"：记下来，别过几分钟又忘。
ALIAS_PATTERN = re.compile(
    r"([\u4e00-\u9fa5A-Za-z0-9_·]{1,12})\s*(?:就是|其实是|其实是|是|=)\s*([\u4e00-\u9fa5A-Za-z0-9_·]{1,12})"
)


# 这些词是"他在问"，不是"他告诉我这人是谁"
_ALIAS_QUESTION_WORDS = ("谁", "什么", "哪", "哪個", "哪个", "吗", "呢", "?", "？", "不知道", "不确定")


def remember_alias(text: str, speaker: str) -> str:
    """从一句话里认出"阿哦 是 熟人C"这种身份对照，写进 chat_aliases.json。

    2026-11-04 爸爸："她怎么一会儿就不记得我在群里面说的了，我刚说了阿哦是熟人C。"
    """
    value = str(text or "").strip()
    who = str(speaker or "").strip()
    if not value:
        return ""
    aliases, _hint = _chat_aliases()
    known_people = set(aliases.values()) | {"爸爸", "熟人C", "熟人B", "熟人D"}
    found: list[tuple[str, str]] = []
    for match in ALIAS_PATTERN.finditer(value):
        left, right = match.group(1).strip(), match.group(2).strip()
        if left == right:
            continue
        # 一边是当前说话的人（在自报），或者一边是已经登记过的人名
        # 2026-11-14 修：代词和"我是…"这类句子会把垃圾别名写进来
        _stop = {"我", "你", "他", "她", "它", "这", "那", "俺", "咱", "我们", "你们", "他们",
                 "说", "不是", "认真", "真的"}
        if left in _stop or right in _stop:
            continue
        if left == who or right == who or left in known_people or right in known_people or left in aliases:
            alias, real = (left, right) if left == who or left not in known_people else (right, left)
            # "爸爸/主人"这种身份只能由主人自己登记，别人说"X是爸爸"不认
            if real in ("爸爸", "主人") and who not in owner_sender_names():
                continue
            if alias and real and alias != real and aliases.get(alias) != real:
                # 2026-11-05：她问了句"阿哦是谁"，结果被记成「阿哦=谁」。
                # 疑问词不是人名；过长的也不是（那是半句话）。
                real = real.strip(" 的就叫是、，,。")
                alias = alias.strip(" 的就叫是、，,。")
                if not real or not alias or any(word in real for word in _ALIAS_QUESTION_WORDS):
                    continue
                if len(real) > 16 or len(alias) > 12:
                    continue
                found.append((alias, real))
    if not found:
        return ""
    path = Path(__file__).resolve().parent.parent / "data" / "chat_aliases.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:  # noqa: BLE001
        data = {}
    bucket = data.setdefault("aliases", {})
    changed = False
    for alias, real in found:
        if bucket.get(alias) != real:
            bucket[alias] = real
            changed = True
    if not changed:
        return ""
    try:
        data["aliases"] = bucket
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        _ALIAS_CACHE["mtime"] = 0.0  # 让下次读文件时重载
    except OSError:
        return ""
    return "；".join(f"「{a}」={r}" for a, r in found)


def contact_names() -> dict[str, str]:
    """会话 id → 当前备注/昵称（你在手机上改的备注会同步到这里）。"""
    try:
        path = Path.home() / ".dsh" / "profiles" / "desktop" / "node_modules" / "dsh-douyin-browser-local" / "state" / "chat-policy.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(item.get("id")): str(item.get("name") or "") for item in data.get("allowlist") or [] if item.get("id")}
    except Exception:  # noqa: BLE001
        return {}


# IM 里所有会话都走同一个 user_id（她只有一份脑），所以"这是谁"只能靠说话人的名字认。
# 名字 → 联系人卡片 id 的映射，按文件修改时间缓存。
_NAME_CACHE: dict[str, Any] = {"mtime": 0.0, "by_name": {}}


ALIAS_FILE = Path(__file__).resolve().parent.parent / "data" / "aliases.json"


def sender_aliases() -> dict[str, str]:
    """IM名/备注 → 本名。neikos 就是爸爸（爸爸）。"""
    try:
        data = json.loads(ALIAS_FILE.read_text(encoding="utf-8"))
        return {str(k).strip(): str(v).strip() for k, v in (data or {}).items() if str(k).strip()}
    except Exception:  # noqa: BLE001
        return {}


def canonical_sender(sender: str) -> str:
    name = str(sender or "").strip()
    return sender_aliases().get(name, name)


def contact_id_for_sender(sender: str) -> str:
    try:
        path = Path(__file__).resolve().parent.parent / "data" / "contact_profiles.json"
        mtime = path.stat().st_mtime
        if _NAME_CACHE["mtime"] != mtime:
            data = json.loads(path.read_text(encoding="utf-8"))
            by_name = {}
            for cid, item in (data or {}).items():
                name = str((item or {}).get("name") or "").strip()
                if name:
                    by_name[name] = str(cid)
            _NAME_CACHE.update({"mtime": mtime, "by_name": by_name})
        for candidate in (str(sender or "").strip(), canonical_sender(sender)):
            if candidate and candidate in _NAME_CACHE["by_name"]:
                return _NAME_CACHE["by_name"][candidate]
        return ""
    except Exception:  # noqa: BLE001
        return ""


def owner_sender_names() -> set[str]:
    owner_id, owner_name = load_owner()
    names = {"爸爸", "爸爸"}
    if owner_name:
        names.add(owner_name.strip())
    for cid, name in contact_names().items():
        if cid == owner_id and name:
            names.add(name.strip())
    # IM 里他用的是 neikos 这种名字；别名表里指到爸爸的都算他
    for alias, canonical in sender_aliases().items():
        if canonical in names:
            names.add(alias)
    return {name for name in names if name}


DIR_ALIASES = {
    "左上": 1, "上中": 2, "右上": 3, "左中": 4, "中间": 5, "正中": 5, "中心": 5,
    "右中": 6, "左下": 7, "下中": 8, "右下": 9,
}


GUEST_ALIASES = Path(__file__).resolve().parent.parent / "data" / "guest_aliases.json"


_ALIAS_PATTERN = re.compile(
    r"(?:我(?:的名字)?(?:是|叫)|我名字是|我的名字是|叫我|你可以叫我|我是)\s*([\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9·]{1,9})"
)


def _load_guest_aliases() -> dict[str, str]:
    try:
        data = json.loads(GUEST_ALIASES.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def remember_guest_alias(sender: str, message: str) -> str:
    """他在自我介绍的话里报了名字，就记住（返回记下的名字，没记就空串）。"""
    who = str(sender or "").strip()
    sample = str(message or "").strip()
    if not who or not sample:
        return ""
    match = _ALIAS_PATTERN.search(sample)
    if not match:
        return ""
    stated = match.group(1).strip()
    if len(stated) < 2 or stated == who:
        return ""
    aliases = _load_guest_aliases()
    if aliases.get(who) == stated:
        return stated
    aliases[who] = stated
    try:
        GUEST_ALIASES.parent.mkdir(parents=True, exist_ok=True)
        GUEST_ALIASES.write_text(json.dumps(aliases, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log.info("记下访客自我介绍：%s -> %s", who, stated)
    except OSError:
        return ""
    return stated
