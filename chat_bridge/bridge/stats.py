# -*- coding: utf-8 -*-
"""线程安全的计数器，用于 /status 观测。"""

from __future__ import annotations

import threading
from collections import Counter
from typing import Any


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Counter[str] = Counter()
        self._last: dict[str, Any] = {}

    def inc(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[key] += amount

    def set_last(self, key: str, value: Any) -> None:
        with self._lock:
            self._last[key] = value

    def get_last(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._last.get(key, default)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            data: dict[str, Any] = dict(self._counters)
            data["last"] = dict(self._last)
            return data

    def merge(self, other: dict[str, int]) -> None:
        with self._lock:
            self._counters.update(other)

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
