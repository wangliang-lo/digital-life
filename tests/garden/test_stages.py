# -*- coding: utf-8 -*-
"""阶段推进（STAGE_DAYS = [0, 1, 3, 5, 8]）。

这些用例只碰 tick()/_stage() 这类纯函数，失败时能直接指出边界差了一格。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

STAGES = ["刚埋下去", "冒芽了", "长叶子了", "开花了", "熟透了"]


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (0, 0),
        (1, 1),
        (2, 1),
        (3, 2),
        (4, 2),
        (5, 3),
        (6, 3),
        (7, 3),
        (8, 4),
        (9, 4),
        (30, 4),
    ],
)
def test_stage_boundaries(garden, clock, plot, age, expected):
    """边界日必须精确：早一天不算，到了当天必须进入下一阶段。"""
    clock.advance(age)
    p = plot("薄荷", clock.today() - timedelta(days=age))
    assert garden._stage(p) == expected
    assert STAGES[garden._stage(p)] == STAGES[expected]


def test_stage_never_goes_backwards_as_age_grows(garden, clock, plot):
    """年龄单调增长时阶段不能回退（防止有人把 >= 写成 ==）。"""
    planted = clock.today()
    previous = -1
    for age in range(0, 15):
        clock.set(planted + timedelta(days=age))
        stage = garden._stage(plot("番茄", planted))
        assert stage >= previous, f"第 {age} 天阶段从 {previous} 退回 {stage}"
        previous = stage


def test_tick_persists_stage_and_announces_change(garden, clock, plot, seed):
    """tick 要把新阶段写回 plant，并给出对应的一句话（开花/长叶/冒芽）。"""
    seed([plot("小葱", clock.today() - timedelta(days=3))])
    data = garden._load()
    notes = garden.tick(data)
    assert data["plots"][0]["stage"] == 2
    assert any("长叶子了" in n for n in notes), notes


def test_tick_is_idempotent_for_same_day(garden, clock, plot, seed):
    """同一天连调两次 tick，第二次不该再报一次生长（否则聊天里会刷屏重复）。"""
    seed([plot("小葱", clock.today() - timedelta(days=1))])
    data = garden._load()
    first = garden.tick(data)
    second = garden.tick(data)
    assert any("冒芽了" in n for n in first)
    assert not any("冒芽了" in n for n in second), second


def test_tick_no_announcement_for_ripe_stage(garden, clock, plot, seed):
    """进入“熟透了”不该混进生长播报，熟透靠 look/harvest 体现。"""
    seed([plot("番茄", clock.today() - timedelta(days=8), clock.today())])
    data = garden._load()
    notes = garden.tick(data)
    assert data["plots"][0]["stage"] == 4
    assert not any("熟透了" in n for n in notes), notes


def test_stage_uses_planted_even_without_watered(garden, clock):
    """没写过 watered 的记录也要按 planted 算年龄，不能当 0 岁。"""
    clock.advance(5)
    p = {"name": "野草", "planted": (clock.today() - timedelta(days=5)).isoformat()}
    assert garden._stage(p) == 3
