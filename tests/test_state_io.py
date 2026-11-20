import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "chat_bridge" / "tools"))

import state_io  # noqa: E402


def test_write_json_roundtrip_and_no_tmp_left(tmp_path):
    p = tmp_path / "sub" / "a.json"
    state_io.write_json(p, {"k": "中文", "n": 1})
    assert json.loads(p.read_text(encoding="utf-8")) == {"k": "中文", "n": 1}
    assert [f.name for f in p.parent.iterdir()] == ["a.json"]


def test_atomic_write_failure_keeps_old_content(tmp_path, monkeypatch):
    p = tmp_path / "a.json"
    state_io.write_json(p, {"old": True})

    def boom(*a, **k):
        raise OSError("simulated crash mid-write")

    monkeypatch.setattr(state_io.os, "replace", boom)
    with pytest.raises(OSError):
        state_io.write_json(p, {"new": True})
    assert json.loads(p.read_text(encoding="utf-8")) == {"old": True}
    assert [f.name for f in tmp_path.iterdir()] == ["a.json"]  # 临时文件已清理


def test_unserializable_data_keeps_old_content(tmp_path):
    p = tmp_path / "a.json"
    state_io.write_json(p, {"old": 1})
    with pytest.raises(TypeError):
        state_io.write_json(p, {"bad": object()})
    assert json.loads(p.read_text(encoding="utf-8")) == {"old": 1}


def test_concurrent_update_different_keys(tmp_path):
    p = tmp_path / "s.json"
    errors = []

    def worker(key):
        try:
            for i in range(30):
                state_io.update_json(p, {key: i})
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(k,)) for k in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert state_io.read_json(p) == {"a": 29, "b": 29}


def test_update_json_merges_existing(tmp_path):
    p = tmp_path / "s.json"
    state_io.write_json(p, {"keep": 1, "x": 1})
    assert state_io.update_json(p, {"x": 2}) == {"keep": 1, "x": 2}


def test_read_json_bad_or_missing_returns_default(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"half": ', encoding="utf-8")
    assert state_io.read_json(bad, {"d": 1}) == {"d": 1}
    assert state_io.read_json(tmp_path / "nope.json") is None
    assert state_io.read_json(tmp_path / "nope.json", []) == []


def test_update_json_recovers_from_bad_file(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("not json", encoding="utf-8")
    assert state_io.update_json(p, {"a": 1}) == {"a": 1}


def test_jsonl_append_parses_line_by_line_and_creates_parent(tmp_path):
    p = tmp_path / "deep" / "dir" / "log.jsonl"
    for i in range(5):
        state_io.write_jsonl_append(p, {"i": i, "t": "中"})
    rows = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()]
    assert rows == [{"i": i, "t": "中"} for i in range(5)]
