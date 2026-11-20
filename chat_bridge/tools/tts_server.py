#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""小鱼的声音：Breeze TTS 2 本地服务（端口 7861）。

她本来只有嘴（文字），这个进程给她一副嗓子。

    C:\\aihuman\\tts-venv\\Scripts\\python.exe tools\\tts_server.py
    C:\\aihuman\\tts-venv\\Scripts\\python.exe tools\\tts_server.py --port 7861

接口（只用标准库，不依赖 fastapi）：
    GET  /health              -> {"ok": true, "device": "cuda:0", "model": "..."}
    POST /tts   {"text": "...", "instruction": "...", "cfg_scale": 1.0, "seed": 42}
                              -> audio/wav 字节
    GET  /voice?name=xxx.wav  -> 之前生成的 wav

设计：
- 模型只加载一次，单飞（同一时刻只合成一条，TTS 不是线程安全的）；
- 结果按 (text, instruction, seed) 哈希缓存在 data/voice/，重复的句子不再烧 GPU；
- 合成失败返回 500 + 错误信息，调用方（网页）自己降级成"只有文字"，绝不卡住聊天。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent          # chat_bridge
PROJECT = ROOT.parent                                   # PROJECT_ROOT
VENDOR = PROJECT / "vendor" / "breeze-tts"
DEFAULT_MODEL = PROJECT / "models" / "breeze-tts-2"
VOICE_DIR = ROOT / "data" / "voice"
# RVC：把 Breeze 合成出来的声音换成她自己的音色（训练脚本见 tools/rvc_train.py）
RVC_ROOT = Path(r"PROJECT_ROOT\vendor\rvc")
RVC_PY = Path(r"C:\Users\Lenovo\anaconda3\envs\rvc312\python.exe")

# 音色交给 RVC（参考录音训出来的），所以这里只要"自然、清楚、语速正常"的底子。
# 以前那段"软软糯糯、语速慢、尾音粘一下"是克隆参考的阶段写的；
# 现在叠在 RVC 上会变成拖腔、语调怪（2026-11-02 爸爸反馈"语调太奇怪"）。
DEFAULT_VOICE = (
    "一个二十岁出头的年轻女孩，声音清亮自然，像平时跟熟人说话："
    "语速正常，停顿自然，句尾干净利落，不要拖长尾音，不要气声，"
    "不要播音腔，不要朗读感，不要刻意卖萌。"
)

# 按她当时的心情给嗓音加一点变化（只改怎么说，不改说什么）
STYLE_MOOD = {
    "joy": "句尾微微上扬，声音轻快一点，可以带笑。",
    "hurt": "声音更小、更闷，慢下来，像忍着不说。",
    "impulse": "说快一点、短一点，气声重一些。",
    "tired": "有气无力，尾音拖下去，像快睡着了。",
}


def build_instruction(base: str, mood: dict[str, float] | None = None) -> str:
    """base 音色 + 当下情绪的说话方式。"""
    text = (base or DEFAULT_VOICE).strip()
    feel = mood or {}
    try:
        joy = float(feel.get("joy") or 0)
        hurt = float(feel.get("hurt") or 0)
        impulse = float(feel.get("impulse") or 0)
    except (TypeError, ValueError):
        return text
    moods = []
    if joy >= 70:
        moods.append(STYLE_MOOD["joy"])
    if hurt >= 60:
        moods.append(STYLE_MOOD["hurt"])
    if impulse >= 65:
        moods.append(STYLE_MOOD["impulse"])
    if not moods and joy and joy <= 35 and hurt <= 45:
        moods.append(STYLE_MOOD["tired"])
    if moods:
        text += "现在" + "；".join(moods)
    return text

sys.path.insert(0, str(VENDOR))

_lock = threading.Lock()
_state: dict[str, object] = {"ready": False, "error": "", "device": "", "model": "", "runtime": None}


