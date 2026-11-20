# -*- coding: utf-8 -*-
"""她画画的关键词表：新形状能画、老形状不退化（2026-11-14）。

背景：以前只能画「圆/线/方块」这类基本笔画，画出来很单调；
现在把太阳/房子/鱼/鸟这些整件的东西接进来了，还加了椭圆/矩形/弧线、粗细和旋转。
这个测试盯三件事：新词能出图、老词不退化、修饰词不炸。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BRIDGE = Path(__file__).resolve().parents[1] / "chat_bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

import doodle  # noqa: E402

OLD_KEYWORDS = ["圆", "圈", "方块", "三角", "星", "心", "线", "波浪", "云", "点", "涂"]
NEW_WHOLE_THINGS = ["太阳", "月亮", "树", "花", "房子", "小人", "猫", "狗", "鱼", "鸟", "雨", "闪电", "彩虹", "蘑菇", "气球", "笑脸", "山", "草地", "天空"]
NEW_PRIMITIVES = ["椭圆", "矩形", "弧线", "海", "沙滩"]


def render(spec: str, out_dir: Path) -> dict:
    return doodle.render(spec, out_dir=out_dir)


@pytest.mark.parametrize("word", OLD_KEYWORDS)
def test_old_keywords_still_work(word: str, tmp_path: Path) -> None:
    result = render(f"红{word} 中间", tmp_path)
    assert result, f"老关键词「{word}」画不出东西了"
    assert Path(result["path"]).exists()


@pytest.mark.parametrize("word", NEW_WHOLE_THINGS)
def test_whole_thing_keywords_render(word: str, tmp_path: Path) -> None:
    result = render(f"黄{word} 中间", tmp_path)
    assert result, f"「{word}」没解析出笔画"
    path = Path(result["path"])
    assert path.exists() and path.stat().st_size > 1024, f"「{word}」画出来的图太小"


@pytest.mark.parametrize("word", NEW_PRIMITIVES)
def test_new_primitive_keywords_render(word: str, tmp_path: Path) -> None:
    result = render(f"蓝{word} 左下", tmp_path)
    assert result and Path(result["path"]).exists()


def test_parse_keeps_shape_and_place() -> None:
    strokes = doodle.parse("红太阳 右上")
    assert strokes, "没解析出笔画"
    first = strokes[0]
    assert first["shape"] == "sun"
    assert first["color"] == "红"
    assert first["place"] == "右上"


def test_scene_composes_multiple_strokes(tmp_path: Path) -> None:
    result = render("黄太阳 右上；蓝鱼 左下；绿草 下", tmp_path)
    assert len(result["strokes"]) >= 3, "一句话里多个形状没被拆开"
    assert "太阳" in result["caption"] and "鱼" in result["caption"]


@pytest.mark.parametrize("spec", ["粗红线 中间", "细红线 中间", "红方块 中间 转30度"])
def test_modifiers_do_not_crash(spec: str, tmp_path: Path) -> None:
    result = render(spec, tmp_path)
    assert result and Path(result["path"]).exists(), f"修饰词「{spec}」把渲染弄挂了"


def test_render_writes_only_inside_out_dir(tmp_path: Path) -> None:
    before = set((BRIDGE / "data" / "drawn").glob("*.png"))
    render("绿草 下", tmp_path)
    after = set((BRIDGE / "data" / "drawn").glob("*.png"))
    assert before == after, "render(out_dir=...) 仍然往 data/drawn 里写了文件"
