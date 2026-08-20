"""Local, monotonic-clock control kernel.

The browser is intentionally not part of this module.  Pose frames and
handheld sensor frames enter here, are converted to the same body-relative
signals, and are written directly to :class:`OutputManager`.
"""

from __future__ import annotations

import copy
import json
import math
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any


MP_NAMES = [
    "nose", "left_eye_inner", "left_eye", "left_eye_outer", "right_eye_inner",
    "right_eye", "right_eye_outer", "left_ear", "right_ear", "mouth_left",
    "mouth_right", "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist", "left_pinky", "right_pinky", "left_index",
    "right_index", "left_thumb", "right_thumb", "left_hip", "right_hip",
    "left_knee", "right_knee", "left_ankle", "right_ankle", "left_heel",
    "right_heel", "left_foot_index", "right_foot_index",
]

# The reference web controller sent head-control frames at most every 28 ms.
# Keep that cadence in the local kernel now that the browser is display-only.
HEAD_UPDATE_INTERVAL_S = 0.028

BODY_ZONES = {
    "leftHandUpper": {"label": "Y", "button": "Y", "points": ("left_wrist",), "kind": "hand"},
    "leftHandLower": {"label": "X", "button": "X", "points": ("left_wrist",), "kind": "hand"},
    "rightHandUpper": {"label": "B", "button": "B", "points": ("right_wrist",), "kind": "hand"},
    "rightHandLower": {"label": "A", "button": "A", "points": ("right_wrist",), "kind": "hand"},
    "leftFoot": {"label": "LB", "button": "LB", "points": ("left_ankle", "left_heel", "left_foot_index"), "kind": "foot"},
    "rightFoot": {"label": "RB", "button": "RB", "points": ("right_ankle", "right_heel", "right_foot_index"), "kind": "foot"},
}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _score(point: dict | None) -> float:
    if not isinstance(point, dict):
        return 0.0
    return _finite(point.get("score", point.get("visibility", point.get("presence", 0.0))))


def _point(point: dict) -> dict:
    return {
        "x": _finite(point.get("x")),
        "y": _finite(point.get("y")),
        "z": _finite(point.get("z")),
        "score": _score(point),
    }


def _midpoint(a: dict, b: dict) -> dict:
    return {"x": (a["x"] + b["x"]) / 2.0, "y": (a["y"] + b["y"]) / 2.0}


def _distance(a: dict, b: dict) -> float:
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"])


def _rect_at(cx: float, cy: float, width_px: float, height_px: float, image_width: int, image_height: int) -> dict:
    ww = width_px / max(1, image_width)
    hh = height_px / max(1, image_height)
    return {
        "x1": _clamp(cx - ww / 2.0, 0.0, 1.0),
        "x2": _clamp(cx + ww / 2.0, 0.0, 1.0),
        "y1": _clamp(cy - hh / 2.0, 0.0, 1.0),
        "y2": _clamp(cy + hh / 2.0, 0.0, 1.0),
    }


