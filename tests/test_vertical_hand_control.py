from __future__ import annotations

import math
import statistics

import pytest

from vertical_hand_control import VerticalHandController


CONFIG = {"point": "right_wrist", "range_y": 0.18, "deadzone": 0.10}


def _point(y: float, score: float = 1.0) -> dict:
    return {"x": 0.5, "y": y, "z": 0.0, "score": score}


def _pose(wrist_y: float = 0.50, shoulder_y: float = 0.32) -> dict:
    return {
        "right_wrist": _point(wrist_y),
        "right_shoulder": _point(shoulder_y),
    }


class _FrozenInlineReference:
    """Independent copy of the pre-extraction vertical signal state."""

    def __init__(self) -> None:
        self.anchor_y = None
        self.anchor_rel_y = None
        self.anchor_samples = []
        self.filtered = 0.0
        self.filter_last_at = 0.0

    def reset(self, now: float | None = None) -> None:
        self.anchor_y = None
        self.anchor_rel_y = None
        self.anchor_samples = []
        self.filtered = 0.0
        self.filter_last_at = 0.0 if now is None else float(now)

    def update(self, pose_map: dict, now: float, config: dict) -> dict:
        wrist = pose_map.get(str(config.get("point", "right_wrist")))
        shoulder = pose_map.get("right_shoulder")

        def score(point: dict | None) -> float:
            if not isinstance(point, dict):
                return 0.0
            value = point.get("score", point.get("visibility", point.get("presence", 0.0)))
            try:
                result = float(value)
            except (TypeError, ValueError):
                return 0.0
            return result if math.isfinite(result) else 0.0

        good = (
            wrist and shoulder
            and score(wrist) >= 0.48 and score(shoulder) >= 0.48
            and math.isfinite(float(wrist.get("y", math.nan)))
            and math.isfinite(float(shoulder.get("y", math.nan)))
        )
        if not good:
            self.filtered = 0.0
            self.filter_last_at = now
            return self._snapshot()

        rel_y = float(wrist["y"]) - float(shoulder["y"])
        self.anchor_y = float(wrist["y"])
        if self.anchor_rel_y is None:
            self.anchor_samples.append(rel_y)
            if len(self.anchor_samples) >= 4:
                self.anchor_rel_y = float(statistics.median(self.anchor_samples))
                self.filtered = 0.0
                self.filter_last_at = now
            return self._snapshot()

        travel = max(0.065, float(config.get("range_y", 0.18)) * 0.72)
        raw = max(-1.0, min(1.0, (rel_y - self.anchor_rel_y) / travel))
        deadzone = max(0.04, min(0.22, float(config.get("deadzone", 0.08))))
        if abs(raw) > deadzone:
            t = (abs(raw) - deadzone) / max(1e-6, 1.0 - deadzone)
            target = math.copysign(t ** 1.12, raw)
        else:
            target = 0.0

        dt = max(1.0 / 120.0, min(0.10, now - (self.filter_last_at or now)))
        self.filter_last_at = now
        alpha = 1.0 - math.exp(-dt / 0.042)
        filtered = self.filtered + alpha * (target - self.filtered)
        max_step = 7.0 * dt
        filtered = self.filtered + max(-max_step, min(max_step, filtered - self.filtered))
        if abs(filtered) < 0.012 and target == 0.0:
            filtered = 0.0
        self.filtered = max(-1.0, min(1.0, filtered))
        return self._snapshot(self.filtered)

    def _snapshot(self, output: float = 0.0) -> dict:
        return {
            "output": float(output),
            "anchor_y": self.anchor_y,
            "anchor_rel_y": self.anchor_rel_y,
            "anchor_samples": len(self.anchor_samples),
            "filtered": float(self.filtered),
            "filter_last_at": float(self.filter_last_at),
        }


def _assert_same(actual: dict, expected: dict) -> None:
    assert actual.keys() == expected.keys()
    for key in actual:
        if isinstance(actual[key], float):
            assert actual[key] == pytest.approx(expected[key], abs=1e-12)
        else:
            assert actual[key] == expected[key]


def test_vertical_hand_matches_frozen_inline_signal_and_gate_reentry():
    controller = VerticalHandController()
    reference = _FrozenInlineReference()
    sequence = [
        (0.00, _pose()),
        (0.03, _pose()),
        (0.06, _pose()),
        (0.09, _pose()),  # fourth sample establishes the relative center
        (0.12, _pose(0.49)),  # inside the deadzone
        (0.15, _pose(0.30)),  # upward movement
        (0.18, _pose(0.72)),  # downward movement
        (0.21, _pose(1.35)),  # teleport spike is slew-limited
        (0.24, {}),  # tracking loss releases immediately
        (0.27, _pose(0.50)),
    ]
    for now, pose in sequence:
        _assert_same(controller.update(pose, now, CONFIG), reference.update(pose, now, CONFIG))

    # Closing and reopening the look gate is represented by the kernel calling
    # reset(now).  A new four-frame center must be required before output.
    controller.reset(1.00)
    reference.reset(1.00)
    _assert_same(controller.status() | {"version": None}, reference._snapshot() | {"version": None})
    for now in (1.03, 1.06, 1.09, 1.12):
        pose = _pose()
        _assert_same(controller.update(pose, now, CONFIG), reference.update(pose, now, CONFIG))
    pose = _pose(0.30)
    _assert_same(controller.update(pose, 1.15, CONFIG), reference.update(pose, 1.15, CONFIG))
