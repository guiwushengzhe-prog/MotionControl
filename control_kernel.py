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
CALIBRATION_PREPARE_DURATION_S = 5.0
CALIBRATION_STAGE_DURATION_S = 1.5
CALIBRATION_TOTAL_DURATION_S = 12.5
CALIBRATION_TRANSITION_DURATION_S = 0.60
CALIBRATION_POSE_TIMEOUT_S = 0.30
CALIBRATION_DIAGNOSTIC_MAX_VALUES = 512
# A calibration endpoint only needs to be measurably different from the
# neutral anchor. The personal dead-zone still protects runtime output.
CALIBRATION_MIN_AXIS_SEPARATION = 0.0005
CALIBRATION_GUIDANCE_DELTA = 0.005
HEAD_SIGNAL_VERSION = "head-shoulder-v2"
CALIBRATION_STAGES = (
    ("center", "正视"),
    ("left", "左转"),
    ("right", "右转"),
    ("up", "抬头"),
    ("down", "低头"),
)

# These are the reference-controller defaults.  They are active from startup
# so calibration is an optional personalisation step, not a prerequisite.
DEFAULT_HEAD_PARAMS = {
    "signal_version": HEAD_SIGNAL_VERSION,
    "yaw0": 0.0, "yaw_left": -0.10, "yaw_right": 0.10,
    # v2 pitch is (nose - selected face midpoint) / shoulder width.  A
    # neutral webcam pose is around 0.15 in this scale; these defaults are
    # deliberately expressed in that new signal space, not the old hip-based
    # torso space.
    "pitch0": 0.15, "pitch_up": 0.08, "pitch_down": 0.22,
    "shoulder_scale0": math.nan,
    "deadzone_x": 0.08, "deadzone_y": 0.12, "gamma": 2.2,
    "max_percent_x": 60.0, "max_percent_y": 45.0,
    "enabled": True, "invert_x": False, "invert_y": False,
}

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
        self.pose_last_valid_at = 0.0
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
            "calibrated": True, "calibrating": False, "stage": "", "stage_label": "",
            "signal_version": HEAD_SIGNAL_VERSION, "face_pair": "",
            "stage_started": 0.0, "calibration_started": 0.0,
            "stage_duration": CALIBRATION_STAGE_DURATION_S, "center_yaw": [], "center_pitch": [], "left_yaw": [],
            "right_yaw": [], "up_pitch": [], "down_pitch": [], "shoulder_scale_samples": [],
            "stage_valid_s": 0.0, "stage_required_s": CALIBRATION_STAGE_DURATION_S,
            "stage_last_sample_at": 0.0, "stage_pause_reason": "", "stage_missing_parts": [],
            "stage_transition_until": 0.0, "stage_transition_message": "",
            "stage_signal_value": None, "stage_signal_center": None,
            "stage_signal_delta": None, "stage_signal_goal": CALIBRATION_GUIDANCE_DELTA,
            "stage_signal_progress": 0.0,
            "calibration_profile": "default", "calibration_message": "",
            "calibration_notice": "", "calibration_notice_text": "", "calibration_notice_until": 0.0,
            "calibration_diag": self._new_calibration_diagnostic(),
            "last_calibration_diagnostic": None,
            "quality": "当前使用：默认参数",
            "yaw0": DEFAULT_HEAD_PARAMS["yaw0"], "yaw_left": DEFAULT_HEAD_PARAMS["yaw_left"],
            "yaw_right": DEFAULT_HEAD_PARAMS["yaw_right"], "pitch0": DEFAULT_HEAD_PARAMS["pitch0"],
            "pitch_up": DEFAULT_HEAD_PARAMS["pitch_up"], "pitch_down": DEFAULT_HEAD_PARAMS["pitch_down"],
            "shoulder_scale0": DEFAULT_HEAD_PARAMS["shoulder_scale0"],
            "raw_yaw": math.nan, "raw_pitch": math.nan, "norm_x": 0.0, "norm_y": 0.0,
            "filtered_x": 0.0, "filtered_y": 0.0, "output_x": 0.0, "output_y": 0.0,
            "last_update": 0.0,
            "deadzone_x": DEFAULT_HEAD_PARAMS["deadzone_x"], "deadzone_y": DEFAULT_HEAD_PARAMS["deadzone_y"],
            "gamma": DEFAULT_HEAD_PARAMS["gamma"],
            "max_percent_x": DEFAULT_HEAD_PARAMS["max_percent_x"],
            "max_percent_y": DEFAULT_HEAD_PARAMS["max_percent_y"],
            "enabled": DEFAULT_HEAD_PARAMS["enabled"],
            "invert_x": DEFAULT_HEAD_PARAMS["invert_x"], "invert_y": DEFAULT_HEAD_PARAMS["invert_y"],
        }
        self.sensor_sources: dict[str, dict] = {}
        self._thread.start()

    @staticmethod
    def _pose_map_is_valid(pose_map: dict[str, dict] | None) -> bool:
        if not isinstance(pose_map, dict) or len(pose_map) < len(MP_NAMES):
            return False
        required = (
            "nose", "left_ear", "right_ear", "left_shoulder", "right_shoulder",
            "left_hip", "right_hip",
        )
        if any(name not in pose_map for name in MP_NAMES):
            return False
        for name in MP_NAMES:
            point = pose_map.get(name)
            if not isinstance(point, dict):
                return False
            if not all(math.isfinite(_finite(point.get(axis), math.nan)) for axis in ("x", "y", "z")):
                return False
        return all(_score(pose_map[name]) >= 0.30 for name in required)

    @staticmethod
    def _point_has_xy(point: dict | None, minimum_score: float) -> bool:
        """Return whether one point used by head control is usable.

        Calibration must not be gated by unrelated low-visibility landmarks.
        The head formulas only need finite nose/face coordinates and a stable
        shoulder-width scale, so this check deliberately ignores hips and all
        other unrelated MediaPipe points.
        """
        if not isinstance(point, dict) or _score(point) < minimum_score:
            return False
        return all(math.isfinite(_finite(point.get(axis), math.nan)) for axis in ("x", "y"))

    @staticmethod
    def _new_calibration_diagnostic() -> dict:
        """Create the in-memory, bounded diagnostic state for one retry.

        The values are signal summaries rather than pose frames: this keeps
        the persistent record small while making an unsuccessful real retry
        explainable (valid/invalid counts, pause reasons, and pitch/yaw
        distributions).
        """
        return {
            "started_at_unix": time.time(),
            "stage_attempts": [],
            "current": None,
            "final_check": None,
        }

    @staticmethod
    def _diagnostic_stats(values: list[float] | None) -> dict:
        finite = sorted(float(value) for value in (values or []) if math.isfinite(float(value)))
        if not finite:
            return {"count": 0, "min": None, "max": None, "median": None}
        return {
            "count": len(finite),
            "min": finite[0],
            "max": finite[-1],
            "median": finite[len(finite) // 2],
        }

    @classmethod
    def _face_pair_available(cls, pose_map: dict[str, dict] | None, pair: str) -> bool:
        names = ("left_eye", "right_eye") if pair == "eyes" else ("left_ear", "right_ear")
        return all(cls._point_has_xy(pose_map.get(name), 0.35) for name in names) if isinstance(pose_map, dict) else False

    @classmethod
    def _calibration_pose_is_valid(cls, pose_map: dict[str, dict] | None, preferred_face_pair: str = "") -> bool:
        """Check only the fixed v2 head + shoulder landmarks."""
        return bool(cls._calibration_pose_diagnostics(pose_map, preferred_face_pair)["valid"])

    @classmethod
    def _calibration_pose_diagnostics(cls, pose_map: dict[str, dict] | None, preferred_face_pair: str = "") -> dict:
        """Return structured validity details without making hips mandatory."""
        if not isinstance(pose_map, dict):
            return {"valid": False, "reason": "未检测到人体，请进入画面", "missing_parts": ["人体"]}
        missing: list[str] = []
        if not cls._point_has_xy(pose_map.get("nose"), 0.35):
            missing.append("鼻")
        pair = preferred_face_pair if preferred_face_pair in {"eyes", "ears"} else ""
        if pair:
            if not cls._face_pair_available(pose_map, pair):
                missing.append("双眼" if pair == "eyes" else "双耳")
        elif not (cls._face_pair_available(pose_map, "eyes") or cls._face_pair_available(pose_map, "ears")):
            missing.append("双眼或双耳")
        if not (
            cls._point_has_xy(pose_map.get("left_shoulder"), 0.30)
            and cls._point_has_xy(pose_map.get("right_shoulder"), 0.30)
        ):
            missing.append("双肩")
        if not missing:
            return {"valid": True, "reason": "", "missing_parts": [], "face_pair": pair or "auto"}
        if "双肩" in missing:
            reason = "请再退后，确保头和双肩入镜"
        elif "鼻" in missing or any(part in missing for part in ("双眼", "双耳", "双眼或双耳")):
            reason = "脸部点不清楚，请正对摄像头"
        else:
            reason = "请保持头部和双肩在画面内"
        return {"valid": False, "reason": reason, "missing_parts": missing, "face_pair": pair or "auto"}

    def _pose_ready_reason_locked(self, now: float | None = None) -> str | None:
        now = time.monotonic() if now is None else now
        if not self.active_body_source:
            return "没有正在运行的人体来源"
        if not self.body_last_at or now - self.body_last_at > self.watchdog_timeout:
            return "人体来源没有持续发送姿态"
        if not self._calibration_pose_is_valid(self.latest_pose, self.head.get("face_pair", "")):
            return "当前头控关键点不足（鼻、双眼/双耳、双肩）"
        if self.pose_last_valid_at and now - self.pose_last_valid_at > self.watchdog_timeout:
            return "人体姿态已过期"
        return None

    def has_valid_pose(self) -> bool:
        with self._lock:
            return self._pose_ready_reason_locked() is None

    def _calibration_diag_current_locked(self) -> dict | None:
        diag = self.head.get("calibration_diag")
        current = diag.get("current") if isinstance(diag, dict) else None
        return current if isinstance(current, dict) else None

    def _record_calibration_frame_locked(
        self, *, valid: bool, reason: str = "", missing_parts=None,
        raw_yaw: float = math.nan, raw_pitch: float = math.nan,
    ) -> None:
        current = self._calibration_diag_current_locked()
        if current is None:
            return
        if valid:
            current["valid_frames"] += 1
            if math.isfinite(raw_yaw) and len(current["yaw_values"]) < CALIBRATION_DIAGNOSTIC_MAX_VALUES:
                current["yaw_values"].append(float(raw_yaw))
            if math.isfinite(raw_pitch) and len(current["pitch_values"]) < CALIBRATION_DIAGNOSTIC_MAX_VALUES:
                current["pitch_values"].append(float(raw_pitch))
            return
        current["invalid_frames"] += 1
        reason = str(reason or "未说明").strip() or "未说明"
        current["pause_reasons"][reason] = current["pause_reasons"].get(reason, 0) + 1
        for part in dict.fromkeys(str(x) for x in (missing_parts or []) if str(x)):
            current["missing_parts"][part] = current["missing_parts"].get(part, 0) + 1

    def _finalize_calibration_diag_stage_locked(self, completed: bool, reason: str = "") -> None:
        diag = self.head.get("calibration_diag")
        current = diag.get("current") if isinstance(diag, dict) else None
        if not isinstance(current, dict):
            return
        entry = {
            "stage": current["stage"],
            "label": current["label"],
            "started_at_unix": current["started_at_unix"],
            "ended_at_unix": time.time(),
            "valid_frames": current["valid_frames"],
            "invalid_frames": current["invalid_frames"],
            "valid_s": round(float(self.head.get("stage_valid_s") or 0.0), 3),
            "completed": bool(completed),
            "reason": str(reason or ""),
            "pause_reasons": dict(current["pause_reasons"]),
            "missing_parts": dict(current["missing_parts"]),
            "yaw": self._diagnostic_stats(current["yaw_values"]),
            "pitch": self._diagnostic_stats(current["pitch_values"]),
        }
        diag["stage_attempts"].append(entry)
        diag["current"] = None

    @staticmethod
    def _calibration_axis_check(center: float, left: float, right: float) -> dict:
        """Check labeled left/center/right anchors without a large magic threshold."""
        finite = all(math.isfinite(value) for value in (center, left, right))
        left_delta = left - center if finite else math.nan
        right_delta = right - center if finite else math.nan
        min_separation = (
            min(abs(left_delta), abs(right_delta)) if finite else math.nan
        )
        span = abs(left - right) if finite else math.nan
        ordered = bool(finite and left_delta * right_delta < 0.0)
        separated = bool(
            finite
            and min_separation >= CALIBRATION_MIN_AXIS_SEPARATION
            and span >= CALIBRATION_MIN_AXIS_SEPARATION
        )
        return {
            "finite": finite,
            "ordered": ordered,
            "separated": separated,
            "ok": bool(ordered and separated),
            "left_delta": left_delta if finite else None,
            "right_delta": right_delta if finite else None,
            "min_separation": min_separation if finite else None,
            "span": span if finite else None,
            "minimum_separation": CALIBRATION_MIN_AXIS_SEPARATION,
        }

    def _set_calibration_final_check_locked(
        self, yaw0: float, yl: float, yr: float,
        pitch0: float, pu: float, pd: float,
        yaw_ok: bool, pitch_ok: bool,
    ) -> None:
        diag = self.head.get("calibration_diag")
        if not isinstance(diag, dict):
            return
        yaw_check = self._calibration_axis_check(yaw0, yl, yr)
        pitch_check = self._calibration_axis_check(pitch0, pu, pd)
        diag["final_check"] = {
            "center_yaw": self._diagnostic_stats(self.head.get("center_yaw")),
            "left_yaw": self._diagnostic_stats(self.head.get("left_yaw")),
            "right_yaw": self._diagnostic_stats(self.head.get("right_yaw")),
            "center_pitch": self._diagnostic_stats(self.head.get("center_pitch")),
            "up_pitch": self._diagnostic_stats(self.head.get("up_pitch")),
            "down_pitch": self._diagnostic_stats(self.head.get("down_pitch")),
            "yaw_center_median": yaw0 if math.isfinite(yaw0) else None,
            "yaw_left_median": yl if math.isfinite(yl) else None,
            "yaw_right_median": yr if math.isfinite(yr) else None,
            "pitch_center_median": pitch0 if math.isfinite(pitch0) else None,
            "pitch_up_median": pu if math.isfinite(pu) else None,
            "pitch_down_median": pd if math.isfinite(pd) else None,
            "yaw_threshold": CALIBRATION_MIN_AXIS_SEPARATION,
            "pitch_threshold": CALIBRATION_MIN_AXIS_SEPARATION,
            "yaw_ok": bool(yaw_ok),
            "pitch_ok": bool(pitch_ok),
            "yaw_order_correct": yaw_check["ordered"],
            "pitch_order_correct": pitch_check["ordered"],
            "yaw_min_separation": yaw_check["min_separation"],
            "pitch_min_separation": pitch_check["min_separation"],
            "yaw_left_delta": (yl - yaw0) if math.isfinite(yl) and math.isfinite(yaw0) else None,
            "yaw_right_delta": (yr - yaw0) if math.isfinite(yr) and math.isfinite(yaw0) else None,
            "pitch_up_delta": (pu - pitch0) if math.isfinite(pu) and math.isfinite(pitch0) else None,
            "pitch_down_delta": (pd - pitch0) if math.isfinite(pd) and math.isfinite(pitch0) else None,
            "pitch_span": (
                abs(pu - pd) if math.isfinite(pu) and math.isfinite(pd) else None
            ),
        }

    def _persist_calibration_diagnostic_locked(self, event: str, reason: str = "") -> None:
        diag = self.head.get("calibration_diag")
        if not isinstance(diag, dict):
            return
        summary = {
            "recorded_at_unix": time.time(),
            "event": str(event),
            "reason": str(reason or ""),
            "signal_version": self.head.get("signal_version", HEAD_SIGNAL_VERSION),
            "face_pair": self.head.get("face_pair", ""),
            "started_at_unix": diag.get("started_at_unix"),
            "stage_attempts": copy.deepcopy(diag.get("stage_attempts") or []),
            "final_check": copy.deepcopy(diag.get("final_check")),
        }
        self.head["last_calibration_diagnostic"] = copy.deepcopy(summary)
        root = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        path = root / "MotionControl" / "calibration_diagnostics.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(summary, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError as exc:
            # Diagnostics must never interfere with control or calibration.
            self.head["calibration_diag_error"] = str(exc)

    def _reset_default_head_locked(self, message: str = "") -> None:
        self.head.update({
            **DEFAULT_HEAD_PARAMS,
            "calibrated": True,
            "calibrating": False,
            "stage": "",
            "stage_label": "",
            "stage_started": 0.0,
            "calibration_started": 0.0,
            "stage_duration": CALIBRATION_STAGE_DURATION_S,
            "calibration_profile": "default",
            "calibration_message": message,
            "quality": "校准已取消，当前使用默认参数" if message else "当前使用：默认参数",
            "face_pair": "",
            "center_yaw": [], "center_pitch": [], "left_yaw": [], "right_yaw": [],
            "up_pitch": [], "down_pitch": [], "shoulder_scale_samples": [],
            "stage_valid_s": 0.0, "stage_required_s": CALIBRATION_STAGE_DURATION_S,
            "stage_last_sample_at": 0.0, "stage_pause_reason": "", "stage_missing_parts": [],
            "stage_transition_until": 0.0, "stage_transition_message": "",
            "stage_signal_value": None, "stage_signal_center": None,
            "stage_signal_delta": None, "stage_signal_goal": CALIBRATION_GUIDANCE_DELTA,
            "stage_signal_progress": 0.0,
            "calibration_notice": "", "calibration_notice_text": "", "calibration_notice_until": 0.0,
            "calibration_diag": self._new_calibration_diagnostic(),
            "norm_x": 0.0, "norm_y": 0.0,
            "filtered_x": 0.0, "filtered_y": 0.0, "output_x": 0.0, "output_y": 0.0,
            "last_update": 0.0,
        })

    def _set_calibration_pause_locked(self, reason: str, missing_parts=None) -> None:
        if not self.head["calibrating"] or self.head.get("stage") == "prepare":
            return
        self.head["stage_pause_reason"] = str(reason or "请保持姿势").strip()
        self.head["stage_missing_parts"] = list(dict.fromkeys(str(x) for x in (missing_parts or []) if str(x)))
        self.head["stage_last_sample_at"] = 0.0

    def _set_calibration_signal_status_locked(self, stage: str, raw_value: float) -> None:
        """Expose the current signal and an honest, non-gating movement hint."""
        if stage == "center":
            center = raw_value if math.isfinite(raw_value) else math.nan
            delta = 0.0 if math.isfinite(center) else math.nan
        elif stage in {"left", "right"}:
            center = self._median(self.head.get("center_yaw") or [])
            delta = raw_value - center if math.isfinite(raw_value) and math.isfinite(center) else math.nan
        elif stage in {"up", "down"}:
            center = self._median(self.head.get("center_pitch") or [])
            delta = raw_value - center if math.isfinite(raw_value) and math.isfinite(center) else math.nan
        else:
            return
        progress = _clamp(abs(delta) / CALIBRATION_GUIDANCE_DELTA, 0.0, 1.0) if math.isfinite(delta) else 0.0
        self.head.update({
            "stage_signal_value": raw_value if math.isfinite(raw_value) else None,
            "stage_signal_center": center if math.isfinite(center) else None,
            "stage_signal_delta": delta if math.isfinite(delta) else None,
            "stage_signal_goal": CALIBRATION_GUIDANCE_DELTA,
            "stage_signal_progress": progress,
        })

    def _abort_calibration_locked(self, reason: str) -> None:
        if not self.head["calibrating"]:
            return
        self._finalize_calibration_diag_stage_locked(False, reason)
        self._persist_calibration_diagnostic_locked("aborted", reason)
        self._reset_default_head_locked(str(reason).strip() or "未完成")
        self._safe_output(self.output.apply, 0.0, 0.0)

    def cancel_calibration(self, reason: str = "用户取消") -> dict:
        with self._lock:
            self._abort_calibration_locked(reason)
            return self.status_locked(time.monotonic())

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
                # Attaching the first source while the user is waiting in the
                # calibration flow is not a source switch.  A real switch
                # from one source to another still cancels atomically.
                waiting_for_first_source = self.active_body_source is None and self.head["calibrating"]
                if waiting_for_first_source:
                    self.latest_pose = None
                    self.pose_last_valid_at = 0.0
                    self._clear_body_outputs_locked()
                else:
                    self._clear_body_locked()
                self.active_body_source = source_id
            self.body_last_at = now
            self.width = max(1, int(width))
            self.height = max(1, int(height))
            self.latest_pose = copy.deepcopy(pose_map) if pose_map else None
            self._ensure_face_pair_locked(pose_map)
            # The prepare stage is deliberately source/pose agnostic.  It lets
            # the user move into position before the five timed sample stages;
            # only those stages enforce the minimal head-control landmarks.
            if (
                self.head["calibrating"]
                and self.head.get("stage") != "prepare"
                and not self.head.get("stage_transition_until")
            ):
                diagnostics = self._calibration_pose_diagnostics(pose_map, self.head.get("face_pair", ""))
                if not diagnostics["valid"]:
                    self._set_calibration_pause_locked(diagnostics["reason"], diagnostics["missing_parts"])
            # Calibration/watchdog validity follows the exact head-control
            # inputs, not visibility of unrelated body landmarks.
            if self._calibration_pose_is_valid(pose_map, self.head.get("face_pair", "")):
                self.pose_last_valid_at = now
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
                # Starting calibration never gates on a camera, source, pose,
                # or visibility.  Defaults remain active until all five
                # sampling stages finish and are committed atomically.
                "calibrating": True, "stage": "prepare", "stage_label": "准备",
                "stage_started": now, "calibration_started": now,
                "stage_duration": CALIBRATION_PREPARE_DURATION_S,
                "calibration_message": "",
                "stage_valid_s": 0.0, "stage_required_s": CALIBRATION_PREPARE_DURATION_S,
                "stage_last_sample_at": 0.0, "stage_pause_reason": "", "stage_missing_parts": [],
                "stage_transition_until": 0.0, "stage_transition_message": "",
                "stage_signal_value": None, "stage_signal_center": None,
                "stage_signal_delta": None, "stage_signal_goal": CALIBRATION_GUIDANCE_DELTA,
                "stage_signal_progress": 0.0,
                "calibration_notice": "", "calibration_notice_text": "", "calibration_notice_until": 0.0,
                "calibration_diag": self._new_calibration_diagnostic(),
                "center_yaw": [], "center_pitch": [], "left_yaw": [], "right_yaw": [],
                "up_pitch": [], "down_pitch": [], "shoulder_scale_samples": [], "filtered_x": 0.0,
                "filtered_y": 0.0, "output_x": 0.0, "output_y": 0.0,
            })
            return self.status_locked(now)

    def set_current_center(self) -> dict:
        with self._lock:
            if not (math.isfinite(self.head["raw_yaw"]) and math.isfinite(self.head["raw_pitch"])):
                raise ValueError("当前还没有稳定的头部关键点")
            self.head.update({
                "yaw0": self.head["raw_yaw"], "pitch0": self.head["raw_pitch"],
                "yaw_left": self.head["raw_yaw"] - 0.10, "yaw_right": self.head["raw_yaw"] + 0.10,
                "pitch_up": self.head["raw_pitch"] - 0.07, "pitch_down": self.head["raw_pitch"] + 0.07,
                "calibrated": True, "calibrating": False, "calibration_profile": "personal",
                "calibration_message": "", "quality": "当前使用：个人校准",
                "signal_version": HEAD_SIGNAL_VERSION,
                "stage": "", "stage_label": "", "stage_started": 0.0, "calibration_started": 0.0,
                "center_yaw": [], "center_pitch": [], "left_yaw": [], "right_yaw": [],
                "up_pitch": [], "down_pitch": [], "shoulder_scale_samples": [], "filtered_x": 0.0,
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

    def _ensure_face_pair_locked(self, pose_map: dict[str, dict] | None) -> str:
        """Lock one face pair for this body-source lifetime to prevent jumps."""
        pair = self.head.get("face_pair", "")
        if pair in {"eyes", "ears"}:
            return pair
        if self._face_pair_available(pose_map, "eyes"):
            pair = "eyes"
        elif self._face_pair_available(pose_map, "ears"):
            pair = "ears"
        else:
            pair = ""
        self.head["face_pair"] = pair
        return pair

    def _shoulder_width(self, pose_map: dict[str, dict]) -> float:
        left, right = pose_map.get("left_shoulder"), pose_map.get("right_shoulder")
        if not (
            self._point_has_xy(left, 0.30)
            and self._point_has_xy(right, 0.30)
        ):
            return math.nan
        width = _distance(left, right)
        return width if math.isfinite(width) and width >= 0.05 else math.nan

    def _face_pair_points(self, pose_map: dict[str, dict]) -> tuple[dict, dict] | None:
        pair = self.head.get("face_pair", "") or self._ensure_face_pair_locked(pose_map)
        names = ("left_eye", "right_eye") if pair == "eyes" else ("left_ear", "right_ear")
        left, right = pose_map.get(names[0]), pose_map.get(names[1])
        if not (
            self._point_has_xy(left, 0.35)
            and self._point_has_xy(right, 0.35)
        ):
            return None
        return left, right

    def _torso_length(self, pose_map: dict[str, dict]) -> float:
        points = [pose_map.get(name) for name in ("left_shoulder", "right_shoulder", "left_hip", "right_hip")]
        if any(point is None for point in points) or min(_score(point) for point in points) < 0.3:
            return math.nan
        return _distance(_midpoint(points[0], points[1]), _midpoint(points[2], points[3]))

    def _yaw_signal(self, pose_map: dict[str, dict]) -> float:
        nose = pose_map.get("nose")
        if not nose or _score(nose) < 0.35 or not math.isfinite(self._shoulder_width(pose_map)):
            return math.nan
        pair = self._face_pair_points(pose_map)
        if pair is None:
            return math.nan
        left, right = pair
        dl, dr = _distance(nose, left), _distance(nose, right)
        if dl <= 0.003 or dr <= 0.003:
            return math.nan
        return math.log((dl + 1e-4) / (dr + 1e-4))

    def _pitch_signal(self, pose_map: dict[str, dict], shoulder_width: float) -> float:
        nose = pose_map.get("nose")
        if not nose or _score(nose) < 0.35 or not math.isfinite(shoulder_width) or shoulder_width < 0.05:
            return math.nan
        pair = self._face_pair_points(pose_map)
        if pair is None:
            return math.nan
        # v2 uses one fixed face pair and one shoulder-width normalization for
        # every frame. Hips are intentionally not part of this signal.
        return (nose["y"] - _midpoint(*pair)["y"]) / shoulder_width

    @staticmethod
    def _normalize_axis(raw: float, center: float, left_anchor: float, right_anchor: float) -> float:
        """Map labeled left/center/right anchors to -1/0/+1 exactly once.

        The raw yaw sign can change with camera-facing/mirror conventions.  The
        calibration labels, not the numeric sign, define the output direction:
        the left anchor is always negative and the right anchor always positive.
        The normal path is piecewise around the center.  A nearest-anchor
        fallback only keeps old, already-persisted profiles usable when their
        two anchors ended up on the same numeric side.
        """
        if not all(math.isfinite(value) for value in (raw, center, left_anchor, right_anchor)):
            return 0.0
        delta = raw - center
        left_delta, right_delta = left_anchor - center, right_anchor - center
        if delta * left_delta >= 0.0 and abs(left_delta) > CALIBRATION_MIN_AXIS_SEPARATION:
            return -_clamp(abs(delta / left_delta), 0.0, 1.5)
        if delta * right_delta >= 0.0 and abs(right_delta) > CALIBRATION_MIN_AXIS_SEPARATION:
            return _clamp(abs(delta / right_delta), 0.0, 1.5)
        # Legacy/same-side anchors: choose the labeled anchor that is nearest
        # to the current signal rather than applying a second global sign flip.
        if abs(raw - left_anchor) <= abs(raw - right_anchor):
            return -_clamp(abs(delta / left_delta), 0.0, 1.5) if abs(left_delta) > CALIBRATION_MIN_AXIS_SEPARATION else 0.0
        return _clamp(abs(delta / right_delta), 0.0, 1.5) if abs(right_delta) > CALIBRATION_MIN_AXIS_SEPARATION else 0.0

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

    @staticmethod
    def _calibration_field(stage: str) -> str:
        return {
            "center": "center_yaw",
            "left": "left_yaw",
            "right": "right_yaw",
            "up": "up_pitch",
            "down": "down_pitch",
        }[stage]

    def _calibration_stage_ready_locked(self, stage: str) -> bool:
        if stage == "center":
            return bool(
                self.head["center_yaw"]
                and self.head["center_pitch"]
                and self.head["shoulder_scale_samples"]
            )
        return bool(self.head[self._calibration_field(stage)])

    def _begin_calibration_stage_locked(self, index: int, now: float) -> None:
        stage, label = CALIBRATION_STAGES[index]
        diag = self.head.get("calibration_diag")
        if not isinstance(diag, dict):
            diag = self._new_calibration_diagnostic()
            self.head["calibration_diag"] = diag
        diag["current"] = {
            "stage": stage,
            "label": label,
            "started_at_unix": time.time(),
            "valid_frames": 0,
            "invalid_frames": 0,
            "pause_reasons": {},
            "missing_parts": {},
            "yaw_values": [],
            "pitch_values": [],
        }
        self.head.update({
            "stage": stage,
            "stage_label": label,
            "stage_started": now,
            "stage_duration": CALIBRATION_STAGE_DURATION_S,
            "stage_valid_s": 0.0,
            "stage_required_s": CALIBRATION_STAGE_DURATION_S,
            "stage_last_sample_at": 0.0,
            "stage_pause_reason": "",
            "stage_missing_parts": [],
            "stage_transition_until": 0.0,
            "stage_transition_message": "",
            "stage_signal_value": None,
            "stage_signal_center": None,
            "stage_signal_delta": None,
            "stage_signal_goal": CALIBRATION_GUIDANCE_DELTA,
            "stage_signal_progress": 0.0,
        })

    def _complete_calibration_stage_locked(self, now: float) -> None:
        index = next(
            (index for index, (stage, _) in enumerate(CALIBRATION_STAGES) if stage == self.head["stage"]),
            0,
        )
        label = CALIBRATION_STAGES[index][1]
        next_label = CALIBRATION_STAGES[index + 1][1] if index + 1 < len(CALIBRATION_STAGES) else "应用个人参数"
        self._finalize_calibration_diag_stage_locked(True)
        self.head.update({
            "stage_valid_s": CALIBRATION_STAGE_DURATION_S,
            "stage_last_sample_at": 0.0,
            "stage_pause_reason": "",
            "stage_missing_parts": [],
            "stage_transition_until": now + CALIBRATION_TRANSITION_DURATION_S,
            "stage_transition_message": f"{label}完成，准备{next_label}" if index + 1 < len(CALIBRATION_STAGES) else f"{label}完成，正在应用个人参数",
        })

    def _advance_calibration_locked(self, now: float) -> None:
        if not self.head["calibrating"]:
            return
        started = self.head["calibration_started"]
        if self.head.get("stage") == "prepare":
            if now - started < CALIBRATION_PREPARE_DURATION_S:
                return
            self._begin_calibration_stage_locked(0, started + CALIBRATION_PREPARE_DURATION_S)
            return
        transition_until = float(self.head.get("stage_transition_until") or 0.0)
        if transition_until and now < transition_until:
            return
        if transition_until:
            index = next(
                (index for index, (stage, _) in enumerate(CALIBRATION_STAGES) if stage == self.head["stage"]),
                len(CALIBRATION_STAGES) - 1,
            )
            if index + 1 < len(CALIBRATION_STAGES):
                self._begin_calibration_stage_locked(index + 1, now)
            else:
                self._finish_calibration_locked(now)

    def _update_calibration_locked(
        self, raw_yaw: float, raw_pitch: float, shoulder_width: float,
        pose_map: dict[str, dict] | None, now: float,
    ) -> None:
        self._advance_calibration_locked(now)
        if not self.head["calibrating"] or self.head.get("stage") == "prepare":
            return
        if self.head.get("stage_transition_until"):
            return
        stage = self.head["stage"]
        diagnostics = self._calibration_pose_diagnostics(pose_map, self.head.get("face_pair", ""))
        reason, missing = diagnostics["reason"], diagnostics["missing_parts"]
        sample_valid = bool(diagnostics["valid"])
        if stage == "center":
            sample_valid = sample_valid and math.isfinite(raw_yaw) and math.isfinite(raw_pitch) and math.isfinite(shoulder_width)
            if not math.isfinite(shoulder_width):
                reason, missing = "请再退后，确保头和双肩入镜", ["双肩"]
            elif not (math.isfinite(raw_yaw) and math.isfinite(raw_pitch)):
                reason, missing = "脸部点不清楚，请正对摄像头", ["脸部"]
        elif stage in {"left", "right"}:
            sample_valid = sample_valid and math.isfinite(raw_yaw)
            if not math.isfinite(raw_yaw):
                reason, missing = "脸部点不清楚，请正对摄像头", ["脸部"]
        else:
            sample_valid = sample_valid and math.isfinite(raw_pitch)
            if not math.isfinite(raw_pitch):
                reason, missing = "脸部点不清楚，请正对摄像头", ["脸部"]
        if not sample_valid:
            self._record_calibration_frame_locked(
                valid=False, reason=reason or "请保持姿势", missing_parts=missing,
                raw_yaw=raw_yaw, raw_pitch=raw_pitch,
            )
            self._set_calibration_pause_locked(reason or "请保持姿势", missing)
            return

        self._record_calibration_frame_locked(valid=True, raw_yaw=raw_yaw, raw_pitch=raw_pitch)
        self.head["stage_pause_reason"] = ""
        self.head["stage_missing_parts"] = []
        last = float(self.head.get("stage_last_sample_at") or 0.0)
        if last:
            self.head["stage_valid_s"] = min(
                CALIBRATION_STAGE_DURATION_S,
                self.head["stage_valid_s"] + _clamp(now - last, 0.0, 0.20),
            )
        self.head["stage_last_sample_at"] = now
        if stage == "center":
            self.head["center_yaw"].append(raw_yaw)
            self.head["center_pitch"].append(raw_pitch)
            self.head["shoulder_scale_samples"].append(shoulder_width)
        elif stage == "left":
            self.head["left_yaw"].append(raw_yaw)
        elif stage == "right":
            self.head["right_yaw"].append(raw_yaw)
        elif stage == "up":
            self.head["up_pitch"].append(raw_pitch)
        elif stage == "down":
            self.head["down_pitch"].append(raw_pitch)
        self._set_calibration_signal_status_locked(
            stage, raw_yaw if stage in {"center", "left", "right"} else raw_pitch,
        )
        if self.head["stage_valid_s"] >= CALIBRATION_STAGE_DURATION_S:
            self._complete_calibration_stage_locked(now)

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

    def _restart_calibration_stage_locked(self, index: int, reason: str, missing_parts=None) -> None:
        # A retry starts a fresh anchor sequence from this stage onward.  Do
        # not pool old right/up/down samples into a later final check; that
        # made a completed retry look directionally inconsistent.
        fields = {
            "center": ("center_yaw", "center_pitch", "shoulder_scale_samples"),
            "left": ("left_yaw",), "right": ("right_yaw",),
            "up": ("up_pitch",), "down": ("down_pitch",),
        }
        for stage, _ in CALIBRATION_STAGES[index:]:
            for field in fields[stage]:
                self.head[field] = []
        self._begin_calibration_stage_locked(index, time.monotonic())
        self._set_calibration_pause_locked(reason, missing_parts)

    def _finish_calibration_locked(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if any(not self._calibration_stage_ready_locked(stage) for stage, _ in CALIBRATION_STAGES):
            self._set_calibration_final_check_locked(
                math.nan, math.nan, math.nan, math.nan, math.nan, math.nan, False, False,
            )
            self._persist_calibration_diagnostic_locked("final_check_incomplete", "阶段样本不完整")
            missing = next(label for stage, label in CALIBRATION_STAGES if not self._calibration_stage_ready_locked(stage))
            index = next(index for index, (_, label) in enumerate(CALIBRATION_STAGES) if label == missing)
            self._restart_calibration_stage_locked(index, f"{missing}阶段还需要有效样本", [missing])
            return
        yaw0 = self._median(self.head["center_yaw"], self.head["raw_yaw"])
        pitch0 = self._median(self.head["center_pitch"], self.head["raw_pitch"])
        if not (math.isfinite(yaw0) and math.isfinite(pitch0)):
            self._set_calibration_final_check_locked(
                yaw0, math.nan, math.nan, pitch0, math.nan, math.nan, False, False,
            )
            self._persist_calibration_diagnostic_locked("final_check_incomplete", "正视阶段信号无效")
            self._restart_calibration_stage_locked(0, "脸部点不清楚，请正对摄像头", ["脸部"])
            return
        yl, yr = self._median(self.head["left_yaw"]), self._median(self.head["right_yaw"])
        pu, pd = self._median(self.head["up_pitch"]), self._median(self.head["down_pitch"])
        yaw_check = self._calibration_axis_check(yaw0, yl, yr)
        pitch_check = self._calibration_axis_check(pitch0, pu, pd)
        yaw_ok, pitch_ok = yaw_check["ok"], pitch_check["ok"]
        self._set_calibration_final_check_locked(yaw0, yl, yr, pitch0, pu, pd, yaw_ok, pitch_ok)
        if not yaw_ok:
            reason = (
                "左右转方向次序未形成，请按提示分别向左、向右转头"
                if not yaw_check["ordered"]
                else "左右转信号几乎没有差异，请再转一些"
            )
            self._persist_calibration_diagnostic_locked("final_check_failed", reason)
            self._restart_calibration_stage_locked(1, reason, ["左右转"])
            return
        if not pitch_ok:
            reason = (
                "抬头/低头方向次序未形成，请按提示分别抬头、低头"
                if not pitch_check["ordered"]
                else "抬头/低头信号几乎没有差异，请再抬高或低一些"
            )
            self._persist_calibration_diagnostic_locked("final_check_failed", reason)
            self._restart_calibration_stage_locked(3, reason, ["上下转头"])
            return
        nx = [abs(self._normalize_axis(value, yaw0, yl, yr)) for value in self.head["center_yaw"]]
        ny = [abs(self._normalize_axis(value, pitch0, pu, pd)) for value in self.head["center_pitch"]]
        deadzone_x = self.head["deadzone_x"]
        deadzone_y = self.head["deadzone_y"]
        if yaw_ok and nx:
            deadzone_x = _clamp(self._qtile(nx, 0.99) * 1.8 + 0.02, 0.06, 0.30)
        if pitch_ok and ny:
            deadzone_y = _clamp(self._qtile(ny, 0.99) * 2.0 + 0.025, 0.08, 0.34)
        self._persist_calibration_diagnostic_locked("success", "")
        self.head.update({
            "signal_version": HEAD_SIGNAL_VERSION,
            "yaw0": yaw0, "yaw_left": yl, "yaw_right": yr, "pitch0": pitch0,
            "pitch_up": pu, "pitch_down": pd, "shoulder_scale0": self._median(self.head["shoulder_scale_samples"]),
            "deadzone_x": deadzone_x, "deadzone_y": deadzone_y,
            "calibrating": False, "calibrated": True,
            "stage": "", "stage_label": "", "stage_started": 0.0, "calibration_started": 0.0,
            "calibration_profile": "personal", "calibration_message": "",
            "quality": "当前使用：个人校准",
            "stage_valid_s": CALIBRATION_STAGE_DURATION_S, "stage_required_s": CALIBRATION_STAGE_DURATION_S,
            "stage_last_sample_at": 0.0, "stage_pause_reason": "", "stage_missing_parts": [],
            "stage_transition_until": 0.0, "stage_transition_message": "",
            "calibration_notice": "success", "calibration_notice_text": "校准成功，已使用个人参数",
            "calibration_notice_until": now + 2.0,
            "center_yaw": [], "center_pitch": [], "left_yaw": [], "right_yaw": [],
            "up_pitch": [], "down_pitch": [], "shoulder_scale_samples": [],
            "filtered_x": 0.0, "filtered_y": 0.0, "output_x": 0.0, "output_y": 0.0, "last_update": 0.0,
        })

    def _update_head_locked(self, pose_map: dict[str, dict], now: float) -> None:
        if self.head.get("signal_version") != HEAD_SIGNAL_VERSION:
            self._reset_default_head_locked("头控参数版本已更新，请重新校准")
        self._ensure_face_pair_locked(pose_map)
        shoulder_width = self._shoulder_width(pose_map)
        raw_yaw, raw_pitch = self._yaw_signal(pose_map), self._pitch_signal(pose_map, shoulder_width)
        self.head["raw_yaw"], self.head["raw_pitch"] = raw_yaw, raw_pitch
        if self.head["calibrating"]:
            self._update_calibration_locked(raw_yaw, raw_pitch, shoulder_width, pose_map, now)
        # Keep the existing/default head parameters active before and after a
        # retry, but hold the output neutral while a personal calibration is
        # being collected.  Calibration itself must never move the cursor.
        if self.head["calibrated"] and not self.head["calibrating"]:
            x = self._normalize_axis(raw_yaw, self.head["yaw0"], self.head["yaw_left"], self.head["yaw_right"])
            y = self._normalize_axis(raw_pitch, self.head["pitch0"], self.head["pitch_up"], self.head["pitch_down"])
        else:
            x, y = 0.0, 0.0
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
        self.head.update({"raw_yaw": math.nan, "raw_pitch": math.nan, "filtered_x": 0.0, "filtered_y": 0.0, "output_x": 0.0, "output_y": 0.0, "face_pair": ""})
        self._safe_output(self.output.set_buttons, [], source="zones")
        self._safe_output(self.output.set_holds, [], source_group="motions")
        self._safe_output(self.output.apply, 0.0, 0.0)

    def _clear_body_locked(self) -> None:
        self._abort_calibration_locked("人体来源已断开")
        self.latest_pose = None
        self.pose_last_valid_at = 0.0
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
        notice_until = float(self.head.get("calibration_notice_until") or 0.0)
        head = {
            "calibrated": bool(self.head["calibrated"]), "calibrating": bool(self.head["calibrating"]),
            "signal_version": self.head.get("signal_version", HEAD_SIGNAL_VERSION),
            "face_pair": self.head.get("face_pair", ""),
            "stage": self.head["stage"], "stage_label": self.head.get("stage_label", ""),
            "quality": self.head["quality"], "calibration_profile": self.head.get("calibration_profile", "default"),
            "calibration_message": self.head.get("calibration_message", ""),
            "calibration_elapsed_s": (
                round(max(0.0, now - self.head["calibration_started"]), 1)
                if self.head["calibrating"] else None
            ),
            # Kept for older display clients; cumulative sampling means there
            # is no honest wall-clock remaining value once a stage pauses.
            "calibration_remaining_s": None,
            "stage_remaining_s": (
                round(
                    max(
                        0.0,
                        self.head["stage_duration"] - (now - self.head["stage_started"])
                        if self.head.get("stage") == "prepare"
                        else self.head.get("stage_required_s", CALIBRATION_STAGE_DURATION_S) - self.head.get("stage_valid_s", 0.0),
                    ), 1,
                )
                if self.head["calibrating"] else None
            ),
            "stage_valid_s": round(float(self.head.get("stage_valid_s") or 0.0), 2),
            "stage_required_s": round(float(self.head.get("stage_required_s") or CALIBRATION_STAGE_DURATION_S), 2),
            "stage_paused": bool(self.head.get("stage_pause_reason")) if self.head["calibrating"] else False,
            "stage_pause_reason": self.head.get("stage_pause_reason", ""),
            "stage_missing_parts": list(self.head.get("stage_missing_parts") or []),
            "stage_transition_message": self.head.get("stage_transition_message", ""),
            "stage_transition_remaining_s": round(max(0.0, float(self.head.get("stage_transition_until") or 0.0) - now), 1),
            "stage_signal_value": self.head.get("stage_signal_value"),
            "stage_signal_center": self.head.get("stage_signal_center"),
            "stage_signal_delta": self.head.get("stage_signal_delta"),
            "stage_signal_goal": self.head.get("stage_signal_goal", CALIBRATION_GUIDANCE_DELTA),
            "stage_signal_progress": round(float(self.head.get("stage_signal_progress") or 0.0), 3),
            "calibration_notice": self.head.get("calibration_notice", ""),
            "calibration_notice_text": self.head.get("calibration_notice_text", ""),
            "calibration_notice_remaining_s": round(max(0.0, notice_until - now), 1),
            "last_calibration_diagnostic": copy.deepcopy(self.head.get("last_calibration_diagnostic")),
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
                if self.head["calibrating"]:
                    self._advance_calibration_locked(now)
                    if self.head.get("stage") not in {"prepare", ""} and not self.head.get("stage_transition_until"):
                        if not self.active_body_source:
                            self._set_calibration_pause_locked("未检测到人体，请进入画面", ["人体"])
                        else:
                            diagnostics = self._calibration_pose_diagnostics(self.latest_pose, self.head.get("face_pair", ""))
                            if not diagnostics["valid"]:
                                self._set_calibration_pause_locked(diagnostics["reason"], diagnostics["missing_parts"])
                if self.active_body_source and self.body_last_at and now - self.body_last_at > self.watchdog_timeout:
                    if self.head["calibrating"]:
                        self.latest_pose = None
                        self.pose_last_valid_at = 0.0
                        self.body_last_at = 0.0
                        self._clear_body_outputs_locked()
                        self._set_calibration_pause_locked("未检测到人体，请进入画面", ["人体"])
                    else:
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

    def start_calibration(self) -> dict:
        with self._lock:
            return self.kernel.start_calibration()

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
