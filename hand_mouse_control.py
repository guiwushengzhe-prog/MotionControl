"""Steer the mouse with one hand, gated by a closed fist.

The pose model gives four points per hand -- wrist, thumb, index, pinky -- and
nothing else.  There are no finger joints, so "is this a fist" has to come from
how far the fingertips sit from the wrist rather than from joint angles.  That
is coarser than a dedicated hand model, but a hand model is a second inference
pass and an extra 8 MB in the APK, and the pose model is already running.

Two things make the coarse measure workable:

*Normalise by the forearm.*  Raw pixel distance is meaningless -- it shrinks
with distance from the camera and changes as the arm extends.  Elbow-to-wrist
is on the same limb, so it scales identically, and it does not change when the
fingers curl.  hand_anchor.py already uses the same reference.

*Hysteresis.*  A single threshold sitting near the measured value makes the
fist chatter open/closed several times a second, which would drop the mouse
mid-movement.  Closing and opening therefore have separate thresholds.

Movement is relative, not absolute.  Closing the fist captures an anchor where
the hand happens to be, and the offset from there drives speed -- the same
shape as head control, and the reason the hand can be re-centred by opening and
closing again, like lifting a mouse off the desk.

The thresholds are defaults, not truths: they were reasoned from the geometry,
not measured against a population of hands.  ``status()`` therefore reports the
live ``spread`` value so a user whose hand does not match can watch the number
and set their own.
"""

from __future__ import annotations

import math

HANDS = ("left", "right")

DEFAULT_CONFIG = {
    "enabled": False,
    "hand": "right",
    # Fraction of forearm length.  Below `fist_close` counts as closed, above
    # `fist_open` as open; between them the previous state persists.
    "fist_close": 0.30,
    "fist_open": 0.40,
    # Offset from the anchor, as a fraction of forearm length, that produces
    # full-speed movement.
    "range": 0.55,
    "deadzone": 0.12,
    "sensitivity": 70.0,
    "invert_x": False,
    # A fingertip below this visibility is ignored; too few usable tips means
    # "unknown", not "open".
    "min_visibility": 0.35,
    "min_tips": 2,
}

_TIPS = ("thumb", "index", "pinky")


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


def _point(pose_map: dict, name: str, min_visibility: float) -> dict | None:
    point = pose_map.get(name)
    if not isinstance(point, dict):
        return None
    score = point.get("score", point.get("visibility", 0.0))
    try:
        if float(score) < min_visibility:
            return None
    except (TypeError, ValueError):
        return None
    if not all(isinstance(point.get(axis), (int, float)) for axis in ("x", "y")):
        return None
    return point


def _distance(a: dict, b: dict) -> float:
    return math.hypot(float(a["x"]) - float(b["x"]), float(a["y"]) - float(b["y"]))


def merge_config(current: dict | None, updates: dict | None) -> dict:
    """Validate an update and fold it into a full config."""
    config = dict(DEFAULT_CONFIG)
    config.update(current or {})
    for key, value in (updates or {}).items():
        if key not in DEFAULT_CONFIG:
            raise ValueError(f"unknown hand mouse setting: {key}")
        if key in {"enabled", "invert_x"}:
            config[key] = bool(value)
        elif key == "hand":
            if value not in HANDS:
                raise ValueError("hand must be left or right")
            config[key] = value
        elif key == "min_tips":
            config[key] = int(_clamp(float(value), 1, 3))
        else:
            config[key] = float(value)
    if config["fist_open"] <= config["fist_close"]:
        # Without a gap the fist flickers, which drops the pointer mid-move.
        raise ValueError("松开阈值必须大于握拳阈值，否则握拳状态会抖动")
    config["deadzone"] = _clamp(config["deadzone"], 0.0, 0.9)
    config["range"] = max(0.05, config["range"])
    config["sensitivity"] = _clamp(config["sensitivity"], 1.0, 200.0)
    config["min_visibility"] = _clamp(config["min_visibility"], 0.0, 1.0)
    return config


