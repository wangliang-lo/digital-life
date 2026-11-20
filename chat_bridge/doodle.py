#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""她自己的涂鸦板：按她写的指令画一张，不是"描述→AI 生图"。

2026-11-08 爸爸："给小鱼娱乐里面加个画画，让她自己涂鸦。"

这里的取舍：`[DRAW: 描述]` 那套是 SD-Turbo，出图漂亮但慢（几秒到几十秒），
而且"她"只是在写提示词。涂鸦要的是**她自己的手**——她指定形状、颜色、位置，
程序照着画，0.1 秒出图，写错了也不会失败（认不出的词直接跳过）。

写法（`；` `;` `、` 分开每一笔）：

    [DOODLE: 黄圆 中间；红线 左下到右上；蓝波浪 下面；粉心 右上 小]

每一笔可以带：
    形状  圆 圈 方块 三角 星 心 线 波浪 云 点 涂
          椭圆 矩形 弧
          太阳 房子 小人 树 花 猫 笑脸 山 天空 草 狗 鱼 鸟 雨 闪电 月亮 彩虹 蘑菇 气球
    颜色  红 橙 黄 绿 青 蓝 紫 粉 棕 金 黑 白 灰
    位置  左上 中上 右上 左 中 右 左下 中下 右下 上 下
    大小  大 中 小（不写按形状默认）
    线宽  粗 细（不写是中等的）
    旋转  转30度（只对方块/矩形/三角/星/心/椭圆/线有意义）

