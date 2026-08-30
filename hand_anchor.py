"""Robust Pose33 hand anchors and short-lived arm-state fallback.

The production pose stream contains MediaPipe Pose33 points rather than a
continuous 21-point Hand Landmarker stream.  This adapter therefore uses the
same-side wrist as the primary observation and nearby thumb/index/pinky points
as confidence-weighted support.  Shoulder/elbow geometry is deliberately kept
outside the anchor calculation except for scale, arm-state reporting, and a
short, bounded occlusion fallback.
"""

from __future__ import annotations

import copy
import math
import statistics
import time
from typing import Any


HAND_ANCHOR_VERSION = "hand-anchor-v1"
HAND_ANCHOR_FALLBACK_TTL_S = 0.25
HAND_ANCHOR_MIN_SCORE = 0.20
HAND_ANCHOR_ZONE_SCORE = 0.42

_SIDE_NAMES = {
    "left": {
        "wrist": "left_wrist",
        "thumb": "left_thumb",
        "index": "left_index",
        "pinky": "left_pinky",
        "elbow": "left_elbow",
        "shoulder": "left_shoulder",
    },
    "right": {
        "wrist": "right_wrist",
        "thumb": "right_thumb",
        "index": "right_index",
        "pinky": "right_pinky",
        "elbow": "right_elbow",
        "shoulder": "right_shoulder",
    },
}


def _finite(value: Any, default: float = math.nan) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _score(point: dict | None) -> float:
    if not isinstance(point, dict):
        return 0.0
    value = point.get("score", point.get("visibility", point.get("presence", 0.0)))
    value = _finite(value, 0.0)
    return max(0.0, min(1.0, value))


def _raw_point(point: dict | None, minimum: float = HAND_ANCHOR_MIN_SCORE) -> dict | None:
    if not isinstance(point, dict):
        return None
    x, y, z = _finite(point.get("x")), _finite(point.get("y")), _finite(point.get("z"), 0.0)
    score = _score(point)
    if not (math.isfinite(x) and math.isfinite(y) and score >= minimum):
        return None
    return {"x": x, "y": y, "z": z, "score": score}


def _distance(a: dict, b: dict) -> float:
    return math.hypot(float(a["x"]) - float(b["x"]), float(a["y"]) - float(b["y"]))


def _weighted_median(items: list[tuple[dict, float]], key: str) -> float:
    ordered = sorted(items, key=lambda item: float(item[0][key]))
    total = sum(max(1e-6, float(weight)) for _, weight in ordered)
    half = total * 0.5
    running = 0.0
    for point, weight in ordered:
        running += max(1e-6, float(weight))
        if running >= half:
            return float(point[key])
    return float(ordered[-1][0][key])


def _blend(old: dict, new: dict, alpha: float) -> dict:
    alpha = max(0.0, min(1.0, float(alpha)))
    return {
        "x": float(old["x"]) + alpha * (float(new["x"]) - float(old["x"])),
        "y": float(old["y"]) + alpha * (float(new["y"]) - float(old["y"])),
        "z": float(old.get("z", 0.0)) + alpha * (float(new.get("z", 0.0)) - float(old.get("z", 0.0))),
    }


def _toward(old: dict | None, target: dict, maximum: float) -> dict:
    """Move toward target by at most maximum normalized image distance."""
    if old is None:
        return {"x": float(target["x"]), "y": float(target["y"]), "z": float(target.get("z", 0.0))}
    dx = float(target["x"]) - float(old["x"])
    dy = float(target["y"]) - float(old["y"])
    distance = math.hypot(dx, dy)
    if distance <= maximum or distance <= 1e-9:
        return {"x": float(target["x"]), "y": float(target["y"]), "z": float(target.get("z", 0.0))}
    ratio = maximum / distance
    return {
        "x": float(old["x"]) + dx * ratio,
        "y": float(old["y"]) + dy * ratio,
        "z": float(old.get("z", 0.0)) + (float(target.get("z", 0.0)) - float(old.get("z", 0.0))) * ratio,
    }