def _load(model_dir: Path) -> None:
    """加载模型（首次请求或启动时调用）。"""
    import torch  # noqa: PLC0415

    from breeze_infer.runtime import (  # noqa: PLC0415
        load_runtime,
        resolve_device,
        set_all_seeds,
        update_generation_config_for_breeze,
    )
    from models.fast_streaming import FastBreezeStreamingRuntime, FastStreamingConfig  # noqa: PLC0415

    device = resolve_device()
    started = time.time()
    # 8GB 的卡放不下 bf16（7.3GB 权重，实测 Peak 7.78/7.96GB、RTF 34），
    # 4-bit 只要 ~4GB、RTF ~3.8，才跑得动。没装 bitsandbytes 就退回 bf16（慢但可用）。
    quantized = False
    try:
        from transformers import AutoTokenizer, BitsAndBytesConfig  # noqa: PLC0415
        from models.breeze import BreezeForConditionalGeneration  # noqa: PLC0415
        from qwen_tts import Qwen3TTSTokenizer  # noqa: PLC0415

        tokenizer = AutoTokenizer.from_pretrained(model_dir, fix_mistral_regex=False)
        quant = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True,
        )
        model = BreezeForConditionalGeneration.from_pretrained(
            model_dir,
            quantization_config=quant,
            device_map=device,
            attn_implementation="eager",
            dtype=torch.bfloat16,
        )
        model.eval()
        audio_tokenizer = Qwen3TTSTokenizer.from_pretrained(
            str(Path(model_dir) / "audio_tokenizer"), device_map=device
        )
        quantized = True
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[voice] 4-bit 加载失败（{type(exc).__name__}: {exc}），退回 bf16\n")
        tokenizer, model, audio_tokenizer = load_runtime(
            model_dir, device=device, attn_implementation="eager"
        )
    update_generation_config_for_breeze(model)
    torch.cuda.empty_cache() if torch.cuda.is_available() else None
    config = FastStreamingConfig(
        max_new_tokens=420,
        max_seq_len=768,
        fast_all=False,
        repetition_penalty=1.1,
    )
    runtime = FastBreezeStreamingRuntime(model, audio_tokenizer, config, tokenizer=tokenizer)
    set_all_seeds(42)
    _state.update({
        "ready": True,
        "error": "",
        "device": device,
        "model": str(model_dir),
        "runtime": runtime,
        "quantized": quantized,
        "load_seconds": round(time.time() - started, 1),
        "free_vram": (
            round(torch.cuda.mem_get_info()[0] / 1024 ** 3, 2)
            if torch.cuda.is_available() else 0
        ),
    })


REF_AUDIO = VOICE_DIR / "ref_voice.wav"      # 用户提供的参考录音（10-30 秒干净人声）
REF_TEXT = VOICE_DIR / "ref_voice.txt"       # 那段录音的准确文字稿


def reference_pair() -> tuple[Path | None, str]:
    """有参考录音就用"克隆/音色迁移"，没有就用声音设计。"""
    try:
        if REF_AUDIO.exists() and REF_AUDIO.stat().st_size > 1024:
            text = REF_TEXT.read_text(encoding="utf-8").strip() if REF_TEXT.exists() else ""
            if text:
                return REF_AUDIO, text
    except OSError:
        pass
    return None, ""


def reference_tag() -> str:
    """参考录音变了，缓存要跟着失效。"""
    try:
        if REF_AUDIO.exists():
            return f"ref:{int(REF_AUDIO.stat().st_mtime)}:{REF_AUDIO.stat().st_size}"
    except OSError:
        pass
    return "ref:none"


