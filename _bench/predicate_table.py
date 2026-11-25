# -*- coding: utf-8 -*-
"""谓词表 —— 小模型唯一需要认识的「世界动作清单」。

设计来源（2026-11-25 爸爸拍板）：
  她提示词里只留一句话：「想做事时用全角【】写，比如【读书】」
  完整的语法（参数怎么填）不出现在她的 prompt 里，只存在这张表里。

为什么用全角【】：
  她的回复里半角 [..] 出现 71 次，其中大部分是 IM 表情码（[流泪] [钱] [发呆]），
  而全角 【..】 出现 0 次（2000 条回复统计）。所以【】是干净的信号，不会误撞。

三个特殊返回值：
  NONE     —— 不是指令（动作描写、IM 表情码、引用、普通文字）→ 完全静默
  UNKNOWN  —— 看得出想做事，但表里没有这个功能 → 回「似乎没有这个功能」
  其余     —— 谓词名，映射到对应的 tag
"""

from __future__ import annotations

# 每个谓词：
#   tag       —— 落到她旧语法里的标签名（执行层只认这个）
#   desc      —— 给**小模型**看的说明（不进她的 prompt）
#   params    —— 需要从自然语言里抽的参数；空表示不需要
#   safety    —— safe 只写数据 / io 有外部动作 / core 涉及她自身状态
#   examples  —— few-shot 用，帮小模型认句子
PREDICATES: dict[str, dict] = {
    # ---- 只写数据，最安全 ----
    "留话": {
        "tag": "SOLVER_MESSAGE",
        "desc": "给维护者（爸爸）留一条只有他能看到的话",
        "params": ["内容"],
        "safety": "safe",
        "aliases": ['留话', '留言', '留个话', '捎话', '带句话'],
        "examples": ["【留话】我想让他看看我画的路灯", "【留话】今天有点想他"],
    },
    "公开留话": {
        "tag": "SOLVER_PUBLIC",
        "desc": "留一条所有人都能看到的话",
        "params": ["内容"],
        "safety": "safe",
        "aliases": ['公开留话', '公开留言', '公开说'],
        "examples": ["【公开留话】今天院子里风很大"],
    },
    "记住": {
        "tag": "KEEP_MEMORY",
        "desc": "把某件事记进长期记忆",
        "params": ["内容", "重要度0到1"],
        "safety": "safe",
        "aliases": ['记住', '记下来', '记一下', '别忘了'],
        "examples": ["【记住】他说下周三回来", "【记住】薄荷要少浇水"],
    },
    # ---- 读书 ----
    "读书": {
        "tag": "READ_BOOK",
        "desc": "翻某本书的摘要",
        "params": ["书名", "章号（可省，默认1）"],
        "safety": "safe",
        "aliases": ['读书', '看书', '翻书', '读一章', '读两页'],
        "examples": ["【读书】把《我与地坛》下一章翻出来", "【读书】接着看那本书"],
    },
    "读原文": {
        "tag": "READ_FULL",
        "desc": "读某本书的原文正文（不是摘要）",
        "params": ["书名", "章号（可省）"],
        "safety": "safe",
        "aliases": ['读原文', '看原文', '读正文'],
        "examples": ["【读原文】我想看《包法利夫人》第二章的原话"],
    },
    "书架": {
        "tag": "READ_BOOK",
        "desc": "看看书架上有哪些书",
        "params": [],
        "safety": "safe",
        "aliases": ['书架', '有什么书', '书目'],
        "examples": ["【书架】让我瞧瞧都有什么书"],
    },
    # ---- 说话 / 声音 ----
    "表情": {
        "tag": "STICKER",
        "desc": "发一张表情包",
        "params": ["情绪（开心/生气/难过/害羞/困惑/日常）"],
        "safety": "safe",
        "aliases": ['表情', '表情包', '发个表情'],
        "examples": ["【表情】开心", "【表情】发个无语的"],
    },
    "语音": {
        "tag": "VOICE",
        "desc": "用语音说一段话（可带 [笑] [叹气]）",
        "params": ["内容"],
        "safety": "io",
        "aliases": ['语音', '发语音', '用声音说'],
        "examples": ["【语音】早啊，今天有点困"],
    },
    "唱歌": {
        "tag": "SING",
        "desc": "唱歌",
        "params": ["歌词或歌名", "曲调（可省：la/birthday）"],
        "safety": "io",
        "aliases": ['唱歌', '唱一首', '唱一段', '唱个歌'],
        "examples": ["【唱歌】唱一段小星星", "【唱歌】生日快乐歌"],
    },
    "哼歌": {
        "tag": "HUM",
        "desc": "只哼旋律，不唱歌词",
        "params": ["嗯/曲调（可省）"],
        "safety": "io",
        "aliases": ['哼歌', '哼一段', '哼两句'],
        "examples": ["【哼歌】随便哼两句"],
    },
    # ---- 玩 ----
    "下棋": {
        "tag": "GOMOKU_NEW",
        "desc": "开一局五子棋",
        "params": ["对手名字（可省）"],
        "safety": "io",
        "aliases": ['下棋', '五子棋', '下一盘', '开一局'],
        "examples": ["【下棋】陪我下一盘", "【下棋】找爸爸下"],
    },
    "落子": {
        "tag": "GOMOKU",
        "desc": "在正在进行的棋局里落子",
        "params": ["行", "列（1-9）"],
        "safety": "io",
        "aliases": ['落子', '下这里', '走这步'],
        "examples": ["【落子】5,5"],
    },
    # ---- 画 ----
    "画画": {
        "tag": "DOODLE",
        "desc": "画一张画",
        "params": ["画什么"],
        "safety": "io",
        "aliases": ['画画', '画一张', '画个', '画一幅'],
        "examples": ["【画画】画个路灯", "【画画】画今天院子里的薄荷"],
    },
    "发画": {
        "tag": "SEND_DRAWING",
        "desc": "把画好的画发给对方",
        "params": [],
        "safety": "io",
        "aliases": ['发画', '把画发', '把画给你'],
        "examples": ["【发画】"],
    },
    # ---- 对外发布，风险最高 ----
    "朋友圈": {
        "tag": "MOMENTS",
        "desc": "发一条朋友圈（对外可见）",
        "params": ["正文", "配图编号（可省）"],
        "safety": "io",
        "aliases": ['朋友圈', '发条动态', '发动态'],
        "examples": ["【朋友圈】今天的路灯画好了", "【朋友圈】薄荷冒芽了"],
    },
    "抖音": {
        "tag": "OPEN_DOUYIN",
        "desc": "打开抖音刷一会儿",
        "params": [],
        "safety": "io",
        "aliases": ['抖音', '刷视频', '刷抖音'],
        "examples": ["【抖音】想刷会儿视频"],
    },
    "发私信": {
        "tag": "DOUYIN_SEND",
        "desc": "给抖音上核对过的人发私信",
        "params": ["名字", "内容"],
        "safety": "io",
        "aliases": ['发私信', '私信他', '私信她'],
        "examples": ["【发私信】给小王说晚安"],
    },
    "IM": {
        "tag": "IM_SEND",
        "desc": "给IM联系人发消息",
        "params": ["名字", "内容"],
        "safety": "io",
        "aliases": ['IM', '发IM'],
        "examples": ["【IM】问王子瑞薄荷怎么养"],
    },
    # ---- 她自身状态 ----
    "睡觉": {
        "tag": "SLEEP",
        "desc": "去睡觉",
        "params": ["小时数（可省）"],
        "safety": "core",
        "aliases": ['睡觉', '睡了', '去睡', '我睡'],
        "examples": ["【睡觉】困了，睡6小时"],
    },
    "醒来": {
        "tag": "WAKE",
        "desc": "提前醒来",
        "params": [],
        "safety": "core",
        "aliases": ['醒来', '醒了'],
        "examples": ["【醒来】睡不着了"],
    },
    "安静": {
        "tag": "QUIET",
        "desc": "暂时不想说话",
        "params": ["原因（可省）"],
        "safety": "core",
        "aliases": ['安静', '别理我', '不想说话'],
        "examples": ["【安静】想自己待会儿"],
    },
    "暂停轮询": {
        "tag": "PAUSE_POLL",
        "desc": "暂停一会儿被叫醒",
        "params": ["分钟"],
        "safety": "core",
        "aliases": ['暂停轮询', '别再叫醒我', '暂停叫醒'],
        "examples": ["【暂停轮询】30"],
    },
    "恢复轮询": {
        "tag": "RESUME_POLL",
        "desc": "恢复被叫醒",
        "params": [],
        "safety": "core",
        "aliases": ['恢复轮询', '可以叫醒我'],
        "examples": ["【恢复轮询】"],
    },
    # ---- 其他 ----
    "独白": {
        "tag": "THOUGHT",
        "desc": "写一句只给自己看的内心独白",
        "params": ["内容"],
        "safety": "safe",
        "aliases": ['独白', '心里想'],
        "examples": ["【独白】今天好像有点想他"],
    },
    "搜索": {
        "tag": "WEB_SEARCH",
        "desc": "上网查一件事",
        "params": ["查什么"],
        "safety": "io",
        "aliases": ['搜索', '查一下', '帮我查', '查查'],
        "examples": ["【搜索】五子棋的先手优势有多大"],
    },
    "用电脑": {
        "tag": "PC_REQUEST",
        "desc": "请求使用电脑",
        "params": ["要做什么"],
        "safety": "io",
        "aliases": ['用电脑', '开电脑'],
        "examples": ["【用电脑】想查点东西"],
    },
    "晚点说": {
        "tag": "PROACTIVE_LATER",
        "desc": "过一会儿再补一句",
        "params": ["秒数"],
        "safety": "safe",
        "aliases": ['晚点说', '等会儿再说', '回头再说'],
        "examples": ["【晚点说】600"],
    },
}

