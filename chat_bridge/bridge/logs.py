# -*- coding: utf-8 -*-
"""日志配置：控制台 + 可选滚动文件，统一 UTF-8。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import LoggingConfig

LOGGER_NAME = "wxbridge"


def _quiet_stdio() -> None:
    """输出被重定向（DSH / 文件）时用 UTF-8，避免中文变乱码；控制台不动。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def setup_logging(cfg: LoggingConfig) -> logging.Logger:
    _quiet_stdio()
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, cfg.level, logging.INFO))
    logger.propagate = False

    # 重复调用（比如测试里）时先清掉旧 handler，避免日志重复
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(threadName)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if cfg.console:
        stream = logging.StreamHandler(stream=sys.stdout)
        stream.setFormatter(formatter)
        stream.setLevel(getattr(logging, cfg.level, logging.INFO))
        logger.addHandler(stream)

    if cfg.file:
        path = Path(cfg.file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path,
            maxBytes=max(0, cfg.max_bytes),
            backupCount=max(0, cfg.backup_count),
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(getattr(logging, cfg.level, logging.INFO))
        logger.addHandler(file_handler)

    return logger


def get_logger(name: str = "") -> logging.Logger:
    """拿子 logger，名字统一挂在 wxbridge 下。"""
    if not name:
        return logging.getLogger(LOGGER_NAME)
    return logging.getLogger(f"{LOGGER_NAME}.{name}")
