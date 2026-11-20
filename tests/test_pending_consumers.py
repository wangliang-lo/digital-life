# -*- coding: utf-8 -*-
"""待回队列的三个消费者（pending_replay / monitor / web_ui）统一走 pending_queue。全部用 tmp 目录。"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "chat_bridge"))
sys.path.insert(0, str(ROOT / "chat_bridge" / "tools"))

import monitor  # noqa: E402
import pending_queue as pq  # noqa: E402
import pending_replay  # noqa: E402
import web_ui  # noqa: E402


@pytest.fixture()
def f(tmp_path, monkeypatch):
    path = tmp_path / "data" / "pending_messages.jsonl"
    monkeypatch.setattr(pending_replay, "PENDING_FILE", path)
    monkeypatch.setattr(web_ui, "ROOT", tmp_path)
    monkeypatch.setattr(monitor, "DATA", path.parent)
    pending_replay._attempts.clear()
    return path


def _web_waiting(tmp_path):
    app = object.__new__(web_ui.WebApp)
    return app.desk_chat()


def _legacy(path, text, at=None):
    at = at or datetime.now().astimezone().isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": at, "sender": "小明", "text": text}, ensure_ascii=False) + "\n")


def _texts(items):
    return sorted(i["text"] for i in items)


def test_ack_hides_from_all_three_consumers(f, tmp_path):
    a = pq.push("小明", "甲", path=f)
    pq.push("小明", "乙", path=f)
    pq.ack(a["id"], path=f)
    assert _texts(pending_replay.read_pending()) == ["乙"]
    assert _texts(monitor.pending_waiting(f)) == ["乙"]
    w = _web_waiting(tmp_path)
    assert [x["text"] for x in w["waiting"]] == ["乙"] and w["waiting_count"] == 1


def test_replay_legacy_record_without_id_acks_and_keeps_new(f, monkeypatch):
    _legacy(f, "旧消息")
    new = pq.push("小明", "新消息", path=f)
    before = f.read_text(encoding="utf-8")
    sent = []
    monkeypatch.setattr(pending_replay, "busy_reason", lambda: "")
    monkeypatch.setattr(pending_replay, "chat", lambda p: "好的")
    monkeypatch.setattr(pending_replay, "send_chat", lambda to, t: sent.append((to, t)) or True)
    assert pending_replay.replay_once() is True
    assert sent
    assert pq.list_pending(path=f) == []          # 两条都 ack 了
    assert f.read_text(encoding="utf-8") == before  # 主文件没被改写
    # replay 期间新来的消息不会被覆盖
    later = pq.push("小明", "后来的", path=f)
    assert [r["id"] for r in pq.list_pending(path=f)] == [later["id"]]
    assert new["id"] in {json.loads(x)["id"] for x in pq.acked_path(f).read_text(encoding="utf-8").splitlines()}


def test_replay_send_failure_counts_and_blocks_without_ack(f, monkeypatch):
    pq.push("小明", "在吗呀", path=f)
    monkeypatch.setattr(pending_replay, "busy_reason", lambda: "")
    monkeypatch.setattr(pending_replay, "chat", lambda p: "好")
    monkeypatch.setattr(pending_replay, "send_chat", lambda to, t: False)
    for _ in range(3):
        assert pending_replay.replay_once() is False
    assert len(pq.list_pending(path=f)) == 1      # 没 ack
    assert pending_replay.read_pending() == []     # 但已搁置


def test_replay_expires_old_by_ack(f):
    old = (datetime.now().astimezone() - timedelta(hours=13)).isoformat(timespec="seconds")
    _legacy(f, "很久以前", at=old)
    assert pending_replay.read_pending() == []
    assert pq.list_pending(path=f) == []
    assert pending_replay.expire_old() == 0


def test_main_and_acked_files_together(f, tmp_path):
    _legacy(f, "旧1")
    legacy = pq.list_pending(path=f)[0]
    n = pq.push("小明", "新1", path=f)
    n2 = pq.push("小明", "新2", path=f)
    with f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": "x", "sender": "s", "text": "老回过", "replied_at": "y"}, ensure_ascii=False) + "\n")
        fh.write("坏行{\n")
    pq.ack(legacy["id"], path=f)
    pq.ack(n["id"], path=f)
    assert _texts(monitor.pending_waiting(f)) == ["新2"]
    w = _web_waiting(tmp_path)
    assert [x["text"] for x in w["waiting"]] == ["新2"]
    assert n2["id"]