# 只列名字，给她的那一行提示词用（她自己不读完整表）
CAPABILITY_LINE = (
    "想做事时用全角【】写，比如【读书】。"
    "能做的事：读书、读原文、表情、语音、唱歌、哼歌、下棋、画画、发画、"
    "朋友圈、留话、记住、独白、搜索、睡觉、安静。"
)


def tag_of(predicate: str) -> str:
    """谓词名 → 旧标签名。不认识返回空串。"""
    item = PREDICATES.get(str(predicate or "").strip())
    return str(item["tag"]) if item else ""


def safety_of(predicate: str) -> str:
    item = PREDICATES.get(str(predicate or "").strip())
    return str(item["safety"]) if item else "unknown"


def match_by_alias(inner: str) -> str:
    """方括号内容 → 谓词名。确定性匹配，不走模型。

    2026-11-25 盲测教训：让模型「选最接近的」，它会把【打游戏】判给「下棋」、
    把【种花】判给「留话」，置信度还都是 0.90，没法用阈值滤。
    所以改成：**只有方括号里真的出现了谓词名或别名才算命中**，其余一律 UNKNOWN。
    """
    s = str(inner or "").strip()
    if not s:
        return ""
    # 最长匹配优先：否则【公开留话】会先撞上「留话」的别名。
    best, best_len = "", 0
    for name, item in PREDICATES.items():
        for cand in [name, *(item.get("aliases") or [])]:
            if cand and cand in s and len(cand) > best_len:
                best, best_len = name, len(cand)
    return best