只给颜色（"红"）就画一条红线；只给形状（"圆"）就用默认颜色。
"""
from __future__ import annotations

import contextlib
import math
import re
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

CANVAS = 512
OUT_DIR = Path(__file__).resolve().parent / "data" / "drawn"

COLORS = {
    "红": (208, 62, 62), "橙": (226, 134, 45), "黄": (232, 190, 60),
    "绿": (86, 148, 96), "青": (74, 156, 156), "蓝": (72, 116, 184),
    "紫": (128, 92, 168), "粉": (222, 128, 160), "棕": (132, 96, 66),
    "金": (196, 158, 66), "黑": (52, 50, 48), "白": (250, 250, 248),
    "灰": (128, 128, 126),
}
DEFAULT_COLOR = (70, 68, 66)

SHAPES = {
    # 基本笔画
    "圆": "circle", "圆形": "circle", "圈": "ring", "圈圈": "ring",
    "方块": "square", "方": "square", "方形": "square",
    "三角": "triangle", "三角形": "triangle",
    "星": "star", "星星": "star", "心": "heart", "心形": "heart",
    "线": "line", "直线": "line", "波浪": "wave", "波浪线": "wave",
    "云": "cloud", "点": "dots", "点点": "dots", "涂": "wash",
    "海": "sea", "大海": "sea", "海水": "sea", "河": "sea", "湖": "sea", "池塘": "sea",
    "沙滩": "beach", "海滩": "beach", "沙": "beach",
    "椭圆": "oval", "椭圆形": "oval",
    "矩形": "rect", "长方形": "rect", "长条": "rect",
    "弧": "arc", "弧线": "arc", "半圆": "arc",
    # 2026-11-14：把本来写好了却没人调的画法接进来（原来是死代码）。
    # 关键词照顾口语说法，长词写前面只是给 _pick 的同位置时长词优先用。
    "太阳": "sun", "日头": "sun",
    "房子": "house", "屋子": "house", "小屋": "house",
    "小人": "stickman", "人": "stickman", "人儿": "stickman",
    "树": "tree", "大树": "tree", "小树": "tree",
    "花": "flower", "花朵": "flower", "小花": "flower",
    "猫": "cat", "小猫": "cat", "猫咪": "cat",
    "笑脸": "smile", "笑": "smile", "笑一笑": "smile",
    "山": "mountain", "远山": "mountain", "小山": "mountain", "高山": "mountain",
    "天空": "sky", "天": "sky",
    "草": "grass", "草地": "grass", "小草": "grass", "青草": "grass",
    "狗": "dog", "小狗": "dog", "狗狗": "dog",
    "鱼": "fish", "小鱼": "fish", "鱼儿": "fish",
    "鸟": "bird", "小鸟": "bird", "飞鸟": "bird",
    "雨": "rain", "下雨": "rain", "雨点": "rain", "小雨": "rain",
    "闪电": "lightning", "雷": "lightning", "打雷": "lightning", "雷电": "lightning",
    "月亮": "moon", "月牙": "moon", "月": "moon", "弯月": "moon", "月儿": "moon",
    "彩虹": "rainbow", "虹": "rainbow",
    "蘑菇": "mushroom", "小蘑菇": "mushroom",
    "气球": "balloon", "汽球": "balloon",
}

# 能转的：方块/矩形/三角/星/心/椭圆/线（描出来的线才转得动）
ROTATABLE = {"square", "rect", "triangle", "star", "heart", "oval", "line", "polyline"}

# 位置 → 画面中心（0~1）
PLACES = {
    "左上": (0.24, 0.24), "中上": (0.5, 0.22), "右上": (0.76, 0.24),
    "左": (0.24, 0.5), "中": (0.5, 0.5), "中间": (0.5, 0.5), "右": (0.76, 0.5),
    "左下": (0.24, 0.76), "中下": (0.5, 0.78), "右下": (0.76, 0.76),
    "上": (0.5, 0.26), "下": (0.5, 0.74),
}


def _pick(text: str, table: dict[str, str]) -> str:
    """把文字映射成内部值：取**最早出现**的（同位置时长词优先）。

    以前按"长词优先"整体搜索，于是"左下到右上"会先命中"右上"，
    一条从左下划到右上的线被画到右上角。改成看出现次序。
    """
    best: tuple[int, int, str] | None = None
    for word in table:
        index = text.find(word)
        if index < 0:
            continue
        key = (index, -len(word))
        if best is None or key < best[:2]:
            best = (index, -len(word), table[word])
    return best[2] if best else ""


def parse(spec: str) -> list[dict]:
    """把她的写法拆成一组笔画。认不出的整笔跳过，绝不报错。"""
    strokes: list[dict] = []
    for chunk in [part for part in spec.replace("，", "、").replace("；", "、").replace(";", "、").split("、") if part.strip()]:
        text = chunk.strip()
        shape = _pick(text, SHAPES)
        color = _pick(text, {name: name for name in COLORS})
        place = _pick(text, {name: name for name in PLACES})
        # 中 只在不是位置词（中间/中上/中下）的时候才算"中等大小"
        bare = text
        for word in PLACES:
            if word.startswith("中"):
                bare = bare.replace(word, " " * len(word))
        size = ("大" if "大" in text else "小" if "小" in text
                else "中" if "中" in bare else "")
        # 线宽：粗 / 细（不写就是中等）
        line_width = "粗" if "粗" in text else ("细" if "细" in text else "")
        # 旋转：转30度 / 转 30 度 / 转-15 度。写错了就当作没写，不报错。
        angle = 0
        turned = re.search(r"转\s*(-?\d{1,3})\s*度?", text)
        if turned:
            try:
                angle = max(-180, min(180, int(turned.group(1))))
            except (TypeError, ValueError):
                angle = 0
        # 歪 / 抖 / 扭 → 抖得厉害；直 → 尽量画直
        wobble = 1.0
        if any(word in text for word in ("歪", "抖", "扭", "乱")):
            wobble = 2.6
        elif any(word in text for word in ("直", "工整")):
            wobble = 0.35

        # 自由坐标：线 20,80 50,40 / 点 30,70 / 路径 10,80 30,70 50,60
        # （先把"转30度"抹掉，免得数字被当成坐标）
        coord_text = re.sub(r"转\s*-?\d{1,3}\s*度?", " ", text)
        coords = re.findall(r"(\d{1,3})\s*[,.，、]\s*(\d{1,3})", coord_text)
        points = [(min(100, max(0, int(cx))) / 100.0, min(100, max(0, int(cy))) / 100.0)
                  for cx, cy in coords]
        if points:
            if not shape:
                shape = "polyline" if len(points) >= 2 else "dot"
            strokes.append({"shape": shape, "color": color, "place": "",
                            "size": size, "points": points, "wobble": wobble,
                            "line_width": line_width, "angle": angle, "raw": text})
            continue

        if not shape and not color:
            continue
        if not shape:
            shape = "line"          # 只写颜色 → 画一条线
        if not color:
            color = ""
        strokes.append({"shape": shape, "color": color, "place": place,
                        "size": size, "wobble": wobble,
                        "line_width": line_width, "angle": angle, "raw": text})
    return strokes


def _draw_sun(draw, cx, cy, radius, color):
    """太阳：圆 + 八条射线（小孩画的那种）。"""
    draw.ellipse([cx - radius * 0.62, cy - radius * 0.62, cx + radius * 0.62, cy + radius * 0.62],
                 fill=tuple(min(255, value + 60) for value in color), outline=color, width=3)
    for index in range(8):
        angle = index / 8 * 2 * math.pi
        inner = radius * 0.78
        outer = radius * 1.18
        _hand_line(draw, [(cx + math.cos(angle) * inner, cy + math.sin(angle) * inner),
                          (cx + math.cos(angle) * outer, cy + math.sin(angle) * outer)],
                   color, width=4)


def _draw_house(draw, cx, cy, radius, color):
    """方房子：方墙 + 三角屋顶 + 一扇门。"""
    wall = radius * 0.8
    body = [cx - wall, cy - wall * 0.15, cx + wall, cy + wall]
    draw.rectangle(body, fill=(240, 232, 214), outline=color, width=5)
    _hand_line(draw, [(cx - wall * 1.18, cy - wall * 0.1), (cx, cy - wall * 0.95),
                      (cx + wall * 1.18, cy - wall * 0.1), (cx - wall * 1.18, cy - wall * 0.1)],
               (176, 72, 60), width=5)
    door_w, door_h = wall * 0.3, wall * 0.55
    draw.rectangle([cx - door_w, cy + wall - door_h, cx + door_w, cy + wall],
                   fill=(150, 112, 78), outline=color, width=3)


def _draw_stickman(draw, cx, cy, radius, color):
    """线条小人：一个圆头、一根身子、两只手、两条腿。"""
    head = radius * 0.36
    draw.ellipse([cx - head, cy - radius * 0.92, cx + head, cy - radius * 0.92 + head * 2],
                 outline=color, width=4)
    top = cy - radius * 0.2
    bottom = cy + radius * 0.62
    _hand_line(draw, [(cx, top), (cx, bottom)], color, width=4)
    _hand_line(draw, [(cx, cy + radius * 0.05), (cx - radius * 0.5, cy - radius * 0.28)],
               color, width=4)
    _hand_line(draw, [(cx, cy + radius * 0.05), (cx + radius * 0.5, cy - radius * 0.28)],
               color, width=4)
    _hand_line(draw, [(cx, bottom), (cx - radius * 0.42, cy + radius * 1.02)], color, width=4)
    _hand_line(draw, [(cx, bottom), (cx + radius * 0.42, cy + radius * 1.02)], color, width=4)


def _draw_tree(draw, cx, cy, radius, color):
    """树：一根树干 + 一团树冠。"""
    _hand_line(draw, [(cx, cy + radius), (cx, cy - radius * 0.1)], (132, 96, 66), width=7)
    draw.ellipse([cx - radius * 0.7, cy - radius, cx + radius * 0.7, cy + radius * 0.3],
                 fill=tuple(min(255, value + 45) for value in color), outline=color, width=4)


def _draw_flower(draw, cx, cy, radius, color):
    """花：一条茎 + 几片花瓣 + 花心。"""
    _hand_line(draw, [(cx, cy + radius), (cx, cy - radius * 0.1)], (86, 148, 96), width=4)
    for index in range(5):
        angle = index / 5 * 2 * math.pi
        px = cx + math.cos(angle) * radius * 0.4
        py = cy - radius * 0.35 + math.sin(angle) * radius * 0.4
        draw.ellipse([px - radius * 0.26, py - radius * 0.26, px + radius * 0.26, py + radius * 0.26],
                     fill=tuple(min(255, value + 55) for value in color), outline=color, width=2)
    draw.ellipse([cx - radius * 0.16, cy - radius * 0.51, cx + radius * 0.16, cy - radius * 0.19],
                 fill=(238, 206, 96))


def _draw_cat(draw, cx, cy, radius, color):
    """猫：圆脸 + 两只三角耳 + 眼睛 + 胡须（模仿表情包那种）。"""
    face = radius * 0.72
    draw.ellipse([cx - face, cy - face, cx + face, cy + face],
                 fill=(232, 224, 208), outline=color, width=4)
    for sign in (-1, 1):
        _hand_line(draw, [(cx + sign * face * 0.55, cy - face * 0.72),
                          (cx + sign * face * 0.82, cy - face * 1.15),
                          (cx + sign * face * 0.95, cy - face * 0.55)], color, width=4)
    for sign in (-1, 1):
        draw.ellipse([cx + sign * face * 0.32 - 4, cy - face * 0.18 - 5,
                      cx + sign * face * 0.32 + 4, cy - face * 0.18 + 5], fill=color)
    for sign in (-1, 1):
        _hand_line(draw, [(cx + sign * face * 0.2, cy + face * 0.34),
                          (cx + sign * face * 0.75, cy + face * 0.18)], color, width=2)
        _hand_line(draw, [(cx + sign * face * 0.2, cy + face * 0.46),
                          (cx + sign * face * 0.78, cy + face * 0.5)], color, width=2)


def _draw_smile(draw, cx, cy, radius, color):
    """笑脸：黄圆 + 两点眼睛 + 一条嘴。"""
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                 fill=(246, 214, 96), outline=color, width=4)
    for sign in (-1, 1):
        draw.ellipse([cx + sign * radius * 0.36 - 5, cy - radius * 0.3 - 7,
                      cx + sign * radius * 0.36 + 5, cy - radius * 0.3 + 7], fill=color)
    draw.arc([cx - radius * 0.55, cy - radius * 0.1, cx + radius * 0.55, cy + radius * 0.7],
             start=10, end=170, fill=color, width=4)


def _draw_mountain(draw, cx, cy, radius, color):
    """山：两个三角。"""
    _hand_line(draw, [(cx - radius * 1.1, cy + radius * 0.8), (cx - radius * 0.2, cy - radius * 0.6),
                      (cx + radius * 0.7, cy + radius * 0.8), (cx - radius * 1.1, cy + radius * 0.8)],
               color, width=5)
    _hand_line(draw, [(cx - radius * 0.1, cy + radius * 0.8), (cx + radius * 0.6, cy - radius * 0.2),
                      (cx + radius * 1.2, cy + radius * 0.8)], color, width=4)


def _draw_sky(draw, cx, cy, radius, color):
    """天空：一整条淡蓝。"""
    draw.rectangle([0, 0, CANVAS, CANVAS * 0.55],
                   fill=(170, 205, 238))


def _draw_sea(draw, cx, cy, radius, color):
    """海：一片蓝，上面几道白的浪。"""
    top = CANVAS * 0.55
    draw.rectangle([0, top, CANVAS, CANVAS], fill=(176, 212, 232))
    for index in range(4):
        y = top + 28 + index * 30
        points = []
        for step in range(9):
            x = 10 + step * (CANVAS - 20) / 8
            points.append((x, y + (8 if step % 2 else -6) + random.uniform(-2, 2)))
        _hand_line(draw, points, (250, 252, 250), width=3)
    for index in range(6):
        x = 40 + index * 80
        _hand_line(draw, [(x, CANVAS * 0.9), (x + 26, CANVAS * 0.9)],
                   (232, 244, 250), width=3)


def _draw_beach(draw, cx, cy, radius, color):
    """沙滩：把海遮住一条，铺上细沙的点。"""
    draw.rectangle([0, CANVAS * 0.72, CANVAS, CANVAS], fill=(240, 224, 190))
    for _ in range(40):
        x = random.uniform(6, CANVAS - 6)
        y = random.uniform(CANVAS * 0.76, CANVAS - 6)
        draw.ellipse([x, y, x + 3, y + 3], fill=(214, 194, 158))


def _draw_grass(draw, cx, cy, radius, color):
    """草地：底下一条绿，加几根草。"""
    draw.rectangle([0, CANVAS * 0.72, CANVAS, CANVAS], fill=(150, 196, 150))
    for index in range(12):
        x = 20 + index * (CANVAS - 40) / 11
        _hand_line(draw, [(x, CANVAS * 0.9), (x + random.uniform(-6, 6), CANVAS * 0.78)],
                   (86, 148, 96), width=2)


def _draw_dog(draw, cx, cy, radius, color):
    """狗：圆脸 + 两只垂耳 + 鼻子 + 眼睛。"""
    face = radius * 0.68
    draw.ellipse([cx - face, cy - face, cx + face, cy + face], fill=(226, 206, 180), outline=color, width=4)
    for sign in (-1, 1):
        draw.ellipse([cx + sign * face * 0.9 - face * 0.3, cy - face * 0.8,
                      cx + sign * face * 0.9 + face * 0.3, cy + face * 0.25],
                     fill=(176, 140, 110), outline=color, width=3)
    draw.ellipse([cx - 9, cy + 2, cx + 9, cy + 18], fill=color)
    for sign in (-1, 1):
        draw.ellipse([cx + sign * face * 0.34 - 4, cy - face * 0.3, cx + sign * face * 0.34 + 4, cy - face * 0.14],
                     fill=color)


def _draw_fish(draw, cx, cy, radius, color):
    """鱼：椭圆身 + 三角尾 + 一只眼。"""
    draw.ellipse([cx - radius * 0.8, cy - radius * 0.4, cx + radius * 0.6, cy + radius * 0.4],
                 fill=tuple(min(255, v + 55) for v in color), outline=color, width=3)
    _hand_line(draw, [(cx + radius * 0.55, cy), (cx + radius * 1.1, cy - radius * 0.35),
                      (cx + radius * 1.1, cy + radius * 0.35), (cx + radius * 0.55, cy)], color, width=3)
    draw.ellipse([cx - radius * 0.5, cy - radius * 0.12, cx - radius * 0.34, cy + radius * 0.04], fill=color)


def _draw_bird(draw, cx, cy, radius, color):
    """鸟：一个 V 字形（小孩画飞鸟就两笔）。"""
    _hand_line(draw, [(cx - radius * 0.9, cy), (cx - radius * 0.3, cy - radius * 0.45),
                      (cx, cy), (cx + radius * 0.3, cy - radius * 0.45),
                      (cx + radius * 0.9, cy)], color, width=4)


def _draw_rain(draw, cx, cy, radius, color):
    """雨：几条斜线。"""
    for index in range(6):
        x = cx - radius + index * radius * 0.4
        _hand_line(draw, [(x, cy - radius * 0.5), (x - radius * 0.2, cy + radius * 0.5)], color, width=3)


def _draw_lightning(draw, cx, cy, radius, color):
    """闪电：一个折线。"""
    _hand_line(draw, [(cx - radius * 0.2, cy - radius), (cx + radius * 0.25, cy - radius * 0.1),
                      (cx - radius * 0.05, cy), (cx + radius * 0.3, cy + radius)],
               color, width=5)


def _draw_moon(draw, cx, cy, radius, color):
    """月亮：两个圆叠出月牙。"""
    draw.ellipse([cx - radius * 0.7, cy - radius * 0.7, cx + radius * 0.7, cy + radius * 0.7],
                 fill=(246, 232, 160))
    draw.ellipse([cx - radius * 0.25, cy - radius * 0.85, cx + radius * 1.15, cy + radius * 0.55],
                 fill=(250, 250, 248))


def _draw_rainbow(draw, cx, cy, radius, color):
    """彩虹：三道弧。"""
    colors = [(224, 96, 96), (232, 190, 60), (86, 148, 96)]
    for index, tone in enumerate(colors):
        box = [cx - radius * (1.0 - index * 0.16), cy - radius * (0.6 - index * 0.1),
               cx + radius * (1.0 - index * 0.16), cy + radius * (1.2 - index * 0.1)]
        draw.arc(box, start=185, end=355, fill=tone, width=7)


def _draw_mushroom(draw, cx, cy, radius, color):
    """蘑菇：半圆盖 + 白点 + 一根腿。"""
    draw.pieslice([cx - radius, cy - radius, cx + radius, cy + radius * 0.7],
                  start=180, end=360, fill=(220, 96, 92), outline=color, width=3)
    for dx, dy in ((-radius * 0.4, -radius * 0.2), (radius * 0.25, -radius * 0.35)):
        draw.ellipse([cx + dx - 8, cy + dy - 8, cx + dx + 8, cy + dy + 8], fill=(250, 246, 238))
    draw.rectangle([cx - radius * 0.28, cy + 6, cx + radius * 0.28, cy + radius * 0.75],
                   fill=(240, 232, 214), outline=color, width=3)


def _draw_balloon(draw, cx, cy, radius, color):
    """气球：一个蛋形 + 一根线。"""
    draw.ellipse([cx - radius * 0.55, cy - radius, cx + radius * 0.55, cy + radius * 0.2],
                 fill=tuple(min(255, v + 45) for v in color), outline=color, width=3)
    _hand_line(draw, [(cx, cy + radius * 0.2), (cx + random.uniform(-8, 8), cy + radius * 1.1)],
               color, width=2)


# 背景先画，前景后画：不然草地/天空会把后画的小鱼盖住（2026-11-14 实测）
BACKGROUND_SHAPES = {"grass", "sky", "wash", "mountain", "sea", "beach"}

COMPOSITES = {
    "sea": _draw_sea,
    "beach": _draw_beach,
    "sun": _draw_sun,
    "house": _draw_house,
    "stickman": _draw_stickman,
    "tree": _draw_tree,
    "flower": _draw_flower,
    "cat": _draw_cat,
    "smile": _draw_smile,
    "mountain": _draw_mountain,
    "sky": _draw_sky,
    "grass": _draw_grass,
    "dog": _draw_dog,
    "fish": _draw_fish,
    "bird": _draw_bird,
    "rain": _draw_rain,
    "lightning": _draw_lightning,
    "moon": _draw_moon,
    "rainbow": _draw_rainbow,
    "mushroom": _draw_mushroom,
    "balloon": _draw_balloon,
}


def _paper() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    """米白纸底，带一点颗粒，不至于像纯电脑色块。"""
    base = Image.new("RGB", (CANVAS, CANVAS), (247, 243, 234))
    noise = Image.effect_noise((CANVAS, CANVAS), 12).convert("L")
    base = Image.composite(base, Image.new("RGB", (CANVAS, CANVAS), (238, 234, 224)), noise)
    return base, ImageDraw.Draw(base)


# 当前这一笔的线宽倍数（"粗"/"细"）。整张图是一笔一笔画的，只在画一笔时改它。
_WIDTH_SCALE = 1.0


@contextlib.contextmanager
def _line_scale(value: float):
    """临时把线宽放大/缩小——组合画法里的 _hand_line 也跟着变粗变细。"""
    global _WIDTH_SCALE
    previous = _WIDTH_SCALE
    _WIDTH_SCALE = float(value)
    try:
        yield
    finally:
        _WIDTH_SCALE = previous


def _width_scale(stroke: dict) -> float:
    return {"粗": 1.8, "细": 0.6}.get(str(stroke.get("line_width") or ""), 1.0)


def _w(stroke: dict, default: int = 4) -> int:
    return max(2, int(round(default * _width_scale(stroke))))


def _rotate_points(points, cx, cy, angle_deg):
    """绕 (cx, cy) 转 angle_deg 度（正数顺时针——屏幕上一眼看过去的方向）。"""
    if not angle_deg:
        return points
    rad = math.radians(angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    turned = []
    for x, y in points:
        dx, dy = x - cx, y - cy
        turned.append((cx + dx * cos_a - dy * sin_a, cy + dx * sin_a + dy * cos_a))
    return turned


def _hand_line(draw, points, color, width=4, passes=3, wobble=1.0):
    """一条"手抖"的线：同一笔重复几次、每次偏一点。

    wobble=1 是平常的抖；2.5 就是小孩那种歪歪扭扭；0.3 是尽量画直。
    """
    amp = 1.6 * max(0.2, float(wobble))
    line_width = max(2, int(round(width * _WIDTH_SCALE)))
    for _ in range(passes):
        jittered = [(x + random.uniform(-amp, amp), y + random.uniform(-amp, amp)) for x, y in points]
        draw.line(jittered, fill=color,
                  width=max(2, line_width + random.randint(-1, 1)), joint="curve")


def _star(cx, cy, r):
    points = []
    for index in range(10):
        radius = r if index % 2 == 0 else r * 0.45
        angle = -math.pi / 2 + index * math.pi / 5
        points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return points


SHAPE_LABELS = {
    "circle": "圆", "ring": "圈", "square": "方块", "triangle": "三角", "star": "星",
    "heart": "心", "wave": "波浪", "cloud": "云", "dots": "点", "wash": "涂", "line": "线",
    "oval": "椭圆", "rect": "矩形", "arc": "弧线",
    "sun": "太阳", "house": "房子", "stickman": "小人", "tree": "树", "flower": "花",
    "cat": "猫", "smile": "笑脸", "mountain": "山", "sky": "天空", "grass": "草地",
    "dog": "狗", "fish": "鱼", "bird": "鸟", "rain": "雨", "lightning": "闪电",
    "moon": "月亮", "rainbow": "彩虹", "mushroom": "蘑菇", "balloon": "气球",
    "sea": "海", "beach": "沙滩",
    "polyline": "画", "dot": "点",
}


def describe_stroke(stroke: dict) -> str:
    """一句话描述一笔，例如"红圆（左边）"。"""
    shape = SHAPE_LABELS.get(stroke.get("shape", ""), "笔")
    color = stroke.get("color") or ""
    place = stroke.get("place") or ""
    label = color + shape
    return f"{label}（{place}）" if place else label


def _paint_points(draw, stroke: dict, color) -> None:
    """自由坐标（她自己写的百分比点）。"""
    wobble = float(stroke.get("wobble", 1.0) or 1.0)
    absolute = [(px * CANVAS, py * CANVAS) for px, py in stroke.get("points") or []]
    if not absolute:
        return
    shape = stroke["shape"]
    if shape in ("polyline", "line"):
        _hand_line(draw, absolute, color, width=max(3, int(4 * wobble)), wobble=wobble)
    elif shape in ("dot", "点"):
        px, py = absolute[0]
        r = 6 if stroke.get("size") != "大" else 10
        draw.ellipse([px - r, py - r, px + r, py + r], fill=color)
    elif shape in ("circle", "圆", "圈"):
        radius = 40 if stroke.get("size") != "大" else 70
        _hand_line(draw, [(absolute[0][0] + math.cos(a / 24 * 2 * math.pi) * radius,
                           absolute[0][1] + math.sin(a / 24 * 2 * math.pi) * radius)
                          for a in range(25)], color, width=4, wobble=wobble)
    else:
        _hand_line(draw, absolute, color, width=4, wobble=wobble)


def _paint_stroke(draw, stroke: dict) -> None:
    """画一笔。render() 和 paint_strokes() 共用，别让两边走样。"""
    color = COLORS.get(stroke.get("color", ""), DEFAULT_COLOR)
    wobble = float(stroke.get("wobble", 1.0) or 1.0)
    angle = 0
    if str(stroke.get("shape")) in ROTATABLE:
        try:
            angle = int(stroke.get("angle") or 0)
        except (TypeError, ValueError):
            angle = 0

    if stroke.get("points"):
        _paint_points(draw, stroke, color)
        return

    size = stroke.get("size") or ""
    scale = 0.72 if size == "小" else (1.0 if size == "大" else 0.86)
    cx, cy = PLACES.get(stroke.get("place", ""), (0.5, 0.5))
    cx, cy = cx * CANVAS, cy * CANVAS
    radius = 92 * scale
    shape = stroke.get("shape", "line")

    with _line_scale(_width_scale(stroke)):
        if shape in COMPOSITES:
            COMPOSITES[shape](draw, cx, cy, radius, color)
        elif shape == "wash":
            draw.rectangle([cx - 150 * scale, cy - 150 * scale, cx + 150 * scale, cy + 150 * scale],
                           fill=tuple(min(255, value + 60) for value in color))
        elif shape in ("circle", "ring"):
            if shape == "circle":
                draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                             fill=tuple(min(255, value + 70) for value in color))
            for _ in range(2):
                box = [cx - radius + random.uniform(-3, 3), cy - radius + random.uniform(-3, 3),
                       cx + radius + random.uniform(-3, 3), cy + radius + random.uniform(-3, 3)]
                draw.arc(box, start=random.randint(-20, 20), end=random.randint(340, 380),
                         fill=color, width=_w(stroke, 4))
        elif shape == "square":
            corners = [(cx - radius, cy - radius), (cx + radius, cy - radius),
                       (cx + radius, cy + radius), (cx - radius, cy + radius)]
            corners = _rotate_points(corners + [corners[0]], cx, cy, angle)
            draw.polygon(corners[:-1], fill=tuple(min(255, value + 62) for value in color))
            _hand_line(draw, corners, color, width=4, wobble=wobble)
        elif shape == "rect":
            # 矩形：比方块扁一点，还是一条抖着描出来的边，中间留白
            half_w, half_h = radius * 1.15, radius * 0.7
            corners = [(cx - half_w, cy - half_h), (cx + half_w, cy - half_h),
                       (cx + half_w, cy + half_h), (cx - half_w, cy + half_h)]
            corners = _rotate_points(corners + [corners[0]], cx, cy, angle)
            _hand_line(draw, corners, color, width=4, wobble=wobble)
        elif shape == "oval":
            points = [(cx + math.cos(step / 40 * 2 * math.pi) * radius,
                       cy + math.sin(step / 40 * 2 * math.pi) * radius * 0.62)
                      for step in range(41)]
            _hand_line(draw, _rotate_points(points, cx, cy, angle), color, width=4, wobble=wobble)
        elif shape == "arc":
            # 弧线：半圆那种，像随手描的一笔
            points = [(cx + math.cos(math.pi + step / 32 * math.pi) * radius,
                       cy + math.sin(math.pi + step / 32 * math.pi) * radius * 0.85)
                      for step in range(33)]
            _hand_line(draw, points, color, width=4, wobble=wobble)
        elif shape == "triangle":
            corners = [(cx, cy - radius), (cx + radius, cy + radius), (cx - radius, cy + radius),
                       (cx, cy - radius)]
            _hand_line(draw, _rotate_points(corners, cx, cy, angle), color, width=4, wobble=wobble)
        elif shape == "star":
            star = _star(cx, cy, radius)
            _hand_line(draw, _rotate_points(star + [star[0]], cx, cy, angle), color, width=4, wobble=wobble)
        elif shape == "heart":
            points = []
            for step in range(48):
                turn = step / 48 * 2 * math.pi
                x = 16 * math.sin(turn) ** 3
                y = -(13 * math.cos(turn) - 5 * math.cos(2 * turn)
                      - 2 * math.cos(3 * turn) - math.cos(4 * turn))
                points.append((cx + x * 5.4 * scale, cy + y * 5.4 * scale))
            _hand_line(draw, _rotate_points(points, cx, cy, angle), color, width=5, wobble=wobble)
        elif shape == "wave":
            points = [(cx - radius * 1.5 + index * 8,
                       cy + math.sin(index / 3.0) * 26) for index in range(40)]
            _hand_line(draw, points, color, width=4, wobble=wobble)
        elif shape == "cloud":
            for dx, dy, r in ((-40, 6, 40), (0, -10, 48), (42, 6, 38), (-6, 14, 42)):
                draw.ellipse([cx + dx - r, cy + dy - r, cx + dx + r, cy + dy + r],
                             fill=tuple(min(255, value + 55) for value in color))
        elif shape == "dots":
            for _ in range(18):
                dx, dy = random.uniform(-radius, radius), random.uniform(-radius, radius)
                r = random.uniform(4, 9)
                draw.ellipse([cx + dx - r, cy + dy - r, cx + dx + r, cy + dy + r], fill=color)
        else:  # line
            ends = [(cx - radius * 1.4, cy + radius * 0.5), (cx + radius * 1.4, cy - radius * 0.5)]
            _hand_line(draw, _rotate_points(ends, cx, cy, angle), color, width=4, wobble=wobble)


def _label(stroke: dict) -> str:
    label = (stroke.get("color") or "") + SHAPE_LABELS.get(stroke.get("shape", ""), "笔")
    place = stroke.get("place") or ""
    return f"{label}（{place or '当中'}）" if place else label


def paint_strokes(strokes: list[dict]) -> Image.Image:
    """把一串笔画画到一张新纸上，返回图片（canvas.py 用它做累积画布）。"""
    base, draw = _paper()
    for stroke in strokes:
        _paint_stroke(draw, stroke)
    return base.filter(ImageFilter.SMOOTH)


def render(spec: str, out_dir: Path | None = None) -> dict:
    """画一张，返回 {'path','caption','strokes'}；没有可画的笔画就返回空。"""
    strokes = parse(spec)
    if not strokes:
        return {}
    base, draw = _paper()
    captions: list[str] = []
    # 先铺背景（草地/天空/山），再画别的，避免前景被盖住
    ordered = sorted(strokes, key=lambda s: 0 if str(s.get("shape")) in BACKGROUND_SHAPES else 1)
    for stroke in ordered:
        _paint_stroke(draw, stroke)
    for stroke in strokes:
        captions.append(_label(stroke))

    base = base.filter(ImageFilter.SMOOTH)
    target = Path(out_dir) if out_dir else OUT_DIR
    target.mkdir(parents=True, exist_ok=True)
    from datetime import datetime

    name = f"doodle-{datetime.now():%Y%m%d-%H%M%S-%f}.png"
    path = target / name
    base.save(path, "PNG")
    return {"path": str(path), "name": name, "caption": "、".join(captions), "strokes": strokes}


if __name__ == "__main__":
    import sys

    spec = " ".join(sys.argv[1:]) or "黄圆 中间；红线 左下到右上；蓝波浪 下"
    result = render(spec)
    print(result.get("path"), "|", result.get("caption"))