def _synthesize(text: str, instruction: str, cfg_scale: float, seed: int) -> bytes:
    """合成一条语音，返回 wav 字节。有参考录音就克隆那个音色。"""
    import soundfile as sf  # noqa: PLC0415
    import io  # noqa: PLC0415

    from breeze_infer.templates import get_template, prepare_inputs, select_template_name  # noqa: PLC0415
    from breeze_infer.runtime import set_all_seeds  # noqa: PLC0415

    runtime = _state.get("runtime")
    if runtime is None:
        raise RuntimeError("模型还没加载好")
    request = {
        "id": "xiaoyu",
        "text": text,
        "speaker": "S0",
    }
    ref_audio, ref_text = reference_pair()
    if ref_audio is not None:
        request["ref_audio_path"] = str(ref_audio)
        request["ref_text"] = ref_text
    if instruction.strip():
        request["instruction"] = instruction.strip()
    template_name = select_template_name(request)

    set_all_seeds(seed)
    inputs = prepare_inputs(
        runtime.tokenizer,
        runtime.audio_tokenizer,
        runtime.model,
        [request],
        get_template(template_name),
        guidance_scale=cfg_scale,
        guidance_scale_ref=None,
        guidance_scale_ins=None,
    )
    buffer = io.BytesIO()
    with sf.SoundFile(
        buffer, mode="w", samplerate=runtime.sample_rate, channels=1,
        subtype="PCM_16", format="WAV",
    ) as handle:
        for chunk in runtime.iter_audio_chunks(inputs, request_id="xiaoyu", seed=seed):
            handle.write(chunk.audio)
    return buffer.getvalue()


def _normalize_pace(wav: bytes, text: str) -> bytes:
    """把语速拉回正常。

    参考录音是"唱"的，克隆出来的话会被拖得很慢（20 多字要 9 秒），听起来很别扭。
    按中文正常语速约 0.24 秒/字折算，只做温和的时间伸缩（0.75-1.4 倍）。
    """
    import io as _io  # noqa: PLC0415

    import librosa  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415
    import soundfile as sf  # noqa: PLC0415

    try:
        data, sr = sf.read(_io.BytesIO(wav), dtype="float32")
        if getattr(data, "ndim", 1) > 1:
            data = data.mean(axis=1)
        chars = len([c for c in str(text or "") if not c.isspace()])
        if chars < 6 or len(data) < sr * 0.5:
            return wav
        target = chars * 0.24
        current = len(data) / sr
        rate = current / target
        if rate <= 1.18 or rate >= 2.2:
            return wav  # 本来就不慢，或者慢得离谱（不是语速问题）
        stretched = librosa.effects.time_stretch(data, rate=rate)
        buf = _io.BytesIO()
        with sf.SoundFile(buf, mode="w", samplerate=sr, channels=1, subtype="PCM_16", format="WAV") as handle:
            handle.write(stretched.astype("float32"))
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return wav


CACHE_VERSION = "v2"  # 改了合成后处理（语速/唱歌）就把这个加一，避免拿到旧缓存


def rvc_tag() -> str:
    """RVC 模型变了，缓存也要失效。"""
    model = _state.get("rvc_model") or ""
    if not model:
        return "rvc:none"
    try:
        return f"rvc:{int(Path(model).stat().st_mtime)}:{_state.get('rvc_pitch', 0)}"
    except OSError:
        return "rvc:none"


