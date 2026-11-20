"""情感动量引擎：Joy / Hurt / Impulse。"""
from __future__ import annotations

import functools
import json
import math
import random
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Sequence

from config import (
    BASE_WALK_PER_HOUR, DESENSITIZATION_CAP, DESENSITIZATION_RATE,
    EVENT_RULES_LIST, HURT_BASELINE, HURT_SPIKE, IMPULSE_BASELINE,
    IMPULSE_GATE, JOY_BASELINE, JOY_SPIKE, MEAN_REVERSION, MOMENTUM_DRAG,
    MOMENTUM_GAIN, MOMENTUM_LIMIT, NIGHT_WALK_MULTIPLIER,
    PULSE_WINDOW_MINUTES, RATE_SPIKE, TRAUMA_WINDOW_MINUTES,
)
from utils import clamp, dt_to_str, is_night, is_soft_tease, match_keyword, str_to_dt

def _emotion_locked(method: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(method)
    def wrapped(self: "EmotionEngine", *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapped

@dataclass
class EmotionPulse:
    joy_delta: float
    hurt_delta: float
    impulse_delta: float
    window_minutes: float
    rate: float
    profound: bool
    label: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "joy_delta": round(self.joy_delta, 2),
            "hurt_delta": round(self.hurt_delta, 2),
            "impulse_delta": round(self.impulse_delta, 2),
            "window_minutes": round(self.window_minutes, 2),
            "rate": round(self.rate, 2),
            "profound": self.profound,
            "label": self.label,
        }

class EmotionEngine:
    """
    情绪三维引擎：Joy / Hurt / Impulse。

    事件不直接改数值，而是提供 target 和 intensity：
        distance = target - current
        momentum = momentum * 0.8 + distance * intensity * 0.1
        current += momentum

    同一事件重复出现时，intensity 会乘 0.8^n（心理脱敏）；
    「小傻瓜」这类亲昵责备走单独规则，不会被误判成辱骂。
    """

    def __init__(
        self,
        joy: float = JOY_BASELINE,
        hurt: float = HURT_BASELINE,
        impulse: float = IMPULSE_BASELINE,
        rng: random.Random | None = None,
        momentum: dict[str, float] | None = None,
        exposure: dict[str, int] | None = None,
        joy_baseline: float = JOY_BASELINE,
        hurt_baseline: float = HURT_BASELINE,
        samples: Sequence[tuple[datetime, float, float]] | None = None,
        lock: threading.RLock | None = None,
    ) -> None:
        self._lock = lock or threading.RLock()
        self.joy = clamp(joy)
        self.hurt = clamp(hurt)
        self.impulse = clamp(impulse)
        # 享乐适应（hedonic adaptation）：基线不是常数，会被长期创伤永久改写
        self.joy_baseline = clamp(joy_baseline)
        self.hurt_baseline = clamp(hurt_baseline)
        self.rng = rng or random.Random()
        self.momentum: dict[str, float] = {"joy": 0.0, "hurt": 0.0, "impulse": 0.0}
        if momentum:
            for key in self.momentum:
                self.momentum[key] = max(
                    -MOMENTUM_LIMIT,
                    min(MOMENTUM_LIMIT, float(momentum.get(key, 0.0))),
                )
        self.exposure: dict[str, int] = dict(exposure or {})
        self.last_soft_tease = False
        self._samples: list[tuple[datetime, float, float]] = list(samples or [])

    @_emotion_locked
    def snapshot(self) -> dict[str, float]:
        return {
            "joy": round(self.joy, 2),
            "hurt": round(self.hurt, 2),
            "impulse": round(self.impulse, 2),
        }

    @_emotion_locked
    def momentum_snapshot(self) -> dict[str, float]:
        return {key: round(value, 4) for key, value in self.momentum.items()}

    def samples_snapshot(self, now: datetime) -> list[list[Any]]:
        """只持久化最近 2 小时的样本，保证创伤检测跨重启连续。"""
        cutoff = now - timedelta(hours=2)
        return [
            [dt_to_str(ts), round(joy, 2), round(hurt, 2)]
            for ts, joy, hurt in self._samples
            if ts >= cutoff
        ]

    def mood_text(self) -> str:
        if self.impulse >= IMPULSE_GATE:
            return "上头，说话不过脑子"
        if self.hurt >= 60:
            return "受伤，防备心很重"
        if self.joy >= 70:
            return "轻快，愿意多说几句"
        if self.joy < 35:
            return "低落，提不起劲"
        return "平静，但心里有点东西"

    def describe(self) -> str:
        return (
            f"Joy(喜)={self.joy:.1f}  Hurt(痛)={self.hurt:.1f}  "
            f"Impulse(冲动)={self.impulse:.1f}\n"
            f"动量 Joy={self.momentum['joy']:+.2f}  "
            f"Hurt={self.momentum['hurt']:+.2f}  "
            f"Impulse={self.momentum['impulse']:+.2f}\n"
            f"基线 Joy={self.joy_baseline:.1f}  Hurt={self.hurt_baseline:.1f}\n"
            f"感受：{self.mood_text()}"
        )

    @_emotion_locked
    def _note(self, now: datetime) -> None:
        self._samples.append((now, self.joy, self.hurt))
        cutoff = now - timedelta(hours=2)
        self._samples = [item for item in self._samples if item[0] >= cutoff]
        if len(self._samples) > 500:
            self._samples = self._samples[-500:]

    @_emotion_locked
    def drift(self, hours: float, now: datetime | None = None) -> dict[str, Any]:
        """时间游走：夜间幅度 ×1.5，叠加一点点均值回归。"""
        now = now or datetime.now()
        hours = max(0.0, min(float(hours), 72.0))
        night = is_night(now.hour)
        before = self.snapshot()
        if hours <= 0.0:
            return {
                "hours": 0.0,
                "night": night,
                "multiplier": NIGHT_WALK_MULTIPLIER if night else 1.0,
                "before": before,
                "after": before,
                "delta": {"joy": 0.0, "hurt": 0.0, "impulse": 0.0},
            }

        amplitude = BASE_WALK_PER_HOUR * math.sqrt(min(hours, 48.0))
        if night:
            amplitude *= NIGHT_WALK_MULTIPLIER
        pull_factor = MEAN_REVERSION * min(hours, 24.0) / 6.0

        def step(value: float, baseline: float) -> float:
            noise = self.rng.gauss(0.0, amplitude)
            pull = (baseline - value) * pull_factor
            return clamp(value + noise + pull)

        self.joy = step(self.joy, self.joy_baseline)
        self.hurt = step(self.hurt, self.hurt_baseline)
        self.impulse = step(self.impulse, IMPULSE_BASELINE)

        # 时间会磨掉动量，离线越久，上一次争吵的冲劲越弱
        damping = MOMENTUM_DRAG ** min(hours, 12.0)
        for key in self.momentum:
            self.momentum[key] *= damping
        self._note(now)

        after = self.snapshot()
        return {
            "hours": round(hours, 2),
            "night": night,
            "multiplier": NIGHT_WALK_MULTIPLIER if night else 1.0,
            "before": before,
            "after": after,
            "delta": {
                "joy": round(after["joy"] - before["joy"], 2),
                "hurt": round(after["hurt"] - before["hurt"], 2),
                "impulse": round(after["impulse"] - before["impulse"], 2),
            },
        }

    @_emotion_locked
    def _momentum_step(self, key: str, target: float, intensity: float) -> float:
        current = float(getattr(self, key))
        distance = float(target) - current
        self.momentum[key] = (
            self.momentum[key] * MOMENTUM_DRAG
            + distance * float(intensity) * MOMENTUM_GAIN
        )
        delta = self.momentum[key]
        setattr(self, key, clamp(current + delta))
        return delta

    @_emotion_locked
    def apply_event(
        self,
        text: str,
        now: datetime | None = None,
    ) -> tuple[list[dict[str, Any]], EmotionPulse | None]:
        """
        扫描输入并推动情绪。返回 (事件列表, 动量脉冲)。

        防误触优先：如果只是「小傻瓜/笨蛋」这类亲昵责备，跳过「被冒犯」，
        只走低强度的「亲昵责备」，不会误触创伤。
        """
        now = now or datetime.now()
        before = self.snapshot()
        events: list[dict[str, Any]] = []
        soft_tease = is_soft_tease(text)
        self.last_soft_tease = soft_tease

        for rule in EVENT_RULES_LIST:
            name = str(rule["name"])
            if name == "被冒犯" and soft_tease:
                continue
            if name == "亲昵责备" and not soft_tease:
                continue

            matched = [kw for kw in rule["any"] if match_keyword(kw, text)]
            if not matched:
                continue

            # 心理脱敏：同一事件每重复一次，强度乘 0.8
            count = int(self.exposure.get(name, 0))
            factor = DESENSITIZATION_RATE ** min(count, DESENSITIZATION_CAP)
            self.exposure[name] = count + 1

            intensity = float(rule.get("intensity", 0.0)) * factor
            targets = dict(rule.get("targets", {}))
            deltas: dict[str, float] = {}
            for key in ("joy", "hurt", "impulse"):
                if key in targets:
                    deltas[key] = self._momentum_step(key, float(targets[key]), intensity)

            events.append(
                {
                    "name": name,
                    "matched": matched,
                    "targets": targets,
                    "original_intensity": round(float(rule.get("intensity", 0.0)), 4),
                    "intensity": round(intensity, 4),
                    "desensitization": round(factor, 4),
                    "delta": {key: round(value, 4) for key, value in deltas.items()},
                    "momentum": self.momentum_snapshot(),
                }
            )

        if not events:
            return [], None

        self._note(now)
        pulse = self._build_pulse(before, now)
        return events, pulse

    def recent_hurt_rise(self, now: datetime, minutes: int = TRAUMA_WINDOW_MINUTES) -> float:
        """短时间内 Hurt 的涨幅；用于识别连续刺激造成的创伤。"""
        cutoff = now - timedelta(minutes=minutes)
        window = [sample for sample in self._samples if sample[0] >= cutoff]
        if len(window) < 2:
            return 0.0
        hurts = [sample[2] for sample in window]
        return max(0.0, max(hurts) - min(hurts))

    def _build_pulse(self, before: dict[str, float], now: datetime) -> EmotionPulse:
        cutoff = now - timedelta(minutes=PULSE_WINDOW_MINUTES)
        window = [sample for sample in self._samples if sample[0] >= cutoff]
        if len(window) >= 2:
            prev_ts, prev_joy, prev_hurt = window[0]
            elapsed = max((now - prev_ts).total_seconds() / 3600.0, 1.0 / 60.0)
        else:
            prev_ts = now
            prev_joy, prev_hurt = before["joy"], before["hurt"]
            elapsed = 1.0 / 60.0

        joy_delta = self.joy - prev_joy
        hurt_delta = self.hurt - prev_hurt
        impulse_delta = self.impulse - before["impulse"]
        rate = max(abs(joy_delta), abs(hurt_delta)) / elapsed
        momentum_peak = max(abs(self.momentum["joy"]), abs(self.momentum["hurt"]))
        profound = (
            abs(hurt_delta) >= HURT_SPIKE
            or abs(joy_delta) >= JOY_SPIKE
            or momentum_peak >= HURT_SPIKE
            or (abs(joy_delta) + abs(hurt_delta) >= 18.0 and rate >= RATE_SPIKE)
        )
        if profound:
            label = "痛觉刻痕" if hurt_delta > 0 else "狂喜闪光"
        elif abs(hurt_delta) + abs(joy_delta) > 3:
            label = "微澜"
        else:
            label = "无波"

        return EmotionPulse(
            joy_delta=joy_delta,
            hurt_delta=hurt_delta,
            impulse_delta=impulse_delta,
            window_minutes=max((now - prev_ts).total_seconds() / 60.0, 1.0),
            rate=rate,
            profound=profound,
            label=label,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "joy": self.joy,
            "hurt": self.hurt,
            "impulse": self.impulse,
            "joy_baseline": self.joy_baseline,
            "hurt_baseline": self.hurt_baseline,
            "momentum": dict(self.momentum),
            "exposure": dict(self.exposure),
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        rng: random.Random | None = None,
        now: datetime | None = None,
        lock: threading.RLock | None = None,
    ) -> "EmotionEngine":
        parsed_samples: list[tuple[datetime, float, float]] = []
        cutoff = (now or datetime.now()) - timedelta(hours=2)
        for item in data.get("samples", []) or []:
            try:
                ts = item[0] if isinstance(item[0], datetime) else str_to_dt(str(item[0]))
                joy = float(item[1])
                hurt = float(item[2])
            except (TypeError, ValueError, IndexError, KeyError, json.JSONDecodeError):
                continue
            if ts >= cutoff:
                parsed_samples.append((ts, joy, hurt))

        return cls(
            joy=float(data.get("joy", JOY_BASELINE)),
            hurt=float(data.get("hurt", HURT_BASELINE)),
            impulse=float(data.get("impulse", IMPULSE_BASELINE)),
            rng=rng,
            momentum=dict(data.get("momentum", {})),
            exposure={key: int(value) for key, value in dict(data.get("exposure", {})).items()},
            joy_baseline=float(data.get("joy_baseline", JOY_BASELINE)),
            hurt_baseline=float(data.get("hurt_baseline", HURT_BASELINE)),
            samples=parsed_samples,
            lock=lock,
        )