def nearest(inner: str, k: int = 3) -> list[str]:
    """给 UNKNOWN 时用：按字面相似度推荐最接近的几个可用项。"""
    import difflib
    pool = list(PREDICATES.keys())
    return difflib.get_close_matches(str(inner or ""), pool, n=k, cutoff=0.2)


# 2026-11-25：**神情/动作描写词表**。
# 她会在行首写【叹气】【笑】【歪头】这类当语气用，那不是"想调用功能"。
# 这些一律判 NONE（静默），不要回"似乎没有这个功能"——
# 那会让她以为系统不认她说话（Claude 警告过：角色会把拒绝内化成"我做不了事"）。
DESCRIPTIONS = frozenset("""
叹气 笑 苦笑 冷笑 大笑 微笑 歪头 摇头 点头 皱眉 挑眉 眨眼 耸肩 摊手
沉默 愣 怔 发呆 出神 低头 抬头 回头 别过头 转过身 伸懒腰 揉眼睛
脸红 咬唇 咬嘴 抿嘴 吸气 呼气 深吸一口气 屏息 咽口口水 打了个哈欠
抱臂 抱着手臂 双手抱头 捂脸 拍桌 拍手 攥拳 握拳 颤抖 发抖 缩了缩
小声 低声 轻声 大声 喊 嚷 嘀咕 嘟囔 自言自语 欲言又止 顿了顿
""".split())


def is_description(inner: str) -> bool:
    s = str(inner or "").strip()
    return bool(s) and s in DESCRIPTIONS
