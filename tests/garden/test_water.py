# -*- coding: utf-8 -*-
"""浇水/蔫/枯死（WATER_NEED_DAYS = 2，5 天以上枯死）。

核心不变式：
- gap == 1 还精神，gap >= 2 蔫；
- 浇水当天立即解除蔫，并且可以跨很多天再浇；
- gap >= 5 且仍然没浇 => 移出 plots，进 died。
"""
from __future__ import annotations

from datetime import timedelta

import pytest


def _gap(planted, watered, today):
    return (today - (watered or planted)).days


@pytest.mark.parametrize(
    ("last_watered_days_ago", "expect_thirsty"),
    [
        (0, False),
        (1, False),
        (2, True),
        (3, True),
        (4, True),
    ],
)
def test_thirsty_threshold(garden, clock, plot, seed, last_watered_days_ago, expect_thirsty):
    """两天不浇就蔫：1 天不蔫、2 天必蔫。把 2 改成 3 这条立刻红。"""
    planted = clock.today() - timedelta(days=10)
    watered = clock.today() - timedelta(days=last_watered_days_ago)
    p = plot("薄荷", planted, watered)
    seed([p])
    data = garden._load()
    garden.tick(data)
    assert data["plots"][0]["thirsty"] is expect_thirsty
    assert _gap(planted, watered, clock.today()) == last_watered_days_ago


def test_thirsty_uses_watered_not_planted(garden, clock, plot, seed):
    """上周种下、昨天浇过水 => 不蔫。谁把 last 取成 planted 就会红。"""
    planted = clock.today() - timedelta(days=9)
    seed([plot("番茄", planted, clock.today() - timedelta(days=1))])
    data = garden._load()
    garden.tick(data)
    assert data["plots"][0]["thirsty"] is False


def test_water_clears_thirst_and_rewrites_watered(garden, clock, plot, seed, state):
    """浇一次水：thirsty 立刻为 False，watered 记成今天，落盘。"""
    planted = clock.today() - timedelta(days=6)
    seed([plot("小葱", planted, clock.today() - timedelta(days=3), stage=3, thirsty=True)])
    result = garden.water()
    assert result["ok"] is True
    saved = state()
    assert saved["plots"][0]["watered"] == clock.today().isoformat()
    assert saved["plots"][0]["thirsty"] is False


def test_water_then_two_days_later_thirsty_again(garden, clock, plot, seed, state):
    """浇水不是一劳永逸：浇完过两天，又得蔫。"""
    seed([plot("薄荷", clock.today())])
    garden.water()
    clock.advance(2)
    data = garden._load()
    garden.tick(data)
    assert data["plots"][0]["thirsty"] is True


def test_death_at_five_days(garden, clock, plot, seed, state):
    """5 天没浇 => 枯死，从 plots 移除，写进 died，并播报一句。"""
    planted = clock.today() - timedelta(days=12)
    seed([plot("向日葵", planted, clock.today() - timedelta(days=5), stage=4)])
    data = garden._load()
    notes = garden.tick(data)
    assert data["plots"] == []
    assert [d["name"] for d in data["died"]] == ["向日葵"]
    assert data["died"][0]["at"] == clock.today().isoformat()
    assert any("枯了" in n for n in notes), notes


@pytest.mark.parametrize("days_without_water", [2, 3, 4])
def test_survives_between_two_and_four(garden, clock, plot, seed, days_without_water):
    """2~4 天：蔫但不死。边界差一天就红。"""
    planted = clock.today() - timedelta(days=15)
    seed([plot("番茄", planted, clock.today() - timedelta(days=days_without_water), stage=4)])
    data = garden._load()
    garden.tick(data)
    assert len(data["plots"]) == 1
    assert data["plots"][0]["thirsty"] is True
    assert data["died"] == []


def test_water_on_dried_plot_returns_empty_and_look_records_death(garden, clock, plot, seed, state):
    """去浇水时才发现已经旱了 6 天：水浇不到，tick 在内存里判死；
    真正把死亡落盘的是紧接着的 look()。之后再浇也不会重复记死亡。"""
    planted = clock.today() - timedelta(days=20)
    seed([plot("多肉", planted, clock.today() - timedelta(days=6))])
    result = garden.water()
    assert result["ok"] is False          # 死光了，等于空院子

    garden.look()
    saved = state()
    assert saved["plots"] == []
    assert [d["name"] for d in saved["died"]] == ["多肉"]

    garden.water()                        # 再浇一次也不该重复记死亡
    assert len(state()["died"]) == 1


@pytest.mark.xfail(
    strict=False,
    reason="已知缺陷：water()/plant() 在提前 return 时丢弃了 tick() 的结果，不落盘",
)
def test_water_alone_persists_death(garden, clock, plot, seed, state):
    """期望：只浇一次水就该把枯死写进 died。现状不落盘，故标 xfail。"""
    planted = clock.today() - timedelta(days=20)
    seed([plot("多肉", planted, clock.today() - timedelta(days=6))])
    garden.water()
    assert state()["plots"] == []


def test_died_history_is_never_resurrected(garden, clock, seed, state):
    """died 里的历史只是账本，不会被任何调用写成 plots 里的活植物。"""
    seed([], died=[{"name": "野草", "at": clock.today().isoformat()}])
    garden.water()
    garden.look()
    saved = state()
    assert saved["plots"] == []
    assert len(saved["died"]) == 1


def test_water_with_empty_garden_does_not_save(garden, state):
    """空院子浇水：返回失败提示，且不创建状态文件。"""
    result = garden.water()
    assert result["ok"] is False
    assert not garden.STATE.exists()
