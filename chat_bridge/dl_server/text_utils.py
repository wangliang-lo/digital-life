"""小鱼的文本处理纯函数（从 dl_api.py 拆出来，逐字搬运，零行为变化）。

不碰文件、不碰网络、不碰全局状态，只做字符串变换。
"""
from __future__ import annotations

import random
import re
from typing import Any

# 开头的「（我认真想了想）」这类舞台提示：模型偶尔会写，本地兜底模板里全是
_STAGE_DIRECTION = re.compile(r"^\s*[（(]([^）)]{0,60})[）)]\s*")

_STAGE_WORDS = (
    "想了想", "沉默", "呼吸", "停顿", "脑子", "心里", "回忆", "认真",
    "走神", "深吸", "苦笑", "叹气", "愣", "犹豫", "斟酌", "压着",
)

_WEEKDAYS = "一二三四五六日"

_BARE_ACK = re.compile(r"^(嗯+|哦+|噢+|好+|行|可以|收到|ok|okay|哈哈+|哈+|知道了|好吧)[。.!！~…\s]*$", re.I)

MEDIA_PREAMBLE_PREFIX = "以下图片和语音转写只属于"


def clean_partial_tags(text: str) -> str:
    """清掉行尾没闭合的标签残渣（她难过时常见："…[STICKER: 难过"）。

    2026-11-16：从处理链路里抽出来，好让回归测试直接打它。
    """
    cleaned = re.sub(r"\[\s*[A-Z_]{3,}\s*[:：]?[^\]\n]*$", "", str(text or ""))
    return cleaned.strip()


def strip_stage_directions(text: str) -> str:
    """把开头的旁白式括号去掉——她要像真人说话，不是念剧本。"""
    cleaned = (text or "").strip()
    for _ in range(3):
        match = _STAGE_DIRECTION.match(cleaned)
        if not match or not any(word in match.group(1) for word in _STAGE_WORDS):
            break
        cleaned = cleaned[match.end():].lstrip()
    return cleaned or (text or "").strip()


def _is_bare_ack(text: str) -> bool:
    """只是应一声的短句（嗯/好/哦/哈哈）：可以不用回。"""
    value = (text or "").strip()
    return bool(value) and len(value) <= 6 and bool(_BARE_ACK.match(value))


def _ascii_word_char(ch: str) -> bool:
    """英文单词/数字的组成部分（这些不能从中间切断）。"""
    return bool(ch) and ch.isascii() and (ch.isalnum() or ch in "-_.'")


def shape_reply(
    bubbles: list[str],
    voice_parts: list[str],
    sing_parts: list[dict[str, str]],
    channel: str,
    roll: float | None = None,
) -> tuple[list[str], list[str], list[dict[str, str]]]:
    """一条回复只用一种表达方式：打字，或语音/哼歌。

    2026-11-03 爸爸："不要既写文字又发语音，单线一点，更像人。"
    IM发不出音频，那边仍然把语音折回文字（内容不能丢）。
    """
    text_bubbles = list(bubbles or [])
    voices = list(voice_parts or [])
    songs = list(sing_parts or [])
    if str(channel or "").startswith("chat"):
        if voices:
            text_bubbles = (text_bubbles + voices)[:6]
            voices = []
        if songs:
            text_bubbles = (text_bubbles + [str(item.get("text") or "") for item in songs])[:6]
            songs = []
        return text_bubbles, voices, songs
    # 2026-11-16 爸爸："要她想发语音就发，不强制，也不限制。"
    # 所以不再自动清掉文字；她没写文字时自然就只有语音。
    return text_bubbles, voices, songs
    return text_bubbles, voices, songs


