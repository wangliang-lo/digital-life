"""SQLite 记忆图书馆、检索结果与原子去重。"""
from __future__ import annotations

import functools
import json
import math
import random
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Sequence

from config import (
    CONTROL_DECAY_MAX, CONTROL_DECAY_MIN, DECAY_AMOUNT, DECAY_IDLE_DAYS,
    DREAM_SAMPLE_LIMIT, EMOTIONAL_RECALL_THRESHOLD, GHOST_MV_THRESHOLD,
    GHOST_TTL_DAYS, MAX_HITS, MEMORY_CUE_PATTERN, MIN_RELEVANCE_SCORE, MV_CAP, MV_PER_HIT,
    NEGATIVE_EMOTIONS, PERSONA_MEMORY_WINDOW, RECENCY_HALF_WINDOW_DAYS, RECENCY_WEIGHT, REINFORCE_MV_BOOST,
    RETRIEVAL_PROB_DIVISOR,
    RETRIEVAL_PROB_MIN, SUMMARY_MV_THRESHOLD,
)
from utils import clamp, dt_to_str, relative_time_label, str_to_dt

# 问句脚手架：这些词只说明"对方在问一件事"，本身不是要检索的内容。
# 不剔掉的话，问「你还记得下雨天的事吗」会命中一堆「你还记得我昨天说加班吗」这种
# 旧问句记忆——它们含的"你还记得"字样拿满关键词分，真正发生的事反而被挤掉。
QUESTION_SCAFFOLD = re.compile(
    r"(你还记得|还记得吗|还记得|记不记得|记得吗|我上次|上次说|上次|跟你说过|跟你说|"
    r"和你说过|和你说|我们之前说过|我们之前说|我们之前|之前说过|之前说|"
    r"那件事|那件事吗|那事儿|你说过|我说过|叫什么|叫啥|什么来着|来着)"
)


def strip_question_scaffold(text: str) -> str:
    """把问句里的"脚手架"去掉，只留下真正要检索的内容。"""
    cleaned = QUESTION_SCAFFOLD.sub(" ", str(text or ""))
    return re.sub(r"\s+", " ", cleaned).strip()


# 关键词打分时不算数的字：只出现在词尾/词首的语气和代词，或者本身就是空词。
_FUNCTION_CHARS = "的了是吗呢吧啊呀嘛哦我你他她它说个这那还就都也"
KEYWORD_STOPWORDS = frozenset(
    {"什么", "怎么", "为什么", "时候", "这个", "那个", "我们", "你们", "他们",
     "一下", "一点", "事情", "东西", "什么样", "告诉我"}
)


_JIEBA = None
_JIEBA_TRIED = False


def _jieba():
    global _JIEBA, _JIEBA_TRIED
    if not _JIEBA_TRIED:
        _JIEBA_TRIED = True
        try:
            import logging as _logging  # noqa: PLC0415
            import jieba as _jb  # noqa: PLC0415

            _jb.setLogLevel(_logging.WARNING)
            _JIEBA = _jb
        except Exception:  # noqa: BLE001
            _JIEBA = None
    return _JIEBA


def content_tokens(text: str) -> list[str]:
    """从一句话里取真正有信息量的词。

    2026-11-16 改：以前是"2~4 字滑窗"，一句"看书呢"会拆出 看书/书呢，
    满库都是这些碎片，稀有度拉不开、检索被噪声淹。现在优先用 jieba 分词，
    只留实词；没有 jieba 时退回滑窗（老行为）。
    """
    raw = str(text or "")
    tokens: list[str] = []

    jb = _jieba()
    if jb is not None:
        for word in jb.lcut(raw):
            word = word.strip()
            if len(word) < 2:
                continue
            if word.isdigit():   # 2026-11-16：纯数字（日期/楼层/编号）不当关键词
                continue
            if re.fullmatch(r"[\u4e00-\u9fff]+", word) and word in KEYWORD_STOPWORDS:
                continue
            if word not in tokens:
                tokens.append(word)
        # 只保留 jieba 分出来的词；不要再补滑窗片段（那会重新把噪声带回来）
    else:
        for run in re.findall(r"[\u4e00-\u9fff]+", raw):
            for size in (4, 3, 2):
                for start in range(len(run) - size + 1):
                    token = run[start : start + size].strip(_FUNCTION_CHARS)
                    if len(token) < 2 or token in KEYWORD_STOPWORDS:
                        continue
                    if token not in tokens:
                        tokens.append(token)

    for latin in re.findall(r"[A-Za-z0-9]{2,}", raw):
        if latin.lower() not in tokens:
            tokens.append(latin.lower())
    return tokens[:24]

PRIVATE_HOURS = (23, 0, 1, 2, 3, 4, 5)
PRIVATE_KEYWORDS = (
    "睡不着", "失眠", "想你", "梦到", "难过", "哭", "崩溃", "眼泪", "孤独", "害怕",
    "委屈", "心口", "抱", "亲", "我爱你", "喜欢我", "秘密", "对不起",
    "深夜", "凌晨", "半夜", "关了灯", "别走", "别离开", "舍不得",
)
PRIVATE_EMOTIONS = ("难过", "委屈", "害怕", "孤独", "愤怒", "亲密", "爱", "想", "依赖")


def is_private_memory(summary: str = "", content: str = "", emotion_tag: str = "",
                      created_at: str = "") -> bool:
    """这条记忆算不算"私事"（对非主人要藏起来的那种）。

    判据（任一命中就算私事）：
      * 发生在深夜 23:00-06:00
      * 摘要/正文里有情绪化、贴身的关键词
      * 情绪标签是重情绪
    """
    from datetime import datetime as _dt

    blob = str(summary or "") + str(content or "")
    try:
        if created_at:
            stamp = str(created_at).replace("Z", "+00:00")
            when = _dt.fromisoformat(stamp)
            if when.tzinfo is None:
                when = when.astimezone()
            if when.hour in PRIVATE_HOURS:
                return True
    except Exception:  # noqa: BLE001
        pass
    if any(word in blob for word in PRIVATE_KEYWORDS):
        return True
    tag = str(emotion_tag or "")
    return any(word in tag for word in PRIVATE_EMOTIONS)


@dataclass
class RetrievalHit:
    book_id: int
    summary: str
    content: str
    emotion_tag: str
    status: str
    mv: float
    score: float
    folder_date: str
    folder_user: str
    folder_event: str
    created_at: str = ""
    decay_rate: float = 1.0
    ghost_hits: int = 0
    deep: bool = False
    score_breakdown: dict[str, float] | None = None
    # 不是"被问到的那件事"，只是时间上最近发生的事（兜底补位），必须让她看得出来
    fallback: bool = False

    def prompt_line(self, mood_congruent: bool = False, now: datetime | None = None) -> str:
        """给大模型的单行记忆。幽灵只给情绪，不给细节。"""
        prefix = "(翻旧账) " if mood_congruent else ""
        stamp = relative_time_label(self.created_at, now)
        stamp_prefix = f"[{stamp}] " if stamp else ""
        if self.fallback and self.status != "ghost":
            # 兜底补位的"最近的事"：标清楚，免得她把它当成对方问的那段回忆。
            label = "[最近的事·未必相关]"
            if self.deep and self.status == "detail" and self.content:
                return f"{prefix}- {stamp_prefix}{label} {self.content}"
            return f"{prefix}- {stamp_prefix}{label} {self.summary or '（只有一点模糊的印象）'}"
        if self.status == "ghost":
            if self.ghost_hits > 3:
                return (
                    f"{prefix}- {stamp_prefix}[幽灵·麻木] 一种连你自己都觉得有些陌生的、遥远的"
                    f"{self.emotion_tag}，仿佛那是上辈子的事。"
                )
            return (
                f"{prefix}- {stamp_prefix}[幽灵] 你不记得具体细节了，"
                f"但心里有一种淡淡的{self.emotion_tag}感。"
            )
        if self.deep and self.status == "detail" and self.content:
            return f"{prefix}- {stamp_prefix}[细节] {self.content}"
        if self.summary:
            return f"{prefix}- {stamp_prefix}[书皮] {self.summary}"
        return f"{prefix}- {stamp_prefix}[余温] 只记得当时有一点{self.emotion_tag}。"

    def to_dict(self) -> dict[str, Any]:
        return {
            "book_id": self.book_id,
            "summary": self.summary,
            "content": self.content,
            "emotion_tag": self.emotion_tag,
            "status": self.status,
            "mv": round(float(self.mv or 0.0), 2),
            "score": round(float(self.score or 0.0), 2),
            "folder_date": self.folder_date,
            "folder_user": self.folder_user,
            "folder_event": self.folder_event,
            "created_at": self.created_at,
            "decay_rate": round(self.decay_rate, 3),
            "ghost_hits": self.ghost_hits,
            "deep": self.deep,
            "fallback": self.fallback,
            "score_breakdown": self.score_breakdown or {},
        }

