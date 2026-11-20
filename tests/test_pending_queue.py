# -*- coding: utf-8 -*-
"""待回队列：只追加、去重、ack、坏行、prune 归档、并发。全部用 tmp 目录。"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent / "chat_bridge" / "tools"
sys.path.insert(0, str(TOOLS))

import pending_queue as pq  # noqa: E402


@pytest.fixture()
def f(tmp_path):
    return tmp_path / "data" / "pending_messages.jsonl"


def _lines(p):
    return [x for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_push_then_list_has_fields(f):
    rec = pq.push("小明", "你好", "睡着了", path=f)
    got = pq.list_pending(path=f)
    assert len(got) == 1
    for k in ("id", "at", "sender", "text", "reason"):
        assert k in got[0]
    assert got[0]["id"] == rec["id"] and got[0]["text"] == "你好" and got[0]["reason"] == "睡着了"


def test_list_newest_first_and_limit(f):
    for i in range(5):
        pq.push("a", f"m{i}", path=f)
    assert [r["text"] for r in pq.list_pending(3, path=f)] == ["m4", "m3", "m2"]


def test_dedup_within_60s(f):
    a = pq.push("a", "同一句", path=f)
    b = pq.push("a", "同一句", path=f)
    assert a["id"] == b["id"]
    assert len(_lines(f)) == 1
    pq.push("b", "同一句", path=f)  # 不同 sender 不去重
    pq.push("a", "另一句", path=f)
    assert len(_lines(f)) == 3


def test_dedup_expires_after_60s(f):
    old = {"id": "x1", "at": "2020-01-01T00:00:00+00:00", "ts": 1.0, "sender": "a", "text": "t", "reason": ""}
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps(old) + "\n", encoding="utf-8")
    pq.push("a", "t", path=f)
    assert len(_lines(f)) == 2


def test_ack_reduces_count_and_hides(f):
    r1 = pq.push("a", "1", path=f)
    pq.push("a", "2", path=f)
    assert pq.pending_count(path=f) == 2
    before = f.read_text(encoding="utf-8")
    pq.ack(r1["id"], path=f)
    assert pq.pending_count(path=f) == 1
    assert [r["text"] for r in pq.list_pending(path=f)] == ["2"]
    assert f.read_text(encoding="utf-8") == before  # 主文件没被改写
    assert pq.acked_path(f).exists()


def test_corrupt_line_skipped(f):
    pq.push("a", "前", path=f)
    with f.open("a", encoding="utf-8") as fh:
        fh.write('{"id": "bad", "text": "半截\n')
    pq.push("a", "后", path=f)
    assert [r["text"] for r in pq.list_pending(path=f)] == ["后", "前"]


def test_prune_archives_old_and_keeps_rest(f):
    ids = [pq.push("a", f"m{i}", path=f)["id"] for i in range(10)]
    n = pq.prune(keep=4, path=f)
    assert n == 6
    main = [json.loads(x)["id"] for x in _lines(f)]
    arch = [json.loads(x)["id"] for x in _lines(pq.archive_path(f))]
    assert main == ids[6:] and arch == ids[:6]
    assert pq.pending_count(path=f) == 4
    assert pq.prune(keep=4, path=f) == 0


def test_legacy_format_compatible(f):
    f.parent.mkdir(parents=True)
    old = {"at": "2026-11-03T10:00:00+08:00", "sender": "x", "text": "旧的", "reason": "r"}
    done = {"at": "2026-11-03T10:01:00+08:00", "sender": "x", "text": "已回", "replied_at": "z"}
    f.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in (old, done)) + "\n", encoding="utf-8")
    got = pq.list_pending(path=f)
    assert [r["text"] for r in got] == ["旧的"]
    assert got[0]["id"] == pq.list_pending(path=f)[0]["id"]  # id 稳定
    pq.ack(got[0]["id"], path=f)
    assert pq.pending_count(path=f) == 0
    assert f.exists()


def test_concurrent_push_keeps_all(f):
    def work(tag):
        for i in range(50):
            pq.push("s", f"{tag}-{i}", path=f)

    with ThreadPoolExecutor(2) as ex:
        list(ex.map(work, ["A", "B"]))
    texts = {r["text"] for r in pq.list_pending(1000, path=f)}
    assert len(texts) == 100 and len(_lines(f)) == 100