def split_bubbles(text: str, limit: int = 120, max_parts: int = 10) -> list[str]:
    """按她自己的换行/|||分气泡，不再按标点硬拆成一句一条。"""
    text = (text or "").strip()
    if not text:
        return []
    chunks = [part.strip() for part in re.split(r"\s*(?:\|\|\||\n)\s*", text) if part.strip()]
    bubbles: list[str] = []
    for chunk in chunks:
        while len(chunk) > limit:
            window = chunk[:limit]
            # 优先在这些地方断开（句号类 > 逗号类），实在没有再硬切
            strong = max(window.rfind("。"), window.rfind("！"), window.rfind("？"))
            weak = max(window.rfind("，"), window.rfind("、"), window.rfind("；"),
                       window.rfind("："), window.rfind("…"))
            if strong >= 10:
                cut_at = strong + 1
            elif weak >= 10:
                cut_at = weak + 1
            else:
                # 英文/数字不能被切成 "Fla / sh"：从 limit 往回找空白或标点
                cut_at = limit
                for i in range(limit - 1, max(9, limit - 24), -1):
                    prev, here = chunk[i - 1], chunk[i]
                    if _ascii_word_char(prev) and _ascii_word_char(here):
                        continue  # 还在词中间，继续往回退
                    if here.isspace() or here in "，,、；;：:—…":
                        cut_at = i + 1
                        break
                    if not _ascii_word_char(here) and _ascii_word_char(prev):
                        cut_at = i + 1  # 刚好在英文词尾巴后
                        break
            bubbles.append(chunk[:cut_at].strip())
            chunk = chunk[cut_at:].strip()
        if chunk:
            bubbles.append(chunk)
    # 2026-11-09：以前直接 bubbles[:max_parts]，她写长了后面的段落会被整段丢掉，
    # 屏幕上就变成"我躺在……"没有下文。多出来的并到最后一条里，宁可长一点。
    if len(bubbles) > max_parts:
        head = bubbles[: max_parts - 1]
        head.append(" ".join(bubbles[max_parts - 1:]))
        bubbles = head
    return bubbles


def _part_of_day(hour: int) -> str:
    if hour < 5:
        return "凌晨"
    if hour < 8:
        return "清晨"
    if hour < 11:
        return "上午"
    if hour < 13:
        return "中午"
    if hour < 17:
        return "下午"
    if hour < 19:
        return "傍晚"
    if hour < 23:
        return "晚上"
    return "深夜"


def clean_incoming_text(text: str) -> str:
    """去掉插件附带的媒体说明与消息元数据，只留下对方真正说的话。"""
    value = str(text or "").strip()
    if not value:
        return ""
    if value.startswith(MEDIA_PREAMBLE_PREFIX):
        start = value.find("{")
        if start >= 0:
            end = value.rfind("}")
            value = value[end + 1:].strip() if end > start else ""
        else:
            value = ""
    if value.startswith("{") and value.endswith("}"):
        value = ""
    return value or "[对方发来一个表情或图片]"


def human_reply_delay_ms(reply: str, rng: random.Random | None = None) -> int:
    """IM上"真人回一条"大概要多久：先看一眼想一想，再一个字一个字打。

    以前的 0.25-1.1s + 客户端每字 90ms，短句不到 1 秒就冒出来，不像人。
    这里按字数估打字时间（约 7 字/秒），再加 1.5-3.2s 的"看+想"，
    偶尔（6%）走个神多停 2-4 秒。上限 15 秒，别让人以为她掉线。
    """
    rnd = rng or random
    text = str(reply or "")
    chars = len(re.sub(r"\s+", "", text))
    think = rnd.uniform(1.5, 3.2)
    typing = min(6.0, chars * 0.14)
    delay = think + typing
    if rnd.random() < 0.06:
        delay += rnd.uniform(2.0, 4.0)
    return int(max(0.0, min(delay, 15.0)) * 1000)


def _make_delay_fn(args: Any):
    low, high = sorted((max(0.0, args.delay_min), max(0.0, args.delay_max)))
    chance = max(0.0, min(1.0, args.delay_long_chance))
    long_low, long_high = sorted((max(low, args.delay_long_min), max(low, args.delay_long_max)))

    def pick() -> int:
        if high <= 0:
            return 0
        if chance and random.random() < chance:
            return int(random.uniform(long_low, long_high) * 1000)
        return int(random.uniform(low, high) * 1000)

    return pick