class ControlKernel:
    """Thread-safe body/action/head kernel with its own watchdog."""

    def __init__(self, output, *, watchdog_timeout: float = 0.30) -> None:
        self.output = output
        self.watchdog_timeout = float(watchdog_timeout)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._watch_loop, name="motion-control-kernel", daemon=True)

        self.active_body_source: str | None = None
        self.body_last_at = 0.0
        self.width = 640
        self.height = 480
        self.latest_pose: dict[str, dict] | None = None
        self.last_error: str | None = None

        self.zone_rects: dict[str, dict] = {}
        self.zone_state = {name: {"inside": 0, "outside": 0, "pressed": False} for name in BODY_ZONES}
        self.last_zone_emit = 0.0

        self.motion_config: list[dict] = []
        self.motion_active: set[str] = set()
        self.motion_debounce = {
            key: {"active": False, "on": 0, "off": 0}
            for key in ("march", "calf_back", "squat", "hands_up")
        }
        self.step = {"left_was": False, "right_was": False, "last_side": "", "last_at": 0.0, "active_until": 0.0}
        self.last_motion_emit = 0.0

        self.head = {
            "calibrated": False, "calibrating": False, "stage": "", "stage_started": 0.0,
            "stage_duration": 1.5, "center_yaw": [], "center_pitch": [], "left_yaw": [],
            "right_yaw": [], "up_pitch": [], "down_pitch": [], "torso_samples": [],
            "yaw0": 0.0, "yaw_left": 0.0, "yaw_right": 0.0, "pitch0": 0.0,
            "pitch_up": 0.0, "pitch_down": 0.0, "torso0": math.nan,
            "raw_yaw": math.nan, "raw_pitch": math.nan, "norm_x": 0.0, "norm_y": 0.0,
            "filtered_x": 0.0, "filtered_y": 0.0, "output_x": 0.0, "output_y": 0.0,
            "last_update": 0.0,
            "deadzone_x": 0.08, "deadzone_y": 0.12, "gamma": 2.2,
            "max_percent_x": 60.0, "max_percent_y": 45.0, "enabled": True,
            "invert_x": False, "invert_y": False, "quality": "未校准",
        }
        self.sensor_sources: dict[str, dict] = {}
        self._thread.start()

    # ---------- public input/config boundary ----------

    @staticmethod
    def pose_map_from_message(message: dict) -> dict[str, dict] | None:
        poses = message.get("poses") if isinstance(message, dict) else None
        landmarks = poses[0].get("pose") if poses and isinstance(poses[0], dict) else None
        if not isinstance(landmarks, list) or len(landmarks) != 33:
            return None
        coordinates_mirrored = bool(message.get("coordinates_mirrored", False)) if isinstance(message, dict) else False
        result = {}
        for index, name in enumerate(MP_NAMES):
            if not isinstance(landmarks[index], dict):
                continue
            point = _point(landmarks[index])
            # The kernel's canonical space is the raw, unmirrored camera
            # frame.  A phone that already mirrored its coordinates is
            # normalized exactly once here; the web UI only mirrors display.
            if coordinates_mirrored:
                point["x"] = 1.0 - point["x"]
            result[name] = point
        return result

    def configure_motions(self, items) -> None:
        with self._lock:
            self.motion_config = [copy.deepcopy(item) for item in (items or []) if isinstance(item, dict)]

    def configure_head(self, *, deadzone_x=None, deadzone_y=None, gamma=None,
                       max_percent_x=None, max_percent_y=None, enabled=None,
                       invert_x=None, invert_y=None) -> dict:
        with self._lock:
            if deadzone_x is not None: self.head["deadzone_x"] = _clamp(deadzone_x, 0.03, 0.30)
            if deadzone_y is not None: self.head["deadzone_y"] = _clamp(deadzone_y, 0.04, 0.35)
            if gamma is not None: self.head["gamma"] = _clamp(gamma, 1.4, 3.2)
            if max_percent_x is not None: self.head["max_percent_x"] = _clamp(max_percent_x, 20.0, 120.0)
            if max_percent_y is not None: self.head["max_percent_y"] = _clamp(max_percent_y, 15.0, 100.0)
            if enabled is not None: self.head["enabled"] = bool(enabled)
            if invert_x is not None: self.head["invert_x"] = bool(invert_x)
            if invert_y is not None: self.head["invert_y"] = bool(invert_y)
            return self.status_locked(time.monotonic())

    def handle_pose_message(self, source_id: str, message: dict) -> dict:
        pose_map = self.pose_map_from_message(message)
        width = int(message.get("width") or 640)
        height = int(message.get("height") or 480)
        return self.handle_pose_map(source_id, pose_map, width=width, height=height)

    def handle_pose_map(self, source_id: str, pose_map: dict[str, dict] | None, *, width: int = 640, height: int = 480) -> dict:
        now = time.monotonic()
        with self._lock:
            source_id = str(source_id)
            if self.active_body_source != source_id:
                self._clear_body_locked()
                self.active_body_source = source_id
            self.body_last_at = now
            self.width = max(1, int(width))
            self.height = max(1, int(height))
            self.latest_pose = copy.deepcopy(pose_map) if pose_map else None
            self._process_pose_locked(pose_map, now)
            return self.status_locked(now)

    def handle_sensor(self, source_id: str, buttons, *, left_trigger: float = 0.0,
                      right_trigger: float = 0.0, stick_x: float = 0.0, stick_y: float = 0.0,
                      quaternion: dict | None = None, rotation_rate: dict | None = None,
                      acceleration: dict | None = None, recenter: bool = False) -> dict:
        now = time.monotonic()
        source_id = str(source_id)
        with self._lock:
            self.sensor_sources[source_id] = {
                "received_at": now, "buttons": sorted({str(item).upper() for item in (buttons or [])}),
                "left_trigger": _clamp(left_trigger, 0.0, 1.0), "right_trigger": _clamp(right_trigger, 0.0, 1.0),
                "stick_x": _clamp(stick_x, -1.0, 1.0), "stick_y": _clamp(stick_y, -1.0, 1.0),
                "quaternion": copy.deepcopy(quaternion or {}), "rotation_rate": copy.deepcopy(rotation_rate or {}),
                "acceleration": copy.deepcopy(acceleration or {}), "recenter": bool(recenter),
            }
            self._safe_output(
                self.output.set_sensor_state, source_id, buttons,
                left_trigger=left_trigger, right_trigger=right_trigger, stick_x=stick_x, stick_y=stick_y,
            )
            return self.status_locked(now)

    def clear_source(self, source_id: str) -> dict:
        source_id = str(source_id)
        with self._lock:
            if self.active_body_source == source_id:
                self._clear_body_locked()
                self.active_body_source = None
                self.body_last_at = 0.0
            if source_id in self.sensor_sources:
                self.sensor_sources.pop(source_id, None)
                self._safe_output(self.output.clear_source, source_id)
            return self.status_locked(time.monotonic())

    def clear_body(self) -> dict:
        with self._lock:
            self._clear_body_locked()
            self.active_body_source = None
            self.body_last_at = 0.0
            return self.status_locked(time.monotonic())

    def start_calibration(self) -> dict:
        with self._lock:
            now = time.monotonic()
            self.head.update({
                "calibrating": True, "calibrated": False, "stage": "center", "stage_started": now,
                "center_yaw": [], "center_pitch": [], "left_yaw": [], "right_yaw": [],
                "up_pitch": [], "down_pitch": [], "torso_samples": [], "filtered_x": 0.0,
                "filtered_y": 0.0, "output_x": 0.0, "output_y": 0.0,
            })
            self._safe_output(self.output.apply, 0.0, 0.0)
            return self.status_locked(now)

    def set_current_center(self) -> dict:
        with self._lock:
            if not (math.isfinite(self.head["raw_yaw"]) and math.isfinite(self.head["raw_pitch"])):
                raise ValueError("当前还没有稳定的头部关键点")
            self.head.update({
                "yaw0": self.head["raw_yaw"], "pitch0": self.head["raw_pitch"],
                "yaw_left": self.head["raw_yaw"] - 0.10, "yaw_right": self.head["raw_yaw"] + 0.10,
                "pitch_up": self.head["raw_pitch"] - 0.04, "pitch_down": self.head["raw_pitch"] + 0.04,
                "calibrated": True, "quality": "手动中心", "filtered_x": 0.0,
                "filtered_y": 0.0, "output_x": 0.0, "output_y": 0.0, "last_update": 0.0,
            })
            self._safe_output(self.output.apply, 0.0, 0.0)
            return self.status_locked(time.monotonic())

    # ---------- pose processing ----------

    def _process_pose_locked(self, pose_map: dict[str, dict] | None, now: float) -> None:
        if not pose_map:
            self._clear_body_outputs_locked()
            return
        self._update_zones_locked(pose_map, now)
        self._update_motion_locked(pose_map, now)
        self._update_head_locked(pose_map, now)

    def _compute_body_zones(self, pose_map: dict[str, dict]) -> dict[str, dict]:
        iw, ih = self.width, self.height
        ls, rs = pose_map.get("left_shoulder"), pose_map.get("right_shoulder")
        lh, rh, nose = pose_map.get("left_hip"), pose_map.get("right_hip"), pose_map.get("nose")
        if not all((ls, rs, lh, rh)) or min(_score(ls), _score(rs), _score(lh), _score(rh)) < 0.4:
            return {}
        shoulder, hip = _midpoint(ls, rs), _midpoint(lh, rh)
        torso_px = _distance(shoulder, hip) * math.hypot(iw, ih)
        if not math.isfinite(torso_px) or torso_px < 35:
            return {}
        left_dir = 1.0 if (ls["x"] - shoulder["x"]) >= 0 else -1.0
        right_dir = 1.0 if (rs["x"] - shoulder["x"]) >= 0 else -left_dir
        rects: dict[str, dict] = {}
        le, re = pose_map.get("left_ear"), pose_map.get("right_ear")
        head_center = None
        if le and re and min(_score(le), _score(re)) >= 0.35:
            head_center = _midpoint(le, re)
        elif nose and _score(nose) >= 0.35:
            head_center = nose
        if head_center:
            hand_w, hand_h = 0.36 * torso_px, 0.30 * torso_px
            for name, direction, dy in (
                ("leftHandUpper", left_dir, -0.14), ("leftHandLower", left_dir, 0.22),
                ("rightHandUpper", right_dir, -0.14), ("rightHandLower", right_dir, 0.22),
            ):
                next_rect = _rect_at(
                    head_center["x"] + direction * 0.82 * torso_px / iw,
                    head_center["y"] + dy * torso_px / ih,
                    hand_w, hand_h, iw, ih,
                )
                old = self.zone_rects.get(name)
                rects[name] = self._smooth_rect(old, next_rect)
        la, ra = pose_map.get("left_ankle"), pose_map.get("right_ankle")
        if la and ra and max(_score(la), _score(ra)) >= 0.4:
            visible = [item for item in (la, ra) if _score(item) >= 0.4]
            floor_y = max(item["y"] for item in visible)
            foot_w, foot_h = 0.42 * torso_px, 0.38 * torso_px
            for name, side_hip, direction in (("leftFoot", lh, left_dir), ("rightFoot", rh, right_dir)):
                next_rect = _rect_at(
                    side_hip["x"] + direction * 0.78 * torso_px / iw,
                    floor_y - 0.50 * torso_px / ih,
                    foot_w, foot_h, iw, ih,
                )
                old = self.zone_rects.get(name)
                rects[name] = self._smooth_rect(old, next_rect)
        return rects

    @staticmethod
    def _smooth_rect(old: dict | None, new: dict) -> dict:
        if not old:
            return new
        return {key: old[key] + 0.50 * (new[key] - old[key]) for key in ("x1", "x2", "y1", "y2")}

    @staticmethod
    def _point_in_rect(point: dict | None, rect: dict | None) -> bool:
        return bool(point and rect and _score(point) >= 0.42 and rect["x1"] <= point["x"] <= rect["x2"] and rect["y1"] <= point["y"] <= rect["y2"])

    def _update_zones_locked(self, pose_map: dict[str, dict], now: float) -> None:
        self.zone_rects = self._compute_body_zones(pose_map)
        changed = False
        for name, definition in BODY_ZONES.items():
            state = self.zone_state[name]
            inside = any(self._point_in_rect(pose_map.get(point), self.zone_rects.get(name)) for point in definition["points"])
            if inside:
                state["inside"] += 1
                state["outside"] = 0
                if not state["pressed"] and state["inside"] >= 2:
                    state["pressed"] = True
                    changed = True
            else:
                state["outside"] += 1
                state["inside"] = 0
                if state["pressed"] and state["outside"] >= 2:
                    state["pressed"] = False
                    changed = True
        keys = self._pressed_keys_locked()
        if changed or (keys and now - self.last_zone_emit >= 0.14):
            self._safe_output(self.output.set_buttons, keys, source="zones")
            self.last_zone_emit = now

    def _pressed_keys_locked(self) -> list[str]:
        return sorted({BODY_ZONES[name]["button"] for name, state in self.zone_state.items() if state["pressed"]})

    # ---------- four existing motion rules ----------

    def _points_good(self, pose_map: dict[str, dict], names: tuple[str, ...], minimum: float = 0.42) -> bool:
        return all(name in pose_map and _score(pose_map[name]) >= minimum for name in names)

    def _angle_at(self, a: dict, b: dict, c: dict) -> float:
        if min(_score(a), _score(b), _score(c)) < 0.4:
            return math.nan
        ux, uy = (a["x"] - b["x"]) * self.width, (a["y"] - b["y"]) * self.height
        vx, vy = (c["x"] - b["x"]) * self.width, (c["y"] - b["y"]) * self.height
        denominator = math.hypot(ux, uy) * math.hypot(vx, vy)
        if denominator < 1e-6:
            return math.nan
        return math.degrees(math.acos(_clamp((ux * vx + uy * vy) / denominator, -1.0, 1.0)))

    def _set_motion_debounced(self, ident: str, raw: bool, on_frames: int, off_frames: int) -> bool:
        state = self.motion_debounce[ident]
        if raw:
            state["on"] += 1
            state["off"] = 0
            if not state["active"] and state["on"] >= on_frames:
                state["active"] = True
        else:
            state["off"] += 1
            state["on"] = 0
            if state["active"] and state["off"] >= off_frames:
                state["active"] = False
        return bool(state["active"])

    def _update_motion_locked(self, pose_map: dict[str, dict], now: float) -> None:
        shoulder = _midpoint(pose_map["left_shoulder"], pose_map["right_shoulder"]) if self._points_good(pose_map, ("left_shoulder", "right_shoulder")) else None
        hip = _midpoint(pose_map["left_hip"], pose_map["right_hip"]) if self._points_good(pose_map, ("left_hip", "right_hip")) else None
        torso = max(0.025, abs(hip["y"] - shoulder["y"])) if shoulder and hip else math.nan
        hands_raw = squat_raw = calf_raw = march_raw = False
        if math.isfinite(torso) and self._points_good(pose_map, ("nose", "left_shoulder", "right_shoulder", "left_elbow", "right_elbow", "left_wrist", "right_wrist")):
            hands_raw = (
                pose_map["left_wrist"]["y"] < pose_map["nose"]["y"] - 0.06 * torso
                and pose_map["right_wrist"]["y"] < pose_map["nose"]["y"] - 0.06 * torso
                and pose_map["left_elbow"]["y"] < pose_map["left_shoulder"]["y"] + 0.08 * torso
                and pose_map["right_elbow"]["y"] < pose_map["right_shoulder"]["y"] + 0.08 * torso
            )
        leg_good = math.isfinite(torso) and self._points_good(pose_map, ("left_hip", "right_hip", "left_knee", "right_knee", "left_ankle", "right_ankle"))
        if leg_good:
            left_angle = self._angle_at(pose_map["left_hip"], pose_map["left_knee"], pose_map["left_ankle"])
            right_angle = self._angle_at(pose_map["right_hip"], pose_map["right_knee"], pose_map["right_ankle"])
            hip_knee = ((pose_map["left_knee"]["y"] - pose_map["left_hip"]["y"]) + (pose_map["right_knee"]["y"] - pose_map["right_hip"]["y"])) / 2.0
            squat_raw = left_angle < 135 and right_angle < 135 and hip_knee < 0.84 * torso
            left_calf = left_angle < 115 and (pose_map["left_knee"]["y"] - pose_map["left_hip"]["y"]) > 0.58 * torso and (pose_map["left_ankle"]["y"] - pose_map["left_knee"]["y"]) < 0.58 * torso
            right_calf = right_angle < 115 and (pose_map["right_knee"]["y"] - pose_map["right_hip"]["y"]) > 0.58 * torso and (pose_map["right_ankle"]["y"] - pose_map["right_knee"]["y"]) < 0.58 * torso
            calf_raw = not squat_raw and (left_calf or right_calf)
            left_lift = (pose_map["right_knee"]["y"] - pose_map["left_knee"]["y"]) > 0.16 * torso and (pose_map["right_ankle"]["y"] - pose_map["left_ankle"]["y"]) > 0.10 * torso
            right_lift = (pose_map["left_knee"]["y"] - pose_map["right_knee"]["y"]) > 0.16 * torso and (pose_map["left_ankle"]["y"] - pose_map["right_ankle"]["y"]) > 0.10 * torso
            def step_event(side: str) -> None:
                if side != self.step["last_side"] and 0.10 <= now - self.step["last_at"] <= 1.15:
                    self.step["active_until"] = now + 0.70
                self.step["last_side"], self.step["last_at"] = side, now
            if left_lift and not self.step["left_was"]:
                step_event("L")
            if right_lift and not self.step["right_was"]:
                step_event("R")
            self.step["left_was"], self.step["right_was"] = left_lift, right_lift
            if now - self.step["last_at"] > 1.20:
                self.step["last_side"], self.step["active_until"] = "", 0.0
            march_raw = not squat_raw and not calf_raw and now < self.step["active_until"]
        else:
            self.step.update({"left_was": False, "right_was": False, "active_until": 0.0})
        active = set()
        if self._set_motion_debounced("march", march_raw, 1, 2): active.add("march")
        if self._set_motion_debounced("calf_back", calf_raw, 3, 4): active.add("calf_back")
        if self._set_motion_debounced("squat", squat_raw, 3, 4): active.add("squat")
        if self._set_motion_debounced("hands_up", hands_raw, 3, 4): active.add("hands_up")
        changed = active != self.motion_active
        self.motion_active = active
        if changed or (active and now - self.last_motion_emit >= 0.15):
            holds = [item for item in self.motion_config if item.get("enabled") and item.get("id") in active and item.get("target")]
            self._safe_output(self.output.set_holds, holds, source_group="motions")
            self.last_motion_emit = now

    # ---------- head-control port of the browser math ----------

    def _torso_length(self, pose_map: dict[str, dict]) -> float:
        points = [pose_map.get(name) for name in ("left_shoulder", "right_shoulder", "left_hip", "right_hip")]
        if any(point is None for point in points) or min(_score(point) for point in points) < 0.3:
            return math.nan
        return _distance(_midpoint(points[0], points[1]), _midpoint(points[2], points[3]))

    def _yaw_signal(self, pose_map: dict[str, dict]) -> float:
        nose = pose_map.get("nose")
        if not nose or _score(nose) < 0.35:
            return math.nan
        values = []
        for left_name, right_name in (("left_ear", "right_ear"), ("left_eye", "right_eye")):
            left, right = pose_map.get(left_name), pose_map.get(right_name)
            if not left or not right or min(_score(left), _score(right)) < 0.35:
                continue
            dl, dr = _distance(nose, left), _distance(nose, right)
            if dl > 0.003 and dr > 0.003:
                values.append(math.log((dl + 1e-4) / (dr + 1e-4)))
        return sum(values) / len(values) if values else math.nan

    def _pitch_signal(self, pose_map: dict[str, dict], torso: float) -> float:
        nose = pose_map.get("nose")
        if not nose or _score(nose) < 0.35 or not math.isfinite(torso) or torso < 0.03:
            return math.nan
        ears = pose_map.get("left_ear"), pose_map.get("right_ear")
        if all(ears) and min(_score(item) for item in ears) >= 0.35:
            return (nose["y"] - _midpoint(*ears)["y"]) / torso
        eyes = pose_map.get("left_eye"), pose_map.get("right_eye")
        if all(eyes) and min(_score(item) for item in eyes) >= 0.35:
            return (nose["y"] - _midpoint(*eyes)["y"]) / torso
        return math.nan

    @staticmethod
    def _normalize_axis(raw: float, center: float, negative: float, positive: float) -> float:
        if not math.isfinite(raw):
            return 0.0
        delta, neg_delta, pos_delta = raw - center, negative - center, positive - center
        if delta * pos_delta >= 0 and abs(pos_delta) > 0.005:
            return _clamp(abs(delta / pos_delta), 0.0, 1.5)
        if delta * neg_delta >= 0 and abs(neg_delta) > 0.005:
            return -_clamp(abs(delta / neg_delta), 0.0, 1.5)
        return 0.0

    def _curve_axis(self, value: float, deadzone: float, maximum: float) -> float:
        amount = abs(value)
        if amount <= deadzone:
            return 0.0
        normalized = _clamp((amount - deadzone) / max(0.001, 1.0 - deadzone), 0.0, 1.0)
        return math.copysign(maximum * normalized ** self.head["gamma"], value)

    @staticmethod
    def _filter_axis(current: float, target: float, norm: float, deadzone: float, maximum: float) -> float:
        if abs(norm) <= deadzone:
            return 0.0
        intensity = _clamp(abs(target) / max(1.0, maximum), 0.0, 1.0)
        alpha = 0.10 + 0.36 * math.sqrt(intensity)
        return current + alpha * (target - current)

    def _calibration_stage(self, now: float) -> str:
        elapsed = now - self.head["stage_started"]
        duration = self.head["stage_duration"]
        if elapsed < duration: return "center"
        if elapsed < 2 * duration: return "left"
        if elapsed < 3 * duration: return "right"
        if elapsed < 4 * duration: return "up"
        if elapsed < 5 * duration: return "down"
        return "done"

    def _update_calibration_locked(self, raw_yaw: float, raw_pitch: float, torso: float, now: float) -> None:
        stage = self._calibration_stage(now)
        self.head["stage"] = stage
        if stage == "done":
            self._finish_calibration_locked()
            return
        if stage == "center":
            if math.isfinite(raw_yaw): self.head["center_yaw"].append(raw_yaw)
            if math.isfinite(raw_pitch): self.head["center_pitch"].append(raw_pitch)
            if math.isfinite(torso): self.head["torso_samples"].append(torso)
        elif stage == "left" and math.isfinite(raw_yaw): self.head["left_yaw"].append(raw_yaw)
        elif stage == "right" and math.isfinite(raw_yaw): self.head["right_yaw"].append(raw_yaw)
        elif stage == "up" and math.isfinite(raw_pitch): self.head["up_pitch"].append(raw_pitch)
        elif stage == "down" and math.isfinite(raw_pitch): self.head["down_pitch"].append(raw_pitch)

    @staticmethod
    def _median(values: list[float], default: float = math.nan) -> float:
        if not values:
            return default
        ordered = sorted(values)
        return ordered[len(ordered) // 2]

    @staticmethod
    def _qtile(values: list[float], probability: float, default: float = math.nan) -> float:
        """Match the reference JavaScript Math.round quantile index."""
        if not values:
            return default
        ordered = sorted(values)
        index = int(math.floor((len(ordered) - 1) * probability + 0.5))
        return ordered[max(0, min(len(ordered) - 1, index))]

    def _finish_calibration_locked(self) -> None:
        yaw0 = self._median(self.head["center_yaw"], self.head["raw_yaw"])
        pitch0 = self._median(self.head["center_pitch"], self.head["raw_pitch"])
        if not (math.isfinite(yaw0) and math.isfinite(pitch0)):
            self.head.update({"calibrating": False, "calibrated": False, "quality": "未取得中心"})
            return
        yl, yr = self._median(self.head["left_yaw"]), self._median(self.head["right_yaw"])
        pu, pd = self._median(self.head["up_pitch"]), self._median(self.head["down_pitch"])
        yaw_ok = math.isfinite(yl) and math.isfinite(yr) and (yl - yaw0) * (yr - yaw0) < 0 and min(abs(yl - yaw0), abs(yr - yaw0)) >= 0.025
        pitch_ok = math.isfinite(pu) and math.isfinite(pd) and (pu - pitch0) * (pd - pitch0) < 0 and min(abs(pu - pitch0), abs(pd - pitch0)) >= 0.008
        if not yaw_ok: yl, yr = yaw0 - 0.10, yaw0 + 0.10
        if not pitch_ok:
            sign = math.copysign(1.0, pu - pitch0) if math.isfinite(pu) and pu != pitch0 else -1.0
            pu, pd = pitch0 + sign * 0.04, pitch0 - sign * 0.04
        nx = [abs(self._normalize_axis(value, yaw0, yl, yr)) for value in self.head["center_yaw"]]
        ny = [abs(self._normalize_axis(value, pitch0, pu, pd)) for value in self.head["center_pitch"]]
        deadzone_x = self.head["deadzone_x"]
        deadzone_y = self.head["deadzone_y"]
        if yaw_ok and nx:
            deadzone_x = _clamp(self._qtile(nx, 0.99) * 1.8 + 0.02, 0.06, 0.30)
        if pitch_ok and ny:
            deadzone_y = _clamp(self._qtile(ny, 0.99) * 2.0 + 0.025, 0.08, 0.34)
        weak = []
        if not yaw_ok: weak.append("左右")
        if not pitch_ok: weak.append("上下")
        self.head.update({
            "yaw0": yaw0, "yaw_left": yl, "yaw_right": yr, "pitch0": pitch0,
            "pitch_up": pu, "pitch_down": pd, "torso0": self._median(self.head["torso_samples"]),
            "deadzone_x": deadzone_x, "deadzone_y": deadzone_y,
            "calibrating": False, "calibrated": True,
            "quality": "自动完成" if not weak else "降级：" + "、".join(weak),
            "filtered_x": 0.0, "filtered_y": 0.0, "output_x": 0.0, "output_y": 0.0, "last_update": 0.0,
        })

    def _update_head_locked(self, pose_map: dict[str, dict], now: float) -> None:
        torso = self._torso_length(pose_map)
        raw_yaw, raw_pitch = self._yaw_signal(pose_map), self._pitch_signal(pose_map, self.head["torso0"] if self.head["calibrated"] and math.isfinite(self.head["torso0"]) else torso)
        self.head["raw_yaw"], self.head["raw_pitch"] = raw_yaw, raw_pitch
        if self.head["calibrating"]:
            self._update_calibration_locked(raw_yaw, raw_pitch, torso, now)
        x = self._normalize_axis(raw_yaw, self.head["yaw0"], self.head["yaw_left"], self.head["yaw_right"]) if self.head["calibrated"] else 0.0
        y = self._normalize_axis(raw_pitch, self.head["pitch0"], self.head["pitch_up"], self.head["pitch_down"]) if self.head["calibrated"] else 0.0
        if self.head["invert_x"]: x = -x
        if self.head["invert_y"]: y = -y
        self.head["norm_x"], self.head["norm_y"] = x, y
        tx = self._curve_axis(x, self.head["deadzone_x"], self.head["max_percent_x"]) if self.head["enabled"] else 0.0
        ty = self._curve_axis(y, self.head["deadzone_y"], self.head["max_percent_y"]) if self.head["enabled"] else 0.0
        self.head["filtered_x"] = self._filter_axis(self.head["filtered_x"], tx, x, self.head["deadzone_x"], self.head["max_percent_x"])
        self.head["filtered_y"] = self._filter_axis(self.head["filtered_y"], ty, y, self.head["deadzone_y"], self.head["max_percent_y"])
        self.head["output_x"], self.head["output_y"] = self.head["filtered_x"], self.head["filtered_y"]
        if getattr(self.output, "enabled", True) and now - self.head["last_update"] >= HEAD_UPDATE_INTERVAL_S:
            self.head["last_update"] = now
            self._safe_output(self.output.apply, self.head["output_x"] / 100.0, self.head["output_y"] / 100.0)

    # ---------- safety/status ----------

    def _clear_body_outputs_locked(self) -> None:
        for state in self.zone_state.values():
            state.update({"inside": 0, "outside": 0, "pressed": False})
        self.zone_rects = {}
        self.motion_active.clear()
        for state in self.motion_debounce.values():
            state.update({"active": False, "on": 0, "off": 0})
        self.step.update({"left_was": False, "right_was": False, "last_side": "", "last_at": 0.0, "active_until": 0.0})
        self.head.update({"raw_yaw": math.nan, "raw_pitch": math.nan, "filtered_x": 0.0, "filtered_y": 0.0, "output_x": 0.0, "output_y": 0.0})
        self._safe_output(self.output.set_buttons, [], source="zones")
        self._safe_output(self.output.set_holds, [], source_group="motions")
        self._safe_output(self.output.apply, 0.0, 0.0)

    def _clear_body_locked(self) -> None:
        self.latest_pose = None
        self._clear_body_outputs_locked()

    def _safe_output(self, function, *args, **kwargs):
        try:
            result = function(*args, **kwargs)
            self.last_error = None
            return result
        except Exception as exc:
            self.last_error = str(exc)
            return None

    def status_locked(self, now: float) -> dict:
        pose_age = round(max(0.0, (now - self.body_last_at) * 1000.0)) if self.body_last_at else None
        zones = {
            name: {"rect": copy.deepcopy(self.zone_rects.get(name)), "pressed": bool(self.zone_state[name]["pressed"])}
            for name in BODY_ZONES
        }
        head = {
            "calibrated": bool(self.head["calibrated"]), "calibrating": bool(self.head["calibrating"]),
            "stage": self.head["stage"], "quality": self.head["quality"],
            "raw_yaw": self.head["raw_yaw"] if math.isfinite(self.head["raw_yaw"]) else None,
            "raw_pitch": self.head["raw_pitch"] if math.isfinite(self.head["raw_pitch"]) else None,
            "output_x": round(self.head["output_x"], 3), "output_y": round(self.head["output_y"], 3),
            "deadzone_x": self.head["deadzone_x"], "deadzone_y": self.head["deadzone_y"],
            "gamma": self.head["gamma"], "max_percent_x": self.head["max_percent_x"],
            "max_percent_y": self.head["max_percent_y"], "enabled": bool(self.head["enabled"]),
            "invert_x": bool(self.head["invert_x"]), "invert_y": bool(self.head["invert_y"]),
        }
        sensors = {
            source: {key: copy.deepcopy(value) for key, value in state.items() if key != "received_at"}
            | {"age_ms": round(max(0.0, (now - state["received_at"]) * 1000.0))}
            for source, state in self.sensor_sources.items()
        }
        return {
            "active_body_source": self.active_body_source, "pose_age_ms": pose_age,
            "width": self.width, "height": self.height, "pose": copy.deepcopy(self.latest_pose),
            "zones": zones, "buttons": self._pressed_keys_locked(), "motions": sorted(self.motion_active),
            "head": head, "handheld_sources": sensors, "last_error": self.last_error,
        }

    def status(self) -> dict:
        with self._lock:
            return self.status_locked(time.monotonic())

    def _watch_loop(self) -> None:
        while not self._stop.wait(0.05):
            now = time.monotonic()
            with self._lock:
                if self.active_body_source and self.body_last_at and now - self.body_last_at > self.watchdog_timeout:
                    self._clear_body_locked()
                    self.active_body_source = None
                    self.body_last_at = 0.0
                stale = [source for source, state in self.sensor_sources.items() if now - state["received_at"] > self.watchdog_timeout]
                for source in stale:
                    self.sensor_sources.pop(source, None)
                    self._safe_output(self.output.clear_source, source)

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            self._clear_body_locked()
            self.active_body_source = None
            for source in list(self.sensor_sources):
                self._safe_output(self.output.clear_source, source)
            self.sensor_sources.clear()


class CameraUnavailable(RuntimeError):
    """Native camera/MediaPipe dependency or device is unavailable."""


class NativeCameraService:
    """Native OpenCV + MediaPipe camera worker with a latest-frame pipeline.

    The capture path keeps the raw OpenCV frame as the canonical, unmirrored
    camera image.  The detector and JPEG preview both consume that same frame,
    so the normalized landmarks and the displayed image cannot drift because
    of a second mirror or aspect-ratio transform in the kernel.
    """

    BACKEND_AUTO = "auto"
    BACKEND_MSMF = "msmf"
    BACKEND_DSHOW = "dshow"
    REQUESTED_WIDTH = 640
    REQUESTED_HEIGHT = 480
    REQUESTED_FPS = 30
    PROBE_SECONDS = 2.5

    def __init__(self, kernel: ControlKernel, model_path=None, camera_index: int = 0) -> None:
        self.kernel = kernel
        self.model_path = model_path
        self.camera_index = int(camera_index)
        self.backend_preference = self.BACKEND_AUTO
        self.selected_backend: str | None = None
        self.selected_backend_name: str | None = None
        self.selected_fourcc: str | None = None
        self.requested_width = self.REQUESTED_WIDTH
        self.requested_height = self.REQUESTED_HEIGHT
        self.requested_fps = self.REQUESTED_FPS
        self.actual_capture_fps: float | None = None
        self._last_probe_results: list[dict] = []
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._capture_thread: threading.Thread | None = None
        self._inference_thread: threading.Thread | None = None
        self._preview_thread: threading.Thread | None = None
        self._capture = None
        self._detector = None
        self.running = False
        self.last_error: str | None = None
        self.last_frame_at = 0.0
        self.frames = 0
        self.capture_width = 0
        self.capture_height = 0
        self.dropped_frames = 0
        self.skipped_frames = 0
        self.last_pose_count = 0
        self.last_inference_at = 0.0
        self.last_latency_ms: float | None = None
        self.last_inference_ms: float | None = None
        self._latest_frame = None
        self._latest_sequence = 0
        self._latest_capture_at = 0.0
        self._last_inference_sequence = 0
        self._last_timestamp_ms = 0
        self._preview_jpeg: bytes | None = None
        self._preview_sequence = 0
        self._preview_at = 0.0
        self._capture_times: deque[float] = deque(maxlen=120)
        self._inference_times: deque[float] = deque(maxlen=120)
        self._inference_durations_ms: deque[float] = deque(maxlen=120)
        self._latencies_ms: deque[float] = deque(maxlen=120)

    @staticmethod
    def _normalize_backend(value: str | None) -> str:
        value = str(value or "auto").strip().lower()
        aliases = {
            "automatic": "auto", "default": "auto", "ms": "msmf",
            "mediafoundation": "msmf", "directshow": "dshow", "ds": "dshow",
        }
        value = aliases.get(value, value)
        return value if value in {"auto", "msmf", "dshow"} else "auto"

    @staticmethod
    def _backend_display_name(backend: str | None) -> str | None:
        return {"auto": "Auto", "msmf": "MSMF", "dshow": "DirectShow"}.get(backend, backend)

    @staticmethod
    def _decode_fourcc(value) -> str | None:
        try:
            value = int(value)
            if value <= 0:
                return None
            text = "".join(chr((value >> (8 * i)) & 0xFF) for i in range(4))
            return text if all(32 <= ord(ch) < 127 for ch in text) else None
        except Exception:
            return None

    @staticmethod
    def _valid_frame(frame) -> bool:
        return frame is not None and getattr(frame, "size", 0) > 0 and len(getattr(frame, "shape", ())) >= 2

    def _backend_cache_path(self) -> Path:
        """Keep hardware-specific selection outside the source tree/portable package."""
        root = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return root / "MotionControl" / "camera_backend.json"

    def _load_backend_cache(self) -> dict:
        path = self._backend_cache_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if int(data.get("camera_index", -1)) != self.camera_index:
                return {}
            backend = self._normalize_backend(data.get("backend"))
            if backend == "auto":
                return {}
            data["backend"] = backend
            return data
        except Exception:
            return {}

    def _save_backend_cache(self, actual_capture_fps: float | None = None) -> None:
        if not self.selected_backend or self.selected_backend in {self.BACKEND_AUTO, "default"}:
            return
        path = self._backend_cache_path()
        data = {
            "camera_index": self.camera_index,
            "backend": self.selected_backend,
            "fourcc": self.selected_fourcc,
            "requested_width": self.requested_width,
            "requested_height": self.requested_height,
            "requested_fps": self.requested_fps,
            "actual_capture_fps": self._round_or_none(actual_capture_fps or self.actual_capture_fps, 2),
            "saved_at_unix": round(time.time(), 3),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(path.suffix + ".tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, path)
        except OSError:
            # A cache is an optimization only; capture must still work if the
            # profile directory is read-only.
            pass

    def configure_backend(self, preference: str | None) -> dict:
        preference = self._normalize_backend(preference)
        with self._lock:
            if self.running and preference != self.backend_preference:
                raise CameraUnavailable("摄像头运行中不能切换采集后端，请先停止摄像头")
            self.backend_preference = preference
            return self.backend_config()

    def backend_config(self) -> dict:
        with self._lock:
            cache = self._load_backend_cache()
            return {
                "preference": self.backend_preference,
                "selected_backend": self.selected_backend,
                "selected_backend_name": self._backend_display_name(self.selected_backend_name),
                "fourcc": self.selected_fourcc,
                "requested_width": self.requested_width,
                "requested_height": self.requested_height,
                "requested_fps": self.requested_fps,
                "actual_capture_fps": self._round_or_none(self.actual_capture_fps, 2),
                "cache_path": str(self._backend_cache_path()),
                "cached": bool(cache),
                "probe_results": list(self._last_probe_results),
            }

    @staticmethod
    def _rate(times: deque[float]) -> float | None:
        if len(times) < 2:
            return None
        elapsed = times[-1] - times[0]
        return (len(times) - 1) / elapsed if elapsed > 1e-6 else None

    @staticmethod
    def _p95(values: deque[float]) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * 0.95) - 1))
        return ordered[index]

    @staticmethod
    def _round_or_none(value: float | None, digits: int = 1):
        return round(value, digits) if value is not None and math.isfinite(value) else None

    def _reset_runtime_locked(self) -> None:
        self.frames = 0
        self.capture_width = 0
        self.capture_height = 0
        self.dropped_frames = 0
        self.skipped_frames = 0
        self.last_pose_count = 0
        self.last_inference_at = 0.0
        self.last_latency_ms = None
        self.last_inference_ms = None
        self._latest_frame = None
        self._latest_sequence = 0
        self._latest_capture_at = 0.0
        self._last_inference_sequence = 0
        self._last_timestamp_ms = 0
        self._preview_jpeg = None
        self._preview_sequence = 0
        self._preview_at = 0.0
        self._capture_times.clear()
        self._inference_times.clear()
        self._inference_durations_ms.clear()
        self._latencies_ms.clear()

    def configure_model(self, model_path) -> None:
        with self._lock:
            self.model_path = model_path

    def _create_detector(self):
        if not self.model_path or not getattr(self.model_path, "is_file", lambda: False)():
            raise CameraUnavailable("MediaPipe Full task 未找到")
        try:
            import mediapipe as mp
            from mediapipe.tasks import python
            from mediapipe.tasks.python import vision
        except Exception as exc:
            raise CameraUnavailable("本地 Python 未安装 mediapipe；电脑摄像头内核无法启动") from exc
        options = vision.PoseLandmarkerOptions(
            base_options=python.BaseOptions(model_asset_path=str(self.model_path)),
            running_mode=vision.RunningMode.VIDEO,
            num_poses=1,
            min_pose_detection_confidence=0.35,
            min_pose_presence_confidence=0.35,
            min_tracking_confidence=0.35,
        )
        return mp, vision.PoseLandmarker.create_from_options(options)

    @staticmethod
    def _backend_api(cv2, backend: str):
        if backend == NativeCameraService.BACKEND_MSMF:
            return getattr(cv2, "CAP_MSMF", 0)
        if backend == NativeCameraService.BACKEND_DSHOW:
            return getattr(cv2, "CAP_DSHOW", 0)
        return 0

    def _open_capture(self, cv2, backend: str, fourcc: str | None = None):
        """Open one camera candidate, configure it, and validate its first frame."""
        if backend == self.BACKEND_DSHOW and not fourcc:
            fourcc = "MJPG"
        api = self._backend_api(cv2, backend)
        capture = cv2.VideoCapture(self.camera_index, api)
        if not capture.isOpened():
            try:
                capture.release()
            except Exception:
                pass
            return None
        try:
            # DSHOW is substantially more reliable at this size when MJPG is
            # requested; MSMF is left to negotiate its native format.
            if backend == self.BACKEND_DSHOW and fourcc:
                capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.requested_width)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.requested_height)
            capture.set(cv2.CAP_PROP_FPS, self.requested_fps)
            # This property is advisory on MSMF and may simply return False.
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            ok, frame = capture.read()
            if not ok or not self._valid_frame(frame):
                capture.release()
                return None
            actual_fourcc = fourcc or self._decode_fourcc(capture.get(cv2.CAP_PROP_FOURCC))
            return capture, frame, actual_fourcc
        except Exception:
            try:
                capture.release()
            except Exception:
                pass
            return None

    def _probe_backend(self, cv2, backend: str, duration_s: float) -> dict:
        requested_fourcc = "MJPG" if backend == self.BACKEND_DSHOW else None
        opened = self._open_capture(cv2, backend, requested_fourcc)
        if opened is None:
            return {
                "backend": backend,
                "backend_name": self._backend_display_name(backend),
                "fourcc": requested_fourcc,
                "opened": False,
                "valid_frames": 0,
                "elapsed_s": 0.0,
                "read_fps": None,
                "resolution": None,
                "error": "open_or_first_frame_failed",
            }
        capture, first_frame, actual_fourcc = opened
        # Measure read cadence after the first valid frame.  Camera backend
        # negotiation/open latency is reported separately by elapsed_s in the
        # failure case and must not make a healthy backend look slower.
        started = time.perf_counter()
        count = 1
        resolution = {
            "width": int(first_frame.shape[1]),
            "height": int(first_frame.shape[0]),
        }
        deadline = started + max(0.5, float(duration_s))
        try:
            while time.perf_counter() < deadline:
                ok, frame = capture.read()
                if ok and self._valid_frame(frame):
                    count += 1
                    resolution = {"width": int(frame.shape[1]), "height": int(frame.shape[0])}
        finally:
            try:
                capture.release()
            except Exception:
                pass
        elapsed = max(1e-6, time.perf_counter() - started)
        return {
            "backend": backend,
            "backend_name": self._backend_display_name(backend),
            "fourcc": actual_fourcc,
            "opened": True,
            "valid_frames": count,
            "elapsed_s": round(elapsed, 3),
            "read_fps": round((count - 1) / elapsed, 2),
            "resolution": resolution,
            "error": None,
        }

    def _probe_candidates(self, cv2, duration_s: float | None = None) -> list[dict]:
        duration_s = self.PROBE_SECONDS if duration_s is None else max(0.5, float(duration_s))
        # Keep the comparison deterministic.  Each camera handle is released
        # before the next backend is opened, so Windows cannot share a stale
        # capture buffer between candidates.
        candidates = [self.BACKEND_MSMF, self.BACKEND_DSHOW]
        results = [self._probe_backend(cv2, backend, duration_s) for backend in candidates]
        self._last_probe_results = results
        return results

    def benchmark_backends(self, duration_s: float = PROBE_SECONDS) -> list[dict]:
        """Run a short real read-FPS comparison without starting inference."""
        with self._lock:
            if self.running:
                raise CameraUnavailable("摄像头运行中不能进行采集后端测速")
            try:
                import cv2
            except Exception as exc:
                raise CameraUnavailable("本地 Python 未安装 opencv-python；无法测速摄像头后端") from exc
            return self._probe_candidates(cv2, duration_s)

    def _select_capture(self, cv2):
        preference = self.backend_preference
        cache = self._load_backend_cache() if preference == self.BACKEND_AUTO else {}
        if cache:
            cached_backend = self._normalize_backend(cache.get("backend"))
            opened = self._open_capture(cv2, cached_backend, cache.get("fourcc"))
            if opened is not None:
                capture, first_frame, actual_fourcc = opened
                self.selected_backend = cached_backend
                self.selected_backend_name = cached_backend
                self.selected_fourcc = actual_fourcc
                self.actual_capture_fps = None
                return capture, first_frame

        if preference != self.BACKEND_AUTO:
            opened = self._open_capture(
                cv2, preference, "MJPG" if preference == self.BACKEND_DSHOW else None,
            )
            if opened is None:
                raise CameraUnavailable(f"电脑摄像头 {self.camera_index} 的 {self._backend_display_name(preference)} 无法打开")
            capture, first_frame, actual_fourcc = opened
            self.selected_backend = preference
            self.selected_backend_name = preference
            self.selected_fourcc = actual_fourcc
            self.actual_capture_fps = None
            return capture, first_frame

        results = self._probe_candidates(cv2)
        valid = [item for item in results if item.get("opened") and item.get("valid_frames", 0) > 0]
        if not valid:
            # Preserve the old OpenCV default as a final fallback for cameras
            # where a backend-specific probe cannot negotiate a stream.
            opened = self._open_capture(cv2, "default", None)
            if opened is None:
                raise CameraUnavailable(f"电脑摄像头 {self.camera_index} 无法打开")
            capture, first_frame, actual_fourcc = opened
            self.selected_backend = "default"
            self.selected_backend_name = "default"
            self.selected_fourcc = actual_fourcc
            self.actual_capture_fps = None
            return capture, first_frame
        valid.sort(key=lambda item: float(item.get("read_fps") or 0.0), reverse=True)
        winner = valid[0]
        opened = None
        for candidate in valid:
            backend = candidate["backend"]
            opened = self._open_capture(cv2, backend, candidate.get("fourcc"))
            if opened is not None:
                winner = candidate
                break
        if opened is None:
            opened = self._open_capture(cv2, "default", None)
            if opened is None:
                raise CameraUnavailable(f"电脑摄像头 {self.camera_index} 的候选后端复开失败")
            capture, first_frame, actual_fourcc = opened
            self.selected_backend = "default"
            self.selected_backend_name = "default"
            self.selected_fourcc = actual_fourcc
            self.actual_capture_fps = None
            return capture, first_frame
        capture, first_frame, actual_fourcc = opened
        self.selected_backend = backend
        self.selected_backend_name = backend
        self.selected_fourcc = actual_fourcc
        self.actual_capture_fps = float(winner.get("read_fps")) if winner.get("read_fps") else None
        self._save_backend_cache(self.actual_capture_fps)
        return capture, first_frame

    def start(self) -> dict:
        with self._lock:
            if self.running:
                return self.status()
            try:
                import cv2
            except Exception as exc:
                self.last_error = "本地 Python 未安装 opencv-python；电脑摄像头内核无法启动"
                raise CameraUnavailable(self.last_error) from exc
            detector = None
            try:
                mp, detector = self._create_detector()
                capture, first_frame = self._select_capture(cv2)
            except CameraUnavailable as exc:
                if detector is not None:
                    try:
                        detector.close()
                    except Exception:
                        pass
                self.last_error = str(exc)
                raise
            except Exception as exc:
                self.last_error = str(exc)
                try:
                    detector.close()
                except Exception:
                    pass
                raise CameraUnavailable(f"电脑摄像头内核初始化失败：{exc}") from exc
            self._capture, self._detector, self._mp = capture, detector, mp
            self._reset_runtime_locked()
            # The first frame was consumed only for backend validation.  It is
            # intentionally not pushed into the inference path so all timing
            # starts at the same boundary for every backend.
            self._stop.clear()
            self.running = True
            self.last_error = None
            self._capture_thread = threading.Thread(target=self._capture_loop, name="motion-camera-capture", daemon=True)
            self._inference_thread = threading.Thread(target=self._inference_loop, name="motion-camera-inference", daemon=True)
            self._preview_thread = threading.Thread(target=self._preview_loop, name="motion-camera-preview", daemon=True)
            self._thread = self._inference_thread
            self._capture_thread.start()
            self._inference_thread.start()
            self._preview_thread.start()
            return self.status()

    def _capture_loop(self) -> None:
        try:
            import cv2
            while not self._stop.wait(0.001):
                ok, frame = self._capture.read()
                if not ok:
                    with self._condition:
                        if not self._stop.is_set():
                            self.last_error = "电脑摄像头读取失败"
                            self._stop.set()
                        self._condition.notify_all()
                    break
                height, width = frame.shape[:2]
                captured_at = time.monotonic()
                with self._condition:
                    # There is deliberately only one pending frame.  Replacing
                    # it is an observable drop, not an unbounded queue.
                    if self._latest_frame is not None and self._latest_sequence > self._last_inference_sequence:
                        self.dropped_frames += 1
                    self._latest_frame = frame
                    self._latest_sequence += 1
                    self._latest_capture_at = captured_at
                    self.capture_width, self.capture_height = int(width), int(height)
                    self._capture_times.append(captured_at)
                    self.actual_capture_fps = self._rate(self._capture_times)
                    self._condition.notify_all()
        except Exception as exc:
            with self._condition:
                if not self._stop.is_set():
                    self.last_error = str(exc)
                    self._stop.set()
                self._condition.notify_all()

    def _inference_loop(self) -> None:
        try:
            import cv2
            while True:
                with self._condition:
                    while not self._stop.is_set() and self._latest_sequence <= self._last_inference_sequence:
                        self._condition.wait(0.10)
                    if self._stop.is_set():
                        break
                    sequence = self._latest_sequence
                    frame = self._latest_frame
                    captured_at = self._latest_capture_at
                    width, height = self.capture_width, self.capture_height
                    skipped = max(0, sequence - self._last_inference_sequence - 1)
                    self._last_inference_sequence = sequence
                    self.skipped_frames += skipped
                if frame is None:
                    continue
                started = time.perf_counter()
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
                timestamp_ms = max(int(captured_at * 1000), self._last_timestamp_ms + 1)
                self._last_timestamp_ms = timestamp_ms
                result = self._detector.detect_for_video(image, timestamp_ms)
                landmarks = result.pose_landmarks[0] if result.pose_landmarks else None
                pose_map = None
                if landmarks:
                    pose_map = {
                        MP_NAMES[index]: {
                            "x": _finite(point.x), "y": _finite(point.y), "z": _finite(point.z),
                            "score": _finite(getattr(point, "visibility", getattr(point, "presence", 1.0)), 1.0),
                        }
                        for index, point in enumerate(landmarks)
                    }
                self.kernel.handle_pose_map("computer_camera", pose_map, width=width, height=height)
                finished = time.monotonic()
                inference_ms = (time.perf_counter() - started) * 1000.0
                with self._condition:
                    self.last_frame_at = finished
                    self.last_inference_at = finished
                    self.last_inference_ms = inference_ms
                    self.last_latency_ms = max(0.0, (finished - captured_at) * 1000.0)
                    self.last_pose_count = len(result.pose_landmarks or [])
                    self._inference_times.append(finished)
                    self._inference_durations_ms.append(inference_ms)
                    self._latencies_ms.append(self.last_latency_ms)
                    self.frames += 1
        except Exception as exc:
            with self._condition:
                if not self._stop.is_set():
                    self.last_error = str(exc)
                    self._stop.set()
                self._condition.notify_all()

    def _preview_loop(self) -> None:
        """Encode the newest raw frame separately from detector inference."""
        try:
            import cv2
            last_sequence = 0
            next_encode_at = 0.0
            while True:
                with self._condition:
                    while not self._stop.is_set() and self._latest_sequence <= last_sequence:
                        self._condition.wait(0.10)
                    if self._stop.is_set():
                        break
                    wait = next_encode_at - time.monotonic()
                    if wait > 0:
                        self._condition.wait(min(wait, 0.10))
                        continue
                    sequence = self._latest_sequence
                    frame = self._latest_frame
                    last_sequence = sequence
                if frame is None:
                    continue
                ok, encoded = cv2.imencode(
                    ".jpg", frame,
                    [int(cv2.IMWRITE_JPEG_QUALITY), 78],
                )
                next_encode_at = time.monotonic() + (1.0 / 15.0)
                if not ok:
                    continue
                with self._condition:
                    self._preview_jpeg = encoded.tobytes()
                    self._preview_sequence = sequence
                    self._preview_at = time.monotonic()
        except Exception as exc:
            with self._condition:
                if not self._stop.is_set():
                    self.last_error = str(exc)
                    self._stop.set()
                self._condition.notify_all()

    def stop(self) -> dict:
        with self._lock:
            self._stop.set()
            self._condition.notify_all()
            threads = [self._capture_thread, self._inference_thread, self._preview_thread]
        for thread in threads:
            if thread and thread is not threading.current_thread():
                thread.join(timeout=1.0)
        with self._lock:
            self._save_backend_cache(self.actual_capture_fps)
            capture, detector = self._capture, self._detector
            self._capture = self._detector = None
            self._capture_thread = self._inference_thread = self._preview_thread = self._thread = None
            self._latest_frame = None
            self._preview_jpeg = None
            self.running = False
        if capture is not None:
            try: capture.release()
            except Exception: pass
        if detector is not None:
            try: detector.close()
            except Exception: pass
        self.kernel.clear_source("computer_camera")
        return self.status()

    def status(self) -> dict:
        with self._lock:
            return {
                "running": self.running, "camera_index": self.camera_index,
                "frames": self.frames, "last_frame_age_ms": round(max(0.0, (time.monotonic() - self.last_frame_at) * 1000.0)) if self.last_frame_at else None,
                "last_error": self.last_error, "model_path": str(self.model_path) if self.model_path else None,
                "resolution": {"width": self.capture_width, "height": self.capture_height},
                "backend_preference": self.backend_preference,
                "backend": self.selected_backend,
                "backend_name": self._backend_display_name(self.selected_backend_name),
                "fourcc": self.selected_fourcc,
                "requested_fps": self.requested_fps,
                "actual_capture_fps": self._round_or_none(self.actual_capture_fps, 2),
                "coordinate_space": "camera_frame_normalized_unmirrored",
                "preview_mirrored": False, "coordinates_mirrored": False,
            }

    def performance(self) -> dict:
        with self._lock:
            now = time.monotonic()
            resolution = {"width": self.capture_width, "height": self.capture_height}
            return {
                "source": "computer",
                "model": "MediaPipe Pose Full",
                "camera_resolution": resolution,
                "capture_fps": self._round_or_none(self._rate(self._capture_times), 2),
                "backend": self.selected_backend,
                "backend_name": self._backend_display_name(self.selected_backend_name),
                "fourcc": self.selected_fourcc,
                "requested_fps": self.requested_fps,
                "actual_capture_fps": self._round_or_none(self.actual_capture_fps, 2),
                "inference_fps": self._round_or_none(self._rate(self._inference_times), 2),
                "inference_avg_ms": self._round_or_none(
                    sum(self._inference_durations_ms) / len(self._inference_durations_ms)
                    if self._inference_durations_ms else None,
                ),
                "inference_p95_ms": self._round_or_none(self._p95(self._inference_durations_ms)),
                "pose_frame_age_ms": round(max(0.0, (now - self.last_inference_at) * 1000.0)) if self.last_inference_at else None,
                "total_latency_ms": self._round_or_none(self.last_latency_ms),
                "dropped_frames": int(self.dropped_frames),
                "skipped_frames": int(self.skipped_frames),
                "web_render_fps": None,
                "recent_humans": int(self.last_pose_count),
                "preview_ready": bool(self._preview_jpeg),
                "running": bool(self.running),
                "last_error": self.last_error,
            }

    def latest_preview(self) -> bytes | None:
        with self._lock:
            return bytes(self._preview_jpeg) if self._preview_jpeg else None


