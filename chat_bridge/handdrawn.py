"""把 SD 出的图压成"手绘"（v3）：形状保住，颜色平涂，铅笔线，纸底。

2026-11-08 爸爸："让她模拟手绘啊，这个也太真实了吧。"
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageOps

CANVAS = 512


def hand_drawnify(src: str | Path, dst: str | Path) -> Path:
    original = Image.open(src).convert("RGB")
    # 1) 柔化：留形状，去照片细节
    soft = original.resize((256, 256), Image.BILINEAR)
    soft = soft.filter(ImageFilter.GaussianBlur(1.2))
    soft = soft.resize((CANVAS, CANVAS), Image.BICUBIC)
    # 2) 平涂：自适应 16 色（保住她的配色，不像固定蜡笔盘那样跑色）
    flat = soft.quantize(colors=16, method=Image.MEDIANCUT, dither=Image.NONE).convert("RGB")
    # 3) 铅笔线：从原图提边，细一点
    gray = ImageOps.grayscale(original).resize((CANVAS, CANVAS), Image.BICUBIC)
    gray = gray.filter(ImageFilter.SMOOTH_MORE)
    edges = gray.filter(ImageFilter.FIND_EDGES)
    edges = ImageOps.autocontrast(edges).point(lambda v: 255 if v > 78 else 0)
    edges = edges.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.MinFilter(3))
    # 4) 纸底 + 轻微暖色
    paper = Image.new("RGB", (CANVAS, CANVAS), (247, 243, 234))
    grain = Image.effect_noise((CANVAS, CANVAS), 12).convert("L")
    paper = Image.composite(paper, Image.new("RGB", (CANVAS, CANVAS), (238, 233, 222)), grain)
    # 5) 合成：平涂 88% + 纸底，把铅笔线压上去
    image = Image.blend(paper, flat, 0.88)
    ink = Image.new("RGB", (CANVAS, CANVAS), (72, 66, 60))
    image = Image.composite(ink, image, edges)
    # 6) 外框，像画在纸上的
    draw = ImageDraw.Draw(image)
    draw.rectangle([8, 8, CANVAS - 9, CANVAS - 9], outline=(132, 122, 106), width=2)
    out = Path(dst)
    image.save(out, "PNG")
    return out


if __name__ == "__main__":
    import sys

    source = sys.argv[1] if len(sys.argv) > 1 else r"PROJECT_ROOT\chat_bridge\data\drawn\20261108-165204-307876.png"
    target = sys.argv[2] if len(sys.argv) > 2 else r"PROJECT_ROOT\chat_bridge\data\drawn\preview-handdrawn-v3.png"
    print(hand_drawnify(source, target))