class HandAnchorTracker:
    """Track one side's hand anchor with confidence and a finite fallback."""

    def __init__(self, side: str, *, fallback_ttl: float = HAND_ANCHOR_FALLBACK_TTL_S) -> None:
        if side not in _SIDE_NAMES:
            raise ValueError("side must be left or right")
        self.side = side
        self.names = _SIDE_NAMES[side]
        self.fallback_ttl = max(0.05, float(fallback_ttl))
        self.point: dict | None = None
        self.confidence = 0.0
        self.source = "missing"
        self.valid = False
        self.fallback = False
        self.last_update_at: float | None = None
        self.last_observed_at: float | None = None
        self.missing_since: float | None = None
        self.last_reason = "no_valid_anchor"
        self._last_observed_confidence = 0.0
        self._support = 0
        self._wrist_observed = False
        self._spread_quality = 0.0
        self._scale = None
        self._forearm_vector: tuple[float, float, float] | None = None
        self._upperarm_vector: tuple[float, float, float] | None = None
        self._forearm_scale: float | None = None
        self._velocity = (0.0, 0.0, 0.0)
        self._arm = {"state": "unknown", "direction": None, "angle_deg": None, "extension": None}

    def reset(self) -> None:
        self.point = None
        self.confidence = 0.0
        self.source = "missing"
        self.valid = False
        self.fallback = False
        self.last_update_at = None
        self.last_observed_at = None
        self.missing_since = None
        self.last_reason = "reset"
        self._last_observed_confidence = 0.0
        self._support = 0
        self._wrist_observed = False
        self._spread_quality = 0.0
        self._scale = None
        self._forearm_vector = None
        self._upperarm_vector = None
        self._forearm_scale = None
        self._velocity = (0.0, 0.0, 0.0)
        self._arm = {"state": "unknown", "direction": None, "angle_deg": None, "extension": None}

    def _arm_scale(self, pose_map: dict[str, dict], wrist: dict | None) -> float:
        elbow = _raw_point(pose_map.get(self.names["elbow"]), 0.15)
        shoulder = _raw_point(pose_map.get(self.names["shoulder"]), 0.15)
        if elbow and wrist:
            length = _distance(elbow, wrist)
            if length >= 0.025:
                return length
        if shoulder and elbow:
            length = _distance(shoulder, elbow) * 0.85
            if length >= 0.025:
                return length
        return 0.12

    def _observe(self, pose_map: dict[str, dict]) -> dict | None:
        entries: list[tuple[str, dict, float]] = []
        for role in ("wrist", "thumb", "index", "pinky"):
            point = _raw_point(pose_map.get(self.names[role]))
            if point is not None:
                # Wrist is the primary anchor; the Pose33 finger points are
                # close-neighbour support rather than equal votes.
                # The wrist is the stable, same-side anchor.  Finger points
                # only refine it when they agree; they must not pull a good
                # wrist estimate across a zone boundary during occlusion.
                base_weight = 16.0 if role == "wrist" else 1.0
                entries.append((role, point, base_weight * max(0.05, point["score"])))
        if not entries:
            return None

        wrist = next((point for role, point, _ in entries if role == "wrist"), None)
        scale = self._arm_scale(pose_map, wrist)
        rough = wrist or {
            "x": _weighted_median([(point, weight) for _, point, weight in entries], "x"),
            "y": _weighted_median([(point, weight) for _, point, weight in entries], "y"),
            "z": 0.0,
        }
        # Finger points must remain within a plausible forearm-sized
        # neighbourhood.  This rejects a stray landmark without allowing a
        # valid out-of-frame hand to drag the anchor toward an edge.
        max_offset = max(0.035, min(0.22, 0.90 * scale))
        inliers = [(role, point, weight) for role, point, weight in entries if _distance(point, rough) <= max_offset]
        if not inliers and wrist is not None:
            inliers = [("wrist", wrist, 4.0 * max(0.05, wrist["score"]))]
        if not inliers:
            return None

        robust_center = {
            "x": _weighted_median([(point, weight) for _, point, weight in inliers], "x"),
            "y": _weighted_median([(point, weight) for _, point, weight in inliers], "y"),
            "z": 0.0,
        }
        residuals = [_distance(point, robust_center) for _, point, _ in inliers]
        mad = statistics.median(residuals) if residuals else 0.0
        radius = max(0.035, min(max_offset, 3.0 * mad + 0.018))
        tight = [(role, point, weight) for role, point, weight in inliers if _distance(point, robust_center) <= radius]
        if not tight:
            tight = inliers[:1]
        total_weight = sum(weight for _, _, weight in tight)
        anchor = {
            "x": sum(point["x"] * weight for _, point, weight in tight) / max(1e-6, total_weight),
            "y": sum(point["y"] * weight for _, point, weight in tight) / max(1e-6, total_weight),
            "z": sum(point.get("z", 0.0) * weight for _, point, weight in tight) / max(1e-6, total_weight),
        }
        confidence = sum(point["score"] * weight for _, point, weight in tight) / max(1e-6, total_weight)
        support = len(tight)
        support_factor = min(1.0, support / 4.0)
        spread_quality = max(0.0, min(1.0, 1.0 - (statistics.median(_distance(point, anchor) for _, point, _ in tight) / max_offset)))
        confidence = max(0.0, min(1.0, confidence * (0.86 + 0.14 * support_factor)))
        if wrist is None:
            confidence *= 0.88
        # _point_in_circle/_point_in_rect in the legacy kernel use 0.42 as
        # their visibility gate.  A compact multi-point cluster can therefore
        # earn an *effective* zone score while its unrounded confidence stays
        # exposed to diagnostics and still decays during fallback.
        zone_score = confidence
        if support >= 2 and confidence >= 0.20 and spread_quality >= 0.55:
            zone_score = max(zone_score, min(1.0, HAND_ANCHOR_ZONE_SCORE + 0.28 * spread_quality))
        anchor["score"] = zone_score
        return {
            "point": anchor,
            "confidence": confidence,
            "support": support,
            "spread_quality": spread_quality,
            "scale": scale,
            "wrist_observed": wrist is not None,
        }

    def _update_vectors(self, pose_map: dict[str, dict], observed_point: dict, scale: float) -> None:
        elbow = _raw_point(pose_map.get(self.names["elbow"]), 0.15)
        shoulder = _raw_point(pose_map.get(self.names["shoulder"]), 0.15)
        if elbow:
            self._forearm_vector = (
                observed_point["x"] - elbow["x"],
                observed_point["y"] - elbow["y"],
                observed_point.get("z", 0.0) - elbow.get("z", 0.0),
            )
        if shoulder and elbow:
            self._upperarm_vector = (
                elbow["x"] - shoulder["x"],
                elbow["y"] - shoulder["y"],
                elbow.get("z", 0.0) - shoulder.get("z", 0.0),
            )
        if math.isfinite(scale) and scale > 0.02:
            self._forearm_scale = scale

    def _predict(self, pose_map: dict[str, dict], now: float) -> tuple[dict | None, str]:
        elbow = _raw_point(pose_map.get(self.names["elbow"]), 0.15)
        shoulder = _raw_point(pose_map.get(self.names["shoulder"]), 0.15)
        if elbow and self._forearm_vector:
            dx, dy, dz = self._forearm_vector
            return {"x": elbow["x"] + dx, "y": elbow["y"] + dy, "z": elbow.get("z", 0.0) + dz}, "forearm_fallback"
        if shoulder and self._upperarm_vector and self._forearm_vector:
            ux, uy, uz = self._upperarm_vector
            fx, fy, fz = self._forearm_vector
            return {"x": shoulder["x"] + ux + fx, "y": shoulder["y"] + uy + fy, "z": shoulder.get("z", 0.0) + uz + fz}, "arm_fallback"
        if self.point is not None:
            dt = max(0.0, min(0.10, now - (self.last_update_at or now)))
            vx, vy, vz = self._velocity
            return {"x": self.point["x"] + vx * dt, "y": self.point["y"] + vy * dt, "z": self.point.get("z", 0.0) + vz * dt}, "predicted"
        return None, "no_fallback"

    def _arm_state(self, pose_map: dict[str, dict], point: dict | None) -> dict:
        if point is None:
            return {"state": "unknown", "direction": None, "angle_deg": None, "extension": None}
        shoulder = _raw_point(pose_map.get(self.names["shoulder"]), 0.20)
        elbow = _raw_point(pose_map.get(self.names["elbow"]), 0.20)
        if not shoulder:
            return {"state": "unknown", "direction": None, "angle_deg": None, "extension": None}
        dx = float(point["x"]) - shoulder["x"]
        dy = float(point["y"]) - shoulder["y"]
        length = math.hypot(dx, dy)
        direction = None
        angle = None
        if length > 1e-6:
            direction = {"x": round(dx / length, 4), "y": round(dy / length, 4)}
            angle = round(math.degrees(math.atan2(dy, dx)), 2)
        extension = None
        scale = self._forearm_scale or 0.12
        if elbow:
            upper = max(1e-6, _distance(shoulder, elbow))
            extension = round(length / upper, 4)
        if length < 1e-6:
            state = "unknown"
        elif dy < -0.08 * max(scale, 0.06):
            state = "raised"
        elif extension is not None and extension >= 1.65:
            state = "extended"
        else:
            state = "bent"
        return {"state": state, "direction": direction, "angle_deg": angle, "extension": extension}

    def update(self, pose_map: dict[str, dict] | None, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else float(now)
        pose_map = pose_map if isinstance(pose_map, dict) else {}
        observed = self._observe(pose_map)
        previous = copy.deepcopy(self.point) if self.point is not None else None
        dt = max(1.0 / 120.0, min(0.25, now - (self.last_update_at or now)))
        self.last_update_at = now

        if observed is not None:
            target = observed["point"]
            self._update_vectors(pose_map, target, observed["scale"])
            # A continuously observed hand follows the robust spatial center
            # directly so existing zone semantics retain their responsiveness.
            # Only re-capture after a fallback/occlusion is step-limited and
            # blended, which prevents a stale anchor from teleporting on return.
            recapturing = previous is not None and (self.fallback or self.missing_since is not None)
            if recapturing:
                max_step = max(0.035, min(0.16, 0.70 * max(0.06, observed["scale"])))
                limited = _toward(previous, target, max_step)
                blended = _blend(previous, limited, 0.72)
            else:
                blended = target if previous is None else _blend(previous, target, 0.88)
            if previous is not None:
                self._velocity = tuple(
                    max(-2.0, min(2.0, (blended[key] - previous[key]) / dt))
                    for key in ("x", "y", "z")
                )
            self.point = {**blended, "score": target["score"]}
            self.confidence = round(float(observed["confidence"]), 4)
            self._support = int(observed["support"])
            self._wrist_observed = bool(observed["wrist_observed"])
            self._spread_quality = float(observed["spread_quality"])
            self._scale = float(observed["scale"])
            self.source = "observed"
            self.valid = True
            self.fallback = False
            self.last_observed_at = now
            self.missing_since = None
            self.last_reason = ""
            self._last_observed_confidence = self.confidence
            self._arm = self._arm_state(pose_map, self.point)
            return self.status(now)

        if self.missing_since is None:
            self.missing_since = now
        age = (now - self.last_observed_at) if self.last_observed_at is not None else math.inf
        if age <= self.fallback_ttl:
            predicted, source = self._predict(pose_map, now)
            if predicted is not None:
                max_step = max(0.025, min(0.12, 0.55 * (self._forearm_scale or 0.12)))
                limited = _toward(previous, predicted, max_step)
                blended = limited if previous is None else _blend(previous, limited, 0.60)
                decay = max(0.0, min(1.0, 1.0 - age / self.fallback_ttl))
                self.point = {**blended, "score": max(HAND_ANCHOR_ZONE_SCORE, self._last_observed_confidence * decay)}
                self.confidence = round(self._last_observed_confidence * decay, 4)
                self.source = source
                self.valid = True
                self.fallback = True
                self.last_reason = "observed_anchor_temporarily_missing"
                self._arm = self._arm_state(pose_map, self.point)
                return self.status(now)

        self.point = None
        self.confidence = 0.0
        self.source = "missing"
        self.valid = False
        self.fallback = False
        self.last_reason = "fallback_ttl_expired" if self.last_observed_at is not None else "no_valid_anchor"
        self._velocity = (0.0, 0.0, 0.0)
        self._arm = self._arm_state(pose_map, None)
        return self.status(now)

    def status(self, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else float(now)
        age_ms = None if self.last_observed_at is None else round(max(0.0, (now - self.last_observed_at) * 1000.0), 1)
        missing_ms = None if self.missing_since is None else round(max(0.0, (now - self.missing_since) * 1000.0), 1)
        return {
            "version": HAND_ANCHOR_VERSION,
            "side": self.side,
            "point": copy.deepcopy(self.point),
            "confidence": round(float(self.confidence), 4),
            "support": self._support,
            "wrist_observed": self._wrist_observed,
            "spread_quality": round(self._spread_quality, 4),
            "forearm_scale": round(self._scale, 5) if self._scale is not None else None,
            "source": self.source,
            "valid": bool(self.valid),
            "fallback": bool(self.fallback),
            "last_observed_age_ms": age_ms,
            "missing_age_ms": missing_ms,
            "fallback_ttl_ms": round(self.fallback_ttl * 1000.0, 1),
            "reason": self.last_reason,
            "arm": copy.deepcopy(self._arm),
        }


def pose_with_hand_anchors(pose_map: dict[str, dict] | None, points: dict[str, dict | None]) -> dict[str, dict] | None:
    """Return a shallow pose copy with stable anchors in wrist slots.

    Existing rules consume wrist names, so this is the narrowest adapter: the
    original map remains untouched and only zone/arm consumers receive the
    stable replacement.  Head-control consumers continue to receive raw pose.
    """
    if not isinstance(pose_map, dict) or not points:
        return pose_map
    adapted = dict(pose_map)
    for side in ("left", "right"):
        point = points.get(side)
        if point is not None:
            adapted[f"{side}_wrist"] = copy.deepcopy(point)
        elif f"{side}_wrist" in adapted:
            # Do not silently fall back to the raw wrist for zone/arm rules:
            # an expired anchor must become unavailable until it is recaptured.
            raw = dict(adapted[f"{side}_wrist"])
            raw["score"] = 0.0
            adapted[f"{side}_wrist"] = raw
    return adapted


def install_hand_anchor_adapter(control_kernel_class) -> None:
    """Install the adapter at the ControlKernel boundary exactly once."""
    if getattr(control_kernel_class, "_hand_anchor_adapter_installed", False):
        return

    original_init = control_kernel_class.__init__
    original_process = control_kernel_class._process_pose_locked
    original_zones = control_kernel_class._update_zones_locked
    original_motion = control_kernel_class._update_motion_locked
    original_cross = getattr(control_kernel_class, "_update_cross_poses_locked", None)
    original_clear = control_kernel_class._clear_body_outputs_locked
    original_status_locked = control_kernel_class.status_locked

    def _init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.hand_anchor_trackers = {
            "left": HandAnchorTracker("left"),
            "right": HandAnchorTracker("right"),
        }
        self.hand_anchor_points = {}

    def _process(self, pose_map, now, world_pose=None):
        if not hasattr(self, "hand_anchor_trackers"):
            self.hand_anchor_trackers = {side: HandAnchorTracker(side) for side in ("left", "right")}
            self.hand_anchor_points = {}
        if pose_map:
            points = {}
            for side, tracker in self.hand_anchor_trackers.items():
                state = tracker.update(pose_map, now)
                if state.get("valid") and state.get("point") is not None:
                    points[side] = state["point"]
            self.hand_anchor_points = points
        else:
            for tracker in self.hand_anchor_trackers.values():
                tracker.reset()
            self.hand_anchor_points = {}
        # Keep the optional metric world stream intact while preserving the
        # old hook signature for callers that do not provide it.
        if world_pose is None:
            return original_process(self, pose_map, now)
        return original_process(self, pose_map, now, world_pose)

    def _zones(self, pose_map, now):
        adapted = pose_with_hand_anchors(pose_map, getattr(self, "hand_anchor_points", {}))
        return original_zones(self, adapted, now)

    def _motion(self, pose_map, now):
        adapted = pose_with_hand_anchors(pose_map, getattr(self, "hand_anchor_points", {}))
        return original_motion(self, adapted, now)

    def _cross(self, pose_map, now):
        adapted = pose_with_hand_anchors(pose_map, getattr(self, "hand_anchor_points", {}))
        return original_cross(self, adapted, now) if original_cross is not None else None

    def _clear(self):
        result = original_clear(self)
        for tracker in getattr(self, "hand_anchor_trackers", {}).values():
            tracker.reset()
        self.hand_anchor_points = {}
        return result

    def _status_locked(self, now):
        result = original_status_locked(self, now)
        trackers = getattr(self, "hand_anchor_trackers", {})
        hand_states = {side: tracker.status(now) for side, tracker in trackers.items()}
        result["hand_anchor_version"] = HAND_ANCHOR_VERSION
        result["hand_anchors"] = hand_states
        result["arm_states"] = {side: copy.deepcopy(state.get("arm", {})) for side, state in hand_states.items()}
        return result

    control_kernel_class.__init__ = _init
    control_kernel_class._process_pose_locked = _process
    control_kernel_class._update_zones_locked = _zones
    control_kernel_class._update_motion_locked = _motion
    if original_cross is not None:
        control_kernel_class._update_cross_poses_locked = _cross
    control_kernel_class._clear_body_outputs_locked = _clear
    control_kernel_class.status_locked = _status_locked
    control_kernel_class._hand_anchor_adapter_installed = True
