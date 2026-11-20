# -*- coding: utf-8 -*-
"""只读视图与容错：look / snapshot / handle / 缺文件 / 坏 json / 日志。"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest


def test_look_empty_garden(garden, state):
    """空院子：ok，提示空，且会创建一个合法状态文件。"""
    result = garden.look()
    assert result["ok"] is True
    assert "空" in result["line"]
    assert state()["plots"] == []


def test_look_reports_name_stage_and_thirst(garden, clock, plot, seed):
    """look 的文案里要同时带上名字、阶段名和蔫的状态。"""
    seed(
        [
            plot("薄荷", clock.today() - timedelta(days=8), clock.today(), stage=4),
            plot("小葱", clock.today() - timedelta(days=3), clock.today() - timedelta(days=2), stage=2),
        ]
    )
    line = garden.look()["line"]
    assert "薄荷" in line and "熟透了" in line and "精神" in line
    assert "小葱" in line and "长叶子了" in line and "有点蔫" in line


def test_look_persists_thirstiness(garden, clock, plot, seed, state):
    """look 不能只说不记：蔫的状态要落盘，供别的进程读。"""
    seed([plot("番茄", clock.today() - timedelta(days=6), clock.today() - timedelta(days=2), stage=3)])
    garden.look()
    assert state()["plots"][0]["thirsty"] is True


def test_snapshot_matches_state(garden, clock, plot, seed):
    """snapshot 的计数与磁盘一致，且是纯只读（不改 tick 结果以外的字段）。"""
    seed(
        [plot("薄荷", clock.today() - timedelta(days=1))],
        harvested=[{"name": "老番茄", "at": "2026-10-01"}],
        died=[{"name": "野草", "at": "2026-10-02"}],
    )
    snap = garden.snapshot()
    assert snap["count"] == 1
    assert snap["plots"] == [{"name": "薄荷", "stage": 0, "thirsty": False}]
    assert snap["harvested"] == 1
    assert snap["died"] == 1


def test_snapshot_does_not_tick(garden, clock, plot, seed, state):
    """snapshot 不该推进生长（它只是看账本），否则面板轮询会养熟植物。"""
    seed([plot("番茄", clock.today() - timedelta(days=9))])
    garden.snapshot()
    assert state()["plots"][0]["stage"] == 0


def test_missing_state_file_is_empty_garden(garden):
    """状态文件不存在时按空院子处理，不抛。"""
    assert not garden.STATE.exists()
    assert garden._load()["plots"] == []
    assert garden.snapshot()["count"] == 0


def test_corrupt_state_file_is_empty_garden(garden, state):
    """状态文件是坏 json 时按空院子处理，且下一次写入能修复它。"""
    garden.STATE.write_text("{ 这不是 json", encoding="utf-8")
    assert garden._load()["plots"] == []
    garden.look()
    assert json.loads(garden.STATE.read_text(encoding="utf-8"))["plots"] == []


def test_handle_aliases(garden, clock, plot, seed):
    """浇水/water 都写今天；空动作落到 look。"""
    for action in ("浇水", "water"):
        seed([plot("薄荷", clock.today() - timedelta(days=2), clock.today() - timedelta(days=2))])
        clock.advance(2)  # gap=4，蔫着但还活着
        assert garden.handle(action)["ok"] is True
        assert state_watered(garden) == clock.today().isoformat()
        garden.STATE.unlink()
    assert garden.handle("", "")["ok"] is True


def state_watered(garden):
    import json as _json

    return _json.loads(garden.STATE.read_text(encoding="utf-8"))["plots"][0]["watered"]


def test_unknown_action_falls_back_to_look(garden, clock, plot, seed):
    """未识别的动作返回的是 look 的结果，不是抛异常。"""
    seed([plot("薄荷", clock.today())])
    assert garden.handle("跳舞") == garden.handle("看看")


def test_log_appends_one_json_line_per_mutation(garden, clock, plot, seed):
    """写操作在 garden_log.jsonl 追加 jsonl，一行一条，at 是 ISO 格式。"""
    garden.plant("薄荷")
    garden.water()
    lines = [json.loads(l) for l in garden.LOG.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 2
    assert "种下薄荷" in lines[0]["text"]
    assert lines[1]["text"] == "浇水"
    assert all("T" in item["at"] for item in lines)


def test_read_only_actions_do_not_log(garden, clock, plot, seed):
    """看看/收不熟的东西不该产生日志噪音。"""
    seed([plot("薄荷", clock.today())])
    garden.look()
    garden.harvest()
    assert not garden.LOG.exists() or garden.LOG.read_text(encoding="utf-8").strip() == ""
