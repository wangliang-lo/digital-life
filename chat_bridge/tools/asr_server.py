#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地语音识别服务（OpenAI 兼容）：给IM 助手的"语音识别"插件用。

2026-11-04 爸爸："改"（语音那条也不能只有占位文字）。
IM插件的 speech-recognition 插件只会把音频**上传到一个接口**去做转写，
它自己不带模型；本机有 faster-whisper，所以这里起一个小服务顶上去：

    POST /v1/audio/transcriptions   multipart，字段 file（wav/mp3/silk）
    GET  /v1/models                 返回一个假模型名，方便插件下拉框能选

    python tools/asr_server.py --port 8005 --model small

第一次用会加载/下载模型（small 约 500MB，medium 更大）。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile
import threading
import time
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MODEL_NAME = "whisper-local"
_MODEL = None
_MODEL_LOCK = threading.Lock()


def load_model(size: str, device: str = "cpu", compute: str = "int8"):
    global _MODEL
    with _MODEL_LOCK:
        if _MODEL is None:
            from faster_whisper import WhisperModel  # noqa: PLC0415

            started = time.time()
            # 本机已经预下载过 medium（C:/aihuman/models/whisper），
            # 给 download_root 才不会去 HuggingFace 现拉（那边多半连不上）。
            root = os.environ.get("ASR_DOWNLOAD_ROOT", r"PROJECT_ROOT\models\whisper")
            kwargs: dict = {"device": device, "compute_type": compute}
            if root and Path(root).exists():
                kwargs["download_root"] = root
            _MODEL = WhisperModel(size, **kwargs)
            print(f"模型已加载：{size}（{time.time() - started:.1f}s）", flush=True)
        return _MODEL


def transcribe_file(path: str, language: str = "") -> str:
    model = load_model(os.environ.get("ASR_MODEL", "small"))
    segments, _info = model.transcribe(path, beam_size=5, vad_filter=True,
                                       language=(language or None))
    return "".join(segment.text for segment in segments).strip()


def parse_multipart(body: bytes, content_type: str) -> dict[str, bytes | str]:
    """极简 multipart 解析：只取普通字段和第一个文件。"""
    if "multipart/form-data" not in content_type.lower():
        return {}
    raw = b"Content-Type: " + content_type.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    message = BytesParser(policy=email_policy).parsebytes(raw)
    fields: dict[str, bytes | str] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        if part.get_filename():
            fields[name] = payload
        else:
            fields[name] = payload.decode("utf-8", "ignore")
    return fields


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"object": "list", "data": [{"id": MODEL_NAME, "object": "model"}]})
            return
        self._json(200, {"ok": True, "service": "xiaoyu-asr", "model": MODEL_NAME})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if "/audio/transcriptions" not in self.path:
            self._json(404, {"error": "not found"})
            return
        try:
            fields = parse_multipart(body, str(self.headers.get("Content-Type") or ""))
            audio = fields.get("file")
            if not isinstance(audio, (bytes, bytearray)) or not audio:
                self._json(400, {"error": "没有音频"})
                return
            language = str(fields.get("language") or "").strip()
            suffix = ".wav"
            ctype = str(self.headers.get("Content-Type") or "")
            if "mpeg" in ctype or "mp3" in ctype:
                suffix = ".mp3"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
                handle.write(bytes(audio))
                temp_path = handle.name
            try:
                text = transcribe_file(temp_path, language)
            finally:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            self._json(200, {"text": text})
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地语音识别（OpenAI 兼容）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8005)
    parser.add_argument("--model", default=os.environ.get("ASR_MODEL", "medium"))
    args = parser.parse_args(argv)
    os.environ["ASR_MODEL"] = args.model

    def warmup() -> None:
        try:
            load_model(args.model)
        except Exception as exc:  # noqa: BLE001
            print(f"模型预加载失败：{type(exc).__name__}: {exc}", flush=True)

    threading.Thread(target=warmup, name="asr-warmup", daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(f"语音识别服务已启动：http://{args.host}:{args.port}  模型={args.model}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
