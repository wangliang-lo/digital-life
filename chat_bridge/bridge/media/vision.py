# -*- coding: utf-8 -*-
"""图片理解：把图片压成一句中文，塞进对话。

    ollama  POST {base}/api/generate   {"model": ..., "prompt": ..., "images": [base64]}
    openai  POST {base}/chat/completions   content = [text, image_url(data URI)]

默认用 Ollama 的 gemma3:4b（如果你已经拉过），中文更准可以换 qwen2.5vl:3b。
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path

from ..config import VisionConfig
from ..logs import get_logger
from .voice import MediaError

log = get_logger("media.vision")

_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

_WS = re.compile(r"\s+")


class ImageDescriber:
    def __init__(self, cfg: VisionConfig) -> None:
        self.cfg = cfg

    @property
    def enabled(self) -> bool:
        return self.cfg.provider != "none"

    def describe(self, path: str, prompt: str | None = None, max_tokens: int = 120) -> str:
        """描述一张图。

        prompt / max_tokens 可以覆盖默认值：表情包打标签要用更细的提示词
        （要抄图上文字、要认人），默认那句"40 字以内"会把文案全丢掉。
        """
        if not self.enabled:
            return ""
        source = Path(path)
        if not source.exists():
            raise MediaError(f"图片文件不存在：{path}")
        size_mb = source.stat().st_size / (1024 * 1024)
        if size_mb > self.cfg.max_image_mb:
            raise MediaError(f"图片太大（{size_mb:.1f}MB > {self.cfg.max_image_mb}MB）")

        data, mime = self._prepare(source)
        image_b64 = base64.b64encode(data).decode("ascii")
        if self.cfg.provider == "ollama":
            text = self._describe_ollama(image_b64, prompt or self.cfg.prompt, max_tokens)
        else:
            text = self._describe_openai(image_b64, mime, prompt or self.cfg.prompt, max_tokens)
        return self._clean(text)

    def _prepare(self, source: Path) -> tuple[bytes, str]:
        """IM原图动辄 5MB，先缩到 max_side 再送模型；没装 Pillow 就原样发。"""
        raw = source.read_bytes()
        mime = _MIME.get(source.suffix.lower(), "image/jpeg")
        if self.cfg.max_side <= 0:
            return raw, mime
        try:
            from PIL import Image  # noqa: PLC0415
        except ImportError:
            return raw, mime
        try:
            with Image.open(BytesIO(raw)) as image:
                image = image.convert("RGB")
                image.thumbnail((self.cfg.max_side, self.cfg.max_side))
                buffer = BytesIO()
                image.save(buffer, format="JPEG", quality=85)
            return buffer.getvalue(), "image/jpeg"
        except Exception:  # noqa: BLE001 - 缩图失败就退回原图
            log.debug("缩图失败，改用原图", exc_info=True)
            return raw, mime

    # ------------------------------------------------------------------ 后端

    def _describe_ollama(self, image_b64: str, prompt: str, max_tokens: int = 120) -> str:
        url = self.cfg.base_url.rstrip("/") + "/api/generate"
        payload = {
            "model": self.cfg.model,
            "prompt": prompt,
            "images": [image_b64],
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": max_tokens},
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.cfg.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")[:300]
            if exc.code == 404:
                raise MediaError(
                    f"Ollama 里没有模型 {self.cfg.model}。执行：ollama pull {self.cfg.model}"
                ) from exc
            raise MediaError(f"Ollama HTTP {exc.code}：{detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise MediaError(f"连不上 Ollama（{self.cfg.base_url}）：{exc}") from exc

        text = str(body.get("response") or "").strip()
        if not text:
            raise MediaError("Ollama 返回空描述")
        return text

    def _describe_openai(self, image_b64: str, mime: str, prompt: str, max_tokens: int = 200) -> str:
        if not self.cfg.api_key:
            raise MediaError("vision.provider=openai 但没有配置 vision.api_key")
        url = self.cfg.base_url.rstrip("/")
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
        payload = {
            "model": self.cfg.model,
            "max_tokens": max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
                    ],
                }
            ],
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.cfg.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.cfg.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")[:300]
            raise MediaError(f"视觉 API HTTP {exc.code}：{detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise MediaError(f"视觉 API 连接失败：{exc}") from exc

        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise MediaError(f"视觉 API 返回格式异常：{str(body)[:200]}") from exc

        if isinstance(text, list):  # 有些兼容层返回分段内容
            text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
        return str(text).strip()

    # ------------------------------------------------------------------ 清理

    def _clean(self, text: str) -> str:
        text = _WS.sub(" ", str(text)).strip().strip("\"“”")
        if len(text) > 120:
            text = text[:120].rstrip() + "…"
        return text
