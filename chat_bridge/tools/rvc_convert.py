# -*- coding: utf-8 -*-
"""把任意一段语音（比如 Breeze 合成的）转成她的音色（RVC 推理）。

    python tools/rvc_convert.py --model logs/xiaoyu/G_xxx.pth --input in.wav --output out.wav
    python tools/rvc_convert.py --model ... --input in.wav --pitch 0 --index logs/xiaoyu/added.index

模型和索引一般用 tools/rvc_train.py 训出来的那个。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

RVC_ROOT = Path(r"PROJECT_ROOT\vendor\rvc")
PY = Path(r"C:\Users\Lenovo\anaconda3\envs\rvc312\python.exe")


def main() -> int:
    parser = argparse.ArgumentParser(description="RVC 推理：把声音换成她的音色")
    parser.add_argument("--model", required=True)
    parser.add_argument("--index", default="")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--pitch", type=int, default=0, help="半音，女声转女声一般 0-2")
    parser.add_argument("--f0-method", default="rmvpe", choices=["rmvpe", "pm"])
    args = parser.parse_args()

    model = Path(args.model)
    if not model.is_absolute():
        model = RVC_ROOT / model
    if not model.exists():
        print(f"模型不存在：{model}")
        return 1
    index = args.index
    if index and not Path(index).is_absolute():
        index = str(RVC_ROOT / index)

    cmd = [
        str(PY), "infer/cli.py",
        "--model", str(model),
        "--input", str(Path(args.input).resolve()),
        "--output", str(Path(args.output).resolve()),
        "--pitch", str(args.pitch),
        "--f0-method", args.f0_method,
    ]
    if index and Path(index).exists():
        cmd += ["--index", str(index)]
    print("$ " + " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, cwd=str(RVC_ROOT))
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
