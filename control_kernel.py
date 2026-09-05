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
import statistics
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from head_control import (
    HeadController,
    HeadPoseEstimator,
    IntentAxis,
    PITCH_INTENT_ANGLE,
    PITCH_INTENT_START_VELOCITY,
    PITCH_INTENT_STOP_VELOCITY,
    HEAD_SIGNAL_VERSION as CLEAN_HEAD_SIGNAL_VERSION,
)
from game_profiles import flatten_bindings
from vertical_hand_control import VerticalHandController


MP_NAMES = [
    "nose", "left_eye_inner", "left_eye", "left_eye_outer", "right_eye_inner",
    "right_eye", "right_eye_outer", "left_ear", "right_ear", "mouth_left",
    "mouth_right", "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist", "left_pinky", "right_pinky", "left_index",
    "right_index", "left_thumb", "right_thumb", "left_hip", "right_hip",
    "left_knee", "right_knee", "left_ankle", "right_ankle", "left_heel",
    "right_heel", "left_foot_index", "right_foot_index",
]

# Head control lives in head_control.py.  Keep only the exported signal-version
# alias here so external diagnostics can identify the active algorithm family.
HEAD_SIGNAL_VERSION = CLEAN_HEAD_SIGNAL_VERSION

BODY_ZONES = {
    "leftHandUpper": {"label": "Y", "button": "Y", "points": ("left_wrist",), "kind": "hand"},
    "leftHandLower": {"label": "X", "button": "X", "points": ("left_wrist",), "kind": "hand"},
    "rightHandUpper": {"label": "B", "button": "B", "points": ("right_wrist",), "kind": "hand"},
    "rightHandLower": {"label": "A", "button": "A", "points": ("right_wrist",), "kind": "hand"},
    "leftFoot": {"label": "LB", "button": "LB", "points": ("left_ankle", "left_heel", "left_foot_index"), "kind": "foot"},
    "rightFoot": {"label": "RB", "button": "RB", "points": ("right_ankle", "right_heel", "right_foot_index"), "kind": "foot"},
}

