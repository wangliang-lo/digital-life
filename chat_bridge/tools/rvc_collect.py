# -*- coding: utf-8 -*-
"""把爸爸给的一堆清唱（任意格式）收进 RVC 训练集。

    python tools/rvc_collect.py --src "PROJECT_ROOT\voice_dataset" --exp xiaoyu
    python tools/rvc_collect.py --src ... --exp xiaoyu --denoise

做的事：
- 扫 src 下所有音频（wav/mp3/m4a/flac/ogg/aac/amr/wma）
- 统一转成 40kHz 单声道 wav，放到 vendor/rvc/dataset/<exp>/
- 去掉首尾静音，太短（<3s）或太长（>10min）的会跳过
- `--denoise` 用仓库自带的 pymss 做一次人声分离（有伴奏的素材用，慢）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".amr", ".wma", ".opus"}
RVC_ROOT = Path(r"PROJECT_ROOT\vendor\rvc")


def main() -> int:
    parser = argparse.ArgumentParser(description="把清唱收进 RVC 训练集")
    parser.add_argument("--src", required=True)
    parser.add_argument("--exp", default="xiaoyu")
    parser.add_argument("--sr", type=int, default=40000)
    parser.add_argument("--denoise", action="store_true")
    args = parser.parse_args()

    import librosa  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415
    import soundfile as sf  # noqa: PLC0415

    src = Path(args.src)
    files = [p for p in src.rglob("*") if p.suffix.lower() in AUDIO_EXTS] if src.is_dir() else [src]
    if not files:
        print(f"{src} 里没有音频")
        return 1
    out_dir = RVC_ROOT / "dataset" / args.exp
    out_dir.mkdir(parents=True, exist_ok=True)

    total = 0.0
    kept = 0
    for index, path in enumerate(sorted(files), 1):
        try:
            y, _ = librosa.load(str(path), sr=args.sr, mono=True)
            y, _ = librosa.effects.trim(y, top_db=35)
            duration = len(y) / args.sr
            if duration < 3.0:
                print(f"跳过 {path.name}：太短（{duration:.1f}s）")
                continue
            if duration > 600:
                y = y[: args.sr * 600]
            peak = float(np.max(np.abs(y))) or 1.0
            y = (y / peak * 0.9).astype("float32")
            target = out_dir / f"src{index:03d}.wav"
            sf.write(str(target), y, args.sr, subtype="PCM_16")
            kept += 1
            total += len(y) / args.sr
            print(f"[{kept}] {path.name} → {target.name}（{len(y)/args.sr:.1f}s）")
        except Exception as exc:  # noqa: BLE001
            print(f"跳过 {path.name}：{type(exc).__name__}: {exc}")

    print(f"\n共 {kept} 段，{total/60:.1f} 分钟 → {out_dir}")
    if total < 300:
        print("提示：不到 5 分钟，RVC 训出来会不太稳；10 分钟以上更保险。")
    if args.denoise:
        print("--denoise 请用仓库的 pymss 工具单独跑（GUI/CLI），这里不做自动分离。")
    print("\n下一步：python tools/rvc_train.py --exp %s --epochs 200 --batch 4" % args.exp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