class LocalControlRuntime:
    """Owns source selection and keeps camera/sensor/body lifetimes atomic."""

    def __init__(self, kernel: ControlKernel, camera: NativeCameraService) -> None:
        self.kernel, self.camera = kernel, camera
        self._lock = threading.RLock()
        self.body_mode = "computer"

    def configure_model(self, model_path) -> None:
        self.camera.configure_model(model_path)

    def configure_camera_backend(self, preference: str | None) -> dict:
        return self.camera.configure_backend(preference)

    def camera_backend_config(self) -> dict:
        return self.camera.backend_config()

    def set_source(self, source: str, *, start_computer: bool = True) -> dict:
        source = str(source).strip().lower()
        if source not in {"computer", "phone"}:
            raise ValueError("source must be computer or phone")
        with self._lock:
            self.camera.stop()
            self.kernel.clear_body()
            self.body_mode = source
            if source == "computer" and start_computer:
                self.camera.start()
            return self.status()

    def stop_body(self) -> dict:
        with self._lock:
            self.camera.stop()
            self.kernel.clear_body()
            return self.status()

    def accept_mobile_pose(self, source_id: str, message: dict) -> dict:
        with self._lock:
            if self.body_mode != "phone":
                return self.status()
        return self.kernel.handle_pose_message(source_id, message)

    def accept_sensor(self, source_id: str, **kwargs) -> dict:
        return self.kernel.handle_sensor(source_id, **kwargs)

    def clear_source(self, source_id: str) -> dict:
        return self.kernel.clear_source(source_id)

    def configure_motions(self, items) -> None:
        self.kernel.configure_motions(items)

    def status(self) -> dict:
        with self._lock:
            return {"body_mode": self.body_mode, "camera": self.camera.status(), "kernel": self.kernel.status()}

    def performance(self) -> dict:
        with self._lock:
            return self.camera.performance()

    def latest_preview(self) -> bytes | None:
        return self.camera.latest_preview()

    def close(self) -> None:
        self.camera.stop()
        self.kernel.close()