BODY_MOTION_GUARD_POINTS = (
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist", "left_hip", "right_hip",
    "left_knee", "right_knee", "left_ankle", "right_ankle",
)


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
        # World landmarks are needed during the v153 personal-PnP center
        # capture, but are deliberately kept separate from the normalized
        # image pose so body zones and renderers never see metric coordinates.
        # Once a personal model is active, HeadController uses them only for
        # diagnostics; the runtime yaw path remains normalized-2D only.
        self.latest_world_pose: dict[str, dict] | None = None
        self.pose_last_valid_at = 0.0
        # Keep a short monotonic history so first-run scene placement and
        # explicit rematch use a robust multi-frame body snapshot instead of
        # trusting one noisy MediaPipe frame.
        self.pose_history: deque[tuple[float, dict[str, dict]]] = deque(maxlen=48)
        self.last_error: str | None = None

        self.zone_rects: dict[str, dict] = {}
        self.fixed_zones: dict[str, dict] = {}
        self.fixed_zones_enabled = False
        self.vertical_look = {
            # Before the first fixed-scene capture we still expose a provisional
            # body-relative lookGate so the seventh region is visible and usable.
            # The first reference capture replaces it with the fixed scene-space
            # gate; later starts load that fixed gate without auto-rematching.
            "enabled": True, "gate_zone_id": "lookGate", "point": "right_wrist",
            "source": "hand", "verticalLookSource": "hand",
            # Optional axis exclusivity: entering the left-hand gate may pause
            # horizontal head output while vertical view control is active.
            "exclusive_axes": False,
            "body_motion_guard": True,
            "center_x": 0.5, "center_y": 0.5, "range_y": 0.18, "deadzone": 0.10,
        }
        self.vertical_gate_active = False
        self.vertical_wrist_norm = 0.0
        self.vertical_hand_controller = VerticalHandController()
        # The left-wrist lookGate is a deliberate arm/hand gate.  When it
        # becomes active we capture the right wrist's current Y as the
        # neutral anchor; head pitch is never allowed to reach final output.
        self.vertical_wrist_anchor_y: float | None = None
        # v0.9.6 vertical look is body-relative: right-wrist Y is measured
        # against right-shoulder Y.  This removes whole-body bobbing and makes
        # natural arm arcs much less likely to disturb the view.
        self.vertical_wrist_anchor_rel_y: float | None = None
        # Keep the historical attribute as an alias for compatibility with
        # status consumers and focused kernel tests.  The state is owned by
        # VerticalHandController from here on.
        self.vertical_anchor_samples = self.vertical_hand_controller.anchor_samples
        self.vertical_wrist_filtered = 0.0
        self.vertical_filter_last_at = 0.0
        self.vertical_head_anchor_pitch: float | None = None
        # A gate re-entry must wait for a short, stable filtered-pitch
        # center.  Capturing one frame lets the filter's old tail look like a
        # fresh vertical gesture and can arm the opposite direction.
        self.vertical_head_anchor_samples: deque[float] = deque(maxlen=3)
        self.vertical_pitch_intent = IntentAxis("vertical_pitch")
        self.vertical_pitch_norm = 0.0
        self.vertical_pitch_relative = 0.0
        self.vertical_pitch_velocity = 0.0
        self.vertical_pitch_acceleration = 0.0
        self.vertical_pitch_intent_state = "IDLE"
        self.zone_state = {name: {"inside": 0, "outside": 0, "pressed": False} for name in BODY_ZONES}
        self.zone_state["lookGate"] = {"inside": 0, "outside": 0, "pressed": False}
        self.last_zone_emit = 0.0

        self.motion_config: list[dict] = []
        self.motion_active: set[str] = set()
        self.motion_debounce = {
            key: {"active": False, "on": 0, "off": 0}
            for key in (
                "march", "calf_back", "squat", "hands_up",
                "jumping_jack", "side_step_jack", "cross_knee_elbow",
            )
        }
        self.step = {"left_was": False, "right_was": False, "last_side": "", "last_at": 0.0, "active_until": 0.0}
        self.last_motion_emit = 0.0

        # Head estimation keeps observing frames, but strong exercise motion
        # must not move the in-game camera. This guard uses body-normalized
        # limb velocity because action labels can be intermittent or absent.
        self.body_motion_guard_enabled = True
        self.body_motion_guard_active = False
        self.body_motion_guard_raw = 0.0
        self.body_motion_guard_score = 0.0
        self.body_motion_guard_previous: dict[str, tuple[float, float]] = {}
        self.body_motion_guard_last_at = 0.0
        self.body_motion_guard_hold_until = 0.0
        self.body_motion_guard_settle_frames = 0

        # v0.9.7 unified trigger -> output layer. Profile bindings are stored
        # independently from recognition so changing games never changes pose rules.
        self.control_bindings: dict[str, dict] = {}
        self.trigger_previous: set[str] = set()
        self.pose_active: set[str] = set()
        self.pose_confidence: dict[str, float] = {}
        self.pose_debounce = {
            key: {"active": False, "on": 0, "off": 0}
            for key in ("hands_cross",)
        }

        # Head control is intentionally isolated from body actions.  The clean
        # engine owns its estimator, center capture, filtering and compact
        # profile.  Legacy five-stage/head-face state is no longer part of the
        # runtime path.
        self.head_controller = HeadController(self._head_profile_path())
        self.head = self.head_controller.status(time.monotonic())
        self.sensor_sources: dict[str, dict] = {}
        self._thread.start()

    def _head_profile_path(self) -> Path:
        root = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return root / "MotionControl" / "head_profile.json"

    def cancel_calibration(self, reason: str = "用户取消") -> dict:
        with self._lock:
            self.head_controller.cancel_center(reason)
            self.head = self.head_controller.status(time.monotonic())
            self._safe_output(self.output.apply, 0.0, 0.0)
            return self.status_locked(time.monotonic())

    def _sync_vertical_hand_locked(self, state: dict | None = None) -> None:
        """Mirror vertical-hand diagnostics kept for the public kernel API."""
        snapshot = state or self.vertical_hand_controller.status()
        # v160.reset() rebuilds its bounded anchor deque.  Refresh the
        # historical alias on every sync so status consumers never retain the
        # deque from before a gate transition, source switch, or watchdog reset.
        self.vertical_anchor_samples = self.vertical_hand_controller.anchor_samples
        self.vertical_wrist_anchor_y = snapshot.get("anchor_y")
        self.vertical_wrist_anchor_rel_y = snapshot.get("anchor_rel_y")
        self.vertical_wrist_filtered = float(snapshot.get("filtered", 0.0) or 0.0)
        self.vertical_filter_last_at = float(snapshot.get("filter_last_at", 0.0) or 0.0)

    def _reset_vertical_hand_locked(self, now: float | None = None) -> None:
        """Reset hand vertical-look state and its legacy diagnostic mirrors."""
        self.vertical_hand_controller.reset(now)
        self._sync_vertical_hand_locked()
        self.vertical_wrist_norm = 0.0

    def _reset_vertical_head_locked(self) -> None:
        """Clear the gated head-pitch center and all vertical intent state."""
        self.vertical_head_anchor_pitch = None
        self.vertical_head_anchor_samples.clear()
        self.vertical_pitch_intent.reset()
        self.vertical_pitch_norm = 0.0
        self.vertical_pitch_relative = 0.0
        self.vertical_pitch_velocity = 0.0
        self.vertical_pitch_acceleration = 0.0
        self.vertical_pitch_intent_state = "IDLE"

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

    @staticmethod
    def world_pose_map_from_message(message: dict) -> dict[str, dict] | None:
        """Convert the first MediaPipe world_pose list to a named map.

        World coordinates are metric/model coordinates and must not receive
        the image-space ``coordinates_mirrored`` correction.  The phone and
        local MediaPipe paths both provide the same canonical 33-point order.
        Missing world landmarks are treated as an optional runtime signal: the
        normalized pose can still drive the ordinary controller, while v153
        simply remains on its safe generic-PnP fallback until calibration has
        enough world samples.
        """
        poses = message.get("poses") if isinstance(message, dict) else None
        item = poses[0] if poses and isinstance(poses[0], dict) else None
        landmarks = item.get("world_pose") if item else None
        if not isinstance(landmarks, list) or len(landmarks) != 33:
            return None
        result: dict[str, dict] = {}
        for index, name in enumerate(MP_NAMES):
            landmark = landmarks[index]
            if not isinstance(landmark, dict):
                continue
            result[name] = _point(landmark)
        return result if result else None

    def configure_motions(self, items) -> None:
        with self._lock:
            self.motion_config = [copy.deepcopy(item) for item in (items or []) if isinstance(item, dict)]

    def configure_bindings(self, bindings: dict | None) -> None:
        """Install one effective Game Profile without touching recognition thresholds."""
        with self._lock:
            self.control_bindings = flatten_bindings(bindings)
            self.trigger_previous.clear()
            # Release any output contributed by the previous profile immediately.
            setter = getattr(self.output, "set_action_holds", None)
            if setter is not None:
                self._safe_output(setter, [], source_group="controls")

    def configure_head(self, *, algorithm=None, deadzone=None, sensitivity_x=None,
                       sensitivity_y=None, enabled=None, invert_x=None, invert_y=None,
                       horizontal_algorithm=None, vertical_look_source=None,
                       vertical_exclusive=None, body_motion_guard=None) -> dict:
        with self._lock:
            self.head_controller.configure(
                algorithm=algorithm,
                deadzone=deadzone,
                sensitivity_x=sensitivity_x,
                sensitivity_y=sensitivity_y,
                enabled=enabled,
                invert_x=invert_x,
                invert_y=invert_y,
                horizontal_algorithm=horizontal_algorithm,
            )
            if vertical_look_source is not None:
                source = str(vertical_look_source).strip().lower()
                if source in {"right_wrist", "hand", "右手"}:
                    source = "hand"
                elif source in {"head", "头部"}:
                    source = "head"
                else:
                    raise ValueError("vertical_look_source must be hand or head")
                self.vertical_look["source"] = source
                self.vertical_look["verticalLookSource"] = source
                self._reset_vertical_head_locked()
            if vertical_exclusive is not None:
                self.vertical_look["exclusive_axes"] = bool(vertical_exclusive)
            if body_motion_guard is not None:
                self.body_motion_guard_enabled = bool(body_motion_guard)
                self.vertical_look["body_motion_guard"] = self.body_motion_guard_enabled
                if not self.body_motion_guard_enabled:
                    self._reset_body_motion_guard_locked()
            self.head = self.head_controller.status(time.monotonic())
            return self.status_locked(time.monotonic())

    def configure_scene_layout(self, layout: dict | None) -> dict:
        """Apply one fixed, camera-space session layout.

        The layout is already adapted by SceneLayoutManager. The kernel never
        moves these circles with the player; they remain fixed until this
        method is called again.
        """
        with self._lock:
            zones = (layout or {}).get("zones") if isinstance(layout, dict) else None
            self.fixed_zones = copy.deepcopy(zones) if isinstance(zones, dict) else {}
            self.fixed_zones_enabled = bool(self.fixed_zones)
            vertical = (layout or {}).get("vertical_look") if isinstance(layout, dict) else None
            if isinstance(vertical, dict):
                raw_source = str(vertical.get("source", vertical.get("verticalLookSource", self.vertical_look.get("source", "hand")))).lower()
                source = "head" if raw_source in {"head", "头部"} else "hand"
                self.vertical_look.update({
                    "enabled": bool(vertical.get("enabled", True)),
                    "gate_zone_id": str(vertical.get("gate_zone_id", "lookGate")),
                    "point": str(vertical.get("point", "right_wrist")),
                    "source": source,
                    "verticalLookSource": source,
                    "exclusive_axes": bool(vertical.get("exclusive_axes", self.vertical_look.get("exclusive_axes", False))),
                    "body_motion_guard": bool(vertical.get("body_motion_guard", self.body_motion_guard_enabled)),
                    "center_x": _clamp(vertical.get("center_x", 0.5), 0.0, 1.0),
                    "center_y": _clamp(vertical.get("center_y", 0.5), 0.0, 1.0),
                    "range_y": _clamp(vertical.get("range_y", 0.18), 0.05, 0.45),
                    "deadzone": _clamp(vertical.get("deadzone", 0.10), 0.0, 0.35),
                })
                self.body_motion_guard_enabled = bool(self.vertical_look["body_motion_guard"])
            else:
                self.vertical_look["enabled"] = False
            for state in self.zone_state.values():
                state.update({"inside": 0, "outside": 0, "pressed": False})
            self.vertical_gate_active = False
            self._reset_vertical_hand_locked()
            self.vertical_head_anchor_samples.clear()
            self._reset_vertical_head_locked()
            self._safe_output(self.output.set_buttons, [], source="zones")
            return self.status_locked(time.monotonic())

    def handle_pose_message(self, source_id: str, message: dict) -> dict:
        pose_map = self.pose_map_from_message(message)
        world_pose = self.world_pose_map_from_message(message)
        width = int(message.get("width") or 640)
        height = int(message.get("height") or 480)
        return self.handle_pose_map(
            source_id, pose_map, width=width, height=height,
            world_pose=world_pose,
        )

    def handle_pose_map(
        self,
        source_id: str,
        pose_map: dict[str, dict] | None,
        *,
        width: int = 640,
        height: int = 480,
        world_pose: dict[str, dict] | list[dict] | None = None,
    ) -> dict:
        now = time.monotonic()
        with self._lock:
            source_id = str(source_id)
            if self.active_body_source != source_id:
                # A real source switch atomically releases all held outputs and
                # resets only the head tracking filters.  A saved neutral center
                # remains valid for the same selected algorithm.
                if self.active_body_source is not None:
                    self._clear_body_locked()
                self.active_body_source = source_id
                self.head_controller.reset_tracking()
                self.pose_history.clear()
            self.body_last_at = now
            self.width = max(1, int(width))
            self.height = max(1, int(height))
            self.latest_pose = copy.deepcopy(pose_map) if pose_map else None
            self.latest_world_pose = copy.deepcopy(world_pose) if world_pose else None
            if pose_map:
                self.pose_last_valid_at = now
                self.pose_history.append((now, copy.deepcopy(pose_map)))
            # Keep compatibility with scene/test adapters that still expose
            # the original two-argument processing hook; only pass the new
            # world stream when one is actually present.
            if world_pose is None:
                self._process_pose_locked(pose_map, now)
            else:
                self._process_pose_locked(pose_map, now, world_pose)
            return self.status_locked(now)

    def stable_pose_snapshot(self, *, window_s: float = 0.90, min_samples: int = 6) -> dict[str, dict] | None:
        """Return a robust recent pose for scene placement/rematch.

        The runtime control path still uses the newest frame.  Only the
        low-frequency scene-authoring path uses this median snapshot, so there
        is no gameplay latency penalty.
        """
        now = time.monotonic()
        with self._lock:
            frames = [pose for ts, pose in self.pose_history if now - ts <= max(0.20, float(window_s))]
            if len(frames) < max(2, int(min_samples)):
                return copy.deepcopy(self.latest_pose) if self.latest_pose else None
            names = set().union(*(frame.keys() for frame in frames))
            stable: dict[str, dict] = {}
            for name in names:
                points = [frame.get(name) for frame in frames]
                points = [p for p in points if isinstance(p, dict) and _score(p) >= 0.20]
                if len(points) < max(3, len(frames) // 3):
                    continue
                stable[name] = {
                    "x": statistics.median(float(p.get("x", 0.0)) for p in points),
                    "y": statistics.median(float(p.get("y", 0.0)) for p in points),
                    "z": statistics.median(float(p.get("z", 0.0)) for p in points),
                    "score": statistics.median(_score(p) for p in points),
                }
            return stable or (copy.deepcopy(self.latest_pose) if self.latest_pose else None)

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
            self.head_controller.start_center(time.monotonic(), kind="manual")
            self.head = self.head_controller.status(time.monotonic())
            self._safe_output(self.output.apply, 0.0, 0.0)
            return self.status_locked(time.monotonic())

    def set_current_center(self) -> dict:
        """Compatibility boundary for the legacy "立即设置中心" endpoint.

        The reference controller owns all center-capture semantics.  This
        endpoint therefore starts the same finite reference capture instead of
        writing a center directly from one frame.
        """
        return self.start_calibration()

    # ---------- pose processing ----------

    def _process_pose_locked(
        self,
        pose_map: dict[str, dict] | None,
        now: float,
        world_pose: dict[str, dict] | list[dict] | None = None,
    ) -> None:
        if not pose_map:
            self._clear_body_outputs_locked()
            return
        self._update_zones_locked(pose_map, now)
        self._update_motion_locked(pose_map, now)
        self._update_cross_poses_locked(pose_map, now)
        self._dispatch_controls_locked(now)
        self._update_body_motion_guard_locked(pose_map, now)
        self._update_head_locked(pose_map, now, world_pose)

    def _reset_body_motion_guard_locked(self) -> None:
        self.body_motion_guard_active = False
        self.body_motion_guard_raw = 0.0
        self.body_motion_guard_score = 0.0
        self.body_motion_guard_previous = {}
        self.body_motion_guard_last_at = 0.0
        self.body_motion_guard_hold_until = 0.0
        self.body_motion_guard_settle_frames = 0

    def _update_body_motion_guard_locked(self, pose_map: dict[str, dict], now: float) -> None:
        """Measure exercise motion without modifying the selected head algorithm."""
        core = ("left_shoulder", "right_shoulder", "left_hip", "right_hip")
        if not self.body_motion_guard_enabled or not self._points_good(pose_map, core, 0.35):
            self._reset_body_motion_guard_locked()
            return
        shoulder = _midpoint(pose_map["left_shoulder"], pose_map["right_shoulder"])
        hip = _midpoint(pose_map["left_hip"], pose_map["right_hip"])
        torso = max(0.04, _distance(shoulder, hip))
        current: dict[str, tuple[float, float]] = {}
        for name in BODY_MOTION_GUARD_POINTS:
            point = pose_map.get(name)
            if _score(point) >= 0.35:
                current[name] = (
                    (float(point["x"]) - float(hip["x"])) / torso,
                    (float(point["y"]) - float(hip["y"])) / torso,
                )

        raw = 0.0
        dt = now - self.body_motion_guard_last_at if self.body_motion_guard_last_at else 0.0
        if 1.0 / 90.0 <= dt <= 0.12:
            speeds = [
                math.hypot(value[0] - self.body_motion_guard_previous[name][0],
                           value[1] - self.body_motion_guard_previous[name][1]) / dt
                for name, value in current.items()
                if name in self.body_motion_guard_previous
            ]
            if len(speeds) >= 2:
                speeds.sort(reverse=True)
                fastest_half = speeds[:max(1, len(speeds) // 2)]
                raw = float(statistics.fmean(fastest_half))
        self.body_motion_guard_previous = current
        self.body_motion_guard_last_at = now
        self.body_motion_guard_raw = raw
        alpha = 1.0 - math.exp(-max(0.0, min(0.12, dt)) / 0.10) if dt > 0.0 else 1.0
        self.body_motion_guard_score += alpha * (raw - self.body_motion_guard_score)

        if self.body_motion_guard_score >= 2.50 or bool(self.motion_active):
            self.body_motion_guard_active = True
            self.body_motion_guard_hold_until = now + 0.10
            self.body_motion_guard_settle_frames = 0
        elif self.body_motion_guard_active and self.body_motion_guard_score >= 1.625:
            self.body_motion_guard_hold_until = now + 0.10
            self.body_motion_guard_settle_frames = 0

    def _guard_horizontal_output_locked(self, x: float, now: float) -> float:
        if not self.body_motion_guard_enabled or not self.body_motion_guard_active:
            return float(x)
        if now < self.body_motion_guard_hold_until or self.body_motion_guard_score >= 1.625:
            self.body_motion_guard_settle_frames = 0
        elif abs(float(x)) <= 0.01:
            self.body_motion_guard_settle_frames += 1
            if self.body_motion_guard_settle_frames >= 3:
                self.body_motion_guard_active = False
                self.body_motion_guard_settle_frames = 0
        else:
            self.body_motion_guard_settle_frames = 0
        return 0.0 if self.body_motion_guard_active else float(x)

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

            # Provisional seventh region for first-run UX.  It intentionally
            # exists only while no fixed Scene Layout has been captured.  Once
            # a reference is recorded the fixed camera-space lookGate takes
            # over and no region follows the player.
            gate_w, gate_h = 0.44 * torso_px, 0.30 * torso_px
            gate_rect = _rect_at(
                head_center["x"] + left_dir * 0.32 * torso_px / iw,
                head_center["y"] + 0.34 * torso_px / ih,
                gate_w, gate_h, iw, ih,
            )
            rects["lookGate"] = self._smooth_rect(self.zone_rects.get("lookGate"), gate_rect)
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

    @staticmethod
    def _point_in_circle(point: dict | None, circle: dict | None) -> bool:
        if not point or not circle or _score(point) < 0.42:
            return False
        try:
            cx, cy, radius = float(circle["cx"]), float(circle["cy"]), float(circle["r"])
        except (KeyError, TypeError, ValueError):
            return False
        return math.hypot(point["x"] - cx, point["y"] - cy) <= radius

    def _update_zones_locked(self, pose_map: dict[str, dict], now: float) -> None:
        previous_gate = bool(self.vertical_gate_active)
        if self.fixed_zones_enabled:
            # Fixed zones live in raw camera normalized coordinates and never
            # follow the body. Rects are generated only for legacy clients.
            self.zone_rects = {}
        else:
            self.zone_rects = self._compute_body_zones(pose_map)
        changed = False
        gate_available = (self.fixed_zones_enabled and "lookGate" in self.fixed_zones) or (not self.fixed_zones_enabled and "lookGate" in self.zone_rects)
        zone_names = list(BODY_ZONES) + (["lookGate"] if gate_available else [])
        for name in zone_names:
            state = self.zone_state.setdefault(name, {"inside": 0, "outside": 0, "pressed": False})
            if name == "lookGate":
                points = ("left_wrist",)
            else:
                points = BODY_ZONES[name]["points"]
            if self.fixed_zones_enabled:
                circle = self.fixed_zones.get(name)
                inside = any(self._point_in_circle(pose_map.get(point), circle) for point in points)
            else:
                inside = any(self._point_in_rect(pose_map.get(point), self.zone_rects.get(name)) for point in points)
            if inside:
                state["inside"] += 1
                state["outside"] = 0
                if not state["pressed"] and state["inside"] >= 2:
                    state["pressed"] = True
                    changed = True
            else:
                state["outside"] += 1
                state["inside"] = 0
                # The look gate is a safety arm, so leaving it must cut
                # vertical output on the very first missing frame.  Body
                # action zones retain their normal two-frame hysteresis.
                exit_frames = 1 if name == "lookGate" else 2
                if state["pressed"] and state["outside"] >= exit_frames:
                    state["pressed"] = False
                    changed = True
        self.vertical_gate_active = bool(self.zone_state.get("lookGate", {}).get("pressed")) if gate_available else False
        if not self.vertical_gate_active:
            self._reset_vertical_hand_locked()
            self.vertical_head_anchor_samples.clear()
            self._reset_vertical_head_locked()
        elif not previous_gate:
            # Do not capture one arbitrary frame as the neutral point.  The
            # next few stable frames are collected in _update_head_locked and
            # their median becomes the anchor.
            self._reset_vertical_hand_locked(now)
            self._reset_vertical_head_locked()
        if changed:
            self.last_zone_emit = now

    def _pressed_keys_locked(self) -> list[str]:
        return sorted({
            BODY_ZONES[name]["button"]
            for name, state in self.zone_state.items()
            if name in BODY_ZONES and BODY_ZONES[name].get("button") and state["pressed"]
        })

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
        jumping_jack_raw = side_step_jack_raw = cross_knee_elbow_raw = False
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

            # Wider, body-relative poses are intentionally detected from a
            # small group of joints instead of one fragile wrist/ankle point.
            # The state becomes a normal configurable trigger below; game
            # profiles decide whether it is unused, held or tapped.
            upper_good = self._points_good(
                pose_map,
                ("left_shoulder", "right_shoulder", "left_elbow", "right_elbow", "left_wrist", "right_wrist"),
                0.38,
            )
            if upper_good:
                ls, rs = pose_map["left_shoulder"], pose_map["right_shoulder"]
                lh, rh = pose_map["left_hip"], pose_map["right_hip"]
                la, ra = pose_map["left_ankle"], pose_map["right_ankle"]
                lw, rw = pose_map["left_wrist"], pose_map["right_wrist"]
                left_ankle_lat = self._lateral_coordinate(la, ls, rs)
                right_ankle_lat = self._lateral_coordinate(ra, ls, rs)
                foot_span = right_ankle_lat - left_ankle_lat
                left_wrist_lat = self._lateral_coordinate(lw, ls, rs)
                right_wrist_lat = self._lateral_coordinate(rw, ls, rs)
                wrist_span = right_wrist_lat - left_wrist_lat
                feet_wide = foot_span > 1.42
                arms_overhead = (
                    float(lw["y"]) < float(ls["y"]) - 0.28 * torso
                    and float(rw["y"]) < float(rs["y"]) - 0.28 * torso
                )
                arms_sideways = (
                    wrist_span > 1.72
                    and max(float(lw["y"]), float(rw["y"])) < float(hip["y"]) - 0.08 * torso
                    and min(float(lw["y"]), float(rw["y"])) > float(shoulder["y"]) - 0.48 * torso
                )
                jumping_jack_raw = feet_wide and arms_overhead
                side_step_jack_raw = feet_wide and arms_sideways and not jumping_jack_raw

        else:
            self.step.update({"left_was": False, "right_was": False, "active_until": 0.0})

        # This action deliberately does not depend on either wrist or ankle.
        # During exercise both are commonly occluded, while the semantic event
        # is still observable from the raised knee and the opposite elbow.
        elbow_knee_good = math.isfinite(torso) and self._points_good(
            pose_map,
            ("left_hip", "right_hip", "left_elbow", "right_elbow", "left_knee", "right_knee"),
            0.36,
        )
        if elbow_knee_good:
            def body_distance(a: dict, b: dict) -> float:
                dx = (float(a["x"]) - float(b["x"])) * self.width
                dy = (float(a["y"]) - float(b["y"])) * self.height
                return math.hypot(dx, dy) / max(1e-6, torso * self.height)

            lh, rh = pose_map["left_hip"], pose_map["right_hip"]
            left_knee_raised = float(pose_map["left_knee"]["y"]) < float(lh["y"]) + 0.58 * torso
            right_knee_raised = float(pose_map["right_knee"]["y"]) < float(rh["y"]) + 0.58 * torso
            cross_knee_elbow_raw = (
                left_knee_raised and body_distance(pose_map["left_knee"], pose_map["right_elbow"]) < 0.72
            ) or (
                right_knee_raised and body_distance(pose_map["right_knee"], pose_map["left_elbow"]) < 0.72
            )
        active = set()
        if self._set_motion_debounced("march", march_raw, 1, 2): active.add("march")
        if self._set_motion_debounced("calf_back", calf_raw, 3, 4): active.add("calf_back")
        if self._set_motion_debounced("squat", squat_raw, 3, 4): active.add("squat")
        if self._set_motion_debounced("hands_up", hands_raw, 3, 4): active.add("hands_up")
        if self._set_motion_debounced("jumping_jack", jumping_jack_raw, 2, 3): active.add("jumping_jack")
        if self._set_motion_debounced("side_step_jack", side_step_jack_raw, 2, 3): active.add("side_step_jack")
        if self._set_motion_debounced("cross_knee_elbow", cross_knee_elbow_raw, 2, 3): active.add("cross_knee_elbow")
        changed = active != self.motion_active
        self.motion_active = active
        if changed:
            self.last_motion_emit = now

    # ---------- cross poses + unified mapping ----------

    def _set_pose_debounced(self, ident: str, raw: bool, on_frames: int = 2, off_frames: int = 2) -> bool:
        state = self.pose_debounce[ident]
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

    @staticmethod
    def _lateral_coordinate(point: dict, left_ref: dict, right_ref: dict) -> float:
        """Body-side coordinate: left ~= -0.5, right ~= +0.5, mirror invariant."""
        span = float(right_ref["x"]) - float(left_ref["x"])
        width = max(1e-5, abs(span))
        sign = 1.0 if span >= 0.0 else -1.0
        mid = (float(left_ref["x"]) + float(right_ref["x"])) * 0.5
        return (float(point["x"]) - mid) * sign / width

    def _update_cross_poses_locked(self, pose_map: dict[str, dict], now: float) -> None:
        active: set[str] = set()
        confidence = {"hands_cross": 0.0}

        torso_good = self._points_good(pose_map, ("left_shoulder", "right_shoulder", "left_hip", "right_hip"), 0.45)
        if torso_good:
            ls, rs = pose_map["left_shoulder"], pose_map["right_shoulder"]
            lh, rh = pose_map["left_hip"], pose_map["right_hip"]
            shoulder_mid = _midpoint(ls, rs)
            hip_mid = _midpoint(lh, rh)
            torso = max(0.025, abs(float(hip_mid["y"]) - float(shoulder_mid["y"])))

            # Hands crossed: real video shows wrist identity/occlusion jitter near
            # the crossing point. Use the forearm-X geometry plus chest location,
            # instead of requiring both wrists to sit deeply on the opposite side.
            hands_good = self._points_good(
                pose_map,
                ("left_elbow", "right_elbow", "left_wrist", "right_wrist"),
                0.44,
            )
            hands_raw = False
            if hands_good:
                le, re = pose_map["left_elbow"], pose_map["right_elbow"]
                lw, rw = pose_map["left_wrist"], pose_map["right_wrist"]
                left_lat = self._lateral_coordinate(lw, ls, rs)
                right_lat = self._lateral_coordinate(rw, ls, rs)
                le_lat = self._lateral_coordinate(le, ls, rs)
                re_lat = self._lateral_coordinate(re, ls, rs)
                y_mid = (float(lw["y"]) + float(rw["y"])) * 0.5
                chest_low = float(hip_mid["y"]) + 0.10 * torso
                chest_high = float(shoulder_mid["y"]) - 0.18 * torso
                vertical_close = abs(float(lw["y"]) - float(rw["y"])) <= 0.55 * torso
                wrist_gap = abs(left_lat - right_lat)
                forearms_point_inward = (left_lat - le_lat) > 0.10 and (right_lat - re_lat) < -0.10
                crossed_order = left_lat > right_lat + 0.07
                near_center = abs(left_lat) < 0.72 and abs(right_lat) < 0.72
                hands_raw = (
                    forearms_point_inward
                    and crossed_order
                    and near_center
                    and wrist_gap < 0.72
                    and chest_high <= y_mid <= chest_low
                    and vertical_close
                )
                cross_depth = max(0.0, min(1.0, (left_lat - right_lat - 0.07) / 0.52))
                confidence["hands_cross"] = round(0.58 + 0.38 * cross_depth, 3) if hands_raw else round(0.30 * cross_depth, 3)

            if self._set_pose_debounced("hands_cross", hands_raw):
                active.add("hands_cross")
        else:
            for ident in self.pose_debounce:
                self._set_pose_debounced(ident, False)

        self.pose_active = active
        self.pose_confidence = confidence

    def _effective_binding_locked(self, trigger: str) -> dict | None:
        binding = self.control_bindings.get(trigger)
        if binding is not None:
            if binding.get("disabled"):
                return None
            return binding
        prefix, _, ident = trigger.partition(".")
        if prefix == "zone" and ident in BODY_ZONES and BODY_ZONES[ident].get("button"):
            return {"action": {"type": "gamepad", "target": BODY_ZONES[ident]["button"], "behavior": "hold"}}
        if prefix == "motion":
            for item in self.motion_config:
                if item.get("id") == ident and item.get("enabled") and item.get("target"):
                    return {"action": {"type": item.get("type", "gamepad"), "target": item.get("target"), "behavior": "hold"}}
        return None

    def _dispatch_controls_locked(self, now: float) -> None:
        active = {f"zone.{name}" for name, state in self.zone_state.items() if name in BODY_ZONES and state.get("pressed")}
        active.update(f"motion.{name}" for name in self.motion_active)
        active.update(f"pose.{name}" for name in self.pose_active)

        holds = []
        for trigger in sorted(active):
            binding = self._effective_binding_locked(trigger)
            if not binding:
                continue
            action = copy.deepcopy(binding.get("action", {}))
            behavior = str(action.get("behavior", "hold")).lower()
            if trigger.startswith("pose."):
                behavior = "tap"
                action["behavior"] = "tap"
            if behavior == "tap":
                if trigger not in self.trigger_previous:
                    action["source"] = f"trigger:{trigger}:{time.monotonic_ns()}"
                    action["nonblocking"] = True
                    executor = getattr(self.output, "execute_action", None)
                    if executor is not None:
                        self._safe_output(executor, action)
            else:
                holds.append({"id": trigger, "action": action})

        setter = getattr(self.output, "set_action_holds", None)
        if setter is not None:
            self._safe_output(setter, holds, source_group="controls")
        else:
            # Keep test doubles and older OutputManager-compatible adapters working.
            # New runtimes use set_action_holds; legacy adapters still understand
            # the previous flat {id,type,target} hold format.
            legacy_setter = getattr(self.output, "set_holds", None)
            if legacy_setter is not None:
                legacy_holds = []
                for item in holds:
                    action = item.get("action", {})
                    ident = str(item.get("id", ""))
                    legacy_holds.append({"id": ident.split(".", 1)[-1], "type": action.get("type", ""), "target": action.get("target", "")})
                self._safe_output(legacy_setter, legacy_holds)
        self.trigger_previous = active

    # ---------- clean head control ----------

    def _update_head_locked(
        self,
        pose_map: dict[str, dict],
        now: float,
        world_pose: dict[str, dict] | list[dict] | None = None,
    ) -> None:
        # Body actions and the output backend remain unchanged; only head
        # estimation/mapping is delegated to HeadController.  The look gate
        # no longer freezes X: yaw remains independent of vertical permission.
        if world_pose is None:
            x, _pitch_y = self.head_controller.update(
                pose_map, self.width, self.height, now,
            )
        else:
            x, _pitch_y = self.head_controller.update(
                pose_map, self.width, self.height, now, world_pose=world_pose,
            )
        self.head = self.head_controller.status(now)

        y = 0.0
        self.vertical_wrist_norm = 0.0
        self.vertical_pitch_norm = 0.0
        self.vertical_pitch_relative = 0.0
        source = "head" if str(self.vertical_look.get("source", "hand")).lower() == "head" else "hand"
        if bool(self.vertical_look.get("enabled")) and self.vertical_gate_active:
            vcfg = self.vertical_look
            if source == "head":
                # A new gate entry establishes a temporary center from the
                # current filtered pitch.  This prevents an already-held nod
                # from causing a jump when the user authorizes vertical look.
                signal_pitch = getattr(self.head_controller, "signal_pitch", math.nan)
                if not math.isfinite(signal_pitch):
                    signal_pitch = _finite(self.head.get("raw_pitch"), math.nan)
                if self.vertical_head_anchor_pitch is None and math.isfinite(signal_pitch):
                    self.vertical_head_anchor_samples.append(float(signal_pitch))
                    if len(self.vertical_head_anchor_samples) >= 3:
                        self.vertical_head_anchor_pitch = float(statistics.median(self.vertical_head_anchor_samples))
                        self.vertical_head_anchor_samples.clear()
                        self.vertical_pitch_intent.reset()
                if self.vertical_head_anchor_pitch is not None and math.isfinite(signal_pitch):
                    try:
                        pitch_span = float(self.head_controller._span()[1])
                    except Exception:
                        pitch_span = 1.0
                    pitch_span = max(1e-6, pitch_span)
                    relative = _clamp((float(signal_pitch) - self.vertical_head_anchor_pitch) / pitch_span, -1.0, 1.0)
                    if bool(self.head_controller.config.get("invert_y")):
                        relative = -relative
                    deadzone = _clamp(vcfg.get("deadzone", 0.08), 0.03, 0.22)
                    intent = self.vertical_pitch_intent.step(
                        relative, now,
                        angle_threshold=PITCH_INTENT_ANGLE,
                        start_velocity=PITCH_INTENT_START_VELOCITY,
                        stop_velocity=PITCH_INTENT_STOP_VELOCITY,
                        release_threshold=deadzone * 0.62,
                    )
                    self.vertical_pitch_relative = relative
                    self.vertical_pitch_velocity = intent["velocity"]
                    self.vertical_pitch_acceleration = intent["acceleration"]
                    self.vertical_pitch_intent_state = intent["state"]
                    # The look gate is already the player's explicit vertical
                    # permission.  Inside it, a calibrated head-pitch
                    # deflection controls view velocity directly; the intent
                    # state remains diagnostic and cannot silence a held nod.
                    if abs(relative) > deadzone:
                        amount = (abs(relative) - deadzone) / max(1e-6, 1.0 - deadzone)
                        shaped = _clamp(amount, 0.0, 1.0) ** 1.12
                        y = math.copysign(shaped, relative) * _clamp(
                            float(self.head_controller.config.get("sensitivity_y", 46.0)) / 100.0,
                            0.15, 1.0,
                        )
                    self.vertical_pitch_norm = _clamp(y, -1.0, 1.0)
            else:
                self._reset_vertical_head_locked()
                hand_state = self.vertical_hand_controller.update(pose_map, now, vcfg)
                self._sync_vertical_hand_locked(hand_state)
                y = float(hand_state["output"])

        if not self.vertical_gate_active:
            self._reset_vertical_head_locked()
        horizontal_paused = bool(self.vertical_gate_active and self.vertical_look.get("exclusive_axes", False))
        if horizontal_paused:
            x = 0.0
        x = self._guard_horizontal_output_locked(x, now)
        self.vertical_wrist_norm = _clamp(y, -1.0, 1.0)
        self.head["normalized_x"] = round(float(x), 4)
        self.head["output_x"] = round(float(x), 3)
        self.head["normalized_y"] = round(float(y), 4)
        self.head["output_y"] = round(float(y), 3)
        self.head["vertical_look_source"] = source
        self.head["verticalLookSource"] = source
        self.head["horizontal_paused_by_vertical_gate"] = horizontal_paused
        self.head["horizontal_paused_by_body_motion"] = bool(self.body_motion_guard_active)
        self.head["vertical_pitch_relative"] = round(float(self.vertical_pitch_relative), 4)
        self.head["vertical_pitch_norm"] = round(float(self.vertical_pitch_norm), 4)
        self.head["vertical_pitch_velocity"] = round(float(self.vertical_pitch_velocity), 4)
        self.head["vertical_pitch_acceleration"] = round(float(self.vertical_pitch_acceleration), 4)
        self.head["vertical_pitch_intent_state"] = self.vertical_pitch_intent_state
        self.head["vertical_head_anchor_pitch"] = (
            round(float(self.vertical_head_anchor_pitch), 5)
            if self.vertical_head_anchor_pitch is not None else None
        )
        self.head["vertical_wrist_anchor_y"] = (
            round(float(self.vertical_wrist_anchor_y), 4)
            if self.vertical_wrist_anchor_y is not None else None
        )
        self.head["vertical_wrist_anchor_rel_y"] = (
            round(float(self.vertical_wrist_anchor_rel_y), 4)
            if self.vertical_wrist_anchor_rel_y is not None else None
        )
        self.head["vertical_anchor_samples"] = len(self.vertical_anchor_samples)
        if getattr(self.output, "enabled", True):
            self._safe_output(self.output.apply, x, y)

    # ---------- safety/status ----------

    def _clear_body_outputs_locked(self) -> None:
        for state in self.zone_state.values():
            state.update({"inside": 0, "outside": 0, "pressed": False})
        self.zone_rects = {}
        self.vertical_gate_active = False
        self._reset_body_motion_guard_locked()
        self._reset_vertical_hand_locked()
        self.vertical_head_anchor_samples.clear()
        self._reset_vertical_head_locked()
        self.motion_active.clear()
        for state in self.motion_debounce.values():
            state.update({"active": False, "on": 0, "off": 0})
        self.pose_active.clear()
        self.pose_confidence = {}
        for state in self.pose_debounce.values():
            state.update({"active": False, "on": 0, "off": 0})
        self.trigger_previous.clear()
        self.step.update({"left_was": False, "right_was": False, "last_side": "", "last_at": 0.0, "active_until": 0.0})
        self.head_controller.reset_tracking()
        self.head = self.head_controller.status(time.monotonic())
        self._safe_output(self.output.set_buttons, [], source="zones")
        self._safe_output(self.output.set_holds, [], source_group="motions")
        setter = getattr(self.output, "set_action_holds", None)
        if setter is not None:
            self._safe_output(setter, [], source_group="controls")
        self._safe_output(self.output.apply, 0.0, 0.0)

    def _clear_body_locked(self) -> None:
        if self.head_controller.calibrating:
            self.head_controller.cancel_center("人体来源已断开")
        self.latest_pose = None
        self.latest_world_pose = None
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
        gate_available = (self.fixed_zones_enabled and "lookGate" in self.fixed_zones) or (not self.fixed_zones_enabled and "lookGate" in self.zone_rects)
        zone_names = list(BODY_ZONES) + (["lookGate"] if gate_available else [])
        zones = {}
        for name in zone_names:
            if self.fixed_zones_enabled:
                zones[name] = {"circle": copy.deepcopy(self.fixed_zones.get(name)), "pressed": bool(self.zone_state.get(name, {}).get("pressed", False))}
            else:
                zones[name] = {"rect": copy.deepcopy(self.zone_rects.get(name)), "pressed": bool(self.zone_state[name]["pressed"])}
        self.head = self.head_controller.status(now)
        source = "head" if str(self.vertical_look.get("source", "hand")).lower() == "head" else "hand"
        vertical_output = self.vertical_pitch_norm if source == "head" else self.vertical_wrist_norm
        self.head["vertical_source"] = "head_pitch" if self.vertical_look.get("enabled") and source == "head" else "right_wrist" if self.vertical_look.get("enabled") else "off"
        self.head["vertical_look_source"] = source
        self.head["verticalLookSource"] = source
        self.head["vertical_gate_active"] = bool(self.vertical_gate_active)
        horizontal_paused = bool(self.vertical_gate_active and self.vertical_look.get("exclusive_axes", False))
        self.head["horizontal_paused_by_vertical_gate"] = horizontal_paused
        if horizontal_paused or self.body_motion_guard_active:
            self.head["normalized_x"] = 0.0
            self.head["output_x"] = 0.0
        self.head["horizontal_paused_by_body_motion"] = bool(self.body_motion_guard_active)
        self.head["vertical_wrist_norm"] = round(float(self.vertical_wrist_norm), 4)
        self.head["vertical_pitch_relative"] = round(float(self.vertical_pitch_relative), 4)
        self.head["vertical_pitch_norm"] = round(float(self.vertical_pitch_norm), 4)
        self.head["vertical_pitch_velocity"] = round(float(self.vertical_pitch_velocity), 4)
        self.head["vertical_pitch_acceleration"] = round(float(self.vertical_pitch_acceleration), 4)
        self.head["vertical_pitch_intent_state"] = self.vertical_pitch_intent_state
        self.head["vertical_head_anchor_pitch"] = (
            round(float(self.vertical_head_anchor_pitch), 5)
            if self.vertical_head_anchor_pitch is not None else None
        )
        self.head["vertical_wrist_anchor_y"] = (
            round(float(self.vertical_wrist_anchor_y), 4)
            if self.vertical_wrist_anchor_y is not None else None
        )
        self.head["vertical_wrist_anchor_rel_y"] = (
            round(float(self.vertical_wrist_anchor_rel_y), 4)
            if self.vertical_wrist_anchor_rel_y is not None else None
        )
        self.head["vertical_anchor_samples"] = len(self.vertical_anchor_samples)
        self.head["body_motion_guard_enabled"] = bool(self.body_motion_guard_enabled)
        self.head["body_motion_guard_active"] = bool(self.body_motion_guard_active)
        self.head["body_motion_guard_score"] = round(float(self.body_motion_guard_score), 4)
        self.head["body_motion_guard_raw"] = round(float(self.body_motion_guard_raw), 4)
        # Always expose the final output Y, never the diagnostic pitch value.
        self.head["normalized_y"] = round(
            float(vertical_output) if self.vertical_gate_active else 0.0, 4
        )
        self.head["output_y"] = round(
            float(vertical_output) if self.vertical_gate_active else 0.0, 3
        )
        sensors = {
            source: {key: copy.deepcopy(value) for key, value in state.items() if key != "received_at"}
            | {"age_ms": round(max(0.0, (now - state["received_at"]) * 1000.0))}
            for source, state in self.sensor_sources.items()
        }
        return {
            "active_body_source": self.active_body_source,
            "pose_age_ms": pose_age,
            "width": self.width,
            "height": self.height,
            "pose": copy.deepcopy(self.latest_pose),
            # Keep metric landmarks out of the regular status payload (it is
            # polled frequently), but expose whether the current frame carried
            # them so calibration diagnostics can distinguish a missing world
            # stream from a rejected personal template.
            "world_pose_available": bool(self.latest_world_pose),
            "zones": zones,
            "buttons": self._pressed_keys_locked(),
            "motions": sorted(self.motion_active),
            "poses_active": sorted(self.pose_active),
            "pose_confidence": copy.deepcopy(self.pose_confidence),
            "control_bindings": copy.deepcopy(self.control_bindings),
            "scene_mode": "fixed" if self.fixed_zones_enabled else "body_relative_provisional",
            "vertical_look": copy.deepcopy(self.vertical_look),
            "vertical_gate_active": bool(self.vertical_gate_active),
            "body_motion_guard_enabled": bool(self.body_motion_guard_enabled),
            "body_motion_guard_active": bool(self.body_motion_guard_active),
            "body_motion_guard_score": round(float(self.body_motion_guard_score), 4),
            "vertical_wrist_norm": round(float(self.vertical_wrist_norm), 4),
            "vertical_look_source": source,
            "vertical_pitch_relative": round(float(self.vertical_pitch_relative), 4),
            "vertical_pitch_norm": round(float(self.vertical_pitch_norm), 4),
            "vertical_pitch_velocity": round(float(self.vertical_pitch_velocity), 4),
            "vertical_pitch_acceleration": round(float(self.vertical_pitch_acceleration), 4),
            "vertical_pitch_intent_state": self.vertical_pitch_intent_state,
            "vertical_wrist_anchor_y": (
                round(float(self.vertical_wrist_anchor_y), 4)
                if self.vertical_wrist_anchor_y is not None else None
            ),
            "head": copy.deepcopy(self.head),
            "handheld_sources": sensors,
            "last_error": self.last_error,
        }

    def status(self) -> dict:
        with self._lock:
            return self.status_locked(time.monotonic())

    def _watch_loop(self) -> None:
        while not self._stop.wait(0.05):
            now = time.monotonic()
            with self._lock:
                # Center capture has a hard wall-clock limit even if valid
                # frames stop arriving.  The controller owns that finite state.
                if self.head_controller.calibrating and now >= self.head_controller.center_deadline:
                    self.head_controller.timeout_center("中心记录超时：姿态流中断")
                    self.head = self.head_controller.status(now)
                    self._safe_output(self.output.apply, 0.0, 0.0)
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
    # The browser requests preview frames at roughly 6.7 Hz.  Keep a small
    # server-side headroom while avoiding an unconditional 15 Hz JPEG encoder
    # when no browser is looking at the preview.
    PREVIEW_FPS = 8.0
    PREVIEW_DEMAND_SECONDS = 1.0

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
        self._preview_requested_until = 0.0
        self._capture_times: deque[float] = deque(maxlen=120)
        self._inference_times: deque[float] = deque(maxlen=120)
        self._inference_durations_ms: deque[float] = deque(maxlen=120)
        self._latencies_ms: deque[float] = deque(maxlen=120)
        self._preview_times: deque[float] = deque(maxlen=120)
        self._preview_durations_ms: deque[float] = deque(maxlen=120)
        self._preview_sizes: deque[int] = deque(maxlen=120)

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
        self._preview_requested_until = 0.0
        self._capture_times.clear()
        self._inference_times.clear()
        self._inference_durations_ms.clear()
        self._latencies_ms.clear()
        self._preview_times.clear()
        self._preview_durations_ms.clear()
        self._preview_sizes.clear()

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
                world_landmarks = (
                    result.pose_world_landmarks[0]
                    if getattr(result, "pose_world_landmarks", None)
                    else None
                )
                pose_map = None
                if landmarks:
                    pose_map = {
                        MP_NAMES[index]: {
                            "x": _finite(point.x), "y": _finite(point.y), "z": _finite(point.z),
                            "score": _finite(getattr(point, "visibility", getattr(point, "presence", 1.0)), 1.0),
                        }
                        for index, point in enumerate(landmarks)
                    }
                world_pose = None
                if world_landmarks:
                    world_pose = {
                        MP_NAMES[index]: {
                            "x": _finite(point.x), "y": _finite(point.y), "z": _finite(point.z),
                            "score": _finite(getattr(point, "visibility", getattr(point, "presence", 1.0)), 1.0),
                        }
                        for index, point in enumerate(world_landmarks)
                    }
                self.kernel.handle_pose_map(
                    "computer_camera", pose_map, width=width, height=height,
                    world_pose=world_pose,
                )
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
                    while not self._stop.is_set():
                        now = time.monotonic()
                        demand_active = now < self._preview_requested_until
                        frame_ready = self._latest_sequence > last_sequence
                        if not demand_active or not frame_ready:
                            self._condition.wait(0.10)
                            continue
                        wait = next_encode_at - now
                        if wait > 0:
                            self._condition.wait(min(wait, 0.10))
                            continue
                        sequence = self._latest_sequence
                        frame = self._latest_frame
                        last_sequence = sequence
                        break
                    if self._stop.is_set():
                        break
                if frame is None:
                    continue
                encode_started = time.perf_counter()
                ok, encoded = cv2.imencode(
                    ".jpg", frame,
                    [int(cv2.IMWRITE_JPEG_QUALITY), 78],
                )
                encode_finished = time.perf_counter()
                next_encode_at = time.monotonic() + (1.0 / self.PREVIEW_FPS)
                if not ok:
                    continue
                preview = encoded.tobytes()
                preview_at = time.monotonic()
                with self._condition:
                    self._preview_jpeg = preview
                    self._preview_sequence = sequence
                    self._preview_at = preview_at
                    self._preview_times.append(preview_at)
                    self._preview_durations_ms.append((encode_finished - encode_started) * 1000.0)
                    self._preview_sizes.append(len(preview))
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
                "preview_fps": self._round_or_none(self._rate(self._preview_times), 2),
                "preview_encode_avg_ms": self._round_or_none(
                    sum(self._preview_durations_ms) / len(self._preview_durations_ms)
                    if self._preview_durations_ms else None,
                ),
                "preview_encode_p95_ms": self._round_or_none(self._p95(self._preview_durations_ms)),
                "preview_jpeg_avg_bytes": round(sum(self._preview_sizes) / len(self._preview_sizes)) if self._preview_sizes else None,
                "preview_last_age_ms": round(max(0.0, (now - self._preview_at) * 1000.0)) if self._preview_at else None,
                "running": bool(self.running),
                "last_error": self.last_error,
            }

    def latest_preview(self) -> bytes | None:
        # A preview request is a short-lived demand signal.  This keeps the
        # encoder asleep when the browser is hidden or the camera preview is
        # not in use, without changing the endpoint's latest-JPEG semantics.
        with self._condition:
            self._preview_requested_until = max(
                self._preview_requested_until,
                time.monotonic() + self.PREVIEW_DEMAND_SECONDS,
            )
            self._condition.notify_all()
            return bytes(self._preview_jpeg) if self._preview_jpeg else None

    def latest_frame(self):
        """Return the latest raw OpenCV frame (BGR) or None. Used by scene capture."""
        with self._lock:
            return self._latest_frame.copy() if self._latest_frame is not None else None


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


# Keep the hand-anchor logic at the input/recognition boundary.  The existing
# head-control and automatic-calibration implementation remains untouched.
from hand_anchor import install_hand_anchor_adapter

install_hand_anchor_adapter(ControlKernel)
