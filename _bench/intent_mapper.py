# -*- coding: utf-8 -*-
"""意图映射器 —— 把她的【自然语言】映射到谓词表里的规范动作。

设计要点（Claude 2026-11-25 评审后定稿）：

1. **约束输出**：用 Ollama 的 `format` 传 JSON schema，`predicate` 是 enum，
   必须包含 NONE / UNKNOWN。不让模型自由生成规范名。
2. **三个返回值**：
     NONE    —— 不是指令（动作描写、IM 表情码、引用、普通文字）→ 完全静默
     UNKNOWN —— 看得出想做事但表里没有 → 回「似乎没有这个功能」
     谓词名  —— 映射成功
3. **全量记日志**：原文 / 映射结果 / 置信度 / 耗时。攒够样本再决定要不要微调。
4. **保留正则快速通道**：她历史和记忆里全是旧语法 `[STICKER: 开心]`，
   遇到旧格式直接走正则，不浪费一次推理。

用法：
    from intent_mapper import map_intent, extract_bracket
    r = map_intent("【留话】我想让他看看我画的路灯")
    # {'predicate': '留话', 'params': {...}, 'tag': 'SOLVER_MESSAGE', ...}
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from pathlib import Path

try:  # 谓词表跟本文件同目录
    from predicate_table import (PREDICATES, is_description, match_by_alias,  # type: ignore
                            nearest, tag_of, safety_of)
except Exception:  # noqa: BLE001
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from predicate_table import PREDICATES, tag_of, safety_of  # type: ignore

OLLAMA = "http://127.0.0.1:11434/api/generate"
MODEL = "qwen2.5vl:3b"
LOG_PATH = Path(__file__).parent / "data" / "intent_map.jsonl"

# 全角【】里 1~24 个字；不吃换行，避免跨段误抓
_BRACKET_ALL = re.compile(r"【([^】\n]{1,24})】")
# 2026-11-25（爸爸拍板）：**只有行首的【】才触发动作**。
# 理由：盲测三的两次误触发（「你以为我真会【睡觉】」「你那个【留话】咋用的啊」）
# 都是方括号藏在句子中间。要求放句首，等于她自己声明"这是指令，不是聊天"。
# 多行消息里任意一行以【开头都算。
_AT_LINE_START = re.compile(r"(?m)^[ \t]*【")
_BRACKET = _BRACKET_ALL
# 没闭合的【：宽松兜底（Claude：格式问题不该让她承担后果）
_BRACKET_OPEN = re.compile(r"【([^】\n]{1,60})$", re.M)
# 旧格式快速通道：[TAG] 或 [TAG: param]
_LEGACY = re.compile(r"\[([A-Z][A-Z0-9_]*)(?::\s*([^\]\n]*))?\]")


def extract_bracket(text: str) -> str:
    """取出【】里的内容。

    2026-11-25 盲测教训：她可能写两个括号——【生气地】【唱歌】。
    只取第一个会抓到"生气地"，什么也匹配不上。
    所以：**先挑一个真的命中谓词名的括号**，都不命中再返回第一个。
    """
    raw = str(text or "")
    if not _AT_LINE_START.search(raw):
        return ""          # 方括号不在行首 → 不当指令
    inner_all = [m.group(1).strip() for m in _BRACKET_ALL.finditer(raw)]
    if not inner_all:
        m = _BRACKET_OPEN.search(raw)
        return m.group(1).strip() if m else ""
    try:
        from predicate_table import match_by_alias as _mba  # noqa: PLC0415
        for cand in inner_all:
            if _mba(cand):
                return cand
    except Exception:  # noqa: BLE001
        pass
    return inner_all[0]


def all_brackets(text: str) -> list[str]:
    return [m.group(1).strip() for m in _BRACKET_ALL.finditer(str(text or ""))]


def legacy_tag(text: str) -> tuple[str, str] | None:
    """她写了旧格式就直接认，不走小模型。返回 (tag, param)。"""
    m = _LEGACY.search(str(text or ""))
    if m and m.group(1) not in ("NONE",):
        return m.group(1), (m.group(2) or "").strip()
    return None


def _schema() -> dict:
    """Ollama 的结构化输出 schema。predicate 用 enum 锁死。"""
    names = list(PREDICATES.keys())
    return {
        "type": "object",
        "properties": {
            "predicate": {"type": "string", "enum": names + ["NONE", "UNKNOWN"]},
            "params": {"type": "object"},
            "confidence": {"type": "number"},
            "reason": {"type": "string"},
        },
        "required": ["predicate", "confidence"],
    }


def _build_prompt(inner: str, full_text: str) -> str:
    rows = []
    for name, item in PREDICATES.items():
        ex = "；".join(item.get("examples") or [])
        ps = "/".join(item.get("params") or []) or "无"
        rows.append(f"- {name}：{item['desc']}（参数：{ps}）例：{ex}")
    table = "\n".join(rows)
    return (
        "你是意图分类器。下面是一个角色写的一句话，方括号里的词是她的意图。\n"
        "你的任务：判断她要做什么，从**给定的动作表**里选一个，并抽出参数。\n\n"
        f"【动作表】\n{table}\n\n"
        "【判断规则】\n"
        "1. NONE —— 方括号里**不是一个想做的动作**。包括：\n"
        "   IM 表情码（流泪/钱/发呆/微笑）、神态描写（笑/叹气/小声）、"
        "   引用、代码、Markdown、以及根本不像动作的词。\n"
        "2. UNKNOWN —— 方括号里**明显是一个动作或想做的事**，但动作表里没有。\n"
        "   例：【跳舞】【做菜】【散步】【剪头发】【看电影】【打游戏】"
        "【打电话】【点外卖】【种花】→ 全部 UNKNOWN。\n"
        "   判断标准：如果这句话的意思是「我想去做某件具体的事」，就一定是 UNKNOWN 或命中，不能是 NONE。\n"
        "3. **谓词只看方括号里的那几个字**，不要因为括号外的句子里出现了别的动作词就改判。\n"
        "   例：【留话】跟他说路灯我画好了，记得来看 → 是「留话」，不是「记住」。\n"
        "   括号外的内容**只用来抽参数**。\n"
        "4. 如果括号里是个动作、但动作表里没有最接近的 → UNKNOWN；"
        "宁可 UNKNOWN 也不要硬套一个不相关的。\n"
        "5. 参数只抽**原句明确写了的**，不要编。\n"
        "6. confidence 用 0~1 表示你有多确定。\n\n"
        f"【方括号里的内容】\n{inner}\n\n"
        f"【她完整的一句话】\n{full_text[:400]}\n"
    )


def _call(prompt: str, timeout: float = 60.0) -> dict:
    body = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "format": _schema(),
        "options": {"temperature": 0.0, "num_predict": 200},
    }
    req = urllib.request.Request(
        OLLAMA, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    return json.loads(data.get("response") or "{}")


# 语用规则：这些标志词出现在方括号【之前】时，说明她只是在「说」不是在「做」。
_COND = ("要是", "如果", "假如", "万一", "除非", "不然", "的话", "若不是", "倘若")
_PAST = ("刚才", "刚刚", "上周", "上个月", "昨天", "前天", "以前", "之前",
         "已经", "曾经", "那天", "小时候")
_REPORT = ("他说", "她说", "他昨天", "她昨天", "听他说", "听她说",
           "他每天", "她每天", "他老是", "她老是", "人家说")
_RHETORIC = ("难道", "怎么不", "要不要我", "凭啥要", "凭什么要")


def _looks_mention_only(text: str) -> tuple[bool, str]:
    """判断她只是在提到，而不是现在要执行。返回 (是否只是提到, 原因)。"""
    raw = str(text or "")
    pos = raw.find("【")
    if pos < 0:
        return False, ""
    head = raw[:pos]          # 括号在开头时 head 就是空串，不能退化成整句
    tail = raw[pos:]

    for w in _COND:
        if w in head:
            return True, f"条件:{w}"
    for w in _PAST:
        if w in head:
            return True, f"过去:{w}"
    for w in _REPORT:
        if w in head:
            return True, f"转述:{w}"
    for w in _RHETORIC:
        if w in head:
            return True, f"反问:{w}"
    # 反问句：整句以问号结尾且带"吗/呢"
    if tail.rstrip().endswith(("？", "?")) and ("吗" in tail or "呢" in tail):
        return True, "反问句"
    # 定语：括号后面紧跟"前/的时候/的习惯/之后"
    after = tail.split("】", 1)[1] if "】" in tail else ""
    for w in ("前", "的时候", "的习惯", "之后", "以前", "那年"):
        if after.lstrip().startswith(w):
            return True, f"定语:{w}"
    return False, ""


def _check_executing(text: str, predicate: str, timeout: float = 45.0) -> tuple[bool, str]:
    """她是在**现在真的要去做**，还是在假设/过去/转述/反问/定语？

    2026-11-25 第二批盲测（30 条难题）暴露的真问题：
      谓词识别准了，但 10/30 错在"她只是在说，不是在执行"。
    所以让模型只做这一个二分类——比"32 选 1"简单得多，也更可靠。
    """
    prompt = (
        f"她刚才说的原话：「{str(text)[:300]}」\n"
        f"其中方括号里的动作是【{predicate}】。\n\n"
        "请判断：她是不是**现在就真的要去做**这件事？\n\n"
        "**默认是 true。** 她写了方括号就是在发起动作，直接执行才是正常的。\n\n"
        "只有句子**明显**属于下面几种「只是提到」才填 false：\n"
        "1. 假设/条件：「要是我【唱歌】你会不会嫌吵」「如果明天下雨我就【用电脑】」\n"
        "2. 过去或已做完：「我刚才【画画】画得挺丑」「上周我【读书】读到一半」\n"
        "3. 转述别人：「他昨天【写信】给我」「她说她每天都要【抖音】」\n"
        "4. 反问或玩笑：「你难道要我【唱歌】哄你吗」\n"
        "5. 当定语用：「我【睡觉】前的习惯」\n"
        "6. 明确拒绝：「我才不要【发私信】给他」\n\n"
        "除此之外——哪怕前后有很多闲话、语气随意、句子不完整——都填 true。\n"
        '只输出 JSON：{"executing": true 或 false, "why": "十个字以内的原因"}'
    )
    body = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "format": {
            "type": "object",
            "properties": {"executing": {"type": "boolean"},
                           "why": {"type": "string"}},
            "required": ["executing"],
        },
        "options": {"temperature": 0.0, "num_predict": 80},
    }
    try:
        req = urllib.request.Request(
            OLLAMA, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        out = json.loads(data.get("response") or "{}")
        return bool(out.get("executing")), str(out.get("why") or "")[:40]
    except Exception as exc:  # noqa: BLE001
        # 判断不了时**不执行**（宁可不做，也不要误触发）
        return False, f"err:{type(exc).__name__}"


def _log(rec: dict) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def map_intent(text: str, *, use_llm: bool = True) -> dict:
    """把她的输出映射成一个动作。

    返回 dict：
      predicate  —— 谓词名 / NONE / UNKNOWN
      tag        —— 对应的旧标签名（NONE/UNKNOWN 时为空）
      params     —— 抽到的参数
      confidence —— 置信度
      source     —— 'legacy' 旧格式直通 / 'llm' 小模型 / 'none' 没有方括号
      ms         —— 耗时
    """
    t0 = time.perf_counter()
    text = str(text or "")

    # ① 旧格式直通（Claude 建议的快速通道）
    old = legacy_tag(text)
    if old:
        tag, param = old
        rec = {"predicate": tag, "tag": tag, "params": {"raw": param},
               "confidence": 1.0, "source": "legacy", "ms": 0}
        _log({"text": text[:300], **rec})
        return rec

    # ② 预处理三类「假指令」（盲测发现的）
    #    a. 嵌套括号 【【画画】】—— 是她在讨论写法，不是执行
    #    b. Markdown 强调 **【注意】** —— 排版，不是指令
    #    c. 明确放弃/犹豫：…算了 / …还是别了 / 不做了
    # 注意：WITHDRAW 不能放「没心情」这类词——它常常只是修饰当下状态
    #（"【晚点说】现在没心情，等会儿再说" 是在用这个功能，不是放弃）
    _WITHDRAW = ("算了", "还是别", "不做了", "不写了", "才不")
    # 谓词名最长 4 个字（公开留话/暂停轮询/恢复轮询）。括号里超过 6 字，
    # 几乎一定是她在写神态/动作描写，例如【揉了揉眼睛，有点不耐烦地瞥了你一眼】。
    _MAX_PRED_LEN = 6
    inner = ""
    _skip_reason = ""
    if "【【" in text or "】】" in text:
        _skip_reason = "nested"
    elif "**【" in text or "】**" in text:
        _skip_reason = "markdown"
    else:
        inner = extract_bracket(text)
        if inner and (inner.endswith("地") or inner.endswith("的")):
            # 【开心地】转了个圈 —— 副词/修饰语，不是动作
            inner, _skip_reason = "", "adverb"
        elif inner and len(inner) > _MAX_PRED_LEN:
            inner, _skip_reason = "", "too_long"
        elif inner and any(w in text for w in _WITHDRAW):
            inner, _skip_reason = "", "withdrawn"
        elif inner:
            # 引号里的【】是"引用/举例子"，不是执行。
            # 判据：左引号在【之前，右引号在【之后（成对包住）。
            _pos = text.find("【")
            _PAIRS = [("\u201c", "\u201d"), ("\u300c", "\u300d"), ('"', '"')]
            for _lo, _hi in _PAIRS:
                if _pos > 0 and _lo in text[:_pos] and _hi in text[_pos:]:
                    inner, _skip_reason = "", "quoted"
                    break

    # ②.5 有【】但不在行首 → MISPLACED（给她一句提示，不要静默）
    #   2026-11-25：行首规则能杜绝误触发，但如果她放错位置而我们什么都不说，
    #   她会以为功能坏了（Claude 警告过：角色会把沉默解释成世界设定）。
    # 引用别人的话（「…【独立信道】…」）里出现【】不该提示她改格式。
    _in_quote = False
    _bp = text.find("【")
    if _bp > 0:
        for _lo, _hi in (("\u201c", "\u201d"), ("\u300c", "\u300d"), ('"', '"')):
            if _lo in text[:_bp] and _hi in text[_bp:]:
                _in_quote = True
                break
    if not inner and _BRACKET_ALL.search(text) and not _in_quote:
        rec = {"predicate": "MISPLACED", "tag": "", "params": {},
               "confidence": 1.0,
               "hint": "指令要放在行首，比如回车后单独写【读书】",
               "source": "misplaced",
               "ms": round((time.perf_counter() - t0) * 1000)}
        _log({"text": text[:300], **rec})
        return rec

    # ③ 没有【】就什么都没发生
    if not inner:
        rec = {"predicate": "NONE", "tag": "", "params": {}, "confidence": 1.0,
               "source": "none", "ms": round((time.perf_counter() - t0) * 1000)}
        return rec

    # ③ 确定性分类：只有方括号里真的出现谓词名/别名才算命中。
    # 2026-11-25 盲测教训：让模型「选最接近的」，它会把【打游戏】判给「下棋」、
    # 【种花】判给「留话」，置信度还都是 0.90，没法用阈值滤。
    # 先做语用判断：她只是在说（过去/条件/转述/反问/定语）就什么都不做。
    # 放在别名匹配之前，因为「他昨天【写信】给我」里写信本来就不在表内，
    # 但正确答案是 NONE（她在转述别人），不是 UNKNOWN。
    _mention, _why0 = _looks_mention_only(text)
    if _mention:
        rec = {"predicate": "NONE", "tag": "", "params": {},
               "confidence": 1.0, "why": _why0, "source": "not_executing",
               "ms": round((time.perf_counter() - t0) * 1000)}
        _log({"text": text[:300], "inner": inner, **rec})
        return rec

    # 神情/动作描写（【叹气】【笑】【歪头】）→ 静默，不是"没这个功能"
    if is_description(inner):
        rec = {"predicate": "NONE", "tag": "", "params": {}, "confidence": 1.0,
               "source": "description",
               "ms": round((time.perf_counter() - t0) * 1000)}
        _log({"text": text[:300], "inner": inner, **rec})
        return rec

    pred = match_by_alias(inner)
    if not pred:
        rec = {"predicate": "UNKNOWN", "tag": "", "params": {},
               "confidence": 1.0, "suggest": nearest(inner),
               "source": "alias_miss",
               "ms": round((time.perf_counter() - t0) * 1000)}
        _log({"text": text[:300], "inner": inner, **rec})
        return rec

    if not use_llm:
            rec = {"predicate": "NONE", "tag": "", "params": {},
                   "confidence": 1.0, "matched": pred, "why": _why,
                   "source": "not_executing",
                   "ms": round((time.perf_counter() - t0) * 1000)}
            _log({"text": text[:300], "inner": inner, **rec})
            return rec

    # ⑤ 小模型只负责抽参数（Claude：难点在参数，不在分类）
    if not use_llm:
        rec = {"predicate": "", "tag": "", "params": {}, "confidence": 0.0,
               "source": "skip", "ms": 0}
        return rec
    try:
        out = _call(_build_prompt(inner, text))
    except Exception as exc:  # noqa: BLE001
        rec = {"predicate": "UNKNOWN", "tag": "", "params": {},
               "confidence": 0.0, "source": f"error:{type(exc).__name__}",
               "ms": round((time.perf_counter() - t0) * 1000)}
        _log({"text": text[:300], "inner": inner, **rec})
        return rec

    _llm_pred = str(out.get("predicate") or "").strip()
    rec = {
        # 谓词以确定性匹配为准；模型给的只在同一动作时用来校验
        "predicate": pred,
        "llm_pred": _llm_pred,
        "tag": tag_of(pred),
        "params": out.get("params") or {},
        "confidence": float(out.get("confidence") or 0.0),
        "safety": safety_of(pred),
        "reason": str(out.get("reason") or "")[:120],
        "source": "llm",
        "ms": round((time.perf_counter() - t0) * 1000),
    }
    _log({"text": text[:300], "inner": inner, **rec})
    return rec


if __name__ == "__main__":
    for s in ("【留话】我想让他看看我画的路灯", "[流泪]", "【跳舞】", "今天有点累"):
        r = map_intent(s)
        print(f"{s!r:40} → {r['predicate']:8} tag={r['tag'] or '-':18} "
              f"conf={r['confidence']:.2f} {r['ms']}ms")
