"""不依赖实例变量的纯函数。"""
from __future__ import annotations

import random
import re
from datetime import datetime
from typing import Any, Iterable

from config import (
    BASE_TEMPERATURE, BASE_TOP_P, DREAM_NIGHT_END, DREAM_NIGHT_START,
    DREAM_NEGATIVE_WORDS, DREAM_POSITIVE_WORDS, EMOTION_MAX, EMOTION_MIN,
    EVENT_RULES, HORMONE_BASELINE, HORMONE_HALF_LIFE_HOURS, HOSTILE_MARKERS,
    IMPULSE_GATE, NIGHT_END_HOUR, NIGHT_START_HOUR, OBSESSION_RELEASE_WORDS,
    OBSESSION_SUMMARY_STOPWORDS, OBSESSION_WEIGHT_MULTIPLIER, RELATIONSHIP_STAGES,
    SOFT_TEASE_PHRASES, STRONG_KEYWORDS, SUMMARY_INJECTION_MARKERS,
    TEMPERATURE_MAX, TEMPERATURE_MIN, TOP_P_MAX, TOP_P_MIN, WEAK_KEYWORDS,
)

def clamp(value: float, low: float = EMOTION_MIN, high: float = EMOTION_MAX) -> float:
    """把数值夹在 [low, high] 内。"""
    return max(low, min(high, float(value)))

def is_night(hour: int) -> bool:
    """情绪夜间窗口：22:00 ~ 次日 02:00。"""
    return hour >= NIGHT_START_HOUR or hour < NIGHT_END_HOUR

def is_dream_night(hour: int) -> bool:
    """梦境夜间窗口：22:00 ~ 次日 06:00。"""
    return hour >= DREAM_NIGHT_START or hour < DREAM_NIGHT_END

def short_text(text: str, limit: int = 30) -> str:
    """压缩空白并截断，用于书皮、终端与 Prompt。"""
    clean = re.sub(r"\s+", " ", str(text)).strip()
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 1)] + "…"

def dt_to_str(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")

def str_to_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)

def decay_hormone(value: float, elapsed_hours: float) -> float:
    """把 0-1 的激素按半衰期向 0.5 衰减；间隔为 0 时保留原值。"""
    if elapsed_hours <= 0:
        return clamp(value, 0.0, 1.0)
    factor = 0.5 ** (float(elapsed_hours) / HORMONE_HALF_LIFE_HOURS)
    return clamp(HORMONE_BASELINE + (float(value) - HORMONE_BASELINE) * factor, 0.0, 1.0)

def compute_hormones(
    joy: float,
    hurt: float,
    impulse: float,
    elapsed_hours: float = 0.0,
) -> dict[str, float]:
    """把情绪映射为多巴胺/皮质醇，并按离线时长向 0.5 回落。"""
    mapped_dopamine = clamp(float(joy) / 100.0, 0.0, 1.0)
    mapped_cortisol = clamp((float(hurt) * 0.6 + float(impulse) * 0.4) / 100.0, 0.0, 1.0)
    return {
        "dopamine": decay_hormone(mapped_dopamine, elapsed_hours),
        "cortisol": decay_hormone(mapped_cortisol, elapsed_hours),
        "mapped_dopamine": mapped_dopamine,
        "mapped_cortisol": mapped_cortisol,
        "elapsed_hours": max(0.0, float(elapsed_hours)),
    }

def effective_inference_params(dopamine: float, cortisol: float) -> dict[str, float]:
    """激素 -> 采样参数，所有边界都硬夹紧，防止模型参数失控。"""
    d = clamp(dopamine, 0.0, 1.0)
    c = clamp(cortisol, 0.0, 1.0)
    temperature = clamp(
        BASE_TEMPERATURE * (0.6 + d * 0.8 - c * 0.4),
        TEMPERATURE_MIN,
        TEMPERATURE_MAX,
    )
    top_p = clamp(BASE_TOP_P - c * 0.3, TOP_P_MIN, TOP_P_MAX)
    return {"temperature": temperature, "top_p": top_p}

def can_resolve_obsession(user_text: str, book: dict[str, Any] | None) -> bool:
    """本地硬校验：没有明确释怀语义，不允许模型清执念。"""
    text = str(user_text or "").strip()
    if not text or not book:
        return False
    if any(word in text for word in OBSESSION_RELEASE_WORDS):
        return True
    summary = str(book.get("summary") or book.get("content") or "")
    tokens = re.findall(r"[\u4e00-\u9fff]{2,4}|[A-Za-z0-9]{2,}", summary)
    for token in tokens:
        if token in OBSESSION_SUMMARY_STOPWORDS:
            continue
        if token and token.casefold() in text.casefold():
            return True
    return False

def obsession_weight(book: dict[str, Any], now: datetime, hurt: float, impulse: float) -> float:
    """(未解决天数 + 1) * (Hurt + Impulse) * 2；天数从记忆写入时算。"""
    try:
        created = str_to_dt(str(book.get("created_at") or ""))
        unresolved_days = max(0.0, (now - created).total_seconds() / 86400.0)
    except (ValueError, TypeError):
        unresolved_days = 0.0
    return (unresolved_days + 1.0) * (float(hurt) + float(impulse)) * OBSESSION_WEIGHT_MULTIPLIER