def _rvc_convert(wav: bytes) -> bytes:
    """用 RVC 把合成的声音换成她自己的音色（多花 2-4 秒）。"""
    model = _state.get("rvc_model") or ""
    if not model:
        return wav
    import subprocess  # noqa: PLC0415

    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_in = VOICE_DIR / "_rvc_in.wav"
    tmp_out = VOICE_DIR / "_rvc_out.wav"
    tmp_in.write_bytes(wav)
    cmd = [
        str(RVC_PY), "infer/cli.py", "--model", model,
        "--input", str(tmp_in), "--output", str(tmp_out),
        "--pitch", str(_state.get("rvc_pitch", 0)),
        "--f0-method", "rmvpe", "--overwrite",
    ]
    index = _state.get("rvc_index") or ""
    if index and Path(index).exists():
        cmd += ["--index", index]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(RVC_ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(cmd, cwd=str(RVC_ROOT), env=env, capture_output=True)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[rvc] 调用失败：{type(exc).__name__}: {exc}\n")
        return wav
    if proc.returncode != 0 or not tmp_out.exists():
        sys.stderr.write("[rvc] 转换失败：\n" + proc.stderr.decode("utf-8", "ignore")[-800:] + "\n")
        return wav
    data = tmp_out.read_bytes()
    for path in (tmp_in, tmp_out):
        try:
            path.unlink()
        except OSError:
            pass
    return data


def voice_path(text: str, instruction: str, cfg_scale: float, seed: int) -> Path:
    key = hashlib.sha256(
        f"{CACHE_VERSION}|{text}|{instruction}|{cfg_scale}|{seed}".encode("utf-8")
    ).hexdigest()[:20]
    return VOICE_DIR / f"{key}.wav"


# 简易"唱歌"：先照常合成一句，再按旋律把每个音段做变调、拼接。
# 不是专业的歌唱合成，先让她能"哼着唱"；要真唱得好，得换 SVS 模型（DiffSinger 之类）。
MELODIES: dict[str, list[tuple[int, float]]] = {
    # (半音, 拍数)；小星星第一句 1 1 5 5 6 6 5
    "twinkle": [(0, 1), (0, 1), (7, 1), (7, 1), (9, 1), (9, 1), (7, 2)],
    # 生日快乐第一句
    "birthday": [(0, 0.75), (0, 0.25), (2, 1), (0, 1), (5, 1), (4, 2)],
    # 简单五声音阶上行，适合"啦啦啦"
    "la": [(0, 1), (2, 1), (4, 1), (7, 1), (9, 2)],
}


def _sing_from_wav(wav: bytes, melody: str, sr_hint: int = 24000) -> bytes:
    """把一句话按旋律变调拼成"歌"。"""
    import io as _io  # noqa: PLC0415

    import librosa  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415
    import soundfile as sf  # noqa: PLC0415

    notes = MELODIES.get(melody) or MELODIES["twinkle"]
    data, sr = sf.read(_io.BytesIO(wav), dtype="float32")
    if getattr(data, "ndim", 1) > 1:
        data = data.mean(axis=1)
    if len(data) == 0:
        return wav
    # 静音头尾去掉一点，避免把停顿也变调唱出来
    trimmed, _ = librosa.effects.trim(data, top_db=35)
    if len(trimmed) > int(0.2 * sr):
        data = trimmed
    pieces = np.array_split(data, len(notes))
    out = []
    rng = np.random.default_rng(7)
    for index, (piece, (step, beats)) in enumerate(zip(pieces, notes)):
        if len(piece) < 512:
            continue
        # 2026-11-02 爸爸："唱得完全就是合成感的电音，让她五音不全一点。"
        # 所以：跨度压到 0.35 倍、每个音再抖 ±0.35 个半音、混回 25% 原声，
        # 听起来像"认真但不太准地唱"，而不是修音器。
        detune = float(rng.uniform(-0.35, 0.35))
        shifted = librosa.effects.pitch_shift(
            piece, sr=sr, n_steps=float(step) * 0.35 + detune
        )
        if len(shifted) == len(piece):
            shifted = shifted * 0.75 + piece * 0.25
        # 按拍数拉长/缩短，稍微不齐一点（像真人抢拍/拖拍）
        target = int(len(shifted) * (0.8 + 0.25 * beats) * float(rng.uniform(0.96, 1.05)))
        if target > 512:
            shifted = librosa.effects.time_stretch(shifted, rate=len(shifted) / target)
        fade = min(240, len(shifted) // 8)
        if fade > 0:
            shifted[:fade] *= np.linspace(0, 1, fade)
            shifted[-fade:] *= np.linspace(1, 0, fade)
        out.append(shifted)
    if not out:
        return wav
    sung = np.concatenate(out)
    peak = float(np.max(np.abs(sung))) or 1.0
    sung = (sung / peak * 0.9).astype("float32")
    buf = _io.BytesIO()
    with sf.SoundFile(buf, mode="w", samplerate=sr, channels=1, subtype="PCM_16", format="WAV") as handle:
        handle.write(sung)
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    server_version = "XiaoyuVoice/1.0"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        sys.stderr.write("[voice] " + (fmt % args) + "\n")

    def _json(self, code: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _wav(self, data: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ---------------------------------------------------------------- 流式
    def _stream_voice(self, query: dict) -> None:
        """/tts_stream：边合成边把 PCM 推给前端。"""
        import numpy as np  # noqa: PLC0415

        from breeze_infer.runtime import set_all_seeds  # noqa: PLC0415
        from breeze_infer.templates import get_template, prepare_inputs, select_template_name  # noqa: PLC0415

        text_in = str((query.get("text") or [""])[0]).strip()
        if not text_in:
            self._json(400, {"ok": False, "error": "text 不能为空"})
            return

        def q_float(name: str, default: float) -> float:
            try:
                return float((query.get(name) or [default])[0])
            except (TypeError, ValueError):
                return default

        mood = {
            "joy": q_float("joy", 50.0),
            "hurt": q_float("hurt", 30.0),
            "impulse": q_float("impulse", 30.0),
        }
        instruction = build_instruction(str((query.get("instruction") or [""])[0]), mood)
        cfg_scale = max(1.0, min(6.0, q_float("cfg_scale", 1.0)))
        try:
            seed = int((query.get("seed") or [42])[0])
        except (TypeError, ValueError):
            seed = 42

        with _lock:
            if not _state["ready"]:
                _load(Path(_state["model"] or DEFAULT_MODEL))
            runtime = _state.get("runtime")
            if runtime is None:
                self._json(500, {"ok": False, "error": "模型还没加载好"})
                return
            request = {"id": "xiaoyu", "text": text_in[:400], "speaker": "S0"}
            ref_audio, ref_text = reference_pair()
            if ref_audio is not None:
                request["ref_audio_path"] = str(ref_audio)
                request["ref_text"] = ref_text
            if instruction.strip():
                request["instruction"] = instruction.strip()
            set_all_seeds(seed)
            inputs = prepare_inputs(
                runtime.tokenizer,
                runtime.audio_tokenizer,
                runtime.model,
                [request],
                get_template(select_template_name(request)),
                guidance_scale=cfg_scale,
                guidance_scale_ref=None,
                guidance_scale_ins=None,
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("X-Sample-Rate", str(runtime.sample_rate))
            self.send_header("X-Channels", "1")
            self.send_header("X-Format", "s16le")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            started = time.time()
            frames = 0
            try:
                for chunk in runtime.iter_audio_chunks(inputs, request_id="xiaoyu", seed=seed):
                    data = np.clip(np.asarray(chunk.audio, dtype="float32"), -1.0, 1.0)
                    pcm = (data * 32767.0).astype("<i2").tobytes()
                    self.wfile.write(pcm)
                    self.wfile.flush()
                    frames += len(pcm)
            except Exception as exc:  # noqa: BLE001
                sys.stderr.write(f"[voice] 流式中断: {type(exc).__name__}: {exc}\n")
            sys.stderr.write(
                f"[voice] 流式 {len(text_in)} 字 -> {frames/1024:.0f} KB（{time.time()-started:.1f}s）\n"
            )

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._json(200, {
                "ok": bool(_state["ready"]),
                "error": _state["error"],
                "device": _state["device"],
                "model": _state["model"],
                "load_seconds": _state.get("load_seconds"),
                "free_vram_gb": _state.get("free_vram"),
            })
            return
        if parsed.path == "/tts_stream":
            self._stream_voice(parse_qs(parsed.query))
            return
        if parsed.path == "/voice":
            name = (parse_qs(parsed.query).get("name") or [""])[0]
            safe = Path(name).name
            target = VOICE_DIR / safe
            if not safe.endswith(".wav") or not target.exists():
                self._json(404, {"ok": False, "error": "没有这条语音"})
                return
            self._wav(target.read_bytes())
            return
        self._json(404, {"ok": False, "error": "unknown"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path not in ("/tts", "/sing"):
            self._json(404, {"ok": False, "error": "unknown"})
            return
        singing = parsed.path == "/sing"
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(length, 20000)).decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            self._json(400, {"ok": False, "error": "body 不是 JSON"})
            return
        text = str(body.get("text") or "").strip()
        if not text:
            self._json(400, {"ok": False, "error": "text 不能为空"})
            return
        instruction = build_instruction(
            str(body.get("instruction") or ""), body.get("mood") or {}
        )
        try:
            cfg_scale = max(1.0, min(6.0, float(body.get("cfg_scale") or 4.0)))
        except (TypeError, ValueError):
            cfg_scale = 1.0
        try:
            seed = int(body.get("seed") or 42)
        except (TypeError, ValueError):
            seed = 42

        melody = str(body.get("melody") or "twinkle")
        target = voice_path(
            text,
            instruction + ("|sing:" + melody if singing else "") + "|" + reference_tag() + "|" + rvc_tag(),
            cfg_scale,
            seed,
        )
        if target.exists() and target.stat().st_size > 0:
            self._wav(target.read_bytes())
            return

        started = time.time()
        with _lock:
            try:
                if not _state["ready"]:
                    _load(Path(_state["model"] or DEFAULT_MODEL))
                data = _synthesize(text[:400], instruction, cfg_scale, seed)
                if singing:
                    data = _sing_from_wav(data, melody)
                else:
                    # 参考录音是唱的，克隆出来会被拖慢；按正常语速收一收
                    data = _normalize_pace(data, text)
                data = _rvc_convert(data)
            except Exception as exc:  # noqa: BLE001
                _state["error"] = f"{type(exc).__name__}: {exc}"
                sys.stderr.write(traceback.format_exc())
                self._json(500, {"ok": False, "error": _state["error"]})
                return
        VOICE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
        sys.stderr.write(
            f"[voice] {len(text)} 字 -> {target.name} ({len(data)/1024:.0f} KB, {time.time()-started:.1f}s)\n"
        )
        self._wav(data)


def main() -> int:
    parser = argparse.ArgumentParser(description="小鱼的声音（Breeze TTS 2）")
    parser.add_argument("--port", type=int, default=7861)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--model", default=str(DEFAULT_MODEL))
    parser.add_argument("--warmup", action="store_true", help="启动时就加载模型（默认第一次请求才加载）")
    parser.add_argument("--rvc-model", default="", help="RVC 模型 .pth（换成她自己的音色）")
    parser.add_argument("--rvc-index", default="", help="RVC 的 faiss 索引 .index")
    parser.add_argument("--rvc-pitch", type=int, default=0, help="RVC 变调半音")
    args = parser.parse_args()

    _state["model"] = str(Path(args.model))
    if args.rvc_model:
        model_path = Path(args.rvc_model)
        if not model_path.is_absolute():
            model_path = RVC_ROOT / model_path
        if model_path.exists():
            _state["rvc_model"] = str(model_path)
            _state["rvc_index"] = str(args.rvc_index or "")
            _state["rvc_pitch"] = int(args.rvc_pitch)
            print(f"RVC 音色：{model_path}（每次合成多 2-4 秒）")
        else:
            print(f"RVC 模型不存在，忽略：{model_path}")
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    if args.warmup:
        try:
            _load(Path(args.model))
            print(f"模型已加载：device={_state['device']}，{_state['load_seconds']}s")
        except Exception as exc:  # noqa: BLE001
            _state["error"] = f"{type(exc).__name__}: {exc}"
            print(f"模型加载失败：{_state['error']}")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"她的声音：http://{args.host}:{args.port}  model={args.model}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
