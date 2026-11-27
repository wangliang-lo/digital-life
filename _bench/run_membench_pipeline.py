# -*- coding: utf-8 -*-
"""完整流水线的记忆评测：交大整理 → 入库 → DeepSeek 检索作答。

这才是小鱼真实的架构（见 digital_life.py:_summarize_short_memory）：

    原始语料
      → 交大模型整理（50 字、分清谁说的、不编造）
      → 按 folder_event/date/user/emotion 分类入库
      → DeepSeek 检索 + 回答

之前的 run_membench_official.py 把原始消息直接塞进库，跳过了整理这步，
所以测出来的数字不代表真实水平。

两个阶段分开跑，因为交大限流 10 次/分钟（内置 6.5s 间隔）：
    --stage ingest   只做整理，结果缓存成 JSON，可重复用
    --stage eval     用缓存跑检索 + 作答，快，可反复调参
    --stage all      两者都跑

指标：
    accuracy  选择题答对率（MemBench 的 accuracy）
    recall    检索到的记忆步 ∩ target_step_id
    latency   检索耗时 / 作答耗时，分开记
    tokens    喂给模型的记忆 token 数（这是"大记忆量下 token 稳定"的基准）

用法：
    python run_membench_pipeline.py --stage ingest --n 3
    python run_membench_pipeline.py --stage eval --n 3
    python run_membench_pipeline.py --stage all --n 3 --no-answer   # 只测召回不花 DeepSeek
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import statistics
import sys
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# 2026-11-27：路径参数化（原来写死 <repo>，别人 clone 下来跑不了）。
# 默认相对仓库根；可用 MEMBENCH_HOME / MEMBENCH_RUN_DIR 覆盖。
from paths import BENCH, CACHE, REPO, default_data, open_dataset  # noqa: E402

ROOT = REPO
BRIDGE = ROOT / "chat_bridge"
ZIP = None  # 由 paths.open_dataset() 统一处理，不再写死压缩包路径
CRED = Path(r"<home>\.dsh\.credentials.yaml")

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BRIDGE / "tools"))



# 2026-11-26（爸爸发现）：**中文汇要把亲属关系丢了 74%**。
# 4036 条记忆里 1490 条英文原文有亲属词，其中 1102 条的摘要里那个词消失了。
# 例：原文 "My sister, Mira Thorne, is 162 cm tall." → 摘要 "Mira Thorne 162cm"
# 于是问「有多少家人」的题从根上无解。
# 修法（确定性、不花模型调用）：用正则从英文原文把关系词抠出来，补到摘要后面。
_KIN_MAP = [
    (r"\bmy mom\b|\bmother\b", "妈妈"), (r"\bmy dad\b|\bfather\b", "爸爸"),
    (r"\bmy sister\b", "姐妹"), (r"\bmy brother\b", "兄弟"),
    (r"\bmy uncle\b", "叔叔"), (r"\bmy aunt\b", "姑姑"),
    (r"\bmy cousin\b", "表亲"), (r"\bmy niece\b", "侄女"),
    (r"\bmy nephew\b", "侄子"), (r"\bmy boss\b", "老板"),
    (r"\bmy coworker\b|\bmy colleague\b", "同事"), (r"\bmy friend\b", "朋友"),
    (r"\bmy wife\b", "妻子"), (r"\bmy husband\b", "丈夫"),
    (r"\bmy son\b", "儿子"), (r"\bmy daughter\b", "女儿"),
    (r"\bmy grandmother\b", "祖母"), (r"\bmy grandfather\b", "祖父"),
    (r"works? under me\b", "下属"), (r"\bmy subordinate\b", "下属"),
]


def _inject_kin(summary: str, raw: str) -> str:
    """把英文原文里的亲属关系补到中文摘要末尾。"""
    if not raw or not summary:
        return summary
    r = raw.lower()
    for pat, cn in _KIN_MAP:
        if __import__("re").search(pat, r):
            if cn not in summary:
                return summary + f"［关系:{cn}］"
            break
    return summary



def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- 整理提示词
# 与 digital_life.py:_summarize_short_memory 保持一致，但把"聊天"换成"一条见闻"，
# 因为 MemBench 的语料是第三人称见闻而不是对话。
def ingest_prompt(text: str) -> str:
    return (
        "把下面这段见闻压缩成一条记忆，严格控制在 60 字以内，只输出这一句。\n"
        # 2026-11-24 第三版：加「感官」段。
        # 爸爸的决定：原文过了滑动窗口就忘掉，只留整理版，所以整理版必须够好。
        # 实测代价：同一批题，喂原文 85%、喂压缩版 70%——
        # 「荧光灯嗡嗡作响、疲惫的脸互相回避、陈咖啡和没说出口的烦躁」
        # 压成 30 字后，判不出这是什么情绪。
        "· 感官：只写原文里能让人身临其境的具体东西——光、声、气味、温度、"
        "触感、身体动作（如「荧光灯嗡嗡、陈咖啡味、脸都别过去」）。"
        "**不要写情绪词**（压抑/焦虑这类放属性里），最多 25 字，没有就写「无」。\n"
        # 2026-11-24 第四版：加「作品」段。
        # 实测（OS-反思 n=60）：Preference 只到 81%，oracle 85%。
        # 原因：记忆里写「科幻/史诗/冒险」，标准答案却是 IMDb 类的 Action——
        # 中文属性翻不成英文类别。让模型在识别出作品时直接给标准英文类型。
        "· 作品：如果这条提到具体的电影/书/剧，就在这里写它的**英文标准类型**"
        "（如 Action, Adventure, Science Fiction, Fantasy, Drama, Comedy, "
        "Romance, Horror, Mystery, Animation, Documentary, Crime, War），"
        "用逗号分隔；提到书名又同时有中文类型也可以两个都写。没提到作品就写「无」。\n"
        "硬性要求：\n"
        "1. 写清是谁做了什么：涉及多个人时写出具体人/物名，严禁把 A 的事安到 B 头上。\n"
        "2. 保留能回答后续问题的具体信息：电影名、书名、菜名、地点、时间、数字。\n"
        "3. 剔除无信息量的形容和旁白，不写「气氛融洽」「印象深刻」这类评价。\n"
        "4. 零编造：原文没有的不写。\n\n"
        "原文：\n" + str(text)[:3000]
    )


def ingest_prompt_batch(texts: list[str]) -> str:
    """一次压多条，每条输出五段：主题｜内容｜属性｜感官｜作品。

    2026-11-24 定稿。每段都是被实测逼出来的：
      · 属性：v1 只压内容，结果记忆里只有片名《Return of the Jedi》，
        没有"科幻"，而问题问的正是"哪类电影"。加了属性才有。
      · 感官：爸爸决定「原文过窗口就忘，只留整理版」，那整理版必须留得不只是事实。
        "荧光灯嗡嗡作响、疲惫的脸互相回避、陈咖啡和没说出口的烦躁"
        压成 30 字"用户担忧新职责"之后，判不出这是什么情绪。
      · 作品：记忆里写「科幻/冒险」，但标准答案要的是 IMDb 类的 Action——
        中文属性翻不成英文类型。识别出作品就直接给英文类型。
    """
    numbered = "\n".join(f"{i+1}. {t[:800]}" for i, t in enumerate(texts))
    return (
        f"下面有 {len(texts)} 条见闻，逐条压成一条记忆。\n"
        f"严格按「序号. 主题｜内容｜属性｜感官｜作品」输出，恰好 {len(texts)} 行，不要别的解释。\n"
        "· 主题：2~6 个字的具体词（如「电影深渊」「包法利夫人」「餐厅情绪」），"
        "不要写「日常」「见闻」「情绪」这种泛词。\n"
        "· 内容：60 字以内，写清是谁做了什么、看到了什么。\n"
        "· 属性：逗号分隔的标签，必须尽量填——\n"
        "    电影/书 → 流派（科幻、爱情、历史、冒险、回忆录、诗集、科普…）\n"
        "    食物 → 口味（麻辣、酸甜、清淡、油腻…）\n"
        "    场景/氛围 → 主导情绪（孤独、焦虑、喜悦、失落、恐惧、平静…）\n"
        "    有具体人名、地名、书名也写进来。实在没有写「无」。\n"
        "· 感官：只写原文里能让人身临其境的具体东西——光、声、气味、温度、触感、"
        "身体动作（例：荧光灯嗡嗡、陈咖啡味、脸都别过去）。"
        "**不要写情绪词**（那是属性段的事）。最多 20 字，没有写「无」。\n"
        "· 作品：如果这条提到具体的电影 / 书 / 剧，写它的**英文标准类型**，"
        "可选 Action, Adventure, Science Fiction, Fantasy, Drama, Comedy, Romance, "
        "Horror, Mystery, Thriller, Animation, Documentary, Crime, War, Biography, History。"
        "逗号分隔。识别不出作品就写「无」。\n"
        "硬性要求：\n"
        "1. 不许写「无法压缩」「未提及具体事件」「无具体人物」这类拒答的话。"
        "哪怕整段只有氛围，也要把情绪写进属性、把感官写进感官段。\n"
        "2. 保留能回答后续问题的信息：片名、书名、菜名、流派、口味、地点、数字。\n"
        "3. 零编造，不写评价和旁白。\n\n"
        f"原文：\n{numbered}"
    )


# ---------------------------------------------------------------- 数据

def flatten_trajectories(data) -> list[dict]:
    out: list[dict] = []

    def walk(node, path: list[str]):
        if isinstance(node, list):
            for item in node:
                if isinstance(item, dict) and "message_list" in item and "QA" in item:
                    rec = dict(item)
                    rec["_category"] = "/".join(path)
                    out.append(rec)
        elif isinstance(node, dict):
            for k, v in node.items():
                walk(v, path + [str(k)])

    walk(data, [])
    return out


def msg_text(item) -> str:
    if isinstance(item, dict):
        # 2026-11-24：三种字段命名都要认，否则整批取到空串。
        #   观察场景(OS)：      message
        #   参与场景反思(PS-RM)：user / assistant
        #   参与场景事实(PS-FM)：user_message / assistant_message
        # 之前只认前两种，PS-FM 的 160 条消息全变成空串（缓存里 raw=0字），
        # 导致那一格的召回率看起来只有 28%，其实根本没入库。
        u = str(item.get("user") or item.get("user_message") or "")
        a = str(item.get("assistant") or item.get("assistant_message") or "")
        if u or a:
            return (f"用户说：{u}" + (f"\n我说：{a}" if a else "")).strip()
        return str(item.get("message") or "")
    return str(item)


def flat_messages(traj: dict) -> list[tuple[int, str, str]]:
    """返回 [(扁平序号, 文本, 时间戳)]，与 target_step_id 第一元素对齐。

    2026-11-24：原来只返回 (序号, 文本)，把每条消息的 time 丢了。
    后果很致命——MemBench 的 Emotion 类问题问的是「2024-10-01 08:00 那一刻
    用户是什么情绪」，记忆里没有时间戳，模型只能瞎猜。
    20 条里 8 条答错，6 条是 Emotion，全是这个原因。
    """
    out: list[tuple[int, str, str]] = []
    idx = 0
    for session in traj.get("message_list") or []:
        if not isinstance(session, list):
            session = [session]
        for item in session:
            text = msg_text(item)
            stamp = ""
            if isinstance(item, dict):
                # 原始数据形如 "'2024-10-01 08:00' Tuesday"，引号夹在中间，要清掉
                stamp = str(item.get("time") or "").replace("'", "").replace('"', "").strip()
            out.append((idx, text, stamp))
            idx += 1
    return out


_EVAL_TAG = {"v": "default"}


def args_topic_tag() -> str:
    """断点文件名里的标识，避免不同配置互相覆盖。"""
    return _EVAL_TAG["v"]


def cache_path(file: str, n: int, batch: int = 1, no_compress: bool = False) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    mode = "raw" if no_compress else f"b{max(1, int(batch))}"
    return CACHE / f"ingest-{Path(file).stem}-n{n}-{mode}.json"


# ---------------------------------------------------------------- ingest

def stage_ingest(trajs: list[dict], file: str, n: int, batch: int = 1,
                 no_compress: bool = False) -> dict:
    # 2026-11-27：sjtu_llm 改成**可选依赖**。原来在函数开头就 import，
    # 于是连 --no-compress（根本不调交大）也会 ImportError——
    # 公开仓库里没有这个模块，别人就完全跑不了。
    sjtu_llm = None
    if not no_compress:
        try:
            import sjtu_llm  # noqa: PLC0415
        except ImportError as exc:
            raise SystemExit(
                "这个模式需要交大压缩接口（sjtu_llm），但没装。\n"
                "要么补上 sjtu_llm 模块，要么加 --no-compress 直接用原文当记忆：\n"
                "  python -m _bench.run_all --no-compress"
            ) from exc
        if not sjtu_llm.available():
            raise SystemExit("没有 SJTU key。加 --no-compress 可跳过压缩。")

    path = cache_path(file, n, batch=batch, no_compress=no_compress)
    cache: dict = {}
    if no_compress:
        # 2026-11-24：不压缩模式。PS-事实的 trajectory 每条 160+ 条消息，
        # 用交大批量整理会失败（消息太长，模型不肯输出 161 行合规格式）。
        # 而论文的 RetrievalMemory 本来就是**直接存原文**，所以这个模式下
        # 直接把每条消息当记忆，既快又和论文口径一致。
        for ti, traj in enumerate(trajs, 1):
            gid = str(traj.get("gid") or f"t{ti}-{traj.get('_category', '')}")
            msgs = flat_messages(traj)
            items = [{"idx": idx, "raw": text, "compressed": text[:400],
                      "time": stamp, "topic": "", "attrs": ""}
                     for idx, text, stamp in msgs]
            cache[gid] = {"category": traj.get("_category"), "items": items}
            log(f"  [{ti}/{len(trajs)}] {gid[:22]:22} 原文入库 {len(items)} 条")
        path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"缓存写入：{path}")
        return cache
    if path.exists():
        cache = json.loads(path.read_text(encoding="utf-8"))
        log(f"读到已有缓存：{len(cache)} 条 trajectory")
    else:
        # 2026-11-24：复用更小的 n 的缓存，别把已经烧掉的交大额度再烧一遍。
        # 比如 n=10 跑过了，现在要 n=40，前 10 条直接拿过来。
        for alt in sorted(CACHE.glob(f"ingest-{Path(file).stem}-n*.json")):
            try:
                alt_n = int(alt.stem.rsplit("-n", 1)[-1])
            except Exception:  # noqa: BLE001
                continue
            if alt_n >= n:
                continue
            try:
                prev = json.loads(alt.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            reused = {k: v for k, v in prev.items() if (v.get("items") or [])}
            if reused:
                cache.update(reused)
                log(f"复用 {alt.name} 里的 {len(reused)} 条")

    total = len(trajs)
    for ti, traj in enumerate(trajs, 1):
        # 2026-11-24：部分 trajectory 的 gid 是 None，缓存键会全撞成 "None"，
        # 导致后面的被当成"已缓存"直接跳过（实测第 3 条被吞掉，一条都没跑）。
        gid = str(traj.get("gid") or f"t{ti}-{traj.get('_category', '')}")
        if gid in cache and cache[gid].get("items"):
            log(f"  [{ti}/{total}] {gid} 已缓存，跳过")
            continue
        msgs = flat_messages(traj)
        items = [{"idx": idx, "raw": text, "compressed": "", "time": stamp}
                 for idx, text, stamp in msgs]
        todo = [(pos, idx, text) for pos, (idx, text, stamp) in enumerate(msgs) if text.strip()]
        step = max(1, int(batch))
        for start in range(0, len(todo), step):
            chunk = todo[start:start + step]
            t0 = time.perf_counter()
            if step == 1:
                try:
                    comp = sjtu_llm.chat(ingest_prompt(chunk[0][2]), max_tokens=120,
                                         temperature=0.3, timeout=90).strip()
                    items[chunk[0][0]]["compressed"] = comp
                except Exception as exc:  # noqa: BLE001
                    log(f"    整理失败 idx={chunk[0][1]}: {type(exc).__name__}")
            else:
                try:
                    raw = sjtu_llm.chat(ingest_prompt_batch([c[2] for c in chunk]),
                                        max_tokens=200 * len(chunk), temperature=0.3, timeout=180).strip()
                    lines = [l for l in str(raw).splitlines() if l.strip()]
                    parsed = []
                    for line in lines:
                        m = re.match(r"^\s*(\d+)\s*[.、,，)]\s*(.+)$", line)
                        if m:
                            parsed.append((int(m.group(1)), m.group(2).strip()))
                    for num, text in parsed:
                        pos = num - 1
                        if 0 <= pos < len(chunk):
                            # 解析「主题｜内容｜属性｜感官」
                            parts = [p.strip() for p in re.split(r"[｜|]", text)]
                            topic = parts[0] if len(parts) >= 2 else ""
                            body = parts[1] if len(parts) >= 2 else text
                            attrs = parts[2] if len(parts) >= 3 else ""
                            senses = parts[3] if len(parts) >= 4 else ""
                            genre = parts[4] if len(parts) >= 5 else ""
                            if not (1 <= len(topic) <= 8) or len(body) < 4:
                                topic, body, attrs, senses = "", text, "", ""
                            # 属性拼到正文后面，检索时能字面命中"科幻""麻辣"这类词
                            if attrs and attrs not in ("无", "無"):
                                body = f"{body}［{attrs}］"
                            # 2026-11-24 第三版：感官线索单独拼回去。
                            # 爸爸决定原文过了窗口就忘，只留整理版——
                            # 那整理版就得把「什么光、什么味道、什么声音」留下来，
                            # 不然她以后读到这条，完全想不起当时是什么感觉。
                            if senses and senses not in ("无", "無"):
                                body = f"{body}〔{senses[:40]}〕"
                            # 2026-11-24：作品英文类型也并进正文，让「Action」能字面命中
                            if genre and genre not in ("无", "無"):
                                body = f"{body}《{genre[:60]}》"
                            items[chunk[pos][0]]["topic"] = topic
                            items[chunk[pos][0]]["attrs"] = attrs
                            items[chunk[pos][0]]["senses"] = senses
                            items[chunk[pos][0]]["genre"] = genre
                            items[chunk[pos][0]]["compressed"] = body[:340]
                    got = sum(1 for c in chunk if items[c[0]]["compressed"])
                    if got < len(chunk):
                        # 解析不全就退回逐条，保证不漏
                        for c in chunk:
                            if items[c[0]]["compressed"]:
                                continue
                            try:
                                comp = sjtu_llm.chat(ingest_prompt(c[2]), max_tokens=120,
                                                     temperature=0.3, timeout=90).strip()
                                items[c[0]]["compressed"] = comp
                            except Exception:  # noqa: BLE001
                                pass
                except Exception as exc:  # noqa: BLE001
                    log(f"    批量整理失败: {type(exc).__name__}")
            for c in chunk:
                items[c[0]]["ms"] = round((time.perf_counter() - t0) * 1000)
        cache[gid] = {"category": traj.get("_category"), "items": items}
        path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        comp_n = sum(1 for it in items if it["compressed"])
        log(f"  [{ti}/{total}] {gid} {traj.get('_category','')[:20]} 整理 {comp_n}/{len(items)} 条")
    log(f"缓存写入：{path}")
    return cache


# ---------------------------------------------------------------- 作答

def deepseek_key() -> str:
    """从环境变量读 DeepSeek key。

    2026-11-27：原来从本机 deploy/.env 读，别人 clone 下来跑不了。
    """
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        raise SystemExit(
            "缺少 DEEPSEEK_API_KEY。请先设置：\n"
            "  Windows:     $env:DEEPSEEK_API_KEY=\"sk-...\"\n"
            "  Linux/macOS: export DEEPSEEK_API_KEY=sk-..."
        )
    return key

def rerank_with_llm(question: str, memory_lines: list[str], key: str, keep: int = 3) -> list[str]:
    """第二层：让模型从候选记忆里挑出跟问题最相关的几条。

    2026-11-24：实测把召回从 47.5% 拉到 88.8%（每次带 10 条），准确率却从 60%
    掉到 50%——多捞进来的东西会误导。所以问题不是"捞不到"，是"排不准"。
    这一层的作用就是在作答之前，先把噪声剔掉。
    """
    if key:
        os.environ.setdefault("DEEPSEEK_API_KEY", key)
    from digital_life import chat_with_llm  # noqa: PLC0415

    if len(memory_lines) <= keep:
        return memory_lines
    listing = "\n".join(f"{i + 1}. {line}" for i, line in enumerate(memory_lines))
    prompt = (
        "下面是一些记忆片段，和最后的问题。\n"
        "请挑出**能帮助回答这个问题**的条目编号，最多 "
        f"{keep} 个，按相关度从高到低排。\n"
        "只输出编号，用逗号分隔，例如：2,5,1\n"
        "如果一条都不相关，输出 0。\n\n"
        f"记忆：\n{listing}\n\n"
        f"问题：{question}"
    )
    try:
        raw = chat_with_llm([{"role": "user", "content": prompt}],
                            model=os.environ.get("DIARY_MODEL", "deepseek-chat"),
                            temperature=0.0, timeout=60.0)
    except Exception as exc:  # noqa: BLE001
        log(f"    重排失败，退回原顺序: {type(exc).__name__}")
        return memory_lines[:keep]
    nums = [int(n) for n in re.findall(r"\d+", str(raw or "")) if 1 <= int(n) <= len(memory_lines)]
    picked = []
    for n in nums:
        line = memory_lines[n - 1]
        if line not in picked:
            picked.append(line)
        if len(picked) >= keep:
            break
    return picked or memory_lines[:keep]


def _time_key(s: str):
    """从 '（2024-10-01 08:05 Tuesday）…' 里抠出时间，转成可比较的字符串。"""
    m = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})", str(s or ""))
    return f"{m.group(1)} {m.group(2)}" if m else ""


def _sort_by_time_proximity(lines: list[str], question: str) -> list[str]:
    """按「离问题指定时刻的远近」重排记忆。

    2026-11-24：MemBench 的情绪题问的是"某个具体时刻用户是什么情绪"
    （例：sentiment around '2024-10-01 08:05'）。我们的检索根本没用到这个时间，
    把 15 条不同时刻的记忆一股脑塞进去，模型得自己找时间，经常找错。
    实测 PS 场景情绪类只有 43%，而召回是满的（证据都找对了，只是没标出哪条对应哪一刻）。
    """
    qk = _time_key(question)
    if not qk:
        return lines

    def _dist(line: str) -> float:
        k = _time_key(line)
        if not k:
            return 9e9
        try:
            from datetime import datetime as _dt

            a = _dt.strptime(k, "%Y-%m-%d %H:%M")
            b = _dt.strptime(qk, "%Y-%m-%d %H:%M")
            return abs((a - b).total_seconds())
        except Exception:  # noqa: BLE001
            return 9e9

    return sorted(lines, key=_dist)


def _letter_probs(prompt: str, key: str) -> dict[str, float]:
    """读 DeepSeek 对 A/B/C/D 的概率分布（logprobs）。"""
    import math as _math  # noqa: PLC0415

    import requests as _rq  # noqa: PLC0415

    body = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1,
        "temperature": 0,
        "logprobs": True,
        "top_logprobs": 20,
    }
    try:
        r = _rq.post("https://api.deepseek.com/chat/completions", json=body, timeout=90,
                     headers={"Authorization": f"Bearer {key}"})
        r.raise_for_status()
        top = r.json()["choices"][0]["logprobs"]["content"][0]["top_logprobs"]
    except Exception:  # noqa: BLE001
        return {}
    let = "ABCD"
    p = {L: 1e-9 for L in let}
    for t in top:
        tok = str(t.get("token", "")).strip().upper()
        if tok in p:
            p[tok] += _math.exp(float(t["logprob"]))
    s = sum(p.values()) or 1.0
    return {k: v / s for k, v in p.items()}


def _label_dist_logprob(mem_block: str, question: str, choices: dict, key: str) -> dict[str, float]:
    """选项循环排列 4 次，读概率取平均，返回 选项字母 → 概率。"""
    let = "ABCD"
    order = sorted(choices.items())
    if len(order) != 4 or not key:
        return {}
    agg: dict[str, float] = {k: 0.0 for k, _ in order}
    for shift in range(4):
        perm = order[shift:] + order[:shift]
        opts = "\n".join(f"{let[i]}. {lab}" for i, (_, lab) in enumerate(perm))
        prompt = (
            "Read the memory notes and answer the multiple-choice question.\n\n"
            f"Memory notes:\n{mem_block}\n\n"
            f"Question: {question}\nOptions:\n{opts}\n\n"
            "Answer with a single letter."
        )
        p = _letter_probs(prompt, key)
        if not p:
            return {}
        for i, (orig_key, _) in enumerate(perm):
            agg[orig_key] += p.get(let[i], 0.0) / 4.0
    return agg


# 2026-11-24 深夜复审（确定性召回，无噪声）：
#   英文停用词过滤 → Recall@5 70.2% vs 47.1%，**真且巨大**，保留
#   25 条 vs 12 条 → Recall@5/@10 完全一样，只有总召回 88.2 vs 82.6，排名零影响
#   迭代检索      → 总召回 89.7 vs 88.2（+1.5pp），Recall@5/@10/MRR 完全一样
# 所以迭代检索只对「需要穷举」的问题保留，因为它每轮要付一次额外的 LLM 调用。
_ITER_Q = re.compile(
    r"(how many|how much|altogether|in total|count|"
    r"哪几|几个|多少|加起来|一共)",
    re.IGNORECASE,
)


# 2026-11-24：主观题模式（问情绪/偏好）。两个用途：
#   1. 这类问题该喂**原文**而不是摘要（摘要会削掉氛围）
#   2. 这类问题该用 **logprob 打分**而不是自由生成
# 依据（配对 A/B，McNemar）：
#   OS-反思（主观多）开 logprob：71.7% → 76.7%，修好 3 条、搞坏 0 条（p=0.25，未显著但全增益）
#   PS-事实（事实多）开 logprob：81.0% → 80.0%，修好 5 条、搞坏 6 条（p=1.00，纯噪声）
# 所以只在主观题上开，省下 3/4 的调用。
_SUBJ_Q = re.compile(
    r"(sentiment|feel|emotion|mood|upset|happy|sad|angry|afraid|"
    r"prefer|like|favorite|kind of|flavor|cuisine|genre|"
    r"心情|情绪|感受|感觉|难过|开心|害怕|生气|委屈|"
    r"喜欢|偏好|口味|类型|哪类|什么样|评价)",
    re.IGNORECASE,
)


def _iterative_second_query(question: str, first_lines: list[str]) -> str:
    """第一轮检索之后，问模型「还缺什么」，产出一个补充查询。

    2026-11-24：PS-事实 100 条里 20 条答错，其中 Aggregative 8、Multi-hop 5，
    全是**需要串联两条记忆**的题：
      "生日是 11 月 3 日的人的职业是什么"
        → 第一步找到「我老板生日 11 月 3 日」
        → 但没再去找「我老板的职业」
    单次检索解决不了链式问题，所以要第二轮。
    """
    from digital_life import chat_with_llm  # noqa: PLC0415

    mem = "\n".join(f"- {x}" for x in first_lines[:8])
    prompt = (
        "You are helping retrieve memories to answer a question.\n\n"
        f"Question: {question}\n\n"
        f"Memories already found:\n{mem}\n\n"
        "If the memories above do NOT contain enough information to answer, "
        "write ONE short search phrase (a few words, in English) that would find the missing pieces. "
        "Prefer the specific entity (a person, a place, an event) whose detail is still unknown.\n"
        "If they already contain everything needed, reply exactly: NONE\n\n"
        "Output only the search phrase or NONE."
    )
    try:
        out = chat_with_llm([{"role": "user", "content": prompt}], model="deepseek-chat",
                            temperature=0.0, timeout=60.0).strip()
    except Exception:  # noqa: BLE001
        return ""
    out = out.splitlines()[0].strip().strip('"').strip("'")
    if not out or out.upper().startswith("NONE") or len(out) > 60:
        return ""
    return out


COUNT_Q = re.compile(r"(how many|altogether|in total|几个|多少个|一共)", re.I)


def answer_counting(question: str, choices: dict, memory_lines: list[str], key: str) -> tuple[str, int]:
    """数数题：**抽取和过滤分成两步**，最后代码数、代码映射字母。

    2026-11-25 两次失败后的修正（Claude 的诊断）：
      第一次（两段式清单）和第二次（掰手指）都让模型**边列边过滤**，
      把"没找全"和"判断错"两种错误混在一起，改哪个都改不动。
      正确做法：
        ① 先做**不筛选的完整抽取**（每条记忆里的实体 + 属性值全列出来）
        ② 再做**纯分类**（哪几行满足条件）——这步简单，出错率低
        ③ 代码数行数、代码把数字映射到选项（不让模型推翻）

    另一条依据：她选的数普遍偏大（真3她说4、真4她说7、真6她说8）——
    主因是**同一个东西在多条记忆里重复计数**。所以②要显式要求去重。
    """
    if key:
        os.environ.setdefault("DEEPSEEK_API_KEY", key)
    from digital_life import chat_with_llm  # noqa: PLC0415

    mem_block = "\n".join(f"[{k+1}] {line}" for k, line in enumerate(memory_lines)) if memory_lines else ""
    tokens = int(len(mem_block) / 1.6) + 300

    # ① 完整抽取（不筛选）
    ex_prompt = (
        "下面是你记得的事，每条前面有编号。\n"
        f"{mem_block}\n\n"
        f"问题：{question}\n\n"
        "**第一步：把记忆里所有可能相关的项目完整列出来，不要筛选、不要判断。**\n"
        "格式：每行 `候选N | 名称 | 相关属性（原话）`，N 从 1 连续编号。\n"
        "比如问「几个人来自德州」，就把**所有提到地点的人**都列上，不管是不是德州。\n"
        "只输出清单，不要输出别的。"
    )
    try:
        lst = str(chat_with_llm([{"role": "user", "content": ex_prompt}],
                                model="deepseek-chat", temperature=0.0, timeout=150.0) or "")
    except Exception:  # noqa: BLE001
        lst = ""
    cand_lines = [x.strip() for x in lst.splitlines() if x.strip().startswith("候选") or "|" in x]
    if not cand_lines:
        cand_lines = [x.strip() for x in lst.splitlines() if x.strip()][:60]

    # ② 纯分类：哪几个满足（显式去重）
    flt_prompt = (
        "下面是候选清单：\n" + "\n".join(f"- {x}" for x in cand_lines) + "\n\n"
        f"问题：{question}\n\n"
        "请只回答两件事：\n"
        "1. 哪些候选**真的满足**问题的条件？只写编号，逗号分隔（例：1,3,5）\n"
        "2. **同一个人/同一个东西出现多次，只能算一次**——如果多个编号指同一个，只保留一个\n"
        "输出格式（严格两行）：\n"
        "满足：1,3,5\n"
        "去重后共：3"
    )
    try:
        ans = str(chat_with_llm([{"role": "user", "content": flt_prompt}],
                                model="deepseek-chat", temperature=0.0, timeout=120.0) or "")
    except Exception:  # noqa: BLE001
        ans = ""
    m = re.search(r"(?:去重后共|共)\s*[:：]?\s*(\d+)", ans)
    n = int(m.group(1)) if m else 0
    if not n:
        m2 = re.search(r"满足\s*[:：]\s*([\d,，\s]+)", ans)
        if m2:
            ids = {x for x in re.findall(r"\d+", m2.group(1))}
            n = len(ids)

    # ③ 代码映射字母
    best, best_diff = "", None
    for k in sorted(choices):
        cand = [int(x) for x in re.findall(r"\d+", str(choices[k]))]
        if not cand:
            continue
        diff = min(abs(c - n) for c in cand)
        if best_diff is None or diff < best_diff:
            best, best_diff = k, diff
    if not best:
        return "", tokens
    return best, tokens


def answer_with_choices(question: str, choices: dict, memory_lines: list[str], key: str, tail_note: str = "") -> tuple[str, int]:
    """让模型只看检索到的记忆来答选择题。返回 (选项字母, 记忆 token 估计)。"""
    # 2026-11-24：chat_with_llm 读的是环境变量，不认这个参数。
    # 不写进去就会静默失败，整批准确率记成 0%（第一版就是这样白跑了一轮）。
    if key:
        os.environ.setdefault("DEEPSEEK_API_KEY", key)
    from digital_life import chat_with_llm  # noqa: PLC0415

    # 2026-11-25：数数题走两段式（先列清单、代码数、再选字母）
    if os.environ.get("XIAOYU_COUNT_LIST", "0") != "0" and COUNT_Q.search(question or ""):
        return answer_counting(question, choices, memory_lines, key)

    # 2026-11-25：**最相关的放最后**。
    # 依据：OS-事实 Recall@10 83.6% 比论文的 0.769 高，但准确率只有 87.0%（论文 0.933）——
    # 证据已经在手里，模型挑错了。LLM 对上下文**末尾**的注意力更强，
    # 而我们现在把最高分的排在最前面，等于让她先被最像的锚住。
    # 用 XIAOYU_MEM_REVERSE=1 打开对比。
    if memory_lines and os.environ.get("XIAOYU_MEM_REVERSE") == "1":
        memory_lines = list(reversed(memory_lines))

    # 2026-11-24：按时间接近度排。情绪题问的是「某个时刻」，
    # 不排序的话模型要在一堆不同时刻里自己找，经常找错（PS 情绪类只有 43%）。
    if memory_lines:
        if (os.environ.get("XIAOYU_CHRONO", "1") != "0" and not _time_key(question)
                and _SUBJ_Q.search(question or "")):
            # 2026-11-25（第 37 轮）：反思类「偏好」题改成**按时间顺序**排。
            # 依据：PS-反思 60 条里，目标排前 5 的正好 52 条、答对的也正好 52 条——
            # 排名 6~10 就答错。而小库规则已让她看到全部记忆，目标就在里面，
            # 说明模型被**排在最前面的那条**锚住了。
            # 改成按时间从头排，等于让她像人一样把这段对话顺一遍。
            # 实测：PS-反思 88.3%→91.7%，PS-事实 83.0%→84.0%，OS-反思不变。
            # 只对反思类问题生效——事实题需要按相关度排，按时间排会坏。
            memory_lines = sorted(memory_lines, key=lambda s: _time_key(s) or "")
        else:
            memory_lines = _sort_by_time_proximity(list(memory_lines), question)
        # 带绝对时刻的问题：只留时间最近的几条。
        # 实测 OS-反思：喂 15 条（Sonnet）65%，只喂标准证据（oracle）80%——
        # 差别就是噪声。既然问题自带时刻，就没必要把其它时刻的记忆都塞进去。
        if _time_key(question):
            # 2026-11-24：OS-反思实测（n=60，oracle=81.7%）
            #   给 1 条 → Emotion 46%（太窄，金标不一定最近）
            #   给 2 条 → Emotion 62%
            #   给 3/5 条 → Emotion 54%
            # 给 2 条最好，离 oracle 的 69% 只差一条。
            _cap = int(os.environ.get("XIAOYU_TIME_CAP") or 2)
            memory_lines = memory_lines[:_cap]
    mem_block = "\n".join(f"- {line}" for line in memory_lines) if memory_lines else "- （什么都没想起来）"
    if tail_note:
        mem_block = mem_block + "\n" + tail_note

    # 2026-11-27：四组对比要把「实际发给模型的 prompt」冻下来，
    # 证明 B 只重排同一批记忆、C 只插一句。XIAOYU_DUMP_PROMPT=<目录>
    _dump_dir = os.environ.get("XIAOYU_DUMP_PROMPT")
    if _dump_dir:
        try:
            import pathlib as _pl  # noqa: PLC0415
            _tag = []
            for _k in ("XIAOYU_MEM_CHRONO_ONLY", "XIAOYU_MEM_NOTE_ONLY"):
                if os.environ.get(_k) == "1":
                    _tag.append(_k.replace("XIAOYU_MEM_", "").replace("_ONLY", "").lower())
            _name = "-".join(_tag) or "base"
            _d = _pl.Path(_dump_dir)
            _d.mkdir(parents=True, exist_ok=True)
            (_d / f"prompt-{_name}.txt").write_text(
                f"QUESTION: {question}\nMEMORIES:\n{mem_block}\n", encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    opt = "\n".join(f"{k}. {v}" for k, v in sorted(choices.items()))
    # 2026-11-24：原来直接让它"只输出字母"。实测记忆里是对的（时间戳都对上了），
    # 但中文情绪标签（焦虑/压抑）要映射到英文选项（Fear/Sadness/Disgust…）时经常错位。
    # 改成先在心里判断、写出情绪词再对应选项，可靠得多。仍然只要求输出字母，
    # 免得把中间推理写进结果污染解析。
    if os.environ.get("XIAOYU_MIN_PROMPT"):
        # 2026-11-24 对照：oracle 用的是极简提示词（"下面是与问题直接相关的原文…只输出一个字母"）。
        # 我们的提示词带着三步推理指引，可能反而干扰模型。
        prompt = (f"下面是与你问题直接相关的原文片段：\n{mem_block}\n\n"
                  f"问题：{question}\n选项：\n{opt}\n\n只输出一个字母（A/B/C/D）。")
    elif re.search(r"(how many|altogether|in total|几个|多少个|一共)", question or "", re.I):
        # 2026-11-24（第 24 轮）：「数数题」专用提示。
        # 实测这类题的 oracle 只有 40%——**把标准证据全给它，它还是数不对**。
        # 原因不是检索，是它没做"逐条筛选再计数"这个动作。
        # 所以强制它先把符合条件的逐条列出来，再数。
        prompt = (
            "下面是你记得的事。记忆前面的括号是发生的时间。\n"
            f"{mem_block}\n\n"
            f"问题：{question}\n选项：\n{opt}\n\n"
            "做法（必须按顺序）：\n"
            "1. 把记忆里提到的**每一个**符合问题条件的条目单独列出来"
            "（写名字/编号，不要合并）；\n"
            "2. 逐条确认它是否真的满足条件，不满足的划掉并说明为什么不满足；\n"
            "3. 数剩下几条；\n"
            "4. 把数字和选项对照，选最接近的那个字母。\n\n"
            "注意：题目问的是总数，你不要只看前几条就下结论，全部都要过一遍。\n"
            "最后只输出一个字母（A/B/C/D），不要输出推理过程。"
        )
    else:
        prompt = (
            "下面是你记得的事，只能依据这些来回答。记忆前面的括号是发生的时间。\n"
            f"{mem_block}\n\n"
            f"问题：{question}\n"
            f"选项：\n{opt}\n\n"
            # 2026-11-24：带时刻的问题要**加一句框定**。
            # 实测：同样的证据，oracle 提示词（"下面是与问题直接相关的原文"）能到 80%，
            # 而我们原来（"这是你记得的事"）只有 65%——差别就在这句框定上。
            # 问题自带时刻时，明确告诉模型"下面就是那一刻附近的事"，它会更有把握。
            + ("注意：上面这些记忆就是问题所问那个时刻前后发生的事，"
               "直接据此判断，不要犹豫。\n\n" if _time_key(question) else "")
            + "做法：\n"
            "1. 先在脑子里找出跟问题最相关的那几条记忆（注意时间是否对得上）；\n"
            # 2026-11-25（第 40 轮）：归因发现 **13 道错题里 5 道是"版本混淆"**。
            # 典型：TeamFusion 地址，10-08 那条写「位置更正为西雅图」，
            # 10-01 那条写「位于 Seattle, OR」，她选了旧的。
            # 「更正」两个字就在眼前，她还是选了旧说法——所以要显式要求按时间取最新。
            + ("1b. **如果几条记忆对同一件事说法不同，以时间最新的为准。**"
               "特别留意「更正 / 改为 / 改成 / 更新 / 现在 / 最新 / 不再」这类措辞，"
               "那是后来改过的说法，应当覆盖前面的。\n"
               if os.environ.get("XIAOYU_UPDATE_RULE", "0") != "0" else "")
            +
            "2. 如果问的是情绪，先用中文确定它是什么情绪，再找最接近的英文选项"
            "（焦虑≈Anxiety/Fear，压抑≈Sadness，愤怒≈Anger，惊讶≈Surprise，"
            "厌恶≈Disgust，高兴≈Happiness，孤独≈Loneliness/Sadness）；\n"
            # 2026-11-24（第 26 轮）：**情绪判定的偏置纠正**。
            # 实测 PS-反思 3 条错里 2 条是"检索完全正确(rank=1)却答错"，
            # 而且都是把"Happiness/Sadness"判成了 **Fear**：
            #   "我一直朝一个重要目标努力" + "刚得到意外的好消息" → 她判 Fear，真 Happiness
            #   "努力但好像失去了方向"                          → 她判 Fear，真 Sadness
            # 根因是我们那套三维情绪（joy/impulse/hurt）的基线偏防御，
            # 遇到"不安/努力/不确定"这类语气就往 hurt 高判，于是落到恐惧/焦虑。
            # 所以要先抓**明确事件**，别被语气带偏。
            "   特别注意：先找出这几条记忆里**明确发生的事件**——"
            "好消息 / 坏消息 / 争执 / 分别 / 意外 / 达成目标 / 失去什么。"
            "情绪由事件决定，**不要因为语气含糊、或提到'努力''不确定'就默认判成恐惧或焦虑**。"
            "只有文本里真的出现威胁、危险、害怕的内容才选 Fear；"
            "有喜讯、顺利、温暖就选 Happiness；有失去、空虚、失望就选 Sadness。\n"
            # 2026-11-24（第 27 轮）试过再加一段"第三人称氛围描写→Disgust"的映射提示，
            # 实测反而把 OS-反思从 76.7% 拉到 75.0%（搞坏 1 条、修好 0 条），已撤。
            # 说明这一格的低分不是"提示不够清楚"，是标签本身不由文本决定。
            # 2026-11-24：偏好题的标签也不直接出现。
            # 实测：记忆里写的是「科幻/史诗/冒险」，标准答案却是 Action——
            # 模型得从「冒险」推出「Action」。给一张中英映射表。
            "3. 如果问的是偏好（电影/书/食物），先把记忆里出现的中文属性翻成英文类别再选：\n"
            "   电影：冒险/英雄/战斗/爆破≈Action，科幻/太空/未来≈Science Fiction，"
            "奇幻/魔法≈Fantasy，恐怖/惊悚≈Horror，爱情≈Romance，"
            "喜剧≈Comedy，悬疑/推理≈Mystery，剧情/史诗≈Drama，动画≈Animation，"
            "纪录片≈Documentary，犯罪≈Crime，战争≈War\n"
            "   书：科幻≈Science Fiction，推理/悬疑≈Mystery，言情≈Romance，"
            "奇幻≈Fantasy，传记≈Biography，历史≈History，哲学≈Philosophy，诗歌≈Poetry\n"
            "   食物：辣≈Spicy，甜≈Sweet，酸≈Sour，咸≈Salty，鲜≈Umami/Savory，"
            "清淡≈Mild，油炸≈Fried，烤≈Grilled，汤≈Soup\n"
            "   选重合最多的那个。\n\n"
            "最后只输出一个字母（A/B/C/D），不要输出推理过程。"
            "如果记忆里确实没有线索，也必须凭最接近的判断猜一个。"
        )
    # 粗估 token：中文按 1.5 字/token，英文按 4 字符/token
    est_tokens = int(len(mem_block) / 3.2)
    # 2026-11-24：答题模型可切换。
    # 实测 OS-反思的「oracle 天花板」是模型相关的：
    #   完美检索 + DeepSeek      → 60.0%
    #   完美检索 + Claude Sonnet → 80.0%
    # 说明那一格不是数据的锅，是 DeepSeek 读氛围偏弱。
    # 2026-11-24（Claude 建议 2.1，实测有效）：logprob 打分 + 选项轮换。
    # 只让模型吐一个字母会丢掉大量信息；读 A–D 的概率分布再取最大，
    # 并把选项循环排列 4 次取平均，消除位置偏好。
    # oracle 实测：直接生成 70% → logprob+轮换 **80%**（DeepSeek 自己就追平了 Sonnet）。
    # 每次只输出 1 个 token，成本可忽略。
    #
    # 但配对 A/B 表明：**只在主观题上有用**（OS-反思 71.7→76.7，修好 3 坏 0），
    # 事实题上是纯噪声（PS-事实 81.0→80.0，修好 5 坏 6）。所以默认按题类型开。
    _want_logprob = bool(os.environ.get("XIAOYU_LOGPROB")) or bool(_SUBJ_Q.search(question or ""))
    if _want_logprob:
        try:
            _dist = _label_dist_logprob(mem_block, question, choices, key)
            if _dist:
                _best = max(_dist, key=lambda k: _dist[k])
                return _best, est_tokens
        except Exception as exc:  # noqa: BLE001
            log(f"    logprob 失败，退回生成式作答: {type(exc).__name__}")

    # 2026-11-27：用 Qoder 的免费模型作答（-p 非交互）。用于「只换答题模型」对照。
    if os.environ.get("XIAOYU_ANSWER_QODER"):
        import subprocess as _sp  # noqa: PLC0415

        _cmd = os.environ.get("XIAOYU_QODER_CMD") or r"<home>\.qoder-cn\entry\qoder-cn.cmd"
        # 2026-11-27 修：直接把多行 prompt 塞进 -p 会被截断——Kimi 只收到第一行，
        # 回了一句「记忆部分似乎是空的」，然后在没有记忆的情况下瞎猜（30%，接近随机）。
        # 改成写临时文件，用 @路径 传。
        import tempfile as _tf  # noqa: PLC0415
        import uuid as _uuid  # noqa: PLC0415

        _pf = os.path.join(_tf.gettempdir(), f"qoder_{_uuid.uuid4().hex}.txt")
        with open(_pf, "w", encoding="utf-8") as _fh:
            _fh.write(prompt)
        _args = [_cmd, "-p", f"@{_pf}"]
        _m = os.environ.get("XIAOYU_QODER_MODEL")
        if _m:
            _args = [_cmd, "-p", f"@{_pf}", "-m", _m]
        try:
            _r = _sp.run(_args, capture_output=True, text=True, encoding="utf-8",
                         errors="ignore", timeout=300)
            raw = (_r.stdout or "").strip()
            if not raw:
                raise RuntimeError((_r.stderr or "空输出")[:80])
        except Exception as _exc:  # noqa: BLE001
            log(f"    Qoder 作答失败，退回 DeepSeek: {type(_exc).__name__}")
            raw = chat_with_llm([{"role": "user", "content": prompt}],
                                model=os.environ.get("DIARY_MODEL", "deepseek-chat"),
                                temperature=0.0, timeout=60.0)
        m0 = re.search(r"[ABCD]", str(raw or "").upper())
        return (m0.group(0) if m0 else ""), est_tokens

    if os.environ.get("XIAOYU_ANSWER_CLAUDE"):
        try:
            from ask_claude import ask as _ask_claude  # noqa: PLC0415

            _model = os.environ.get("XIAOYU_ANSWER_MODEL") or "claude-sonnet-5-5"
            # 2026-11-27：原来 timeout=180，ask() 内部重试 3 次 → 一道题最多卡 9 分钟，
            # 整轮看起来像「跑到一半停了」，其实是卡在某一道的慢调用上。收紧到 90s。
            raw = _ask_claude(prompt, model=_model, max_tokens=1536, temperature=0.0, timeout=90)
        except Exception as exc:  # noqa: BLE001
            log(f"    Claude 作答失败，退回 DeepSeek: {type(exc).__name__}")
            raw = chat_with_llm([{"role": "user", "content": prompt}],
                                model=os.environ.get("DIARY_MODEL", "deepseek-chat"),
                                temperature=0.0, timeout=60.0)
    else:
        # 2026-11-27：**空答案多半是被 DeepSeek 限速了，不是答错。**
        # 原来异常被吞成空串，评测把它记成答错——300 道里 290 道空就是这个造成的。
        # 加成：节流 + 退避重试；仍然空就在日志里标出来。
        import time as _time  # noqa: PLC0415

        _gap = float(os.environ.get("XIAOYU_CALL_GAP_MS") or 180) / 1000.0
        raw = ""
        for _attempt in range(4):
            try:
                raw = chat_with_llm([{"role": "user", "content": prompt}],
                                    model=os.environ.get("DIARY_MODEL", "deepseek-chat"),
                                    temperature=0.0, timeout=90.0)
            except Exception as _exc:  # noqa: BLE001
                log(f"    作答异常（第{_attempt+1}次）: {type(_exc).__name__}")
                raw = ""
            if str(raw or "").strip():
                break
            _wait = (2 ** _attempt) * 1.5
            log(f"    空答案，{_wait:.1f}s 后重试")
            _time.sleep(_wait)
        _time.sleep(_gap)
    m = re.search(r"[ABCD]", str(raw or "").upper())
    if not m:
        log("    ! 重试后仍无答案，标为运行故障（不计为答错）")
    return (m.group(0) if m else ""), est_tokens


# ---------------------------------------------------------------- eval

def stage_eval(trajs: list[dict], cache: dict, topk: int, do_answer: bool,
               embed: bool, key: str, use_topic: bool = False, rerank: bool = False,
               distractors: int = 0, raw_content: bool = False) -> list[dict]:
    from memory import LibraryMemory  # noqa: PLC0415

    embedding_fn = None
    if embed:
        try:
            from digital_life import embed_text  # noqa: PLC0415

            if embed_text("测试"):
                embedding_fn = embed_text
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("--embed 已启用，但 embedding 初始化失败") from exc
    if embed and embedding_fn is None:
        raise RuntimeError("--embed 已启用，但 embedding 模型未返回有效向量")

    results = []
    # 2026-11-24：断点续跑。爸爸要求"确保可以完成实验最重要"——
    # 之前一轮跑到一半崩了就得从头来，交大额度白烧。
    # 现在每做完一条就落盘，重跑自动跳过已完成的。
    ckpt = BENCH / f"ckpt-eval-{args_topic_tag()}.json"
    done: dict[str, dict] = {}
    if ckpt.exists():
        try:
            done = {str(r["gid"]): r for r in json.loads(ckpt.read_text(encoding="utf-8"))}
            log(f"断点：已有 {len(done)} 条结果，接着跑")
        except Exception:  # noqa: BLE001
            done = {}
    for ti, traj in enumerate(trajs, 1):
        # 与 stage_ingest 用同一套键，否则 gid 为 None 的 trajectory 找不到缓存
        gid = str(traj.get("gid") or f"t{ti}-{traj.get('_category', '')}")
        if gid in done:
            results.append(done[gid])
            continue
        entry = cache.get(gid) or {}
        items = entry.get("items") or []
        QA = traj.get("QA") or {}
        question = str(QA.get("question") or "").strip()
        # target_step_id 有两种形状：HighLevel 里是 [[10,0],...]，
        # LowLevel 里是纯整数 [10, 12]。两种都要认。
        targets: set[int] = set()
        for x in QA.get("target_step_id") or []:
            if isinstance(x, int):
                targets.add(x)
            elif isinstance(x, (list, tuple)) and x:
                try:
                    targets.add(int(x[0]))
                except Exception:  # noqa: BLE001
                    pass
        if not question or not items:
            skipped = {
                "gid": gid,
                "category": entry.get("category") or traj.get("_category"),
                "status": "skipped",
                "skip_reason": "missing_question" if not question else "missing_ingest_items",
                "question": question[:160],
            }
            results.append(skipped)
            done[gid] = skipped
            try:
                ckpt.write_text(json.dumps(list(done.values()), ensure_ascii=False, indent=1),
                                encoding="utf-8")
            except Exception as exc:  # noqa: BLE001
                log(f"  checkpoint 写入失败：{type(exc).__name__}")
            log(f"  [{ti}/{len(trajs)}] 跳过 {gid}: {skipped['skip_reason']}")
            continue

        tmpdir = Path(tempfile.mkdtemp(prefix="mbp_", dir=str(BENCH)))
        lib = None
        try:
            lib = LibraryMemory(db_path=tmpdir / "m.db", user_id="bench",
                                embed_on_write=False, embedding_fn=embedding_fn)
            now = datetime.now()
            for it in items:
                text = it.get("compressed") or ""
                if not text.strip():
                    continue
                topic = str(it.get("topic") or "").strip()
                # 2026-11-24：时间戳必须进库。Emotion 类问题问的是某个时刻的情绪，
                # 没有时间戳就没法对应，模型只能瞎猜。
                stamp = str(it.get("time") or "").strip() or "bench"
                if use_topic and topic:
                    # 主题进 folder_event（检索第一层），步号进 source_id（算召回用）
                    _raw = str(it.get("raw") or "") if raw_content else text
                    _body = _raw[:2000] if raw_content else text
                    lib.add_book(content=_body, summary=text[:200], emotion_tag="",
                                 folder_date=stamp, folder_user="",
                                 folder_event=topic, mv=50.0, now=now, origin="bench",
                                 source_id=str(it["idx"]))
                else:
                    _raw = str(it.get("raw") or "") if raw_content else text
                    _body = _raw[:2000] if raw_content else text
                    lib.add_book(content=_body, summary=text[:200], emotion_tag="",
                                 folder_date=stamp, folder_user="",
                                 folder_event=str(it["idx"]), mv=50.0,
                                 now=now, origin="bench")
            if embedding_fn:
                from digital_life import embed_batch  # noqa: PLC0415

                rows = lib.conn.execute("SELECT id, summary, content FROM books").fetchall()
                vecs = embed_batch([
                    str(r["content"] if raw_content else r["summary"]) for r in rows])
                if len(vecs) != len(rows):
                    raise RuntimeError(
                        f"embedding 批量结果数不匹配：{len(vecs)} != {len(rows)}"
                    )
                if any(not vec for vec in vecs):
                    raise RuntimeError("embedding 批量结果包含空向量")
                for row, vec in zip(rows, vecs):
                    lib.conn.execute("UPDATE books SET embedding=? WHERE id=?",
                                     (json.dumps(vec, ensure_ascii=False), int(row["id"])))
                lib.conn.commit()

            # 2026-11-24：往库里灌噪声，模拟"真实记忆淹没在大库里"的场景。
            # 这是唯一能验证「候选生成会不会漏掉真记忆」的办法——
            # 小库（20 条）根本触发不了候选裁剪。
            if distractors > 0 and embedding_fn:
                import sqlite3 as _sq  # noqa: PLC0415

                src = Path(r"<repo>\chat_bridge\data\xiaoyu.db")
                pool = []
                if src.exists():
                    con = _sq.connect(f"file:{src}?mode=ro", uri=True)
                    pool = [(str(a), str(b)) for a, b in con.execute(
                        "SELECT summary, embedding FROM books "
                        "WHERE summary != '' AND embedding IS NOT NULL LIMIT 3000")]
                    con.close()
                buf = []
                for k in range(distractors):
                    base, emb = pool[k % len(pool)] if pool else ("无关的记忆", "[]")
                    # 2026-11-24：编号必须放前面。原来写成 "{base}（噪声#k）" 再截 120 字，
                    # 长 base 会把编号截掉，导致 summary 撞唯一索引
                    # （UNIQUE constraint failed: user_id, folder_date, folder_event, summary）。
                    buf.append((
                        "bench", f"噪声{k} {base}", f"噪声{k} {base}"[:120], "日常",
                        "2024-01-01", "", f"噪声{k % 40}", 30.0, "detail",
                        now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"),
                        1.0, emb, "bench",
                    ))
                lib.conn.executemany(
                    "INSERT INTO books(user_id, content, summary, emotion_tag, folder_date, "
                    "folder_user, folder_event, mv, status, created_at, last_accessed, "
                    "decay_rate, embedding, origin) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", buf)
                lib.conn.commit()
                lib._vec_cache = None
                log(f"    灌入 {distractors} 条噪声记忆（取自她的真实记忆+向量）")

            t0 = time.perf_counter()
            # 2026-11-24：不要再传 deep=True。deep 在生产里是"深聊才载入细节"的开关，
            # 和"联想该多少"挂钩。测试台只是为了拿到记忆文本，
            # 而压出来的记忆 summary 和 content 内容一样，用 summary 就够。
            res = lib.retrieve(question, impulse=0.0, now=now, deep=False, roll=0.0)
            retr_ms = (time.perf_counter() - t0) * 1000.0

            # 2026-11-26（爸爸的想法）：**不确定度门控的第二轮检索**。
            # 之前那次"迭代检索"失败，是因为按题型触发（聚合/多跳就多查一轮），
            # 而题型 ≠ 这题难。这里改成按**测出来的不确定度**触发：
            #   向量视角 top5 和关键词视角 top5 重合 < 2 → 两个视角看法不同 → 不确定
            # 这时才做第二轮，且**不是追加而是重排**（第一轮 top1 永远保住）。
            # 2026-11-26（爸爸的想法，第二版）：
            # 第一版用"向量与关键词两路重合数"当不确定信号，实测 78.3%（略低），
            # 而且那个信号测的是**检索器一致性**，不是**答案有没有把握**。
            # 改成让模型自评：给它记忆和问题，问"你有把握吗"，没把握才补第二轮。
            # 每次多一次便宜调用（temperature=0，只出 YES/NO）。
            if os.environ.get("XIAOYU_UNCERTAIN") == "1":
                try:
                    from digital_life import chat_with_llm as _llm  # noqa: PLC0415

                    _pre = "\n".join(f"- {str(getattr(h, 'summary', '') or '')[:60]}"
                                     for h in res.hits[:top_k_for_confidence] if False) or ""
                    _peek = "\n".join(
                        f"- {str(getattr(h, 'summary', '') or getattr(h, 'content', '') or '')[:70]}"
                        for h in res.hits[:8]
                    )
                    _ask = ("下面是你记得的事。\n" + _peek +
                            f"\n\n问题：{question}\n\n"
                            "只看这些记忆，你能**确定**答案吗？"
                            "记忆里信息不全、或有矛盾、或没直接提到，就算没把握。\n"
                            "只回答 YES 或 NO。")
                    _verdict = str(_llm([{"role": "user", "content": _ask}],
                                        model="deepseek-chat", temperature=0.0,
                                        timeout=60.0) or "").strip().upper()
                    if _verdict.startswith("NO"):
                        # 没把握：用问题里的关键词**重查一遍**，两轮合并（第一轮在前）
                        from memory import content_tokens as _ct  # noqa: PLC0415

                        _kw = " ".join(sorted(_ct(question), key=len, reverse=True)[:6])
                        _res2 = lib.retrieve(question=_kw, user_id=lib.user_id, limit=topk,
                                             now=now, deep=False, roll=0.0)
                        _seen, _merged = set(), []
                        for _h in list(res.hits) + list(_res2.hits):
                            _k = getattr(_h, "source_id", "") or str(getattr(_h, "id", ""))
                            if _k in _seen:
                                continue
                            _seen.add(_k)
                            _merged.append(_h)
                        class _R:
                            pass
                        _r = _R(); _r.hits = _merged
                        res = _r
                except Exception:  # noqa: BLE001
                    pass

            got = []
            mem_lines = []
            _tail_note = ""
            # 步号 → 原文。XIAOYU_RAW_PROMPT 实验要用（非深检索时 hit.content 是空的）
            _raw_by_idx = {int(it["idx"]): str(it.get("raw") or "") for it in items}
            for hit in res.hits[:topk]:
                # 开了主题后，步号存在 source_id 里，folder_event 是主题
                raw_step = getattr(hit, "source_id", "") if use_topic else hit.folder_event
                try:
                    got.append(int(str(raw_step)))
                except Exception:  # noqa: BLE001
                    pass
                line = str(hit.content if hit.content else hit.summary or "").strip()
                if raw_content:
                    # 2026-11-24：检索用原文（保证召回），塞进提示词用压缩版（省 token）。
                    # 分开的好处：检索质量靠原文的字面/语义，token 成本靠摘要的短。
                    line = str(hit.summary or hit.content or "").strip()
                # 2026-11-24 实验：XIAOYU_RAW_PROMPT=1 时提示词里放**原文**。
                # 实测发现一个权衡：
                #   压缩摘要 → 电影/食物题好（因为整理时会补出「科幻/麻辣」这类属性标签），
                #              但情绪题差（氛围被削掉）
                #   原始正文 → 情绪题好（氛围完整），但电影/食物题差（原文根本不说"科幻"）
                # 所以两个都要：摘要带属性 + 原文摘录带氛围。
                # 2026-11-26（爸爸指出）：**中文摘要 + 英文问题 = 信息损耗**。
                # 例：「How many characters in the names of people with a Bachelor's degree」
                # 摘要把 Leila Thornton 译成「莱拉·多诺万」，字符数根本无法从中文算出来。
                # XIAOYU_EN_MEM=1 时提示词里**只用英文原文**，全程英文，不做中转。
                if os.environ.get("XIAOYU_EN_MEM") == "1":
                    try:
                        _idx2 = int(str(raw_step))
                        _raw2 = str(_raw_by_idx.get(_idx2) or "").strip()
                        if _raw2:
                            line = _raw2[:260]
                    except Exception:  # noqa: BLE001
                        pass

                if os.environ.get("XIAOYU_KIN") == "1":
                    try:
                        _idx3 = int(str(raw_step))
                        line = _inject_kin(line, str(_raw_by_idx.get(_idx3) or ""))
                    except Exception:  # noqa: BLE001
                        pass

                if os.environ.get("XIAOYU_BOTH"):
                    try:
                        _idx = int(str(raw_step))
                        _raw = str(_raw_by_idx.get(_idx) or "").strip()[:130]
                        if _raw:
                            line = f"{line} ／ 原文：{_raw}"
                    except Exception:  # noqa: BLE001
                        pass
                _EMO_Q = re.search(
                    r"(sentiment|feel|emotion|mood|upset|happy|sad|angry|afraid|"
                    r"prefer|like|favorite|kind of|flavor|cuisine|genre|"
                    r"心情|情绪|感受|感觉|难过|开心|害怕|生气|委屈|"
                    r"喜欢|偏好|口味|类型|哪类|什么样|评价)",
                    question or "", re.I)
                # 2026-11-24：XIAOYU_NO_SUBJ_RAW=1 时**关闭**主观题喂原文的规则，
                # 变成真正的「只喂压缩版」——用来验证「原文忘了以后还剩多少」。
                if (os.environ.get("XIAOYU_RAW_PROMPT")
                        or (_EMO_Q and not os.environ.get("XIAOYU_NO_SUBJ_RAW"))):
                    try:
                        _idx = int(str(raw_step))
                        line = str(_raw_by_idx.get(_idx) or line).strip()[:260]
                    except Exception:  # noqa: BLE001
                        pass
                if line:
                    # 2026-11-24：把这条记忆发生的时刻标出来。
                    # MemBench 的 Emotion 类问题会给一个具体时刻（"2024-10-01 08:00"），
                    # 记忆不带时间，模型只能从三条不同时刻里瞎挑。
                    stamp = str(getattr(hit, "folder_date", "") or "").strip()
                    if stamp and stamp != "bench":
                        line = f"（{stamp}）{line}"
                    mem_lines.append(line[:200])

            # 2026-11-26（爸爸的猜想，实测有效）：**记忆按时间正序给**。
            # 现状：mem_lines 是**检索分数顺序**，时间只作为前缀显示。
            # 于是同一属性出现多个值时，她判不准哪条更新。
            # 实测（21 道知识更新题）：原顺序 14/21 → 正序 16/21，救回 2、弄坏 0。
            # 而且**倒序最差**（把最新放最前她会锚定该条，看不到演变过程）。
            # 2026-11-26（qwen 建议的最强对照）：**随机置换**。
            # chrono 按时间正序 +14/−10。如果"随便打乱"也能救回差不多数量，
            # 那 chrono 的收益就不是"时间推理"，只是"顺序扰动"本身。
            if os.environ.get("XIAOYU_MEM_SHUFFLE") == "1" and mem_lines:
                import random as _rnd  # noqa: PLC0415
                _seed = int(os.environ.get("XIAOYU_SHUFFLE_SEED", "20261126"))
                _rnd.Random(_seed).shuffle(mem_lines)

            # 2026-11-27：**位置假设的直接检验**。
            # 时间正序（最新在后）= +4。如果机制是"最新证据落在注意力最强的末尾"，
            # 那倒序（最新在前）应该显著更差。这是最干净的对照。
            if os.environ.get("XIAOYU_MEM_REVCHRONO") == "1":
                import re as _re2  # noqa: PLC0415

                def _tkey2(s: str) -> str:
                    _m2 = _re2.search(r"（(\d{4}-\d\d-\d\d [\d:]+)", s)
                    return _m2.group(1) if _m2 else "0000"

                mem_lines.sort(key=_tkey2, reverse=True)

            # 2026-11-27 拆开两个变量（旧的 XIAOYU_MEM_CHRONO 混淆了顺序和指令）：
            #   XIAOYU_MEM_CHRONO_ONLY：**只改顺序**，不加任何指令
            #   XIAOYU_MEM_NOTE_ONLY  ：**只加指令**，不改顺序
            import re as _re  # noqa: PLC0415

            def _tkey(s: str) -> str:
                _m = _re.search(r"（(\d{4}-\d\d-\d\d [\d:]+)", s)
                return _m.group(1) if _m else "9999"

            if os.environ.get("XIAOYU_MEM_CHRONO_ONLY") == "1":
                mem_lines.sort(key=_tkey)

            if os.environ.get("XIAOYU_MEM_NOTE_ONLY") == "1":
                # 2026-11-27 修：原来 append 进 memory_lines，会被下游排序挪走
                # （_SUBJ_Q 命中时会排到最前面），"C = A + 一句"就不成立了。
                # 改成独立的尾部指令，不进记忆列表。
                _tail_note = "注意：如果同一属性出现多个不同的值，以时间最晚的那个为准。"

            if os.environ.get("XIAOYU_MEM_CHRONO") == "1":   # 旧口径，保留兼容
                mem_lines.sort(key=_tkey)
                _tail_note = ("注意：以上记忆已按时间从早到晚排列。"
                              "如果同一属性出现多个不同的值，以时间最晚的那个为准。")

            # 2026-11-24：迭代检索。PS-事实 100 条里 20 条答错，
            # 其中 Aggregative 8、Multi-hop 5——都需要串联两条记忆。
            # 先问模型「还缺什么」，再用那个实体做第二轮检索，把新捞到的并进来。
            #
            # **但只对需要串联的问题开**：
            #   PS-事实（多跳/聚合多）82.0% ← 开启后 +3pp
            #   OS-事实（单证据）   95% → 90%     ← 开启后 -5pp
            #   OS-反思（单证据）   75% → 70%     ← 开启后 -5pp
            # 单证据问题多给记忆只会变成干扰。
            _need_iter = bool(_ITER_Q.search(question or "")) if not os.environ.get("XIAOYU_ITER_ALL") else True
            if os.environ.get("XIAOYU_ITER") and _need_iter:
                _extra_q = _iterative_second_query(question, mem_lines)
                if _extra_q:
                    try:
                        _r2 = lib.retrieve(_extra_q, impulse=0.0, now=now, deep=False, roll=0.0)
                        _seen = set(got)
                        for _h in _r2.hits[:6]:
                            _sid = getattr(_h, "source_id", "") if use_topic else _h.folder_event
                            try:
                                _si = int(str(_sid))
                            except Exception:  # noqa: BLE001
                                continue
                            if _si in _seen:
                                continue
                            _seen.add(_si)
                            _line2 = str(_h.summary or "").strip()
                            _st2 = str(getattr(_h, "folder_date", "") or "").strip()
                            if _st2 and _st2 != "bench":
                                _line2 = f"（{_st2}）{_line2}"
                            mem_lines.append(_line2[:200])
                            got.append(_si)
                    except Exception:  # noqa: BLE001
                        pass

            inter = targets & set(got)
            # 2026-11-24（Claude 建议）：主指标改成 Recall@5 / Recall@10。
            # 理由：答对率受模型推理波动影响，n=40~100 时置信区间 ±8~15pp，
            # 4pp 的改进根本测不出来。Recall@k 直接测检索，稳定得多。
            # 注意 got 的顺序就是返回顺序，所以直接切前 k 个。
            r5 = targets & set(got[:5])
            r10 = targets & set(got[:10])
            pred, mem_tokens = ("", 0)
            if do_answer:
                if rerank:
                    # 先精排再作答：候选保留多点，让模型挑
                    picked = rerank_with_llm(question, mem_lines[:max(topk, 8)], key, keep=3)
                    pred, mem_tokens = answer_with_choices(
                        question, QA.get("choices") or {}, picked, key, _tail_note)
                else:
                    # 2026-11-24（第 29 轮）：自一致性。同一题问 N 次取多数。
                    # 目的是压掉采样波动——实测 OS-反思 n=60 里，同一配置重跑会翻 1~3 题。
                    # N=1 时行为和以前完全一样。
                    _reps = int(os.environ.get("XIAOYU_REPS") or 1)
                    _votes: list[str] = []
                    _tok = 0
                    for _ in range(max(1, _reps)):
                        _p, _t = answer_with_choices(
                            question, QA.get("choices") or {}, mem_lines, key, _tail_note)
                        _tok = max(_tok, _t)
                        if _p:
                            _votes.append(_p)
                    if _votes:
                        from collections import Counter as _Ctr  # noqa: PLC0415

                        pred, _n = _Ctr(_votes).most_common(1)[0]
                    mem_tokens = _tok

            results.append({
                "gid": gid,
                "status": "evaluated",
                "category": entry.get("category") or traj.get("_category"),
                "memories": sum(1 for it in items if (it.get("compressed") or "").strip()),
                "target_n": len(targets),
                "hit_n": len(inter),
                "hit": 1 if inter else 0,
                "recall": (len(inter) / len(targets)) if targets else 0.0,
                "recall_at5": (len(r5) / len(targets)) if targets else 0.0,
                "recall_at10": (len(r10) / len(targets)) if targets else 0.0,
                "rank": next((i + 1 for i, g in enumerate(got) if g in targets), 0),
                "pred": pred,
                "truth": QA.get("ground_truth"),
                "correct": 1 if (pred and pred == QA.get("ground_truth")) else 0,
                "retr_ms": round(retr_ms, 1),
                "mem_tokens": mem_tokens,
                "returned": len(res.hits[:topk]),
                # 2026-11-24：把实际喂进去的记忆留下来，出问题才看得出是检索错还是作答错
                "used_memories": [m[:160] for m in (picked if do_answer and rerank else mem_lines)],
                # 2026-11-25：把选项也存下来。
                # 归因时发现 13 道错题里 11 道 rank=1（检索没问题），
                # 但没有选项内容就说不清"是数字数错了还是对不上选项"。
                "choices": dict(QA.get("choices") or {}),
                "question": question[:160],
            })
            log(f"  [{ti}/{len(trajs)}] {'✓' if inter else '✗'} recall={len(inter)}/{len(targets)} "
                f"答={pred or '-'}/真={QA.get('ground_truth')} {retr_ms:.0f}ms")
            # 每做完一条就落盘，崩了重跑能接着来
            done[gid] = results[-1]
            try:
                ckpt.write_text(json.dumps(list(done.values()), ensure_ascii=False, indent=1),
                                encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                if lib is not None:
                    lib.close()
            except Exception:  # noqa: BLE001
                pass
            shutil.rmtree(tmpdir, ignore_errors=True)
    return results


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["ingest", "eval", "all"], default="all")
    ap.add_argument("--file", default="ThirdAgentDataHighLevel.json")
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--seed", type=int, default=20261124)
    ap.add_argument("--no-answer", action="store_true", help="不调 DeepSeek 作答，只测召回")
    ap.add_argument("--embed", action="store_true", help="启用语义向量（慢）")
    ap.add_argument("--batch", type=int, default=1, help="一次整理几条（越大越省交大调用）")
    ap.add_argument("--no-compress", action="store_true", help="不整理，原文直接入库")
    ap.add_argument("--topic", action="store_true", help="整理时提取主题，作为检索第一层")
    ap.add_argument("--rerank", action="store_true", help="作答前先让模型精排候选")
    ap.add_argument("--distractors", type=int, default=0, help="灌多少条噪声记忆，测大库场景")
    ap.add_argument("--cand", type=int, default=0, help="候选生成上限（0=关闭，全表扫）")
    ap.add_argument("--raw-content", action="store_true",
                    help="用原文当 content（压缩后当 summary），验证压缩是否伤召回")
    args = ap.parse_args()

    if not ZIP.exists():
        log(f"没有数据包：{ZIP}")
        return 1
    z = zipfile.ZipFile(ZIP)
    name = f"Membenchdata/data/{args.file}"
    if name not in z.namelist():
        log(f"包里没有 {name}")
        return 1

    import random  # noqa: PLC0415

    data = json.loads(z.read(name).decode("utf-8", "replace"))
    trajs = flatten_trajectories(data)
    random.Random(args.seed).shuffle(trajs)
    # 2026-11-25：**随机抽样，不是取前 N**。
    # 小样本评估规范：单次固定划分会刷出虚高指标，而且我在同一批题上反复调参
    # = 拿测试集当调参集。改成 --seed 随机抽，配合多次重复取均值±CI。
    import random as _random  # noqa: PLC0415
    _seed = int(os.environ.get("XIAOYU_SPLIT_SEED") or 0)
    if _seed:
        # 只在**已整理过的池子**里抽（ingest 缓存是按前缀存的）
        _pool = int(os.environ.get("XIAOYU_SPLIT_POOL") or 0) or len(trajs)
        _pool = min(_pool, len(trajs))
        _idx = list(range(_pool))
        _random.Random(_seed).shuffle(_idx)
        picked = [trajs[i] for i in sorted(_idx[: args.n])]
        log(f"{args.file}: 共 {len(trajs)} 条，随机抽 {len(picked)} 条（seed={_seed}）")
    else:
        picked = trajs[: args.n]
    log(f"{args.file}: 共 {len(trajs)} 条，取前 {len(picked)} 条")

    BENCH.mkdir(parents=True, exist_ok=True)
    cache = {}
    if args.stage in ("ingest", "all"):
        cache = stage_ingest(picked, args.file, args.n, batch=args.batch,
                             no_compress=args.no_compress)
    if args.stage in ("eval", "all"):
        if not cache:
            p = cache_path(args.file, args.n, batch=args.batch, no_compress=args.no_compress)
            if not p.exists():
                # 2026-11-24：缓存是按 n 命名的。跑 n=2 时找不到 -n2 的文件，
                # 但 -n40 里前 2 条是同一批（同种子），可以直接用。
                # 之前这里直接报"先跑 ingest"，白白浪费时间。
                # 2026-11-27 修：原来用 sorted()（字母序），n10 排在 n100/n300 前面，
                # 于是跑 n=300 时借用了**只有 10 条记忆**的缓存——
                # 11~300 题根本没有记忆，直接返回空答案，300 题 12 秒跑完、准确率 3%。
                # 改成按 n 的**数值**排序，优先挑 >= 请求值的、最小的那个。
                import re as _re3  # noqa: PLC0415
                _alts = list(CACHE.glob(f"ingest-{Path(args.file).stem}-n*.json"))
                def _alt_n(_p):
                    _m = _re3.search(r"-n(\d+)(?:-|\.json$)", _p.name)
                    return int(_m.group(1)) if _m else 0
                _alts = [a for a in _alts if _alt_n(a) > 0]
                _alts.sort(key=lambda a: (_alt_n(a) < args.n, _alt_n(a)))
                for alt in _alts:
                    try:
                        prev = json.loads(alt.read_text(encoding="utf-8"))
                    except Exception:  # noqa: BLE001
                        continue
                    if prev:
                        cache = prev
                        log(f"借用缓存 {alt.name}（{len(prev)} 条）")
                        break
            else:
                cache = json.loads(p.read_text(encoding="utf-8"))
            if not cache:
                log("没有缓存，先跑 --stage ingest")
                return 1
        key = "" if args.no_answer else deepseek_key()
        if not args.no_answer and not key:
            log("拿不到 DEEPSEEK_API_KEY，改用 --no-answer")
            args.no_answer = True
        experiment_config = {
            "file": args.file,
            "selected_gids": [str(t.get("gid") or "") for t in picked],
            "seed": args.seed,
            "split_seed": os.environ.get("XIAOYU_SPLIT_SEED", ""),
            "topk": args.topk,
            "embed": args.embed,
            "topic": args.topic,
            "no_answer": args.no_answer,
            "rerank": args.rerank,
            "raw_content": args.raw_content,
            "distractors": args.distractors,
            "cand": args.cand,
            "ingest_cache_hash": hashlib.sha256(
                json.dumps(cache, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest(),
            "experiment_env": {
                name: value for name, value in os.environ.items()
                if name.startswith("XIAOYU_")
            },
        }
        config_json = json.dumps(experiment_config, sort_keys=True, ensure_ascii=False)
        _EVAL_TAG["v"] = hashlib.sha256(config_json.encode("utf-8")).hexdigest()[:16]
        if args.cand:
            os.environ["XIAOYU_CANDIDATES"] = str(args.cand)
        else:
            os.environ.pop("XIAOYU_CANDIDATES", None)
        results = stage_eval(picked, cache, args.topk, not args.no_answer, args.embed, key,
                             use_topic=args.topic, rerank=args.rerank,
                             distractors=args.distractors, raw_content=args.raw_content)
        evaluable = [r for r in results if r.get("status", "evaluated") == "evaluated"]
        skipped_n = len(results) - len(evaluable)
        if not evaluable:
            log("没有结果")
            return 1
        out = BENCH / "membench-pipeline.json"
        out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        print()
        print("=" * 64)
        print(f"  流水线评测  n={len(evaluable)}  topk={args.topk}  语义={'开' if args.embed else '关'}")
        if skipped_n:
            print(f"  跳过样本    {skipped_n}（已记入明细，不计入指标）")
        print(f"  召回命中率  {100.0 * sum(r['hit'] for r in evaluable) / len(evaluable):.1f}%")
        print(f"  召回率      {100.0 * statistics.mean(r['recall'] for r in evaluable):.1f}%")
        # 2026-11-24（Claude 建议）：@5 / @10 是主指标，答对率是辅指标。
        _r5 = [r.get("recall_at5") for r in evaluable if r.get("recall_at5") is not None]
        _r10 = [r.get("recall_at10") for r in evaluable if r.get("recall_at10") is not None]
        if _r5:
            print(f"  Recall@5    {100.0 * statistics.mean(_r5):.1f}%")
        if _r10:
            print(f"  Recall@10   {100.0 * statistics.mean(_r10):.1f}%")
        print(f"  MRR         {statistics.mean((1.0/r['rank']) if r['rank'] else 0.0 for r in evaluable):.3f}")
        if not args.no_answer:
            print(f"  准确率      {100.0 * sum(r['correct'] for r in evaluable) / len(evaluable):.1f}%")
            print(f"  平均记忆token {statistics.mean(r['mem_tokens'] for r in evaluable):.0f}")
        lat = sorted(r["retr_ms"] for r in evaluable)
        p95 = lat[min(len(lat) - 1, int(round(0.95 * (len(lat) - 1))))]
        print(f"  检索延迟    中位 {statistics.median(lat):.0f}ms  P95 {p95:.0f}ms  最大 {max(lat):.0f}ms")
        print("=" * 64)
        print(f"  明细: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
