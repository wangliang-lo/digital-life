# -*- coding: utf-8 -*-
"""时间语义：按真实日历天数算，不是按调用次数算。

这里的用例最容易在“把日期换成计数器”这类重构时变红：
反复调用 look() 但不推进时钟，植物一点都不该长。
注意：连跳 5 天以上却不浇水，植物是会被判枯死的——所以“离线生长”的用例
统一用“状态文件里记着中途浇过水”来表达，而不是硬跳过去。
"""
from __future__ import annotations

from datetime import timedelta

import pytest


def test_calls_do_not_age_plants(garden, clock, plot, seed, state):
    """同一天狂调 look() 50 次，阶段和蔫的状态都不能变。"""
    seed([plot("薄荷", clock.today())])
    for _ in range(50):
        garden.look()
    saved = state()
    assert saved["plots"][0]["stage"] == 0
    assert saved["plots"][0]["thirsty"] is False


def test_same_day_repeat_look_is_stable_after_first(garden, clock, plot, seed):
    """进入新阶段的第一眼会播报一次，之后的同日 look 必须完全一样。"""
    seed([plot("番茄", clock.today() - timedelta(days=3), clock.today())])
    garden.look()  # 第一次把 stage 推进，带一句播报
    first = garden.look()["line"]
    second = garden.look()["line"]
    assert first == second
    assert first.count("长叶子了") == 1, "阶段名只在括号里出现一次，播报不该被追加"


def test_growth_follows_calendar_jump(garden, clock, plot, seed, state):
    """状态里记着“两天前浇过水”，今天一次 look 就该按真实 8 天年龄算。"""
    seed([plot("向日葵", clock.today() - timedelta(days=8), clock.today() - timedelta(days=2))])
    garden.look()  # 中间没有任何 tick，只这一次
    assert state()["plots"][0]["stage"] == 4


def test_growth_happens_without_any_intermediate_call(garden, clock, plot, seed, state):
    """种下后 4 天内一次都不调用，下次 look 直接到第 3 天该有的阶段。"""
    planted = clock.today()
    seed([plot("小葱", planted, planted)])
    clock.set(planted + timedelta(days=4))
    assert state()["plots"][0]["stage"] == 0  # 还没被 tick
    garden.look()
    saved = state()
    assert saved["plots"][0]["stage"] == 2
    assert saved["plots"][0]["thirsty"] is True


def test_day_by_day_watering_reaches_ripe_on_schedule(garden, clock, plot, seed, state):
    """每天浇水、每天看一眼：第 1/3/5/8 天依次进入 1/2/3/4 阶段。"""
    planted = clock.today()
    seed([plot("番茄", planted, planted)])
    seen = {}
    for day in range(1, 9):
        clock.set(planted + timedelta(days=day))
        garden.water()
        seen[day] = garden.snapshot()["plots"][0]["stage"]
    assert seen == {1: 1, 2: 1, 3: 2, 4: 2, 5: 3, 6: 3, 7: 3, 8: 4}


def test_skipping_five_days_without_water_kills_despite_clock(garden, clock, plot, seed, state):
    """一次跨 5 天不浇：不是“长了一截”，是直接枯死——时间语义的另一面。"""
    planted = clock.today()
    seed([plot("薄荷", planted, planted)])
    clock.set(planted + timedelta(days=5))
    garden.look()
    saved = state()
    assert saved["plots"] == []
    assert [d["name"] for d in saved["died"]] == ["薄荷"]


def test_watering_uses_today_not_last_tick(garden, clock, plot, seed, state):
    """浇水记的是“今天”，跟上次 look 是哪天无关。"""
    seed([plot("薄荷", clock.today() - timedelta(days=8), clock.today() - timedelta(days=1))])
    clock.advance(3)  # gap=4：蔫着但还活着
    garden.water()
    assert state()["plots"][0]["watered"] == clock.today().isoformat()


def test_future_planted_date_is_treated_as_stage_zero(garden, clock, plot):
    """脏数据（种在未来）不能让阶段算出负数或越界。"""
    p = plot("薄荷", clock.today() + timedelta(days=5))
    stage = garden._stage(p)
    assert stage == 0


def test_corrupt_watered_date_does_not_crash(garden, clock, plot, seed):
    """watered 写成垃圾字符串时 _water_gap 返回 0（当作刚浇过），不能抛异常。"""
    p = plot("薄荷", clock.today() - timedelta(days=3))
    p["watered"] = "不是日期"
    seed([p])
    data = garden._load()
    assert garden._water_gap(data["plots"][0]) == 0
    garden.tick(data)  # 不抛
