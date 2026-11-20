#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地生图服务：加载一个 diffusers 管线，对外提供 A1111 兼容的 txt2img 接口。

默认模型目录 C:\\aihuman\\models\\sd-turbo（可用环境变量 DRAW_MODEL 覆盖），
监听 http://127.0.0.1:7860，供 tools/draw.py 调用。

    C:\\aihuman\\draw-venv...   →  实际用 PYTHONPATH=C:\\aihuman\\drawlibs 跑

接口：
    GET  /sdapi/v1/options        → 200，用于探活
    POST /sdapi/v1/txt2img        → {"images": [base64 png]}
    GET  /health                  → {"ok": true, "model": ...}
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MODEL_DIR = Path(os.environ.get("DRAW_MODEL", r"PROJECT_ROOT\models\sd-turbo"))
OUT_DIR = Path(os.environ.get("DRAW_OUT", r"PROJECT_ROOT\chat_bridge\data\drawn"))
PORT = int(os.environ.get("DRAW_PORT", "7860"))

# 依赖装在 PROJECT_ROOT\drawlibs（没走 pip，因为沙箱里 pip 的临时目录会被拒）。
if r"PROJECT_ROOT\drawlibs" not in sys.path:
    sys.path.insert(0, r"PROJECT_ROOT\drawlibs")
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

_pipe = None
_pipe_lock = threading.Lock()


def translate(prompt: str) -> str:
    """中文描述先翻成英文：SD-Turbo 是英文模型，直接喂中文只会画出怪东西。"""
    if not any("\u4e00" <= ch <= "\u9fff" for ch in prompt):
        return prompt
    payload = {
        "model": os.environ.get("DRAW_LLM", "qwen2.5:14b"),
        "prompt": (
            "把下面这句中文描述翻译成一段英文绘画提示词，只输出英文，"
            "不要解释、不要引号，保留具体的形象、动作、颜色、画风：\n" + prompt
        ),
        "stream": False,
        "options": {"temperature": 0.2},
    }
    try:
        request = urllib.request.Request(
            os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434") + "/api/generate",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=180) as resp:
            text = json.loads(resp.read().decode("utf-8", "replace")).get("response", "").strip()
        text = text.strip().strip('"').strip()
        return text or prompt
    except Exception as exc:  # noqa: BLE001
        print(f"[draw] 翻译失败，直接用原文：{type(exc).__name__}: {exc}", flush=True)
        return prompt


def load_pipeline():
    global _pipe
    with _pipe_lock:
        if _pipe is not None:
            return _pipe
        import torch
        from diffusers import AutoPipelineForText2Image

        dtype = torch.float16 if torch.cuda.is_available() else torch.float32
        device = "cuda" if torch.cuda.is_available() else "cpu"
        pipe = AutoPipelineForText2Image.from_pretrained(str(MODEL_DIR), torch_dtype=dtype, safety_checker=None)
        pipe = pipe.to(device)
        pipe.set_progress_bar_config(disable=True)
        try:
            pipe.enable_attention_slicing()
        except Exception:  # noqa: BLE001
            pass
        _pipe = pipe
        print(f"[draw] 模型已加载：{MODEL_DIR} device={device} dtype={dtype}", flush=True)
        return _pipe


def render(prompt: str, negative: str, width: int, height: int, steps: int, cfg: float, seed: int) -> bytes:
    import torch

    pipe = load_pipeline()
    prompt = translate(prompt)
    width = max(256, min(1024, int(width) // 8 * 8))
    height = max(256, min(1024, int(height) // 8 * 8))
    generator = None
    if seed is not None and int(seed) >= 0:
        generator = torch.Generator(device="cuda" if torch.cuda.is_available() else "cpu").manual_seed(int(seed))
    # sd-turbo 这类蒸馏模型必须 guidance_scale=0、步数 1-4；普通模型给正常值。
    is_turbo = "turbo" in MODEL_DIR.name.lower()
    kwargs = dict(
        prompt=prompt,
        num_inference_steps=max(1, min(50, int(steps))),
        width=width,
        height=height,
        guidance_scale=0.0 if is_turbo else max(1.0, float(cfg)),
        generator=generator,
    )
    if negative and not is_turbo:
        kwargs["negative_prompt"] = negative
    image = pipe(**kwargs).images[0]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def _json(self, code: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/sdapi/v1/options"):
            self._json(200, {"model": str(MODEL_DIR), "ready": _pipe is not None})
            return
        if self.path.startswith("/health"):
            self._json(200, {"ok": True, "model": str(MODEL_DIR), "loaded": _pipe is not None})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if not self.path.startswith("/sdapi/v1/txt2img"):
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8", "replace") or "{}")
            png = render(
                str(body.get("prompt") or ""),
                str(body.get("negative_prompt") or ""),
                int(body.get("width") or 512),
                int(body.get("height") or 512),
                int(body.get("steps") or 4),
                float(body.get("cfg_scale") or 0.0),
                int(body.get("seed") or -1),
            )
            self._json(200, {"images": [base64.b64encode(png).decode("ascii")]})
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})


def main() -> int:
    if not (MODEL_DIR / "model_index.json").exists():
        print(f"[draw] 模型目录不完整：{MODEL_DIR}", flush=True)
        return 2
    print(f"[draw] 载入模型 {MODEL_DIR} ...", flush=True)
    load_pipeline()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[draw] 生图服务已启动 http://127.0.0.1:{PORT}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
