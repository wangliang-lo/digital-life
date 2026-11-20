# -*- coding: utf-8 -*-
"""收成：熟透才能收、收完地块清空、harvested +1、重复收不能凭空加计数。"""
from __future__ import annotations

from datetime import timedelta

import pytest


def test_harvest_requires_ripe_stage(garden, clock, plot, seed, state):
    """stage 3（开花了）收不了，返回 ok=False，地块原样留着。"""
    seed([plot("番茄", clock.today() - timedelta(days=5), clock.today(), stage=3)])
    result = garden.harvest()
    assert result["ok"] is False
    saved = state()
    assert len(saved["plots"]) == 1
    assert saved["harvested"] == []


def test_harvest_ripe_clears_plot_and_counts(garden, clock, plot, seed, state):
    """熟透的收下：plots 清空、harvested 恰好 +1、at 是今天。"""
    planted = clock.today() - timedelta(days=8)
    seed([plot("薄荷", planted, clock.today(), stage=4)])
    assert len(state()["harvested"]) == 0

    result = garden.harvest()

    assert result["ok"] is True
    saved = state()
    assert saved["plots"] == []
    assert len(saved["harvested"]) == 1
    assert saved["harvested"][0]["name"] == "薄荷"
    assert saved["harvested"][0]["at"] == clock.today().isoformat()


def test_harvest_twice_does_not_inflate_count(garden, clock, plot, seed, state):
    """第二次收同一块地：ok=False，harvested 还是 1（防重复计数）。"""
    seed([plot("小葱", clock.today() - timedelta(days=8), clock.today(), stage=4)])
    garden.harvest()
    second = garden.harvest()
    assert second["ok"] is False
    assert len(state()["harvested"]) == 1
    assert state()["plots"] == []


def test_harvest_only_picks_ripe_ones(garden, clock, plot, seed, state):
    """一熟一生：只收熟的，生的留在原地，harvested 只 +1。"""
    seed(
        [
            plot("番茄", clock.today() - timedelta(days=9), clock.today(), stage=4),
            plot("小葱", clock.today() - timedelta(days=2), clock.today(), stage=1),
        ]
    )
    garden.harvest()
    saved = state()
    assert [p["name"] for p in saved["plots"]] == ["小葱"]
    assert [h["name"] for h in saved["harvested"]] == ["番茄"]


def test_harvest_many_ripe_counts_each_once(garden, clock, plot, seed, state):
    """三块同时熟：harvested 加 3，而且每块恰好一次。"""
    plots = [
        plot(name, clock.today() - timedelta(days=10), clock.today(), stage=4)
        for name in ("番茄", "薄荷", "向日葵")
    ]
    seed(plots)
    garden.harvest()
    saved = state()
    assert saved["plots"] == []
    assert sorted(h["name"] for h in saved["harvested"]) == sorted(["番茄", "薄荷", "向日葵"])


def test_harvest_accumulates_across_rounds(garden, clock, plot, seed, state):
    """跨轮累计：先收一块，再种再收，harvested == 2 而不是被覆盖成 1。"""
    seed([plot("番茄", clock.today() - timedelta(days=8), clock.today(), stage=4)])
    garden.harvest()
    garden.plant("薄荷")
    clock.advance(4)
    garden.water()  # 中间照看一次，否则 5 天就枯了
    clock.advance(4)
    garden.harvest()
    saved = state()
    assert len(saved["harvested"]) == 2
    assert saved["harvested"][-1]["name"] == "薄荷"


def test_harvest_keeps_immature_plot_stage_in_sync(garden, clock, plot, seed, state):
    """收熟的同时 tick 会把另一块地推进到新阶段（收成不能跳过 tick）。"""
    seed(
        [
            plot("番茄", clock.today() - timedelta(days=8), clock.today(), stage=4),
            plot("小葱", clock.today() - timedelta(days=3), clock.today(), stage=1),
        ]
    )
    garden.harvest()
    saved = state()
    assert saved["plots"][0]["stage"] == 2


def test_harvest_dead_plant_does_not_get_counted(garden, clock, plot, seed, state):
    """该枯的先枯：6 天没浇的“熟透”植物既收不到也不该被当成收成。"""
    seed([plot("多肉", clock.today() - timedelta(days=12), clock.today() - timedelta(days=6), stage=4)])
    result = garden.harvest()
    saved = state()
    assert result["ok"] is False
    assert saved["plots"] == []
    assert saved["harvested"] == []
    assert len(saved["died"]) == 1
