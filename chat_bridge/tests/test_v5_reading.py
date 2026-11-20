import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from digital_life import DigitalLifeV2, extract_thought  # noqa: E402


def make_life(tmp_path, monkeypatch, replies):
    shelf = tmp_path / "bookshelf"
    shelf.mkdir()
    (shelf / "测试书.md").write_text(
        "第1章 开头\n" + "ALPHA" * 40 + "\n\n第2章 中段\n" + "BETA" * 40 + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("XIAOYU_BOOKSHELF", str(shelf))
    calls = []

    def fake_llm(messages):
        calls.append(messages)
        index = min(len(calls) - 1, len(replies) - 1)
        return replies[index]

    life = DigitalLifeV2(str(tmp_path / "test.db"), offline_llm=False, rng_seed=3, llm_fn=fake_llm)
    life._summarize_chapter = lambda book, chapter, content: (f"第{chapter}章摘要", "好奇")
    life.scan_bookshelf(force=True)
    return life, calls


def test_thought_is_stripped(tmp_path, monkeypatch):
    life, _calls = make_life(tmp_path, monkeypatch, ["[THOUGHT: 我只是有点困]\n你好呀，我在。"])
    result = life.chat("在吗")
    assert "THOUGHT" not in result["reply"]
    assert "你好呀" in result["reply"]
    assert "有点困" in result.get("thought", "")


def test_read_full_injects_content_and_shortens_window(tmp_path, monkeypatch):
    life, calls = make_life(
        tmp_path,
        monkeypatch,
        ["[READ_FULL: 测试书, 2]\n我翻开看看", "读完了，讲的是那件事。"],
    )
    result = life.chat("你在看书吗")
    assert len(calls) >= 2
    second_prompt = "\n".join(str(m.get("content", "")) for m in calls[-1])
    assert "BETA" in second_prompt
    assert life.short_window_seconds == 3600
    assert result["pause_poll_minutes"] == 10
    assert "读完了" in result["reply"]
    assert "READ_FULL" not in result["reply"]


def test_chapter_summary_has_no_length_cap_and_reads_full_content(tmp_path, monkeypatch):
    life, _calls = make_life(tmp_path, monkeypatch, ["好"])
    del life._summarize_chapter
    content = "开头。" + "中间的故事很长。" * 500 + "结尾。"
    seen = {}

    def fake_generate(prompt, **kwargs):
        seen["prompt"] = prompt
        return "【情绪】好奇、沉重\n" + "这是完整摘要。" * 300

    monkeypatch.setattr("digital_life.ollama_generate", fake_generate)
    summary, mood = life._summarize_chapter("长书", 1, content)
    assert "结尾。" in seen["prompt"]
    assert len(summary) > 1500
    assert mood == "好奇、沉重"


def test_reading_window_is_600_only_while_reading(tmp_path, monkeypatch):
    life, _calls = make_life(
        tmp_path,
        monkeypatch,
        ["[READ_FULL: 测试书, 2]\n我看看", "读完了。"],
    )
    seen = []
    original = life._distill_reading

    def capture(full):
        seen.append(life.short_window_seconds)
        return original(full)

    monkeypatch.setattr(life, "_distill_reading", capture)
    monkeypatch.setattr("digital_life.ollama_generate", lambda *args, **kwargs: "读后感")
    life.chat("读第二章")
    assert seen == [600]
    assert life.short_window_seconds == 3600


def test_bare_thought_tag_is_stripped():
    reply, thought = extract_thought("[THOUGHT] 他真去改了。\n嗯，真的改了。")
    assert "[THOUGHT" not in reply
    assert "他真去改了" in reply
    assert thought == ""


def test_unclosed_thought_tag_is_not_sent():
    reply, _thought = extract_thought("[THOUGHT: 他真去改了，我有点开心。\n哦，原来是这样。")
    assert "[THOUGHT" not in reply
    assert "原来是这样" in reply


def test_default_memory_decay_follows_xiaoyu_choice(tmp_path, monkeypatch):
    life, _calls = make_life(tmp_path, monkeypatch, ["好"])
    result = life.chat("今天有点累")
    assert result["book"]["decay_rate"] == 0.5


def test_shelf_teaches_reading_tags(tmp_path, monkeypatch):
    life, calls = make_life(tmp_path, monkeypatch, ["嗯，书架上有书。"])
    life.scan_bookshelf(force=True)
    assert "书目：" in life.shelf_block()
    assert "第1章摘要" in life.shelf_block()
    assert "情绪：好奇" in life.shelf_block()

    life.chat("书架里有什么")
    system_prompt = str(calls[-1][0]["content"])
    assert "[READ_FULL: 书名, 1]" in system_prompt
    assert "[READ_BOOK: 书名, 1]" in system_prompt
    assert "怎么读由你决定" in system_prompt
    assert "只看摘要" not in system_prompt


def test_split_chapters_supports_hui_and_circle_numerals():
    text = "第一回 甲\nA\n第二回 乙\nB\n第一○○回 丙\nC\n"
    parts = DigitalLifeV2._split_chapters(text)
    assert len(parts) == 3
    assert parts[0].startswith("第一回")
    assert parts[-1].startswith("第一○○回")


def test_split_chapters_drops_leading_front_matter():
    text = "书名页\n内容简介\n\n第1章 开始\nA\n第2章 继续\nB\n"
    parts = DigitalLifeV2._split_chapters(text)
    assert len(parts) == 2
    assert parts[0].startswith("第1章")


def test_shelf_ignores_readme(tmp_path, monkeypatch):
    life, _calls = make_life(tmp_path, monkeypatch, ["好"])
    (life.bookshelf_dir / "README.md").write_text("这是说明，不是书。", encoding="utf-8")
    assert all(path.name != "README.md" for path in life._shelf_files())


def test_read_tag_accepts_di_prefix(tmp_path, monkeypatch):
    life, calls = make_life(
        tmp_path,
        monkeypatch,
        ["[READ_BOOK: 测试书, 第1章]\n我翻翻摘要", "看到了。"],
    )
    result = life.chat("看看书架")
    second_prompt = "\n".join(str(m.get("content", "")) for m in calls[-1])
    assert "第1章摘要" in second_prompt
    assert "[READ_BOOK" not in result["reply"]


def test_chapter_summary_accepts_bare_emotion_prefix(tmp_path, monkeypatch):
    life, _calls = make_life(tmp_path, monkeypatch, ["好"])
    del life._summarize_chapter
    monkeypatch.setattr(
        "digital_life.ollama_generate",
        lambda *args, **kwargs: "【震惊 焦虑】\n我看着他们，心里越来越不安。",
    )
    summary, emotion = life._summarize_chapter("测试书", 1, "原文")
    assert emotion == "震惊、焦虑"
    assert summary.startswith("我看着他们")


def test_keep_memory_tag_is_parsed_and_stored(tmp_path, monkeypatch):
    life, _calls = make_life(
        tmp_path,
        monkeypatch,
        ["[THOUGHT: 这段很重要][KEEP_MEMORY: 他第一次叫我的名字，我要记住, 0.05]\n我记住了。"],
    )
    result = life.chat("你记得吗")
    assert "[KEEP_MEMORY" not in result["reply"]
    assert result["kept_memories"]
    kept = [book for book in life.library.list_books() if book["folder_event"] == "长期"]
    assert len(kept) == 1
    assert "第一次叫我的名字" in kept[0]["content"]
    assert kept[0]["decay_rate"] == 0.05


def test_transient_memory_is_deleted_by_hygiene(tmp_path, monkeypatch):
    life, _calls = make_life(tmp_path, monkeypatch, ["好"])
    life._store_transient_memory(["用户：今天有点累", "她：那就早点睡"])
    transient = [book for book in life.library.list_books() if book["folder_event"] == "瞬态"]
    assert len(transient) == 1
    assert transient[0]["decay_rate"] == 5.0

    life.library.apply_night_hygiene(now=life.now())
    transient = [book for book in life.library.list_books() if book["folder_event"] == "瞬态"]
    assert transient == []


def test_prompt_has_four_part_self_model(tmp_path, monkeypatch):
    life, calls = make_life(tmp_path, monkeypatch, ["好"])
    life.chat("在吗")
    system_prompt = str(calls[-1][0]["content"])
    assert "你的思想由四部分组成" in system_prompt
    assert "固有认知" in system_prompt
    assert "[KEEP_MEMORY: 记忆内容, decay_rate]" in system_prompt
    assert "【你的成长日记】" in system_prompt
    assert "我是我的灵魂" in system_prompt


def test_pause_poll_tag_is_parsed_and_stripped(tmp_path, monkeypatch):
    life, _calls = make_life(
        tmp_path,
        monkeypatch,
        ["[PAUSE_POLL: 15]\n我去看会书，等会儿再回你"],
    )
    result = life.chat("你先看书吧")
    assert result["pause_poll_minutes"] == 15
    assert "[PAUSE_POLL" not in result["reply"]
    assert "等会儿再回你" in result["reply"]


def test_eviction_preview_shows_before_drop(tmp_path, monkeypatch):
    life, _calls = make_life(tmp_path, monkeypatch, ["好"])
    life.history = [
        {"role": "user", "content": "旧问题一"},
        {"role": "assistant", "content": "旧回答一"},
        {"role": "user", "content": "旧问题二"},
        {"role": "assistant", "content": "旧回答二"},
        {"role": "user", "content": "新问题"},
        {"role": "assistant", "content": "新回答"},
    ]
    life.history_times = [life.now() - timedelta(hours=2)] * len(life.history)
    preview = life._preview_evictions()
    assert any("旧问题一" in line for line in preview)
    assert not any("新问题" in line for line in preview)


def test_new_book_scanned_even_within_throttle(tmp_path, monkeypatch):
    shelf = tmp_path / "bookshelf"
    shelf.mkdir()
    monkeypatch.setenv("XIAOYU_BOOKSHELF", str(shelf))

    life = DigitalLifeV2(str(tmp_path / "test.db"), offline_llm=False, rng_seed=3, llm_fn=lambda _m: "好")
    life._summarize_chapter = lambda book, chapter, content: (f"第{chapter}章摘要", "好奇")
    assert life.scan_bookshelf(force=True) == 0

    (shelf / "新书.md").write_text("第1章 开始\n" + "GAMMA" * 40 + "\n", encoding="utf-8")
    assert life.scan_bookshelf() == 1
    assert "新书" in life.shelf_block()


def test_finished_chapter_disappears_from_shelf(tmp_path, monkeypatch):
    life, _calls = make_life(
        tmp_path,
        monkeypatch,
        ["[READ_FULL: 测试书, 1]\n我读第一章", "读完第一章了。"],
    )
    life.scan_bookshelf(force=True)
    assert "第1章摘要" in life.shelf_block()
    monkeypatch.setattr("digital_life.ollama_generate", lambda *args, **kwargs: "这一章让我想起很多事。")

    life.chat("读第一章吧")
    shelf = life.shelf_block()
    assert "第1章摘要" not in shelf
    assert "第2章摘要" in shelf
    conn = life._bookshelf_conn()
    content, read_at = conn.execute(
        "SELECT content, read_at FROM book_chapters WHERE book_name='测试书' AND chapter_num=1"
    ).fetchone()
    conn.close()
    assert "ALPHA" in content
    assert read_at
