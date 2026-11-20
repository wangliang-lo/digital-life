# -*- coding: utf-8 -*-
"""用 RVC 训练她自己的音色（一条龙）。

    python tools/rvc_train.py --exp xiaoyu --epochs 200 --batch 4
    python tools/rvc_train.py --exp xiaoyu --only-index

数据放：vendor/rvc/dataset/<exp>/*.wav
跑之前先停掉 tts_server（Breeze 常驻占 4GB，训练要 5GB，8G 卡会抢显存）。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import time
from pathlib import Path

RVC_ROOT = Path(r"PROJECT_ROOT\vendor\rvc")
PY = Path(r"C:\Users\Lenovo\anaconda3\envs\rvc312\python.exe")
SR = "40k"


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(RVC_ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    env["weight_root"] = str(RVC_ROOT / "assets" / "weights")
    env["index_root"] = str(RVC_ROOT / "logs")
    env["outside_index_root"] = str(RVC_ROOT / "assets" / "indices")
    env["rmvpe_root"] = str(RVC_ROOT / "assets" / "rmvpe")
    env["OMP_NUM_THREADS"] = "4"
    return env


def run(args: list[str]) -> None:
    print("\n$ " + " ".join(str(a) for a in args), flush=True)
    started = time.time()
    proc = subprocess.run([str(a) for a in args], cwd=str(RVC_ROOT), env=_env())
    if proc.returncode != 0:
        raise SystemExit(f"失败（{proc.returncode}）：{' '.join(str(a) for a in args)}")
    print(f"  完成，用时 {time.time() - started:.0f}s", flush=True)


def ensure_config(exp_dir: Path) -> None:
    config = exp_dir / "config.json"
    if config.exists():
        return
    template = RVC_ROOT / "configs" / "v2" / "40k.json"
    if not template.exists():
        template = RVC_ROOT / "configs" / "v1" / "40k.json"
    config.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"写入默认训练配置：{config}")


def write_filelist(exp_dir: Path) -> int:
    names = sorted(p.stem for p in (exp_dir / "0_gt_wavs").glob("*.wav"))
    lines = []
    for name in names:
        lines.append(
            f"{exp_dir / '0_gt_wavs' / (name + '.wav')}|"
            f"{exp_dir / '3_feature768' / (name + '.npy')}|"
            f"{exp_dir / '2a_f0' / (name + '.wav.npy')}|"
            f"{exp_dir / '2b-f0nsf' / (name + '.wav.npy')}|0"
        )
    # 无 BOM：RVC 用 split('|') 读，BOM 会变成路径一部分（踩过）
    (exp_dir / "filelist.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"filelist.txt：{len(lines)} 条")
    return len(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="训练 RVC 音色")
    parser.add_argument("--exp", default="xiaoyu")
    parser.add_argument("--epochs", type=int, default=200)
    parser.add_argument("--batch", type=int, default=4)
    parser.add_argument("--save-every", type=int, default=20)
    parser.add_argument("--nproc", type=int, default=2)
    parser.add_argument("--cpu", action="store_true")
    parser.add_argument("--only-index", action="store_true")
    parser.add_argument("--only-preprocess", action="store_true")
    parser.add_argument("--skip-existing", action="store_true", help="已有特征就跳过提取")
    args = parser.parse_args()

    dataset = RVC_ROOT / "dataset" / args.exp
    exp_dir = RVC_ROOT / "logs" / args.exp
    if not dataset.exists() or not any(dataset.glob("*.wav")):
        print(f"没有数据：往 {dataset} 里放 wav")
        return 1
    exp_dir.mkdir(parents=True, exist_ok=True)
    ensure_config(exp_dir)

    if not args.only_index:
        # 数据集比切片新（或者切片数量对不上）就重做，否则会拿旧切片训练（踩过）
        slices = list((exp_dir / "0_gt_wavs").glob("*.wav"))
        newest_audio = max((f.stat().st_mtime for f in dataset.glob("*.wav")), default=0.0)
        newest_slice = max((f.stat().st_mtime for f in slices), default=0.0)
        if slices and newest_audio > newest_slice:
            print("数据集更新了，清掉旧的切片/特征/索引")
            for sub in ("0_gt_wavs", "1_16k_wavs", "2a_f0", "2b-f0nsf", "3_feature768"):
                for f in (exp_dir / sub).glob("*"):
                    f.unlink(missing_ok=True)
            for f in exp_dir.glob("filelist.txt"):
                f.unlink(missing_ok=True)
            for f in (RVC_ROOT / "logs" / args.exp).glob("*.index"):
                f.unlink(missing_ok=True)
            for f in (RVC_ROOT / "assets" / "indices").glob(f"{args.exp}*.index"):
                f.unlink(missing_ok=True)
            for f in (RVC_ROOT / "assets" / "weights").glob(f"{args.exp}*.pth"):
                f.unlink(missing_ok=True)
            slices = []
        have_slices = bool(slices)
        if not have_slices:
            run([PY, "-m", "train.preprocess", dataset, "40000", args.nproc, exp_dir, "True", "3.7"])
        if args.only_preprocess:
            return 0
        if not args.skip_existing or not any((exp_dir / "2a_f0").glob("*.npy")):
            run([PY, "train/dataset/extract_f0.py", "cuda", "1", "0", "0", exp_dir, "rmvpe", "True"])
        if not args.skip_existing or not any((exp_dir / "3_feature768").glob("*.npy")):
            run([PY, "train/dataset/extract_hubert_feature.py", "cuda", "1", "0", "0", exp_dir, "v2", "True"])
        write_filelist(exp_dir)
        cmd = [
            PY, "-m", "train.train",
            "-e", args.exp, "-sr", SR, "-f0", "1",
            "-bs", str(args.batch), "-g", "0",
            "-te", str(args.epochs), "-se", str(args.save_every),
            "-pg", "assets/pretrained_v2/f0G40k.pth",
            "-pd", "assets/pretrained_v2/f0D40k.pth",
            "-l", "1", "-c", "1", "-sw", "1", "-v", "v2",
        ]
        if args.cpu:
            cmd.append("-cpu")
        run(cmd)

    ckpts = sorted(exp_dir.glob("G_*.pth"), key=lambda p: p.stat().st_mtime)
    if not ckpts:
        print("没有找到 G_*.pth")
        return 1
    run([PY, "train/train_index.py", args.exp, "v2", "assets/indices", "4"])
    run([PY, "-m", "train.process_ckpt", args.exp])
    latest = sorted((RVC_ROOT / "assets" / "weights").glob(f"{args.exp}*.pth"), key=lambda p: p.stat().st_mtime)
    print(f"\n完成。权重：{latest[-1] if latest else '（没生成）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
