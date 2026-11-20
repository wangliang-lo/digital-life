# -*- coding: utf-8 -*-
"""种下：记录日期、默认浇水、容量上限、名字清洗。"""
from __future__ import annotations

from datetime import timedelta

import pytest


def test_plant_writes_full_record(garden, clock, state):
    """新种的地块：planted/watered 都是今天，stage=0，不蔫。"""
    result = garden.plant("薄荷")
    assert result["ok"] is True
    saved = state()
    assert len(saved["plots"]) == 1
    record = saved["plots"][0]
    assert record == {
        "name": "薄荷",
        "planted": clock.today().isoformat(),
        "watered": clock.today().isoformat(),
        "stage": 0,
        "thirsty": False,
    }


def test_plant_max_six(garden, clock, state):
    """6 块满员后第 7 次返回失败，且不动已存在的地块。"""
    for index in range(6):
        assert garden.plant(f"植物{index}")["ok"] is True
    result = garden.plant("第七棵")
    assert result["ok"] is False
    assert "种不下" in result["line"]
    assert len(state()["plots"]) == 6


def test_plant_after_harvest_has_room_again(garden, clock, plot, seed, state):
    """收掉一块后有空位，能继续种——容量算的是当前地块数。"""
    seed(
        [
            plot(f"旧{index}", clock.today() - timedelta(days=10), clock.today(), stage=4)
            for index in range(6)
        ]
    )
    garden.harvest()
    assert garden.plant("薄荷")["ok"] is True
    assert len(state()["plots"]) == 1


def test_plant_tick_runs_first(garden, clock, plot, seed, state):
    """种之前先 tick：原先渴了的地块会先变蔫，而不是被新种的掩盖掉。"""
    seed([plot("老番茄", clock.today() - timedelta(days=9), clock.today() - timedelta(days=3))])
    garden.plant("薄荷")
    saved = state()
    assert saved["plots"][0]["name"] == "老番茄"
    assert saved["plots"][0]["thirsty"] is True
    assert saved["plots"][1]["name"] == "薄荷"


def test_plant_strips_action_word(garden, clock, state):
    """“种 薄荷”这种带动作词的参数要洗干净，只留植物名。"""
    garden.plant("种 牵牛花")
    assert state()["plots"][0]["name"] == "牵牛花"


def test_plant_empty_name_falls_back_to_seed(garden, clock, state):
    """空名字会从 SEEDS 里挑一个，而不是种出个空字符串。"""
    garden.plant("   ")
    assert state()["plots"][0]["name"] in garden.SEEDS


def test_plant_name_truncated(garden, clock, state):
    """名字最多 12 字，防超长输入撑爆显示。"""
    garden.plant("一二三四五六七八九十甲乙丙丁戊")
    assert len(state()["plots"][0]["name"]) == 12


def test_handle_dispatches_plant(garden, clock, state):
    """handle("种") 走 plant，handle("看看") 走 look。"""
    garden.handle("种", "小葱")
    assert state()["plots"][0]["name"] == "小葱"
    assert garden.handle("看看")["ok"] is True


def test_new_plant_watered_today_is_not_thirsty_next_day(garden, clock, state):
    """种下当天算浇过水；第二天不蔫，第三天蔫。"""
    garden.plant("薄荷")
    clock.advance(1)
    assert garden.look()["line"].count("有点蔫") == 0
    clock.advance(1)
    assert "有点蔫" in garden.look()["line"]
