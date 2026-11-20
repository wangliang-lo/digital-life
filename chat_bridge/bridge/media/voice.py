# -*- coding: utf-8 -*-
"""语音转文字。

好消息：ntchat 收到语音时，`data["mp3_file"]` 已经是转好的 mp3，
所以**不需要** silk 解码器、也不需要单独装 ffmpeg（faster-whisper 自带 PyAV）。

两种后端：
    faster-whisper  本地，首次用会自动下载模型（small ≈ 500MB）
    openai          任意 OpenAI 兼容的 /audio/transcriptions 接口
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from ..config import ASRConfig
from ..logs import get_logger

log = get_logger("media.voice")


class MediaError(RuntimeError):
    """媒体处理失败（缺依赖、文件不存在、模型报错）。"""


class VoiceTranscriber:
    def __init__(self, cfg: ASRConfig) -> None:
        self.cfg = cfg
        self._model = None
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.cfg.provider != "none"

    @property
    def ready(self) -> bool:
        """大致判断"能不能用"：本地看包是否装了，远端看有没有 key。"""
        if not self.enabled:
            return False
        if self.cfg.provider == "faster-whisper":
            try:
                import faster_whisper  # noqa: F401,PLC0415
            except ImportError:
                return False
            return True
        return bool(self.cfg.api_key)

    def transcribe(self, path: str) -> str:
        if not self.enabled:
            return ""
        source = Path(path)
        if not source.exists():
            raise MediaError(f"语音文件不存在：{path}")
        if source.stat().st_size == 0:
            raise MediaError(f"语音文件是空的：{path}")

        if self.cfg.provider == "faster-whisper":
            return self._transcribe_local(source)
        return self._transcribe_remote(source)

    # ------------------------------------------------------------------ 本地

    def _transcribe_local(self, path: Path) -> str:
        try:
            from faster_whisper import WhisperModel  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - 依赖环境
            raise MediaError(
                "未安装 faster-whisper。执行：pip install faster-whisper\n"
                "（它自带 PyAV，不需要另外装 ffmpeg）"
            ) from exc

        with self._lock:
            if self._model is None:
                log.info(
                    "首次加载 whisper 模型 %s（device=%s, compute_type=%s），可能会下载模型……",
                    self.cfg.local_model, self.cfg.device, self.cfg.compute_type,
                )
                self._model = WhisperModel(
                    self.cfg.local_model,
                    device=self.cfg.device,
                    compute_type=self.cfg.compute_type,
                    download_root=self.cfg.download_root or None,
                )

        segments, _info = self._model.transcribe(
            str(path),
            language=self.cfg.language or None,
            beam_size=1,
            vad_filter=True,
            initial_prompt="以下是普通话的日常对话。",
        )
        text = "".join(segment.text for segment in segments).strip()
        if not text:
            raise MediaError("whisper 没识别出内容")
        return text

    # ------------------------------------------------------------------ 远端

    def _transcribe_remote(self, path: Path) -> str:
        if not self.cfg.api_key:
            raise MediaError("asr.provider=openai 但没有配置 asr.api_key")

        boundary = "----wxbridge" + uuid.uuid4().hex
        parts: list[bytes] = []

        def field(name: str, value: str) -> None:
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
            )

        def file_field(name: str, filename: str, data: bytes, ctype: str) -> None:
            head = (
                f"--{boundary}\r\n"
                f"Content-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n"
                f"Content-Type: {ctype}\r\n\r\n"
            ).encode()
            parts.append(head + data + b"\r\n")

        field("model", self.cfg.api_model)
        if self.cfg.language:
            field("language", self.cfg.language)
        file_field("file", path.name, path.read_bytes(), "audio/mpeg")
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)

        url = self.cfg.base_url.rstrip("/") + "/audio/transcriptions"
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        request.add_header("Authorization", f"Bearer {self.cfg.api_key}")

        try:
            with urllib.request.urlopen(request, timeout=self.cfg.timeout) as response:
                raw = response.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")[:300]
            raise MediaError(f"ASR HTTP {exc.code}：{detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise MediaError(f"ASR 连接失败：{exc}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MediaError(f"ASR 返回不是 JSON：{raw[:200]}") from exc
        text = str(data.get("text") or "").strip()
        if not text:
            raise MediaError("ASR 返回空文本")
        return text