@dataclass
class RetrievalResult:
    hits: list[RetrievalHit]
    deep: bool
    impulse: float
    retrieve_prob: float
    roll: float
    note: str
    mood_congruent: bool = False
    suppressed_skipped: int = 0
    hurt_feedback: float = 0.0

    @property
    def has_ghost(self) -> bool:
        return any(hit.status == "ghost" for hit in self.hits)

    @property
    def refused(self) -> bool:
        return self.note.startswith("[冲动脑")

    @property
    def is_empty(self) -> bool:
        return not self.hits

    def to_dict(self) -> dict[str, Any]:
        return {
            "hits": [hit.to_dict() for hit in self.hits],
            "deep": self.deep,
            "impulse": round(self.impulse, 2),
            "retrieve_prob": round(self.retrieve_prob, 4),
            "roll": round(self.roll, 4),
            "note": self.note,
            "mood_congruent": self.mood_congruent,
            "suppressed_skipped": self.suppressed_skipped,
            "hurt_feedback": round(self.hurt_feedback, 2),
        }

    def to_prompt_text(self, now: datetime | None = None) -> str:
        if self.note.startswith("[冲动脑"):
            return self.note
        mode = "深聊（加载细节）" if self.deep else "日常（只读书皮/幽灵）"
        lines = [
            f"检索模式={mode}；冲动={self.impulse:.0f}；"
            f"概率={self.retrieve_prob:.2f}；{self.note}"
        ]
        if any(hit.fallback for hit in self.hits):
            lines.append(
                "（标了[最近的事·未必相关]的只是最近发生的事，不一定是对方问的那件；"
                "对不上就说不知道，别把最近的事当成答案。）"
            )
        for hit in self.hits:
            lines.append(hit.prompt_line(self.mood_congruent, now))
        if not self.hits:
            lines.append("- （没有可用的记忆，只剩当下的感觉）")
        return "\n".join(lines)

