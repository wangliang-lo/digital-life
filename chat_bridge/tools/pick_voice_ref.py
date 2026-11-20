#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从一堆"清唱"里挑出最适合当音色参考的一小段，并自动写出文字稿。

    python tools/pick_voice_ref.py --dir "D:\我的录音" --seconds 12
    python tools/pick_voice_ref.py --file bgm_clean.mp3 --seconds 12

为什么需要这个：
- Breeze 的克隆要一段 10-20 秒的**参考音频 + 准确文字稿**；
- 用户手里常常只有清唱。唱腔的拖音、颤音、大跳都会被克隆带进说话里，
  所以要挑"最像说话"的那一段：字密、音高起伏小、拖长音少、干净。

输出：
- data/voice/ref_voice.wav   （挑出来的那段，mono 24k）
- data/voice/ref_voice.txt   （用 faster-whisper 转写，人工再校一遍）
- 屏幕报告：每段的得分和理由
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".amr"}
ROOT = Path(__file__).resolve().parent.parent          # chat_bridge
VOICE_DIR = ROOT / "data" / "voice"
SR = 24000


def load_audio(path: Path) -> np.ndarray:
    import librosa  # noqa: PLC0415

    y, _ = librosa.load(str(path), sr=SR, mono=True)
    y, _ = librosa.effects.trim(y, top_db=32)
    return y


def score_window(y: np.ndarray) -> tuple[float, dict[str, float]]:
    """越像"说话"分越高：有声音占比高、字密、拖长音少、音高起伏别太大。"""
    import librosa  # noqa: PLC0415

    if len(y) < SR:
        return -99.0, {}
    duration = len(y) / SR
    f0, voiced_flag, _ = librosa.pyin(y, fmin=70, fmax=800, sr=SR, frame_length=1024)
    voiced = f0[~np.isnan(f0)]
    voicing_ratio = float(np.mean(voiced_flag)) if voiced_flag is not None else 0.0
    if len(voiced) < 10:
        return -99.0, {"voicing_ratio": voicing_ratio}

    # 拖长音：连续两帧音高几乎不动（唱腔特征）
    semitone = 12 * np.log2(np.maximum(voiced, 1e-6) / 220.0)
    steps = np.abs(np.diff(semitone))
    sustained_ratio = float(np.mean(steps < 0.25))

    # 音高跨度：唱腔往往跨得大
    span = float(np.percentile(semitone, 90) - np.percentile(semitone, 10))

    # 字密：每秒钟起音数（说话约 4-6，拖长音的慢歌 2-3）
    onsets = librosa.onset.onset_detect(y=y, sr=SR, units="time", backtrack=False)
    onset_rate = len(onsets) / max(duration, 0.1)

    # 音量稳定度：忽大忽小多半是唱到高音
    rms = librosa.feature.rms(y=y)[0]
    loud_cv = float(np.std(rms) / (np.mean(rms) + 1e-6))

    score = (
        voicing_ratio * 2.0
        + min(onset_rate, 6.0) / 6.0 * 2.0
        - sustained_ratio * 2.0
        - max(0.0, span - 6.0) * 0.25
        - max(0.0, loud_cv - 0.8) * 0.5
    )
    detail = {
        "voicing_ratio": round(voicing_ratio, 2),
        "sustained_ratio": round(sustained_ratio, 2),
        "pitch_span_semitone": round(span, 1),
        "onset_rate_per_s": round(onset_rate, 1),
        "loudness_cv": round(loud_cv, 2),
        "score": round(score, 2),
    }
    return score, detail


def transcribe(path: Path) -> str:
    """用 faster-whisper 转写；失败就返回空，让用户手填。"""
    try:
        from faster_whisper import WhisperModel  # noqa: PLC0415

        model = WhisperModel(
            "medium", device="cpu", compute_type="int8",
            download_root=str(Path(r"C:/aihuman/models/whisper")),
        )
        segments, _ = model.transcribe(
            str(path), language="zh", beam_size=1, vad_filter=True,
            initial_prompt="以下是普通话的日常对话。",
        )
        return "".join(seg.text for seg in segments).strip()
    except Exception as exc:  # noqa: BLE001
        print(f"  （转写失败：{type(exc).__name__}: {exc}）", flush=True)
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="从清唱里挑一段最像说话的当音色参考")
    parser.add_argument("--dir", help="音频文件夹")
    parser.add_argument("--file", help="单个音频文件")
    parser.add_argument("--seconds", type=float, default=12.0)
    parser.add_argument("--out-dir", default=str(VOICE_DIR))
    parser.add_argument("--no-transcribe", action="store_true")
    args = parser.parse_args()

    files: list[Path] = []
    if args.file:
        files = [Path(args.file)]
    elif args.dir:
        files = [p for p in Path(args.dir).rglob("*") if p.suffix.lower() in AUDIO_EXTS]
    if not files:
        print("没有找到音频文件（wav/mp3/m4a/flac）")
        return 1

    import librosa  # noqa: PLC0415
    import soundfile as sf  # noqa: PLC0415

    best: tuple[float, Path, float, dict] | None = None
    for path in files:
        try:
            y = load_audio(path)
        except Exception as exc:  # noqa: BLE001
            print(f"跳过 {path.name}：{exc}")
            continue
        win = int(args.seconds * SR)
        hop = max(int(win * 0.5), SR)
        if len(y) <= win:
            windows = [(0.0, y)]
        else:
            windows = [(i / SR, y[i:i + win]) for i in range(0, len(y) - win + 1, hop)]
        for offset, chunk in windows:
            if len(chunk) < SR:
                continue
            score, detail = score_window(chunk)
            if detail:
                print(f"{path.name} @{offset:5.1f}s  score={score:+.2f}  {detail}")
            if best is None or score > best[0]:
                best = (score, path, offset, detail)

    if best is None:
        print("没找到可用的片段")
        return 1
    score, path, offset, detail = best
    y = load_audio(path)
    chunk = y[int(offset * SR): int(offset * SR) + int(args.seconds * SR)]
    chunk = chunk / (np.max(np.abs(chunk)) or 1.0) * 0.9
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    wav_path = out_dir / "ref_voice.wav"
    sf.write(str(wav_path), chunk.astype("float32"), SR, subtype="PCM_16")
    print(f"\n选中：{path.name} @{offset:.1f}s，{len(chunk)/SR:.1f} 秒，score={score:+.2f}")
    print(f"写入：{wav_path}")

    txt_path = out_dir / "ref_voice.txt"
    if not args.no_transcribe:
        print("转写中（faster-whisper medium，可能要一两分钟）…", flush=True)
        text = transcribe(wav_path)
        if text:
            txt_path.write_text(text, encoding="utf-8")
            print(f"写入：{txt_path}\n  内容：{text}")
        else:
            print("转写没成功：请手动把这段录音的文字写进 ref_voice.txt（必须一字不差）")
    print("\n下一步：确认文字稿无误后，直接让她说话/唱歌就会用这个音色（不用重启）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
