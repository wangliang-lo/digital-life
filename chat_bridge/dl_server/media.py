"""表情包打标相关的纯函数（图片下载 + 标签/文案清洗）。

2026-11-16 从 dl_api.py 拆出；这里只放纯字符串/图片下载相关的函数。
不碰数据库、不碰全局状态；sticker_label / MediaEnricher 仍留在 dl_api.py。
"""
from __future__ import annotations

import re
import urllib.error
import urllib.request


def _fetch_image(url: str, timeout: float = 15.0) -> bytes:
    """下载图片（抖音 CDN 需要带 referer）。"""
    if not url.startswith(("http://", "https://")):
        return b""
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://www.douyin.com/",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(8 * 1024 * 1024 + 1)
    except (urllib.error.URLError, OSError, ValueError):
        return b""
    return data if len(data) <= 8 * 1024 * 1024 else b""


_TAG_HINTS = {
    "开心": ("笑", "开心", "高兴", "乐", "哈哈"),
    "委屈": ("哭", "委屈", "难过", "泪"),
    "生气": ("生气", "怒", "凶", "火"),
    "无语": ("无语", "尴尬", "翻白眼", "汗"),
    "可爱": ("可爱", "萌", "猫", "狗", "兔"),
    "搞笑": ("搞笑", "滑稽", "丑", "魔性"),
}


def _tags_from_description(desc: str) -> list[str]:
    tags = [tag for tag, words in _TAG_HINTS.items() if any(word in desc for word in words)]
    return tags or ["未分类"]


# 表情包的标签：只用来在「候选」里按对方的话挑图，不需要很精确。
_STICKER_TAG_WORDS = (
    ("开心", ("笑", "开心", "高兴", "可爱", "萌", "喜欢", "爱心", "比心", "赞", "棒", "嗨", "哈")),
    ("无语", ("无语", "汗", "尴尬", "沉默", "白眼", "扶额", "翻白眼")),
    ("生气", ("生气", "怒", "凶", "吼", "炸", "急")),
    ("难过", ("哭", "难过", "委屈", "伤心", "泪", "叹气", "可怜")),
    ("害羞", ("害羞", "脸红", "遮脸", "捂脸", "捂嘴")),
    ("困惑", ("问号", "疑惑", "懵", "困惑", "茫然", "挠头")),
    ("狗", ("狗", "犬", "柴犬", "哈士奇", "金毛", "柯基")),
    ("猫", ("猫", "喵")),
    ("动物", ("兔", "熊", "猪", "鸟", "鱼", "仓鼠", "熊猫", "鸭", "鹅", "羊")),
    ("人物", ("女孩", "男孩", "女生", "男生", "人", "脸")),
)


def _sticker_tags(text: str) -> list[str]:
    value = str(text or "")
    tags = [tag for tag, words in _STICKER_TAG_WORDS if any(word in value for word in words)]
    return tags[:4] or ["表情"]


_STICKER_EMOTIONS = (
    "开心", "难过", "生气", "委屈", "无语", "困惑", "害羞", "惊讶", "可爱",
    "喜欢", "困", "得意", "撒娇", "鼓励", "拒绝", "吐槽", "日常",
)


# 模型偶尔会把提示词里的句子当描述抄回来，这类"描述"当没看懂处理。
_STICKER_ECHO = ("若是", "或鲸鱼", "或蓝头发", "否则写", "四行", "画面里若是", "30 字以内")


def _repetitive(text: str) -> bool:
    """小模型有时会输出「一个蓝色方块内，一个蓝色圆圈内，……」这种复读。"""
    value = str(text or "")
    if len(value) < 12:
        return False
    return any(value.count(value[i : i + 4]) >= 3 for i in range(len(value) - 3))


def _clean_scene(text: str) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip(" ，。\"'「」")
    value = re.sub(r"^(这张?(表情包)?(画的|里|上是)?[:：]?)", "", value).strip()
    if not value or value in ("无", "没有", "看不出", "无法判断"):
        return ""
    if any(echo in value for echo in _STICKER_ECHO) or _repetitive(value):
        return ""
    return value[:40]


def _clean_caption(text: str) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip(" ，。")
    value = re.sub(r"^(图(上|中)?(的)?文字(是|为)?[:：]?)", "", value).strip(" ：「」\"'")
    if value.rstrip("。.") in ("无", "沒有", "没有", "none", "None", "-", "—", ""):
        return ""
    # 太短或只有标点/符号的，不当成文案（宁可不写，也别让她以为图上真写着那个）。
    core = re.sub(r"[\W_]+", "", value)
    if len(core) < 2:
        return ""
    if len(value) > 40 or any(echo in value for echo in _STICKER_ECHO) or _repetitive(value):
        return ""
    return value[:40]


def _sticker_tags_from(scene: str, caption: str, raw: str = "") -> list[str]:
    haystack = " ".join([str(scene or ""), str(caption or ""), str(raw or "")])
    emotions = [word for word in _STICKER_EMOTIONS if word in haystack]
    if not emotions:
        emotions = [tag for tag in _sticker_tags(haystack) if tag not in ("表情", "动物", "人物")][:3]
    return emotions[:3]