class LibraryMemory:
    """
    SQLite 关系型图书馆，替代 V1 的 NetworkX MemoryGraph。

    books 的每一行是一本书，且强制带 user_id：
        同一 SQLite 文件里，不同用户的数据在查询层物理隔离。

        content        书页细节（detail 才保留）
        summary        书皮摘要（summary/ghost 阶段的唯一文本）
        emotion_tag    情绪极性（幽灵节点只保留它）
        folder_date    日期软链接
        folder_user    人物软链接
        folder_event   事件类型软链接
        mv             记忆值
        decay_rate     衰老速率：普通 1.0，情绪刻录 0.1
        status         detail / summary / ghost / suppressed
        created_at / last_accessed

    三阶遗忘：
        mv < 30 -> summary，抹掉 content；
        mv < 10 -> ghost，抹掉 summary，只留下 emotion_tag；
        ghost 超过 14 天未被命中 -> DELETE。

    创伤隔离：
        status='suppressed' 的书白天检索完全不可见，只在夜间梦境中被抽取。
    """

    def __init__(
        self,
        db_path: str | Path,
        user_id: str = "default",
        rng: random.Random | None = None,
        embedding_fn: Callable[[str], Sequence[float]] | None = None,
        embed_on_write: bool = True,
    ) -> None:
        self.path = Path(db_path)
        self.user_id = str(user_id).strip() or "default"
        # 2026-11-12：非主人来聊天时，这些人的记忆不参与检索（说漏嘴就不好了）
        self.hide_owner_names: set[str] | None = None
        self.prefer_user: str | None = None
        self.embedding_fn = embedding_fn
        # 2026-11-08：写入时算向量很慢（ollama 单次请求约 2 秒）。
        # 线上把写入交给后台批量补（embed_on_write=False），
        # 查询时才算一次，别让每轮聊天多等两秒。
        self.embed_on_write = embed_on_write
        if self.path.parent and not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.rng = rng or random.Random()
        # 后台梦境线程也会写库；连接跨线程复用，所有 DB 操作由 RLock 串行化。
        self._db_lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.conn.execute("PRAGMA busy_timeout=5000;")
        self._init_schema()

    def now(self) -> datetime:
        """当前时间（测试里可以打补丁）。

        2026-11-02：_score_book 一直在调 self.now()，但 LibraryMemory 根本没有这个方法，
        异常被吞掉 → recency_score 永远是 0，"越近的事越先想起来"其实从来没生效过。
        """
        return datetime.now()

    def _init_schema(self) -> None:
        # 先建表（CREATE IF NOT EXISTS 不会改动旧表），再迁移旧列，最后建索引。
        # 这样旧版 V2 数据库即使没有 user_id / decay_rate 也能平滑升级。
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS books (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id       TEXT NOT NULL DEFAULT 'default',
                    content       TEXT NOT NULL DEFAULT '',
                    summary       TEXT NOT NULL DEFAULT '',
                    emotion_tag   TEXT NOT NULL DEFAULT '平静',
                    folder_date   TEXT NOT NULL,
                    folder_user   TEXT NOT NULL,
                    folder_event  TEXT NOT NULL,
                    mv            REAL NOT NULL DEFAULT 0,
                    decay_rate    REAL NOT NULL DEFAULT 1.0,
                    ghost_hits    INTEGER NOT NULL DEFAULT 0,
                    embedding     TEXT NOT NULL DEFAULT '[]',
                    status        TEXT NOT NULL DEFAULT 'detail',
                    unresolved    INTEGER NOT NULL DEFAULT 0,
                    last_mentioned TEXT NOT NULL DEFAULT '',
                    origin        TEXT NOT NULL DEFAULT 'chat',
                    source_id     TEXT NOT NULL DEFAULT '',
                    version       INTEGER NOT NULL DEFAULT 1,
                    created_at    TEXT NOT NULL,
                    last_accessed TEXT NOT NULL,
                    weight_source TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS dreams (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id      TEXT NOT NULL DEFAULT 'default',
                    content      TEXT NOT NULL,
                    source       TEXT NOT NULL DEFAULT 'ollama',
                    affect       TEXT NOT NULL DEFAULT '{}',
                    created_at   TEXT NOT NULL,
                    expire_at    TEXT NOT NULL,
                    is_discussed INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS diaries (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id      TEXT NOT NULL DEFAULT 'default',
                    content      TEXT NOT NULL,
                    source       TEXT NOT NULL DEFAULT 'ollama',
                    created_at   TEXT NOT NULL,
                    is_discussed INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS meta (
                    user_id TEXT NOT NULL DEFAULT 'default',
                    key     TEXT NOT NULL,
                    value   TEXT NOT NULL,
                    PRIMARY KEY(user_id, key)
                );

                CREATE TABLE IF NOT EXISTS snapshots (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id          TEXT NOT NULL DEFAULT 'default',
                    created_at       TEXT NOT NULL,
                    emotion          TEXT NOT NULL DEFAULT '{}',
                    joy_baseline     REAL NOT NULL DEFAULT 55,
                    hurt_baseline    REAL NOT NULL DEFAULT 20,
                    intimacy         INTEGER NOT NULL DEFAULT 50,
                    book_count       INTEGER NOT NULL DEFAULT 0,
                    ghost_count      INTEGER NOT NULL DEFAULT 0,
                    suppressed_count INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS traces (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id        TEXT NOT NULL DEFAULT 'default',
                    created_at     TEXT NOT NULL,
                    user_text      TEXT NOT NULL,
                    reply          TEXT NOT NULL,
                    self_control   TEXT NOT NULL DEFAULT '{}',
                    memory_control TEXT NOT NULL DEFAULT '{}',
                    memory_action  TEXT NOT NULL DEFAULT 'fallback',
                    emotion        TEXT NOT NULL DEFAULT '{}',
                    retrieval      TEXT NOT NULL DEFAULT '{}',
                    external_info  TEXT NOT NULL DEFAULT '',
                    safety_blocked INTEGER NOT NULL DEFAULT 0,
                    trauma         TEXT NOT NULL DEFAULT '{}',
                    source         TEXT NOT NULL DEFAULT 'local',
                    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_tokens     INTEGER NOT NULL DEFAULT 0
                );
                """
            )

        self._migrate_legacy_schema()
        self._migrate_trace_tokens()
        self._migrate_book_weight()

        self._dedupe_books_for_unique_index()

        with self.conn:
            self.conn.executescript(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_books_dedup
                    ON books(user_id, folder_date, folder_event, summary)
                    WHERE status != 'ghost';
                CREATE INDEX IF NOT EXISTS idx_books_user_status ON books(user_id, status);
                CREATE INDEX IF NOT EXISTS idx_books_user_date ON books(user_id, folder_date);
                CREATE INDEX IF NOT EXISTS idx_books_user_event ON books(user_id, folder_event);
                CREATE INDEX IF NOT EXISTS idx_books_user_mv ON books(user_id, mv);
                CREATE INDEX IF NOT EXISTS idx_books_user_access ON books(user_id, last_accessed);
                CREATE INDEX IF NOT EXISTS idx_books_user_obsession ON books(user_id, unresolved, last_mentioned);
                CREATE INDEX IF NOT EXISTS idx_books_source ON books(user_id, source_id) WHERE source_id != '';
                CREATE INDEX IF NOT EXISTS idx_dreams_user ON dreams(user_id, is_discussed, expire_at);
                CREATE INDEX IF NOT EXISTS idx_dreams_expire ON dreams(expire_at);
                CREATE INDEX IF NOT EXISTS idx_diaries_user ON diaries(user_id, is_discussed, created_at);
                CREATE INDEX IF NOT EXISTS idx_snapshots_user ON snapshots(user_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_traces_user ON traces(user_id, created_at);
                """
            )

    def _table_columns(self, table: str) -> set[str]:
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row["name"]) for row in rows}

    def _migrate_book_weight(self) -> None:
        """老库补 weight_source 列（2026-11-05 依恋权重）。"""
        try:
            cols = {row[1] for row in self.conn.execute("PRAGMA table_info(books)")}
        except Exception:  # noqa: BLE001
            return
        if "weight_source" in cols:
            return
        try:
            with self.conn:
                self.conn.execute(
                    "ALTER TABLE books ADD COLUMN weight_source TEXT NOT NULL DEFAULT ''")
        except Exception:  # noqa: BLE001
            pass

    def _migrate_trace_tokens(self) -> None:
        """给老库的 traces 表补上 token 三列（2026-11-05 加的计量）。"""
        try:
            cols = {row[1] for row in self.conn.execute("PRAGMA table_info(traces)")}
        except Exception:  # noqa: BLE001
            return
        for name in ("prompt_tokens", "completion_tokens", "cached_tokens"):
            if name in cols:
                continue
            try:
                with self.conn:
                    self.conn.execute(
                        f"ALTER TABLE traces ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
            except Exception:  # noqa: BLE001
                pass

    def _migrate_legacy_schema(self) -> None:
        """
        自动迁移旧版 V2 数据库：

            books 缺 user_id / decay_rate  -> ALTER TABLE 补列，旧数据归 'default'
            dreams / diaries 缺 user_id     -> ALTER TABLE 补列
            meta 还是 (key PRIMARY KEY)     -> 重建为 (user_id, key) 复合主键并搬运旧数据

        旧数据不会被删除，只会被归入 default 用户，保证升级不崩。
        """
        book_cols = self._table_columns("books")
        if "user_id" not in book_cols:
            with self.conn:
                self.conn.execute(
                    "ALTER TABLE books ADD COLUMN user_id TEXT NOT NULL DEFAULT 'default'"
                )
        if "decay_rate" not in book_cols:
            with self.conn:
                self.conn.execute(
                    "ALTER TABLE books ADD COLUMN decay_rate REAL NOT NULL DEFAULT 1.0"
                )
        if "ghost_hits" not in book_cols:
            with self.conn:
                self.conn.execute(
                    "ALTER TABLE books ADD COLUMN ghost_hits INTEGER NOT NULL DEFAULT 0"
                )
        if "embedding" not in book_cols:
            with self.conn:
                self.conn.execute(
                    "ALTER TABLE books ADD COLUMN embedding TEXT NOT NULL DEFAULT '[]'"
                )
        if "unresolved" not in book_cols:
            with self.conn:
                self.conn.execute(
                    "ALTER TABLE books ADD COLUMN unresolved INTEGER NOT NULL DEFAULT 0"
                )
        if "last_mentioned" not in book_cols:
            with self.conn:
                self.conn.execute(
                    "ALTER TABLE books ADD COLUMN last_mentioned TEXT NOT NULL DEFAULT ''"
                )
        # 记忆溯源：origin 说明这条记忆从哪来（自己聊的 / 阅读 / 抖音同步 / IM），
        # source_id 是外部系统里的原始 id，version 用来判断同一条外部记录的新旧。
        if "origin" not in book_cols:
            with self.conn:
                self.conn.execute(
                    "ALTER TABLE books ADD COLUMN origin TEXT NOT NULL DEFAULT 'chat'"
                )
        if "source_id" not in book_cols:
            with self.conn:
                self.conn.execute(
                    "ALTER TABLE books ADD COLUMN source_id TEXT NOT NULL DEFAULT ''"
                )
        if "version" not in book_cols:
            with self.conn:
                self.conn.execute(
                    "ALTER TABLE books ADD COLUMN version INTEGER NOT NULL DEFAULT 1"
                )

        for table in ("dreams", "diaries"):
            cols = self._table_columns(table)
            if "user_id" not in cols:
                with self.conn:
                    self.conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN user_id TEXT NOT NULL DEFAULT 'default'"
                    )

        meta_cols = self._table_columns("meta")
        if "user_id" not in meta_cols:
            with self.conn:
                self.conn.execute("DROP TABLE IF EXISTS meta_legacy_v2")
                self.conn.execute("ALTER TABLE meta RENAME TO meta_legacy_v2")
                self.conn.execute(
                    """
                    CREATE TABLE meta (
                        user_id TEXT NOT NULL DEFAULT 'default',
                        key     TEXT NOT NULL,
                        value   TEXT NOT NULL,
                        PRIMARY KEY(user_id, key)
                    )
                    """
                )
                self.conn.execute(
                    "INSERT INTO meta(user_id, key, value) "
                    "SELECT 'default', key, value FROM meta_legacy_v2"
                )
                self.conn.execute("DROP TABLE meta_legacy_v2")

    def _dedupe_books_for_unique_index(self) -> None:
        """旧库可能已有重复书页；先合并再建唯一索引，避免迁移直接失败。"""
        groups = self.conn.execute(
            "SELECT user_id, folder_date, folder_event, summary, COUNT(*) AS n "
            "FROM books WHERE status != 'ghost' "
            "GROUP BY user_id, folder_date, folder_event, summary HAVING n > 1"
        ).fetchall()
        for group in groups:
            rows = self.conn.execute(
                "SELECT id, mv, last_accessed, unresolved FROM books "
                "WHERE user_id = ? AND folder_date = ? AND folder_event = ? AND summary = ? "
                "AND status != 'ghost' ORDER BY mv DESC, last_accessed DESC, id DESC",
                (
                    group["user_id"],
                    group["folder_date"],
                    group["folder_event"],
                    group["summary"],
                ),
            ).fetchall()
            if len(rows) <= 1:
                continue
            keep = rows[0]
            duplicate_ids = [int(row["id"]) for row in rows[1:]]
            with self.conn:
                self.conn.execute(
                    "UPDATE books SET mv = ?, last_accessed = ?, "
                    "unresolved = MAX(unresolved, ?) "
                    "WHERE id = ? AND user_id = ?",
                    (
                        max(float(row["mv"]) for row in rows),
                        max(str(row["last_accessed"]) for row in rows),
                        max(int(row["unresolved"] or 0) for row in rows),  # Row 没有 .get()
                        int(keep["id"]),
                        str(group["user_id"]),
                    ),
                )
                self.conn.executemany(
                    "DELETE FROM books WHERE id = ? AND user_id = ?",
                    [(book_id, str(group["user_id"])) for book_id in duplicate_ids],
                )

    # ---------- 通用 ----------

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def _make_embedding(self, text: str) -> list[float]:
        if self.embedding_fn is None:
            return []
        try:
            # 2026-11-08：原来截到 128 维。bge-m3 出来是 1024 维，砍到 128 会丢掉大部分语义，
            # 而且余弦要求两边长度一致——截断后新旧记忆永远对不上。上限放到 1536。
            vector = [float(value) for value in self.embedding_fn(text)][:1536]
        except Exception:  # noqa: BLE001
            return []
        return vector if all(math.isfinite(value) for value in vector) else []

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    # ---------- 写入与查询 ----------

    def add_book(
        self,
        content: str,
        summary: str,
        emotion_tag: str,
        folder_date: str,
        folder_user: str,
        folder_event: str,
        mv: float,
        status: str = "detail",
        now: datetime | None = None,
        decay_rate: float = 1.0,
        unresolved: bool = False,
        last_mentioned: datetime | None = None,
        origin: str = "chat",
        source_id: str = "",
        version: int = 1,
        weight_source: str = "",
    ) -> dict[str, Any]:
        """写一条记忆。

        origin / source_id / version 是记忆溯源：
        - origin 说明这段记忆从哪来（chat / reading / douyin_dm / douyin_sync / chat ...）
        - source_id 是外部系统的原始 id，用来认出“同一条记录又同步了一次”
        - version 只增不减；外部记录被改写时传更大的版本号，旧正文才会被新正文替换
        """
        now = now or datetime.now()
        embedding = self._make_embedding(f"{summary} {content}") if self.embed_on_write else []
        now_text = dt_to_str(now)
        decay_value = max(CONTROL_DECAY_MIN, min(CONTROL_DECAY_MAX, float(decay_rate)))
        origin_value = str(origin or "chat").strip()[:40] or "chat"
        source_value = str(source_id or "").strip()[:120]
        version_value = max(1, int(version or 1))
        with self.conn:
            self.conn.execute(
                "INSERT INTO books(user_id, content, summary, emotion_tag, folder_date, "
                "folder_user, folder_event, mv, decay_rate, ghost_hits, embedding, status, "
                "unresolved, last_mentioned, origin, source_id, version, created_at, last_accessed, "
                "weight_source) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(user_id, folder_date, folder_event, summary) "
                "WHERE status != 'ghost' DO UPDATE SET "
                "mv = MIN(?, mv + 5), "
                "last_accessed = excluded.last_accessed, "
                "unresolved = MAX(unresolved, excluded.unresolved), "
                # 同一条外部记录的新版本才允许覆盖旧正文；普通聊天重复提及不覆盖，保住最早的细节。
                "content = CASE WHEN excluded.version > books.version THEN excluded.content "
                "ELSE books.content END, "
                "source_id = CASE WHEN excluded.source_id != '' THEN excluded.source_id "
                "ELSE books.source_id END, "
                "version = MAX(books.version, excluded.version)",
                (
                    self.user_id,
                    str(content),
                    str(summary),
                    str(emotion_tag),
                    str(folder_date),
                    str(folder_user),
                    str(folder_event),
                    float(mv),
                    decay_value,
                    0,
                    json.dumps(embedding, ensure_ascii=False),
                    str(status),
                    1 if unresolved else 0,
                    dt_to_str(last_mentioned) if last_mentioned else "",
                    origin_value,
                    source_value,
                    version_value,
                    now_text,
                    now_text,
                    str(weight_source or ""),
                    MV_CAP,
                ),
            )
            row = self.conn.execute(
                "SELECT * FROM books WHERE user_id = ? AND folder_date = ? "
                "AND folder_event = ? AND summary = ? AND status != 'ghost' "
                "ORDER BY id DESC LIMIT 1",
                (self.user_id, str(folder_date), str(folder_event), str(summary)),
            ).fetchone()
        book = self._row(row)
        if book is None:
            raise RuntimeError("写入书籍失败")
        # created_at 是原写入时间；只有本次新插入才会等于 now_text。
        book["deduped"] = str(book.get("created_at") or "") != now_text
        return book

    def get_book(self, book_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM books WHERE id = ? AND user_id = ?",
            (int(book_id), self.user_id),
        ).fetchone()
        return self._row(row)

    def latest_book(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM books WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (self.user_id,),
        ).fetchone()
        return self._row(row)

    def list_books(
        self,
        status: str | None = None,
        include_suppressed: bool = True,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM books WHERE user_id = ?"
        params: list[Any] = [self.user_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        if not include_suppressed:
            sql += " AND status != 'suppressed'"
        sql += " ORDER BY id DESC"
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def status_counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM books WHERE user_id = ? GROUP BY status",
            (self.user_id,),
        ).fetchall()
        return {str(row["status"]): int(row["n"]) for row in rows}

    def count_unresolved(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM books WHERE user_id = ? AND unresolved = 1",
            (self.user_id,),
        ).fetchone()
        return int(row["n"]) if row else 0

    def list_unresolved(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM books WHERE user_id = ? AND unresolved = 1 ORDER BY id DESC",
            (self.user_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def auto_resolve_stale_obsessions(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """超过 10 天还没被回应的执念，夜间自动放下（unresolved -> 0）。"""
        now = now or datetime.now()
        resolved: list[dict[str, Any]] = []
        for book in self.list_unresolved():
            try:
                created = str_to_dt(str(book.get("created_at") or ""))
            except (ValueError, TypeError):
                continue
            if now - created > timedelta(days=10):
                updated = self.clear_obsession(int(book["id"]))
                if updated is not None:
                    resolved.append(updated)
        return resolved

    def mark_unresolved(self, book_id: int) -> dict[str, Any] | None:
        with self.conn:
            self.conn.execute(
                "UPDATE books SET unresolved = 1 WHERE id = ? AND user_id = ?",
                (int(book_id), self.user_id),
            )
        return self.get_book(book_id)

    def mark_obsession_mentioned(self, book_id: int, now: datetime) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE books SET last_mentioned = ? WHERE id = ? AND user_id = ?",
                (dt_to_str(now), int(book_id), self.user_id),
            )

    def clear_obsession(self, book_id: int) -> dict[str, Any] | None:
        with self.conn:
            self.conn.execute(
                "UPDATE books SET unresolved = 0 WHERE id = ? AND user_id = ?",
                (int(book_id), self.user_id),
            )
        return self.get_book(book_id)

    def persona_rows(self, limit: int = PERSONA_MEMORY_WINDOW) -> list[dict[str, Any]]:
        """最近 N 条记忆的统计原料；只取字段，不读正文，省内存。"""
        rows = self.conn.execute(
            "SELECT folder_event, emotion_tag, status, unresolved "
            "FROM books WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (self.user_id, max(1, int(limit))),
        ).fetchall()
        return [dict(row) for row in rows]

    def recent_hurt_average(self, limit: int = 30) -> float | None:
        """从 traces 的情绪快照里算长期 Hurt 均值；没有 trace 时返回 None。"""
        rows = self.conn.execute(
            "SELECT emotion FROM traces WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (self.user_id, max(1, int(limit))),
        ).fetchall()
        values: list[float] = []
        for row in rows:
            try:
                emotion = json.loads(str(row["emotion"] or "{}"))
                values.append(float(emotion.get("hurt")))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return sum(values) / len(values) if values else None

    def set_mv(self, book_id: int, mv: float, now: datetime | None = None) -> dict[str, Any] | None:
        now = now or datetime.now()
        with self.conn:
            self.conn.execute(
                "UPDATE books SET mv = ?, last_accessed = ? WHERE id = ? AND user_id = ?",
                (clamp(mv, 0, MV_CAP), dt_to_str(now), int(book_id), self.user_id),
            )
        return self.get_book(book_id)

    # ---------- 三阶遗忘 ----------

    def _degrade_row(self, row: dict[str, Any], now: datetime) -> dict[str, Any] | None:
        """按 MV 执行 detail -> summary -> ghost 的单向降级。"""
        status = str(row["status"])
        if status in ("ghost", "suppressed"):
            return None

        mv = float(row["mv"])
        if mv < GHOST_MV_THRESHOLD:
            with self.conn:
                self.conn.execute(
                    "UPDATE books SET content = '', summary = '', status = 'ghost', "
                    "last_accessed = ? WHERE id = ? AND user_id = ?",
                    (dt_to_str(now), int(row["id"]), self.user_id),
                )
            return {
                "book_id": int(row["id"]),
                "from": status,
                "to": "ghost",
                "emotion_tag": row["emotion_tag"],
            }

        if mv < SUMMARY_MV_THRESHOLD and status == "detail":
            with self.conn:
                self.conn.execute(
                    "UPDATE books SET content = '', status = 'summary', last_accessed = ? "
                    "WHERE id = ? AND user_id = ?",
                    (dt_to_str(now), int(row["id"]), self.user_id),
                )
            return {
                "book_id": int(row["id"]),
                "from": "detail",
                "to": "summary",
                "emotion_tag": row["emotion_tag"],
            }
        return None

    def degrade_all(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """扫描所有可降级的书，执行三阶遗忘。"""
        now = now or datetime.now()
        rows = self.conn.execute(
            "SELECT * FROM books WHERE user_id = ? AND status IN ('detail', 'summary')",
            (self.user_id,),
        ).fetchall()
        changes: list[dict[str, Any]] = []
        for row in rows:
            change = self._degrade_row(dict(row), now)
            if change:
                changes.append(change)
        return changes

    def purge_ghosts(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """删除超过 14 天没有被检索命中的幽灵节点。"""
        now = now or datetime.now()
        cutoff = now - timedelta(days=GHOST_TTL_DAYS)
        rows = self.conn.execute(
            "SELECT * FROM books WHERE user_id = ? AND status = 'ghost' "
            "AND last_accessed <= ?",
            (self.user_id, dt_to_str(cutoff)),
        ).fetchall()
        deleted = [dict(row) for row in rows]
        if deleted:
            with self.conn:
                self.conn.executemany(
                    "DELETE FROM books WHERE id = ? AND user_id = ?",
                    [(int(row["id"]), self.user_id) for row in deleted],
                )
        return deleted

    def apply_night_hygiene(self, now: datetime | None = None) -> dict[str, Any]:
        """
        夜间图书馆维护：
            1. 超过 7 天未访问的非幽灵/非压抑书籍 MV -2；
            2. 执行三阶降级；
            3. 清理过期幽灵。
        """
        now = now or datetime.now()
        cutoff = now - timedelta(days=DECAY_IDLE_DAYS)
        rows = self.conn.execute(
            "SELECT id, mv, decay_rate FROM books WHERE user_id = ? "
            "AND status IN ('detail', 'summary') AND last_accessed <= ?",
            (self.user_id, dt_to_str(cutoff)),
        ).fetchall()
        decayed_ids: list[int] = []
        with self.conn:
            for row in rows:
                # 情绪刻录：decay_rate=0.1 的记忆，衰老速度只有普通记忆的十分之一
                amount = DECAY_AMOUNT * float(row["decay_rate"])
                self.conn.execute(
                    "UPDATE books SET mv = ? WHERE id = ? AND user_id = ?",
                    (max(0.0, float(row["mv"]) - amount), int(row["id"]), self.user_id),
                )
                decayed_ids.append(int(row["id"]))
        degraded = self.degrade_all(now)
        ghosts_deleted = self.purge_ghosts(now)
        with self.conn:
            transient_deleted = self.conn.execute(
                "DELETE FROM books WHERE user_id = ? AND folder_event = '瞬态'",
                (self.user_id,),
            ).rowcount
        return {
            "decayed": decayed_ids,
            "degraded": degraded,
            "ghosts_deleted": ghosts_deleted,
            "transient_deleted": max(0, int(transient_deleted or 0)),
        }

    # ---------- 检索 ----------

    @staticmethod
    def _char_ngrams(text: str, size: int = 2) -> set[str]:
        clean = re.sub(r"[\s\W_]+", "", str(text).lower())
        if len(clean) < size:
            return {clean} if clean else set()
        return {clean[index : index + size] for index in range(len(clean) - size + 1)}

    @classmethod
    def _ngram_similarity(cls, left: str, right: str) -> float:
        a = cls._char_ngrams(left)
        b = cls._char_ngrams(right)
        if not a or not b:
            return 0.0
        return len(a & b) / math.sqrt(len(a) * len(b))

    @staticmethod
    def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        dot = sum(float(a) * float(b) for a, b in zip(left, right))
        norm_a = math.sqrt(sum(float(a) ** 2 for a in left))
        norm_b = math.sqrt(sum(float(b) ** 2 for b in right))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return max(0.0, dot / (norm_a * norm_b))

    def _score_book(
        self,
        row: dict[str, Any],
        query: str,
        deep: bool,
        mood_congruent: bool,
        query_embedding: Sequence[float] | None = None,
    ) -> tuple[float, dict[str, float]]:
        """按书皮/情绪/文件夹/记忆值/语义相似度打分；日常绝不读 content。

        MV 只保留很小的 importance 权重，主要用于决定记忆寿命，不再主导召回。
        """
        status = str(row["status"])
        if status == "ghost":
            haystack = " ".join(
                [
                    str(row["emotion_tag"]),
                    str(row["folder_user"]),
                    str(row["folder_event"]),
                    str(row["folder_date"]),
                ]
            )
        else:
            parts = [
                str(row["summary"]),
                str(row["emotion_tag"]),
                str(row["folder_user"]),
                str(row["folder_event"]),
                str(row["folder_date"]),
            ]
            if deep and status == "detail":
                parts.append(str(row["content"]))
            haystack = " ".join(parts)

        q = query.strip()
        keyword_score = 0.0
        if q and q in haystack:
            keyword_score += 20.0
        matched_spans: set[int] = set()
        df_map = getattr(self, "_token_df", None) or {}
        total_docs = getattr(self, "_token_total", 0) or 0
        for token in content_tokens(q):
            # IDF：只在一小部分书里出现的词更值钱；烂大街的词几乎不加分
            rarity = 1.0
            if df_map and total_docs:
                df = df_map.get(token, 1)
                rarity = 0.25 + 0.75 * (1.0 - df / total_docs)
            base = 5.0 if len(token) >= 3 else 3.0
            # 同一个字区间只算一次：否则"叫什么/叫什/什么"互相重叠，一句普通问句就能顶掉真事
            start = haystack.find(token)
            while start != -1:
                span = range(start, start + len(token))
                if not any(position in matched_spans for position in span):
                    matched_spans.update(span)
                    keyword_score += base * rarity
                start = haystack.find(token, start + 1)

        emotion_score = 0.0
        if str(row["emotion_tag"]) in q:
            emotion_score += 8.0
        if str(row["folder_event"]) in q:
            emotion_score += 4.0
        if mood_congruent and str(row["emotion_tag"]) in NEGATIVE_EMOTIONS:
            emotion_score += 6.0

        semantic_score = 0.0
        if q:
            # 语义召回：有 embedding 用余弦；没有就用字符 bigram 做轻量近似
            if query_embedding:
                try:
                    book_embedding = json.loads(str(row.get("embedding") or "[]"))
                except json.JSONDecodeError:
                    book_embedding = []
                semantic_score = 12.0 * self._cosine(query_embedding, book_embedding)
            else:
                semantic_score = 6.0 * self._ngram_similarity(q, haystack)

        # 强压低 MV：最多只贡献 0.5 分，让它只决定遗忘，不决定“相关”。
        importance_score = min(float(row["mv"]), MV_CAP) * 0.005

        # 时间衰减加权：同样的关键词，越近的事越先被想起来。
        # 不改"相不相关"（关键词/语义分不动），只在相关度接近时让新的压过旧的。
        recency_score = 0.0
        try:
            age_days = max(0.0, (self.now() - str_to_dt(str(row["last_accessed"]))).total_seconds() / 86400.0)
            recency_score = max(0.0, 1.0 - age_days / RECENCY_HALF_WINDOW_DAYS) * RECENCY_WEIGHT
        except Exception:  # noqa: BLE001
            recency_score = 0.0

        total = keyword_score + semantic_score + emotion_score + importance_score + recency_score
        breakdown = {
            "semantic_score": round(semantic_score, 4),
            "keyword_score": round(keyword_score, 4),
            "emotion_score": round(emotion_score, 4),
            "importance_score": round(importance_score, 4),
            "recency_score": round(recency_score, 4),
        }
        return total, breakdown

    def search_by_keywords(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """翻旧账：不看分数，直接在书皮/正文里按词找。

        2026-11-16：她"想不起来"的时候，应该能主动翻库，而不是只回最近的事。
        """
        tokens = [tok for tok in content_tokens(strip_question_scaffold(query)) if len(tok) >= 2][:4]
        if not tokens:
            return []
        conds = " OR ".join("(summary LIKE ? OR content LIKE ?)" for _ in tokens)
        params: list[Any] = []
        for tok in tokens:
            escaped = str(tok).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.extend([f"%{escaped}%", f"%{escaped}%"])
        sql = (
            "SELECT * FROM books WHERE user_id = ? AND status != 'suppressed' AND ("
            + conds + ") ORDER BY mv DESC LIMIT ?"
        )
        try:
            rows = self.conn.execute(sql, (self.user_id, *params, int(limit) * 3)).fetchall()
        except Exception:  # noqa: BLE001
            return []
        # 2026-11-16：翻库也要守访客边界——否则访客问一句"你还记得…"
        # 就能把主人的私事翻出来（Gemini 复核发现）。
        hide_users = getattr(self, "hide_owner_users", None) or set()
        hide_names = getattr(self, "hide_owner_names", None) or set()
        names = tuple(str(n) for n in (set(hide_users) | set(hide_names)) if str(n).strip())
        out = []
        for row in rows:
            data = dict(row)
            if str(data.get("folder_user") or "") in hide_users:
                continue
            blob = str(data.get("summary") or "") + str(data.get("content") or "")
            if names and any(name in blob for name in names):
                continue
            out.append(data)
            if len(out) >= limit:
                break
        return out

    def retrieve(
        self,
        query: str,
        impulse: float,
        now: datetime | None = None,
        deep: bool = False,
        roll: float | None = None,
    ) -> RetrievalResult:
        """
        概率检索：

            retrieve_prob = max(0.1, 1 - Impulse / 110)

        冲动越高，越容易拒绝检索；但再愤怒也保留 10% 概率。
        日常只读 summary/emotion_tag；只有 deep=True 才加载 detail 的 content。
        """
        now = now or datetime.now()
        impulse = clamp(impulse)
        retrieve_prob = max(RETRIEVAL_PROB_MIN, 1.0 - impulse / RETRIEVAL_PROB_DIVISOR)
        # 明确在问回忆的时候，不许"冲动脑"把检索整个拒掉——那是失忆，不是情绪。
        memory_cue = bool(MEMORY_CUE_PATTERN.search(str(query or "")))
        if memory_cue:
            retrieve_prob = 1.0
        roll_value = 0.0 if memory_cue else (float(self.rng.random()) if roll is None else float(roll))
        mood_congruent = impulse > EMOTIONAL_RECALL_THRESHOLD

        if roll_value > retrieve_prob:
            return RetrievalResult(
                hits=[],
                deep=deep,
                impulse=impulse,
                retrieve_prob=retrieve_prob,
                roll=roll_value,
                note="[冲动脑：拒绝深度检索]",
            )

        self.purge_ghosts(now)
        # 合并记忆时（cross_user=True）：她只有一份脑，检索要能看到所有渠道的记忆，
        # 否则IM上就认不出抖音聊过的人。
        if getattr(self, "cross_user", False):
            rows = self.conn.execute(
                "SELECT * FROM books WHERE status != 'suppressed'"
            ).fetchall()
            suppressed_skipped = int(
                self.conn.execute(
                    "SELECT COUNT(*) AS n FROM books WHERE status = 'suppressed'"
                ).fetchone()["n"]
            )
        else:
            rows = self.conn.execute(
                "SELECT * FROM books WHERE user_id = ? AND status != 'suppressed'",
                (self.user_id,),
            ).fetchall()
            suppressed_skipped = int(
                self.conn.execute(
                    "SELECT COUNT(*) AS n FROM books WHERE user_id = ? AND status = 'suppressed'",
                    (self.user_id,),
                ).fetchone()["n"]
            )

        # 2026-11-15 修：访客会把主人的事当成自己的事（"你考试过了没"）。
        # 除了按名字藏私事，还要按 folder_user 整类藏掉主人相关的记忆。
        hide_users = getattr(self, "hide_owner_users", None)
        hide = getattr(self, "hide_owner_names", None)
        if hide_users:
            rows = [
                row for row in rows
                if str(dict(row).get("folder_user") or "") not in hide_users
            ]
        if hide or hide_users:
            names = tuple(str(name) for name in (set(hide or ()) | set(hide_users or ())) if str(name).strip())

            def _mentions_owner(data: dict) -> bool:
                blob = str(data.get("summary") or "") + str(data.get("content") or "")
                folder_user = str(data.get("folder_user") or "")
                if folder_user and folder_user in names:
                    return True
                return any(name in blob for name in names)

            def _is_private_row(data: dict) -> bool:
                """深夜聊的、情绪重的、贴身的，算私事。"""
                return is_private_memory(
                    summary=str(data.get("summary") or ""),
                    content=str(data.get("content") or ""),
                    emotion_tag=str(data.get("emotion_tag") or ""),
                    created_at=str(data.get("created_at") or ""),
                )

            # 2026-11-15 修：原来只有"提到主人 + 私事"才藏，导致访客会拿主人的事
            # 当成自己的事（"你考试过了没"）。对非主人访客，只要提到主人就整条藏掉。
            rows = [
                row for row in rows
                if not _mentions_owner(dict(row))
            ]

        # 打分用"去掉问句脚手架"的查询：问的是"下雨天"，不是"你还记得"。
        # 回忆判定（memory_cue）仍然看原句，两者别混。
        scoring_query = strip_question_scaffold(query)
        query_embedding = self._make_embedding(scoring_query) if self.embedding_fn else None
        scored: list[tuple[float, dict[str, Any], dict[str, float]]] = []
        # 词的稀有度：满库都有的词（"叫什么""什么"）不能和"只猫"这种只在一本书里出现的词同分。
        # 不这样做，问"我那只猫叫什么"会命中一堆"你叫什么名字"的旧记忆。
        query_tokens = content_tokens(scoring_query)
        token_df: dict[str, int] = {}
        if query_tokens and rows:
            for token in query_tokens:
                # 2026-11-16 修：原来 summary+content 都算，正文很长、子串到处都是，
                # df 被撑大、稀有度拉不开。改成只看书皮（summary）。
                token_df[token] = sum(1 for row in rows if token in str(row["summary"]))
        self._token_df = token_df
        self._token_total = max(1, len(rows))
        for row in rows:
            book = dict(row)
            score, breakdown = self._score_book(
                book,
                scoring_query,
                # 打分永远用完整内容：否则"下雨天"这种说法只有在 deep 检索时才命中，
                # 日常回忆就会莫名其妙想不起来（2026-11-02 修的漏检）。
                True,
                mood_congruent,
                query_embedding=query_embedding,
            )
            # 2026-11-15：她得认人。当前说话人自己的记忆加权，
            # 别人的记忆降权，这样不会把张三的事算到李四头上。
            prefer = getattr(self, "prefer_user", None)
            speaker = str(book.get("folder_user") or "")
            if prefer:
                if speaker == prefer:
                    score *= 1.12
                elif speaker and speaker not in ("", "你", "对方"):
                    score *= 0.94
            scored.append((score, book, breakdown))

        q = query.strip()
        fallback_ids: set[int] = set()
        if q:
            ranked_all = sorted(
                scored,
                key=lambda item: (
                    item[0],
                    float(item[1]["mv"]),
                    str(item[1]["last_accessed"]),
                ),
                reverse=True,
            )
            # 二次精排防呆：如果**整批**都没有语义/字面信号，情绪和 MV 再高也不要。
            # 2026-11-16 修：原来只看第一名——第一名是"情绪分高但内容无关"的书时，
            # 整批结果被清空，真正字面命中的书也跟着被丢（静默漏检）。
            any_signal = any(
                item[2].get("semantic_score", 0) > 0 or item[2].get("keyword_score", 0) > 0
                for item in ranked_all
            )
            # 2026-11-16：门槛和下游 _has_content_signal 统一成 >0；
            # 也去掉 not any_signal 的短路——低信号该交给下面 memory_cue 放宽，
            # 不然"她明明记得"的查询会被这一行直接清空。
            if not ranked_all:
                chosen = []
                note = "没有相关记忆（语义/字面都未命中）"
            else:
                # 只有"内容上真的沾边"的才算命中：关键词/语义有分（翻旧账模式下允许情绪相关）。
                # 光靠"最近"和"记忆值高"不够——那正是最近的事冒充答案的路子。
                def _has_content_signal(item: tuple[float, dict[str, Any], dict[str, float]]) -> bool:
                    detail = item[2]
                    # 语义分 1.0 以下基本是"的事吗/天下"这种巧合 bigram，不算真沾边。
                    if detail.get("keyword_score", 0) > 0 or detail.get("semantic_score", 0) > 1.0:
                        return True
                    return bool(mood_congruent and detail.get("emotion_score", 0) > 0)

                positive = [
                    item for item in ranked_all
                    if item[0] >= MIN_RELEVANCE_SCORE and _has_content_signal(item)
                ]
                if positive:
                    _limit = max(MAX_HITS, 6) if memory_cue else max(MAX_HITS, 3)
                    chosen = positive[:_limit]
                    note = f"命中 {len(chosen)} 本书"
                elif memory_cue:
                    # 明确在问回忆、但打分没过线：把字面命中的也给她，别让她"真的想不起来"。
                    kw_hits = [item for item in ranked_all if item[2].get("keyword_score", 0) > 0][:3]
                    chosen = kw_hits
                    note = "回忆问询：放宽到字面命中" if kw_hits else "没有相关记忆"
                    if not kw_hits:
                        # 2026-11-16：先真的翻旧账——按查询里的词在库里扫一遍，
                        # 扫到了就给她；扫不到才退回"最近发生的事"。
                        try:
                            found = self.search_by_keywords(scoring_query, limit=5)
                        except Exception:  # noqa: BLE001
                            found = []
                        if found:
                            chosen = [(float(row.get("mv") or 0), dict(row), {}) for row in found]
                            fallback_ids.update(int(row["id"]) for row in found)
                            note = "回忆问询：翻库按关键词找到的"
                        else:
                            recent = sorted(
                                scored,
                                key=lambda item: str(item[1]["last_accessed"]),
                                reverse=True,
                            )[:5]
                            chosen = recent
                            fallback_ids.update(int(item[1]["id"]) for item in recent)
                            note = "回忆问询：没找到直接相关，给她最近几天的经历"
                else:
                    # 有明确问题却没有相关记忆：不拿"最近的书"硬凑，避免串题和幻觉。
                    chosen = []
                    note = "没有相关记忆"
        else:
            # 只有查询为空时才允许泛泛地回忆最近的书
            recent = sorted(
                scored,
                key=lambda item: (str(item[1]["last_accessed"]), float(item[1]["mv"])),
                reverse=True,
            )[: min(3, MAX_HITS)]
            chosen = recent
            note = "空查询，回忆最近的书"

        hits: list[RetrievalHit] = []
        hurt_feedback = 0.0
        if memory_cue:
            # 问回忆时，额外补几条"最近真实发生的事"，避免只翻到重复的问句本身
            chosen_ids = {item[1]["id"] for item in chosen}
            extra = [
                item for item in sorted(scored, key=lambda item: str(item[1]["last_accessed"]), reverse=True)
                if item[1]["id"] not in chosen_ids
            ][:3]
            if extra:
                chosen = list(chosen) + extra
                fallback_ids.update(int(item[1]["id"]) for item in extra)
                note = (note + "；另附最近发生的事") if note else "另附最近发生的事"
        for score, book, breakdown in chosen:
            status = str(book["status"])
            content = str(book["content"]) if deep and status == "detail" else ""
            summary = str(book["summary"])
            ghost_hits = int(book.get("ghost_hits", 0))
            if status == "ghost":
                summary = ""
                ghost_hits += 1
                # 幽灵不再增加 MV；多次命中会触发情感麻木
                with self.conn:
                    self.conn.execute(
                        "UPDATE books SET ghost_hits = ?, last_accessed = ? "
                        "WHERE id = ? AND user_id = ?",
                        (ghost_hits, dt_to_str(now), int(book["id"]), self.user_id),
                    )
                if ghost_hits <= 3:
                    hurt_feedback += 2.0
            else:
                # 普通书页命中：+5 MV，刷新 TTL
                with self.conn:
                    self.conn.execute(
                        "UPDATE books SET mv = ?, last_accessed = ? "
                        "WHERE id = ? AND user_id = ?",
                        (
                            min(MV_CAP, float(book["mv"]) + MV_PER_HIT),
                            dt_to_str(now),
                            int(book["id"]),
                            self.user_id,
                        ),
                    )

            hits.append(
                RetrievalHit(
                    book_id=int(book["id"]),
                    summary=summary,
                    content=content,
                    emotion_tag=str(book["emotion_tag"]),
                    status=status,
                    mv=float(book["mv"]),
                    score=score,
                    folder_date=str(book["folder_date"]),
                    folder_user=str(book["folder_user"]),
                    folder_event=str(book["folder_event"]),
                    created_at=str(book.get("created_at") or ""),
                    decay_rate=float(book.get("decay_rate", 1.0)),
                    ghost_hits=ghost_hits,
                    deep=deep,
                    score_breakdown=breakdown,
                    fallback=int(book["id"]) in fallback_ids,
                )
            )

        # 排序：把"对方问我记不记得…"这类问句回声压到最后，
        # 否则她每次回忆都只看到自己被告知"你记不记得"，看不到真正发生的事。
        def _is_echo(hit) -> bool:
            text = str(getattr(hit, "summary", "") or "")
            # 她自己对"我记不记得"的追问，也是一种回声：不能挤掉真正发生过的事。
            if re.search(r"(记忆空白|没有这段记忆|记不起来|想知道发生了什么|不知具体发生|不知道怎么回答)", text):
                return True
            return bool(re.search(r"[吗呢？?]\s*$", text)) and ("说：" in text or "问" in text)

        if len(hits) > 1:
            # 先放真答案，再放问句回声，最后放兜底的"最近的事"。
            hits = sorted(hits, key=lambda h: (1 if h.fallback else 0, 1 if _is_echo(h) else 0))

        self._reinforce(hits, now)

        return RetrievalResult(
            hits=hits,
            deep=deep,
            impulse=impulse,
            retrieve_prob=retrieve_prob,
            roll=roll_value,
            note=note,
            mood_congruent=mood_congruent,
            suppressed_skipped=suppressed_skipped,
            hurt_feedback=hurt_feedback,
        )

    def _reinforce(self, hits: Sequence[RetrievalHit], now: datetime) -> None:
        """回忆会加深印象：被想起来的那条，记忆值和"最后碰过的时间"一起往前挪。

        这是她"越常想起越清楚、越久不碰越淡"的物理实现——
        不是设定，是每次真的想起就真的延缓衰减。
        """
        if not hits:
            return
        try:
            stamp = now.isoformat() if hasattr(now, "isoformat") else str(now)
            for hit in hits[:8]:
                book_id = getattr(hit, "id", None)
                if not book_id:
                    continue
                self.conn.execute(
                    "UPDATE books SET mv = MIN(100, mv + ?), last_accessed = ? WHERE id = ? AND user_id = ?",
                    (REINFORCE_MV_BOOST, stamp, book_id, self.user_id),
                )
            self.conn.commit()
        except Exception:  # noqa: BLE001
            try:
                self.conn.rollback()
            except Exception:  # noqa: BLE001
                pass

    # ---------- 梦境素材 ----------

    def sample_dream_books(self, limit: int = DREAM_SAMPLE_LIMIT) -> list[dict[str, Any]]:
        """
        夜间抽取：压抑记忆优先，其次是低 MV 的 summary/ghost/detail。
        """
        rows = self.conn.execute(
            "SELECT * FROM books "
            "WHERE user_id = ? AND (status = 'suppressed' "
            "   OR (status != 'suppressed' AND mv < 40)) "
            "ORDER BY CASE status WHEN 'suppressed' THEN 0 ELSE 1 END, "
            "         mv ASC, RANDOM() LIMIT ?",
            (self.user_id, int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]

    def suppressed_books(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM books WHERE user_id = ? AND status = 'suppressed' "
            "ORDER BY id DESC",
            (self.user_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ---------- 梦境 ----------

    def add_dream(
        self,
        content: str,
        source: str,
        now: datetime | None = None,
        affect: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        写入梦境。expire_at = 第二天中午 12:00。
        过期且 is_discussed=0 的梦会被物理删除。
        """
        now = now or datetime.now()
        expire = (now + timedelta(days=1)).replace(
            hour=12, minute=0, second=0, microsecond=0
        )
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO dreams(user_id, content, source, affect, created_at, "
                "expire_at, is_discussed) VALUES(?,?,?,?,?,?,0)",
                (
                    self.user_id,
                    str(content),
                    str(source),
                    json.dumps(affect or {}, ensure_ascii=False),
                    dt_to_str(now),
                    dt_to_str(expire),
                ),
            )
            dream_id = int(cursor.lastrowid)
        row = self.conn.execute(
            "SELECT * FROM dreams WHERE id = ? AND user_id = ?",
            (dream_id, self.user_id),
        ).fetchone()
        return dict(row) if row else {}

    def next_undiscussed_dream(self, now: datetime | None = None) -> dict[str, Any] | None:
        now = now or datetime.now()
        row = self.conn.execute(
            "SELECT * FROM dreams WHERE user_id = ? AND is_discussed = 0 "
            "AND expire_at > ? ORDER BY id DESC LIMIT 1",
            (self.user_id, dt_to_str(now)),
        ).fetchone()
        return self._row(row)

    def mark_dream_discussed(self, dream_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE dreams SET is_discussed = 1 WHERE id = ? AND user_id = ?",
                (int(dream_id), self.user_id),
            )

    def purge_expired_dreams(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """删除超过 expire_at 且尚未被讨论的梦。"""
        now = now or datetime.now()
        rows = self.conn.execute(
            "SELECT * FROM dreams WHERE user_id = ? AND is_discussed = 0 "
            "AND expire_at <= ?",
            (self.user_id, dt_to_str(now)),
        ).fetchall()
        deleted = [dict(row) for row in rows]
        if deleted:
            with self.conn:
                self.conn.executemany(
                    "DELETE FROM dreams WHERE id = ? AND user_id = ?",
                    [(int(row["id"]), self.user_id) for row in deleted],
                )
        return deleted

    def count_dreams(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM dreams WHERE user_id = ?", (self.user_id,)
        ).fetchone()
        return int(row["n"]) if row else 0

    # ---------- 日记 ----------

    def add_diary(
        self,
        content: str,
        source: str,
        now: datetime | None = None,
        is_discussed: bool = False,
    ) -> dict[str, Any]:
        now = now or datetime.now()
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO diaries(user_id, content, source, created_at, is_discussed) "
                "VALUES(?,?,?,?,?)",
                (
                    self.user_id,
                    str(content),
                    str(source),
                    dt_to_str(now),
                    1 if is_discussed else 0,
                ),
            )
            diary_id = int(cursor.lastrowid)
        row = self.conn.execute(
            "SELECT * FROM diaries WHERE id = ? AND user_id = ?",
            (diary_id, self.user_id),
        ).fetchone()
        return dict(row) if row else {}

    def has_diary_on(self, date_str: str) -> bool:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM diaries WHERE user_id = ? "
            "AND substr(created_at, 1, 10) = ?",
            (self.user_id, date_str),
        ).fetchone()
        return bool(row and int(row["n"]) > 0)

    def next_undiscussed_diary(self, now: datetime | None = None) -> dict[str, Any] | None:
        """优先找"昨天"的日记；没有就退而求其次找最近未讨论的一篇。"""
        now = now or datetime.now()
        today = now.date().isoformat()
        row = self.conn.execute(
            "SELECT * FROM diaries WHERE user_id = ? AND is_discussed = 0 "
            "AND substr(created_at, 1, 10) < ? ORDER BY id DESC LIMIT 1",
            (self.user_id, today),
        ).fetchone()
        if row is None:
            row = self.conn.execute(
                "SELECT * FROM diaries WHERE user_id = ? AND is_discussed = 0 "
                "ORDER BY id DESC LIMIT 1",
                (self.user_id,),
            ).fetchone()
        return self._row(row)

    def mark_diary_discussed(self, diary_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE diaries SET is_discussed = 1 WHERE id = ? AND user_id = ?",
                (int(diary_id), self.user_id),
            )

    def count_diaries(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM diaries WHERE user_id = ?", (self.user_id,)
        ).fetchone()
        return int(row["n"]) if row else 0

    # ---------- 人格快照 ----------

    def add_snapshot(
        self,
        now: datetime,
        emotion: dict[str, float],
        joy_baseline: float,
        hurt_baseline: float,
        intimacy: int,
        book_count: int,
        ghost_count: int,
        suppressed_count: int,
    ) -> dict[str, Any]:
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO snapshots(user_id, created_at, emotion, joy_baseline, "
                "hurt_baseline, intimacy, book_count, ghost_count, suppressed_count) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    self.user_id,
                    dt_to_str(now),
                    json.dumps(emotion, ensure_ascii=False),
                    float(joy_baseline),
                    float(hurt_baseline),
                    int(intimacy),
                    int(book_count),
                    int(ghost_count),
                    int(suppressed_count),
                ),
            )
            snapshot_id = int(cursor.lastrowid)
        row = self.conn.execute(
            "SELECT * FROM snapshots WHERE id = ? AND user_id = ?",
            (snapshot_id, self.user_id),
        ).fetchone()
        return self._snapshot_row(row) if row else {}

    @staticmethod
    def _snapshot_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        try:
            data["emotion"] = json.loads(data.get("emotion") or "{}")
        except json.JSONDecodeError:
            data["emotion"] = {}
        return data

    def get_snapshots(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM snapshots WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (self.user_id, int(limit)),
        ).fetchall()
        return [self._snapshot_row(row) for row in rows]

    def count_snapshots(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM snapshots WHERE user_id = ?", (self.user_id,)
        ).fetchone()
        return int(row["n"]) if row else 0

    # ---------- 可观测性 trace ----------

    def add_trace(
        self,
        now: datetime,
        user_text: str,
        reply: str,
        self_control: dict[str, Any] | None,
        memory_control: dict[str, Any] | None,
        memory_action: str,
        emotion: dict[str, float],
        retrieval: dict[str, Any],
        external_info: str,
        safety_blocked: bool,
        trauma: dict[str, Any],
        source: str,
        usage: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO traces(user_id, created_at, user_text, reply, self_control, "
                "memory_control, memory_action, emotion, retrieval, external_info, "
                "safety_blocked, trauma, source, prompt_tokens, completion_tokens, cached_tokens) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.user_id,
                    dt_to_str(now),
                    str(user_text),
                    str(reply),
                    json.dumps(self_control or {}, ensure_ascii=False),
                    json.dumps(memory_control or {}, ensure_ascii=False),
                    str(memory_action),
                    json.dumps(emotion, ensure_ascii=False),
                    json.dumps(retrieval, ensure_ascii=False),
                    str(external_info),
                    1 if safety_blocked else 0,
                    json.dumps(trauma, ensure_ascii=False),
                    str(source),
                    int((usage or {}).get("prompt_tokens") or 0),
                    int((usage or {}).get("completion_tokens") or 0),
                    int((usage or {}).get("cached_tokens")
                        or (usage or {}).get("prompt_cache_hit_tokens") or 0),
                ),
            )
            trace_id = int(cursor.lastrowid)
        row = self.conn.execute(
            "SELECT * FROM traces WHERE id = ? AND user_id = ?",
            (trace_id, self.user_id),
        ).fetchone()
        return self._trace_row(row) if row else {}

    @staticmethod
    def _trace_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for key in ("self_control", "memory_control", "emotion", "retrieval", "trauma"):
            try:
                data[key] = json.loads(data.get(key) or "{}")
            except json.JSONDecodeError:
                data[key] = {}
        data["safety_blocked"] = bool(data.get("safety_blocked"))
        return data

    def get_traces(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM traces WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (self.user_id, int(limit)),
        ).fetchall()
        return [self._trace_row(row) for row in rows]

    def count_traces(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM traces WHERE user_id = ?", (self.user_id,)
        ).fetchone()
        return int(row["n"]) if row else 0

    # ---------- 去重与冲突消解 ----------

    def find_similar_book(
        self,
        summary: str,
        folder_date: str,
        folder_user: str,
        folder_event: str,
        now: datetime,
        hours: int = 12,
    ) -> dict[str, Any] | None:
        """同一用户、同一天、同一事件、同一书皮 -> 视为重复记忆。"""
        cutoff = dt_to_str(now - timedelta(hours=hours))
        row = self.conn.execute(
            "SELECT * FROM books WHERE user_id = ? AND summary = ? AND folder_date = ? "
            "AND folder_user = ? AND folder_event = ? AND status != 'ghost' "
            "AND status != 'suppressed' AND last_accessed >= ? "
            "ORDER BY id DESC LIMIT 1",
            (
                self.user_id,
                str(summary),
                str(folder_date),
                str(folder_user),
                str(folder_event),
                cutoff,
            ),
        ).fetchone()
        return self._row(row)

    def reinforce_book(
        self,
        book_id: int,
        now: datetime,
        bonus: float = 3.0,
        decay_rate: float | None = None,
    ) -> dict[str, Any] | None:
        book = self.get_book(book_id)
        if book is None:
            return None
        new_mv = min(MV_CAP, float(book["mv"]) + float(bonus))
        new_decay = float(book.get("decay_rate", 1.0))
        if decay_rate is not None:
            new_decay = min(new_decay, float(decay_rate))
        with self.conn:
            self.conn.execute(
                "UPDATE books SET mv = ?, decay_rate = ?, last_accessed = ? "
                "WHERE id = ? AND user_id = ?",
                (new_mv, new_decay, dt_to_str(now), int(book_id), self.user_id),
            )
        return self.get_book(book_id)

    # ---------- meta ----------

    def load_meta(self) -> dict[str, Any]:
        rows = self.conn.execute(
            "SELECT key, value FROM meta WHERE user_id = ?", (self.user_id,)
        ).fetchall()
        return {str(row["key"]): json.loads(row["value"]) for row in rows}

    def save_meta(self, values: dict[str, Any]) -> None:
        with self.conn:
            self.conn.executemany(
                "INSERT INTO meta(user_id, key, value) VALUES(?, ?, ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value",
                [
                    (self.user_id, str(key), json.dumps(value, ensure_ascii=False))
                    for key, value in values.items()
                ],
            )

# LibraryMemory 的所有触库入口统一走同一把可重入锁；RLock 允许 retrieve
# 内部继续调用 purge_ghosts / _degrade_row 等已加锁方法。
def _with_db_lock(method: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(method)
    def wrapped(self: "LibraryMemory", *args: Any, **kwargs: Any) -> Any:
        with self._db_lock:
            return method(self, *args, **kwargs)

    return wrapped


for _db_method_name in (
    "close", "add_book", "get_book", "latest_book", "list_books", "status_counts",
    "count_unresolved", "list_unresolved", "auto_resolve_stale_obsessions",
    "mark_unresolved", "mark_obsession_mentioned", "clear_obsession",
    "persona_rows", "recent_hurt_average", "set_mv", "_degrade_row",
    "degrade_all", "purge_ghosts", "apply_night_hygiene", "retrieve",
    "sample_dream_books", "suppressed_books", "add_dream", "next_undiscussed_dream",
    "mark_dream_discussed", "purge_expired_dreams", "count_dreams", "add_diary",
    "has_diary_on", "next_undiscussed_diary", "mark_diary_discussed", "count_diaries",
    "add_snapshot", "get_snapshots", "count_snapshots", "add_trace",
    "get_traces", "count_traces", "find_similar_book", "reinforce_book",
    "load_meta", "save_meta",
):
    setattr(LibraryMemory, _db_method_name, _with_db_lock(getattr(LibraryMemory, _db_method_name)))