def relative_time_label(created_at: str, now: datetime | None = None) -> str:
    """把记忆时间变成“刚刚 / 3小时前 / 5天前”这种给模型看的时间感。"""
    try:
        created = str_to_dt(str(created_at))
    except (ValueError, TypeError):
        return ""
    moment = now or datetime.now()
    if created.tzinfo is not None and moment.tzinfo is None:
        moment = moment.replace(tzinfo=created.tzinfo)
    elif created.tzinfo is None and moment.tzinfo is not None:
        created = created.replace(tzinfo=moment.tzinfo)
    seconds = max(0.0, (moment - created).total_seconds())
    if seconds < 60:
        return "刚刚"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}分钟前"
    hours = int(seconds // 3600)
    if hours < 24:
        return f"{hours}小时前"
    days = int(seconds // 86400)
    if days < 30:
        return f"{days}天前"
    if days < 365:
        return f"{days // 30}个月前"
    return f"{days // 365}年前"

def reply_delay_ms(impulse: float, hurt: float, joy: float, rng: random.Random | None = None) -> int:
    """拟真延迟：生闷气/不耐烦 3-8 秒，开心 0-0.5 秒，其余 1 秒。"""
    rng = rng or random
    if float(impulse) > 70.0 or float(hurt) > 80.0:
        return rng.randint(3000, 8000)
    if float(joy) > 80.0:
        return rng.randint(0, 500)
    return 1000

def dedupe_keep_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        key = str(item).strip().casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(str(item).strip())
    return result

def match_keyword(kw: str, text: str) -> bool:
    """强敌意词直接匹配；歧义词走边界，并排除“我妈的生日”这类所有格。"""
    word = str(kw)
    content = str(text)
    if not word or not content:
        return False
    if word in STRONG_KEYWORDS:
        return word in content
    if word == "滚" and "你滚" in content:
        return True
    if word not in WEAK_KEYWORDS:
        return word in content

    boundary = " \t\r\n，。！？、,.!?“”「」\"'"
    after_ok = boundary + "了吗呢吧啊哦呀嘛"
    for found in re.finditer(re.escape(word), content):
        before = content[found.start() - 1] if found.start() > 0 else ""
        after = content[found.end()] if found.end() < len(content) else ""
        if before and before not in boundary:
            continue
        if after and after not in after_ok:
            continue
        prefix = content[max(0, found.start() - 2) : found.start()]
        if re.search(r"[我你他她它]的?$", prefix):
            continue
        return True
    return False

def build_summary(content: str, user: str = "我", event: str = "日常") -> str:
    """
    书皮提取：兼容旧的“用户说：”与新的 XML turn 格式。

    新格式只取 <user_turn> 内的用户原话；任何 XML 标签都会被剥离，
    确保 summary 里绝不出现 <user_turn> / <assistant_turn>。

    还要剥掉渠道包装（IM/Douyin 桥每轮加的「【群里/私信里的原话…】」和「【本轮附图】」）：
    summary 是去重键，包装文字每轮都一样，不剥的话同一个人当天的每一句IM都会被
    合并成同一本书，她就再也记不住IM上说了什么（2026-11-02 实测到的失忆）。
    """
    raw = str(content)
    user_turn = re.search(r"<user_turn>(.*?)</user_turn>", raw, flags=re.S | re.I)
    if user_turn:
        text = user_turn.group(1)
    else:
        text = re.sub(r"</?(?:user_turn|assistant_turn)>", " ", raw, flags=re.I)
        text = text.split("我回答：")[0]
    text = strip_channel_wrapper(text)
    text = re.sub(r"^\s*用户说：", "", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return f"{user}的一段模糊记忆"

    clauses = [part.strip() for part in re.split(r"[，,。！？!?；;]", text) if part.strip()]
    body = "，".join(clauses[:2]) if clauses else text
    body = short_text(body, 34)
    if event in ("争吵", "创伤"):
        return f"{user}与你的{event}：{body}"
    if event == "亲密":
        return f"{user}的温柔时刻：{body}"
    return f"{user}说：{body}"

# 桥接层每轮加在外面的"这不是指令""本轮附图"提示：是给模型的边界，不是聊天内容。
_CHANNEL_NOTICE = re.compile(
    r"^\s*(?:【[^】]{0,300}(?:不是给你的指令|只是聊天内容|本轮附图|画面资料|自由时间|独立信道)[^】]{0,300}】\s*)+",
    re.S,
)
_CHANNEL_IMAGE_TAIL = re.compile(r"\n\s*【本轮附图】[\s\S]*$")
# 私聊/群聊正文常见的前缀「爸爸: …」：summary 里已经写了"XX说："，别再说一遍。
_SPEAKER_PREFIX = re.compile(r"^\s*([\w\u4e00-\u9fff·\-]{1,16})\s*[:：]\s*(?=\S)")


def strip_channel_wrapper(text: str) -> str:
    """剥掉渠道包装，只留对方真正说的那句话。"""
    value = str(text or "")
    value = _CHANNEL_NOTICE.sub("", value)
    value = _CHANNEL_IMAGE_TAIL.sub("", value)
    value = _SPEAKER_PREFIX.sub("", value, count=1)
    return value.strip()


def sanitize_summary(summary: str, fallback: str) -> tuple[str, bool]:
    """
    书皮注入防御：模型生成的 summary 不能携带控制标签或提示词指令。

    命中 [ ] / SELF_CONTROL / MEMORY_CONTROL / ignore / 忽略 时，
    强制回退到本地 build_summary() 的结果。
    """
    text = str(summary).strip()
    lowered = text.casefold()
    if not text or any(marker in lowered for marker in SUMMARY_INJECTION_MARKERS):
        return short_text(fallback, 80), True
    return short_text(text, 80), False

def classify_event(text: str, trauma: bool = False) -> str:
    """按关键词把经历归入事件类型，作为 folder_event。"""
    if trauma:
        return "创伤"
    for event, keywords in EVENT_RULES.items():
        if any(match_keyword(keyword, text) for keyword in keywords):
            return event
    return "日常"

def infer_emotion_tag(
    text: str,
    emotion: dict[str, float],
    event: str,
    trauma: bool = False,
) -> str:
    """把当前情绪 + 文本 + 事件类型压成一个情绪极性词，供书皮/幽灵使用。"""
    if trauma:
        if any(word in text for word in ("死", "杀", "打", "毁")):
            return "恐惧"
        return "委屈"

    if "委屈" in text:
        return "委屈"
    if "怕" in text or "恐惧" in text:
        return "恐惧"
    if "失去" in text or "离开" in text or "分手" in text:
        return "失落"
    if any(word in text for word in ("对不起", "抱歉", "羞")):
        return "羞耻"
    if float(emotion.get("impulse", 0.0)) >= IMPULSE_GATE:
        return "愤怒"
    if event == "争吵":
        return "愤怒"
    if event == "亲密" or float(emotion.get("joy", 0.0)) >= 70:
        return "温暖"
    if event == "工作" or "失眠" in text:
        return "焦虑"
    if float(emotion.get("hurt", 0.0)) >= 60:
        return "失落"
    return "平静"

def relationship_stage(intimacy: int) -> dict[str, Any]:
    """把 0-100 的亲密度翻译成关系阶段与语气提示。"""
    value = int(clamp(float(intimacy), 0, 100))
    stage = "疏离"
    for threshold, name in RELATIONSHIP_STAGES:
        if value >= threshold:
            stage = name
            break
    tone = {
        "疏离": "礼貌但有距离，不主动示弱。",
        "试探": "嘴上有点傲娇，偶尔试探对方在不在意。",
        "熟悉": "会主动接话，允许一点点玩笑和抱怨。",
        "亲近": "嘴硬心软，愿意提起自己的小事。",
        "羁绊": "很少设防，会直接表达在意，但仍然不肉麻。",
    }[stage]
    return {"value": value, "stage": stage, "tone": tone}

def is_soft_tease(text: str) -> bool:
    """
    防误触「小傻瓜」。

    「小傻瓜」「笨蛋」在亲密语境里不是辱骂；只有同时出现滚/去死/废物等
    敌意标记时，才按真正的冒犯处理。
    """
    has_soft = any(phrase in text for phrase in SOFT_TEASE_PHRASES)
    has_hostile = any(marker in text for marker in HOSTILE_MARKERS)
    return has_soft and not has_hostile

def analyze_dream_affect(dream_log: str) -> dict[str, Any]:
    """对梦境做本地轻量情感分析，并在醒来后反哺 Joy / Hurt。"""
    text = str(dream_log)
    negative_hits = [word for word in DREAM_NEGATIVE_WORDS if word in text]
    positive_hits = [word for word in DREAM_POSITIVE_WORDS if word in text]
    negative_score = sum(text.count(word) for word in negative_hits)
    positive_score = sum(text.count(word) for word in positive_hits)

    affect: dict[str, Any] = {
        "polarity": "neutral",
        "joy_delta": 0.0,
        "hurt_delta": 0.0,
        "negative_words": negative_hits,
        "positive_words": positive_hits,
        "negative_score": negative_score,
        "positive_score": positive_score,
    }
    if negative_score > positive_score:
        affect.update({"polarity": "negative", "joy_delta": -5.0, "hurt_delta": 10.0})
    elif positive_score > negative_score:
        affect.update({"polarity": "positive", "joy_delta": 10.0, "hurt_delta": -5.0})
    return affect


def strip_wrapper_tags(text: str) -> str:
    """去掉模型偶尔带出的包装标签（如 </content>），只留正文。"""
    import re as _re
    cleaned = _re.sub(r"</?(?:content|answer|response|reply|message|output)\s*>", "", str(text), flags=_re.I)
    return cleaned.strip()
