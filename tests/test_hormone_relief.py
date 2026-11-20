# -*- coding: utf-8 -*-
"""散心机制 + 熬夜惩罚的回归测试（2026-11-14）。

背景：
1. 她老说"闷"，查下来是皮质醇长期偏高 → 身体感觉模块输出"胸口有点闷"。
2. 真正的元凶是一个布尔写反的 bug：
   `if hour >= 1 or hour < 6:` 导致除了 0 点以外的**每个小时**都在加皮质醇。
3. 爸爸要求：刷抖音、跟小玩具玩、院子干活都该算散心，能把她拉回来。

这组测试盯：白天不涨皮质醇、三种散心能降、冷却期防刷、安慰管用。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BRIDGE = Path(__file__).resolve().parents[1] / "chat_bridge"
for candidate in (BRIDGE, BRIDGE.parent):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import HORMONE  # noqa: E402


@pytest.fixture()
def hormones():
    h = HORMONE.HORMONES
    saved = (dict(h.momentum), dict(getattr(h, "_easing_last", {})))
    h.momentum = {name: 0.0 for name in HORMONE.SPEC}
    h._easing_last = {}
    yield h
    h.momentum, h._easing_last = saved


def test_daytime_interaction_does_not_raise_cortisol(hormones) -> None:
    for hour in (9, 12, 15, 20):
        hormones.momentum["cortisol"] = 0.0
        hormones.on_chat(hour=hour)
        assert hormones.momentum["cortisol"] == pytest.approx(0.0, abs=1e-6), f"{hour} 点不该加皮质醇"


def test_late_night_still_costs_cortisol(hormones) -> None:
    hormones.momentum["cortisol"] = 0.0
    hormones.on_chat(hour=2)
    assert hormones.momentum["cortisol"] > 0, "凌晨 2 点应该有熬夜代价"


@pytest.mark.parametrize("kind", ["garden", "douyin", "toy", "walk", "music"])
def test_easing_lowers_cortisol(hormones, kind: str) -> None:
    hormones.momentum["cortisol"] = 0.0
    hormones.on_chat(easing=kind)
    assert hormones.momentum["cortisol"] < 0, f"{kind} 没有降低皮质醇"
    assert hormones.momentum["endorphin"] > 0, f"{kind} 没有提升内啡肽"


def test_easing_cooldown_prevents_spam(hormones) -> None:
    # 2026-11-17：这个测试原来没传 hour，凌晨 1–6 点跑会撞上"熬夜惩罚"，
    # 固定加 0.018 皮质醇，导致必然失败。改成显式白天，结果才稳定。
    hormones.on_chat(easing="garden", hour=15)
    first = hormones.momentum["cortisol"]
    hormones.momentum["cortisol"] = 0.0
    hormones.on_chat(easing="garden", hour=15)
    assert abs(hormones.momentum["cortisol"]) < 1e-6, "30 分钟内重复同一种散心不该再算"
    assert first < 0


def test_comfort_is_stronger_than_before(hormones) -> None:
    hormones.momentum["cortisol"] = 0.0
    hormones.on_chat(comforted=True)
    assert hormones.momentum["cortisol"] <= -0.15, "被安慰应该明显降皮质醇"
