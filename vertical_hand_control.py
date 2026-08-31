"""Body-relative hand vertical-look control.

This module owns the existing right-wrist vertical-look signal only.  The
look gate, body-zone state, output backend, and head-control path remain in
``control_kernel``.  Keeping the signal state here makes later experiments
possible without changing the production kernel's other input paths.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from typing import Any


def _clamp(value: Any, low: float, high: float) -> float:
    return max(float(low), min(float(high), float(value)))


def _score(point: dict[str, Any]) -> float:
    """Return the same confidence score used by the kernel's pose helpers."""
    value = point.get("score", point.get("visibility", point.get("presence", 0.0)))
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


class VerticalHandController:
    """Compute the existing gated, right-wrist vertical signal.

    ``update`` returns a small state snapshot so the kernel can mirror its
    legacy diagnostic attributes.  The numeric constants intentionally match
    the former inline implementation: four samples for the relative center,
    72% of the configured travel, the existing deadzone/curve, a 42 ms
    one-pole filter, and the existing 7.0*dt slew limit.
    """

    VERSION = "vertical-hand-v1"
    _MIN_SCORE = 0.48
    _ANCHOR_SAMPLE_COUNT = 4
    _TRAVEL_SCALE = 0.72
    _FILTER_TAU = 0.042
    _MAX_STEP_PER_SECOND = 7.0

    def __init__(self) -> None:
        self.anchor_y: float | None = None
        self.anchor_rel_y: float | None = None
        self.anchor_samples: deque[float] = deque(maxlen=5)
        self.filtered = 0.0
        self.filter_last_at = 0.0

    def reset(self, now: float | None = None) -> None:
        """Release the signal and discard the hand center."""
        self.anchor_y = None
        self.anchor_rel_y = None
        self.anchor_samples.clear()
        self.filtered = 0.0
        self.filter_last_at = 0.0 if now is None else float(now)

    def _snapshot(self, output: float = 0.0) -> dict[str, Any]:
        return {
            "output": float(output),
            "anchor_y": self.anchor_y,
            "anchor_rel_y": self.anchor_rel_y,
            "anchor_samples": len(self.anchor_samples),
            "filtered": float(self.filtered),
            "filter_last_at": float(self.filter_last_at),
        }

    def status(self) -> dict[str, Any]:
        """Return diagnostics without mutating signal state."""
        return {"version": self.VERSION, **self._snapshot(self.filtered)}

    def update(
        self,
        pose_map: dict[str, dict[str, Any]],
        now: float,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update from one normalized pose and return output/diagnostics."""
        cfg = config or {}
        point_name = str(cfg.get("point", "right_wrist"))
        wrist = pose_map.get(point_name)
        shoulder = pose_map.get("right_shoulder")
        good = bool(
            wrist and shoulder
            and _score(wrist) >= self._MIN_SCORE
            and _score(shoulder) >= self._MIN_SCORE
            and math.isfinite(float(wrist.get("y", math.nan)))
            and math.isfinite(float(shoulder.get("y", math.nan)))
        )
        if not good:
            # Tracking loss is fail-safe.  Do not keep the last camera Y.
            self.filtered = 0.0
            self.filter_last_at = float(now)
            return self._snapshot()

        rel_y = float(wrist["y"]) - float(shoulder["y"])
        self.anchor_y = float(wrist["y"])
        if self.anchor_rel_y is None:
            self.anchor_samples.append(rel_y)
            if len(self.anchor_samples) >= self._ANCHOR_SAMPLE_COUNT:
                self.anchor_rel_y = float(statistics.median(self.anchor_samples))
                self.filtered = 0.0
                self.filter_last_at = float(now)
            return self._snapshot()

        # Existing scene layouts use body-relative wrist travel.  Retain the
        # authored range while applying the old 72% signal scale.
        travel = max(0.065, float(cfg.get("range_y", 0.18)) * self._TRAVEL_SCALE)
        raw = _clamp((rel_y - self.anchor_rel_y) / travel, -1.0, 1.0)
        deadzone = _clamp(cfg.get("deadzone", 0.08), 0.04, 0.22)
        if abs(raw) > deadzone:
            t = (abs(raw) - deadzone) / max(1e-6, 1.0 - deadzone)
            target = math.copysign(t ** 1.12, raw)
        else:
            target = 0.0

        current_time = float(now)
        dt = max(
            1.0 / 120.0,
            min(0.10, current_time - (self.filter_last_at or current_time)),
        )
        self.filter_last_at = current_time
        alpha = 1.0 - math.exp(-dt / self._FILTER_TAU)
        filtered = self.filtered + alpha * (target - self.filtered)
        max_step = self._MAX_STEP_PER_SECOND * dt
        filtered = self.filtered + _clamp(
            filtered - self.filtered,
            -max_step,
            max_step,
        )
        if abs(filtered) < 0.012 and target == 0.0:
            filtered = 0.0
        self.filtered = _clamp(filtered, -1.0, 1.0)
        return self._snapshot(self.filtered)