class HandMouseController:
    """One hand, one fist gate, two axes of relative movement."""

    def __init__(self) -> None:
        self.config = dict(DEFAULT_CONFIG)
        self.reset()

    def reset(self, now: float | None = None) -> None:
        self.engaged = False
        self.anchor: tuple[float, float] | None = None
        self.spread: float | None = None
        self.output = (0.0, 0.0)
        self.offset = (0.0, 0.0)
        self.tips_seen = 0
        self.reason = "idle"

    def configure(self, updates: dict | None) -> dict:
        self.config = merge_config(self.config, updates)
        # Any settings change invalidates the captured anchor: keeping it would
        # make the pointer jump the moment the next fist closes.
        self.reset()
        return self.status()

    # -- measurement -------------------------------------------------------

    def measure_spread(self, pose_map: dict, hand: str) -> tuple[float | None, int]:
        """Fingertip spread as a fraction of forearm length, and tips used."""
        min_visibility = float(self.config["min_visibility"])
        wrist = _point(pose_map, f"{hand}_wrist", min_visibility)
        elbow = _point(pose_map, f"{hand}_elbow", min_visibility)
        if wrist is None or elbow is None:
            return None, 0
        forearm = _distance(elbow, wrist)
        if forearm <= 1e-6:
            return None, 0
        distances = []
        for tip in _TIPS:
            point = _point(pose_map, f"{hand}_{tip}", min_visibility)
            if point is not None:
                distances.append(_distance(wrist, point) / forearm)
        if len(distances) < int(self.config["min_tips"]):
            return None, len(distances)
        return sum(distances) / len(distances), len(distances)

    # -- update ------------------------------------------------------------

    def update(self, pose_map: dict, now: float) -> dict:
        config = self.config
        if not config["enabled"]:
            self.reset()
            self.reason = "disabled"
            return self.status()

        hand = str(config["hand"])
        spread, tips = self.measure_spread(pose_map, hand)
        self.spread = spread
        self.tips_seen = tips

        if spread is None:
            # Losing sight of the hand must release, not freeze: a stuck
            # engagement would keep driving the pointer from a stale anchor.
            if self.engaged:
                self._release("lost")
            else:
                self.reason = "lost"
            return self.status()

        was_engaged = self.engaged
        if self.engaged:
            if spread > float(config["fist_open"]):
                self._release("opened")
                return self.status()
        elif spread < float(config["fist_close"]):
            self.engaged = True

        if not self.engaged:
            self.output = (0.0, 0.0)
            self.offset = (0.0, 0.0)
            self.reason = "open"
            return self.status()

        wrist = _point(pose_map, f"{hand}_wrist", float(config["min_visibility"]))
        elbow = _point(pose_map, f"{hand}_elbow", float(config["min_visibility"]))
        if wrist is None or elbow is None:
            self._release("lost")
            return self.status()
        forearm = max(1e-6, _distance(elbow, wrist))

        if not was_engaged or self.anchor is None:
            self.anchor = (float(wrist["x"]), float(wrist["y"]))
            self.output = (0.0, 0.0)
            self.offset = (0.0, 0.0)
            self.reason = "engaged"
            return self.status()

        # Offsets are in forearm lengths so the feel does not change when the
        # player stands closer to or further from the camera.
        dx = (float(wrist["x"]) - self.anchor[0]) / forearm
        dy = (float(wrist["y"]) - self.anchor[1]) / forearm
        self.offset = (round(dx, 4), round(dy, 4))
        gain = float(config["sensitivity"]) / 100.0
        x = self._shape(dx) * gain
        y = self._shape(dy) * gain
        if config["invert_x"]:
            x = -x
        self.output = (round(_clamp(x, -1.0, 1.0), 4), round(_clamp(y, -1.0, 1.0), 4))
        self.reason = "moving"
        return self.status()

    def _shape(self, offset: float) -> float:
        """Deadzone, then a linear ramp to full speed at `range`."""
        span = float(self.config["range"])
        deadzone = float(self.config["deadzone"]) * span
        magnitude = abs(offset)
        if magnitude <= deadzone:
            return 0.0
        usable = max(1e-6, span - deadzone)
        return math.copysign(_clamp((magnitude - deadzone) / usable, 0.0, 1.0), offset)

    def _release(self, reason: str) -> None:
        self.engaged = False
        self.anchor = None
        self.output = (0.0, 0.0)
        self.offset = (0.0, 0.0)
        self.reason = reason

    # -- reporting ---------------------------------------------------------

    def status(self) -> dict:
        return {
            "enabled": bool(self.config["enabled"]),
            "hand": str(self.config["hand"]),
            "engaged": bool(self.engaged),
            "state": self.reason,
            # Surfaced so a user whose hand does not match the default
            # thresholds can watch the number and set their own.
            "spread": None if self.spread is None else round(float(self.spread), 4),
            "tips_seen": int(self.tips_seen),
            "offset_x": self.offset[0],
            "offset_y": self.offset[1],
            "output_x": self.output[0],
            "output_y": self.output[1],
            "config": dict(self.config),
        }
