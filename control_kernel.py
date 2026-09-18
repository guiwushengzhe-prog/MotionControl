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
from motioncontrol_shared.profile_schema import flatten_bindings
from motioncontrol_shared.motion_conflicts import validate_motion_config
from hand_mouse_control import HANDS, HandMouseController
from pose_recorder import PoseRecorder


def _user_recordings_dir():
    """Where skeleton recordings land: beside the user's other data.

    Not in the program folder -- recordings are the user's, and since 2.0.x the
    program folder is treated as read-only so an upgrade can replace it.
    """
    from user_paths import user_data_root

    return user_data_root() / "recordings"
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

# Runtime body zones use one broad hand area per side.  The four historical
# hand ids remain in BODY_ZONES above so old profiles and API consumers keep
# working; ZONE_ALIASES below maps them to the new physical regions.
RUNTIME_BODY_ZONES = {
    "leftHand": {"label": "X", "button": "X", "points": ("left_wrist",), "kind": "hand"},
    "rightHand": {"label": "B", "button": "B", "points": ("right_wrist",), "kind": "hand"},
    "leftFoot": BODY_ZONES["leftFoot"],
    "rightFoot": BODY_ZONES["rightFoot"],
    # A nose entering the fixed area above the head is the explicit jump
    # trigger.  Its default A output is only a starting mapping and is
    # editable through the normal game-profile settings.
    "headJump": {"label": "A", "button": "A", "points": ("nose",), "kind": "head_jump"},
}
ZONE_ALIASES = {
    "leftHandUpper": "leftHand",
    "leftHandLower": "leftHand",
    "rightHandUpper": "rightHand",
    "rightHandLower": "rightHand",
}
RUNTIME_ZONE_NAMES = tuple(RUNTIME_BODY_ZONES)

BODY_MOTION_GUARD_POINTS = (
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist", "left_hip", "right_hip",
    "left_knee", "right_knee", "left_ankle", "right_ankle",
)

# Body-guard temporal semantics are expressed in seconds, not frame counts.
# The values preserve the promoted 30 FPS behavior while avoiding materially
# earlier confirmation/recovery at 45/60 FPS. Sampling still quantizes the
# observed transition time, especially at 20 FPS.
BODY_MOTION_CHAIN_CONFIRM_S = 0.030
BODY_MOTION_STRONG_BURST_CONFIRM_S = 0.095
BODY_MOTION_SETTLE_S = 0.060
BODY_MOTION_QUALITY_GRACE_S = 0.150
# Public runtime label for the body-motion guard implementation.  This is a
# diagnostic/UI identifier only; it does not select or alter a head algorithm.
BODY_MOTION_GUARD_VERSION = "C2.10"
# Body-guard-only mirror of the existing action debounce semantics at 30 FPS.
# This does not alter motion_active or any game/action trigger; it only prevents
# the body guard from inheriting frame-rate-dependent activation times.
BODY_MOTION_ACTION_RISK_TIMING = {
    "march": (0.000, 0.030),
    "calf_back": (0.060, 0.095),
    "squat": (0.060, 0.095),
    "hands_up": (0.060, 0.095),
    "jumping_jack": (0.030, 0.060),
    "side_step_jack": (0.030, 0.060),
    "cross_knee_elbow": (0.030, 0.060),
}

# Head-jump anchor tuning.  The anchor exists so the target above the head can
# track a changed stance without also riding up with a jump.  Lateral drift is
# followed promptly; vertical drift is followed slowly and stops entirely above
# the freeze speed.  0.35 torso lengths per second matches the coherent-vertical
# threshold the body-motion guard already uses, and a jump peaks near 1.2.
HEAD_JUMP_FREEZE_VY = 0.35
HEAD_JUMP_FOLLOW_X_S = 0.35
HEAD_JUMP_FOLLOW_Y_S = 1.50
# A jump spans roughly 0.3-0.5 torso, so this only fires when the player truly
# relocated or the camera was re-aimed.
HEAD_JUMP_SNAP_TORSO = 1.20


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


def _enclose_rects(rects: list[dict]) -> dict | None:
    """Return one camera-space rectangle containing the supplied rectangles."""
    valid = [item for item in rects if isinstance(item, dict)]
    if not valid:
        return None
    return {
        "x1": _clamp(min(float(item.get("x1", 0.0)) for item in valid), 0.0, 1.0),
        "x2": _clamp(max(float(item.get("x2", 1.0)) for item in valid), 0.0, 1.0),
        "y1": _clamp(min(float(item.get("y1", 0.0)) for item in valid), 0.0, 1.0),
        "y2": _clamp(max(float(item.get("y2", 1.0)) for item in valid), 0.0, 1.0),
    }


def _enclose_circles(circles: list[dict]) -> dict | None:
    """Return one circle containing old per-side circles for migration."""
    valid = []
    for item in circles:
        if not isinstance(item, dict):
            continue
        try:
            cx, cy, radius = float(item["cx"]), float(item["cy"]), float(item["r"])
        except (KeyError, TypeError, ValueError):
            continue
        if not all(math.isfinite(v) for v in (cx, cy, radius)) or radius <= 0.0:
            continue
        valid.append((cx, cy, radius))
    if not valid:
        return None
    if len(valid) == 1:
        cx, cy, radius = valid[0]
    else:
        x1 = min(cx - radius for cx, _cy, radius in valid)
        x2 = max(cx + radius for cx, _cy, radius in valid)
        y1 = min(cy - radius for _cx, cy, radius in valid)
        y2 = max(cy + radius for _cx, cy, radius in valid)
        cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        radius = max(math.hypot(cx - px, cy - py) + pr for px, py, pr in valid)
    radius = _clamp(radius, 0.025, 0.30)
    return {
        "shape": "circle",
        "cx": _clamp(cx, radius, 1.0 - radius),
        "cy": _clamp(cy, radius, 1.0 - radius),
        "r": radius,
    }


def _canonical_fixed_zones(zones: dict | None) -> dict:
    """Normalize old six-zone layouts to the merged hand/head-jump schema."""
    source = zones if isinstance(zones, dict) else {}
    result: dict = {}
    for name in ("leftFoot", "rightFoot", "lookGate", "headJump"):
        value = source.get(name)
        if isinstance(value, dict):
            result[name] = copy.deepcopy(value)
    for name, aliases in (
        ("leftHand", ("leftHandUpper", "leftHandLower")),
        ("rightHand", ("rightHandUpper", "rightHandLower")),
    ):
        value = source.get(name)
        if isinstance(value, dict):
            result[name] = copy.deepcopy(value)
        else:
            merged = _enclose_circles([source.get(alias) for alias in aliases])
            if merged:
                result[name] = merged
    # Old layouts had two head-side circles but no jump target.  Place the new
    # target just above their combined center so a small head rise can enter it.
    if "headJump" not in result:
        upper = [source.get("leftHandUpper"), source.get("rightHandUpper")]
        merged = _enclose_circles(upper)
        if merged:
            result["headJump"] = {
                "shape": "circle",
                "cx": merged["cx"],
                "cy": _clamp(merged["cy"] - merged["r"] * 1.65, merged["r"], 1.0 - merged["r"]),
                "r": _clamp(merged["r"] * 0.90, 0.04, 0.12),
            }
    return result


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
        # 21 points per hand, keyed "left"/"right".  Only present while the
        # desktop has asked a device for them; everything here still works
        # without it, just from the coarser pose fingertips.
        self.latest_hands: dict[str, list[dict]] | None = None
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
        # Provisional head-jump target anchor.  It deliberately does not track
        # the nose frame by frame: a jump lifts the whole body, so a fast
        # follower carries the target upward and the nose can never enter it.
        self.head_jump_anchor: dict[str, float] | None = None
        self.head_jump_prev: tuple[float, float, float] | None = None
        self.vertical_look = {
            # Before the first fixed-scene capture we still expose a provisional
            # body-relative lookGate so the six-region layout is visible and usable.
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
        self.hand_mouse_controller = HandMouseController()
        self.pose_recorder = PoseRecorder(_user_recordings_dir())
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
        self.zone_state = {name: {"inside": 0, "outside": 0, "pressed": False} for name in RUNTIME_BODY_ZONES}
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
        self.body_motion_action_risk: set[str] = set()
        self.body_motion_action_risk_debounce = {
            key: {"active": False, "on_since": 0.0, "off_since": 0.0}
            for key in (
                "march", "calf_back", "squat", "hands_up",
                "jumping_jack", "side_step_jack", "cross_knee_elbow",
            )
        }
        self.step = {"left_was": False, "right_was": False, "last_side": "", "last_at": 0.0, "active_until": 0.0}
        self.foot_neutral: dict[str, float] = {}
        self.last_motion_emit = 0.0

        # Head estimation keeps observing frames, but strong exercise motion
        # must not move the in-game camera. This guard uses body-normalized
        # limb velocity because action labels can be intermittent or absent.
        self.body_motion_guard_enabled = True
        self.body_motion_guard_active = False
        self.body_motion_guard_raw = 0.0
        self.body_motion_guard_score = 0.0
        self.body_motion_guard_previous: dict[str, tuple[float, float]] = {}
        self.body_motion_guard_previous_centers: tuple[float, float] | None = None
        self.body_motion_guard_early_evidence = False
        self.body_motion_guard_early_until = 0.0
        self.body_motion_guard_early_run = 0
        self.body_motion_guard_early_started_at = 0.0
        self.body_motion_guard_early_last_at = 0.0
        self.body_motion_guard_postburst_budget = 0
        self.body_motion_guard_postburst_until = 0.0
        self.body_motion_guard_distal_runs = {
            "left_arm": 0, "right_arm": 0, "left_leg": 0, "right_leg": 0
        }
        self.body_motion_guard_distal_since = {
            "left_arm": 0.0, "right_arm": 0.0, "left_leg": 0.0, "right_leg": 0.0
        }
        self.body_motion_guard_segment_runs = {
            "left_arm": 0, "right_arm": 0, "left_leg": 0, "right_leg": 0
        }
        self.body_motion_guard_segment_since = {
            "left_arm": 0.0, "right_arm": 0.0, "left_leg": 0.0, "right_leg": 0.0
        }
        self.body_motion_guard_last_at = 0.0
        self.body_motion_guard_hold_until = 0.0
        self.body_motion_guard_settle_frames = 0
        self.body_motion_guard_settle_started_at = 0.0
        self.body_motion_guard_output_blocked = False
        self.body_motion_guard_veto_reason = ""

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
        # 用户自己录的姿势。id 是运行时才知道的，所以去抖条目按需建。
        # 存取在 custom_poses.CustomPoseStore 里，由 server.py 装进来——内核不碰
        # 文件，这样测试里可以直接塞一个假的。
        self.custom_pose_store = None
        self.custom_pose_scores: dict[str, float] = {}

        # Head control is intentionally isolated from body actions.  The clean
        # engine owns its estimator, center capture, filtering and compact
        # profile.  Legacy five-stage/head-face state is no longer part of the
        # runtime path.
        self.head_controller = HeadController(self._head_profile_path())
        self._load_general_settings()
        self.head = self.head_controller.status(time.monotonic())
        self.sensor_sources: dict[str, dict] = {}
        self._thread.start()

    def _user_file(self, key: str) -> Path:
        """用户数据文件的位置，走 user_paths 而不是自己拼。

        自己读 LOCALAPPDATA 会绕过 MOTIONCONTROL_USER_DIR，而那正是测试用来
        避开开发者真实数据的开关——绕过它，跑一次测试就可能覆盖掉你自己的
        头控校准或者设置。文件名也只在 motioncontrol_shared 里写一次。
        """
        from user_paths import user_path

        return user_path(key)

    def _head_profile_path(self) -> Path:
        return self._user_file("head_profile")

    def _general_settings_path(self) -> Path:
        return self._user_file("general_settings")


    def _load_general_settings(self) -> None:
        """把「通用设置」里不跟游戏走的那几项读回来。

        这两组原来一个都不存盘：手控鼠标压根没写过盘，上下视角只写在
        scene_layout.json 里，而没定位过区域的人根本没有那个文件。于是每次启动
        都悄悄回到默认值——玩家只会觉得「我明明开过」，界面上看不出任何异常。

        读不出来就当没有：这份文件丢了或者坏了，不该让整个程序起不来。
        """
        try:
            data = json.loads(self._general_settings_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        hand_mouse = data.get("hand_mouse")
        if isinstance(hand_mouse, dict):
            try:
                self.hand_mouse_controller.configure(hand_mouse)
            except (ValueError, TypeError):
                pass
        vertical = data.get("vertical_look")
        if isinstance(vertical, dict):
            if "enabled" in vertical:
                self.vertical_look["enabled"] = bool(vertical["enabled"])
            source = str(vertical.get("source", "")).lower()
            if source in {"hand", "head"}:
                self.vertical_look["source"] = source
                self.vertical_look["verticalLookSource"] = source

    def _save_general_settings(self) -> None:
        """写盘。失败不抛：存不下设置也不该打断正在进行的游戏。"""
        path = self._general_settings_path()
        payload = {
            "saved_at_unix": time.time(),
            "hand_mouse": dict(self.hand_mouse_controller.config),
            "vertical_look": {
                "enabled": bool(self.vertical_look.get("enabled", True)),
                "source": str(self.vertical_look.get("source", "hand")),
            },
        }
        temp = path.with_suffix(path.suffix + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, path)
        except OSError:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

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
    def hand_map_from_message(message: dict) -> dict[str, list[dict]] | None:
        """Named 21-point hands from a frame, in the kernel's canonical space.

        The device labels each hand with the side the desktop asked it to
        watch, so no handedness has to be inferred here.  Mirroring is undone
        exactly as it is for the pose: this kernel only ever reasons in raw,
        unmirrored camera coordinates.
        """
        hands = message.get("hands") if isinstance(message, dict) else None
        if not isinstance(hands, list) or not hands:
            return None
        coordinates_mirrored = bool(message.get("coordinates_mirrored", False))
        result: dict[str, list[dict]] = {}
        for hand in hands:
            if not isinstance(hand, dict):
                continue
            side = str(hand.get("handedness", "")).lower()
            landmarks = hand.get("landmarks")
            if side not in HANDS or not isinstance(landmarks, list) or len(landmarks) != 21:
                continue
            if not all(isinstance(item, dict) for item in landmarks):
                continue
            points = []
            for item in landmarks:
                point = _point(item)
                if coordinates_mirrored:
                    point["x"] = 1.0 - point["x"]
                points.append(point)
            result[side] = points
        return result or None

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
            new_config = [copy.deepcopy(item) for item in (items or []) if isinstance(item, dict)]
            validate_motion_config(new_config)
            self.motion_config = new_config
            self._prune_body_motion_action_risk_locked()
            # Apply enable/disable/remap changes to currently held controls now,
            # instead of waiting for the next Pose frame or watchdog tick.
            # Recognition/debounce state is intentionally preserved.
            self._dispatch_controls_locked(time.monotonic())

    def configure_bindings(self, bindings: dict | None) -> None:
        """Install one effective Game Profile without touching recognition thresholds."""
        with self._lock:
            self.control_bindings = flatten_bindings(bindings)
            self.trigger_previous.clear()
            # A profile switch can disable a motion while its guard-risk debounce
            # is still active. Drop only now-unmapped action-derived evidence;
            # raw/EMA body-motion evidence continues to own the safety guard.
            self._prune_body_motion_action_risk_locked()
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
                if source in {"off", "none", "关闭"}:
                    # 开关必须走这条路，不能只存进场景布局：没定位过区域的玩家
                    # 根本不会保存布局，那样「关闭」点了等于没点。
                    # 保留上一次选的是右手还是头部，重新打开不用再选一次。
                    self.vertical_look["enabled"] = False
                    self.vertical_gate_active = False
                    self._reset_vertical_hand_locked()
                    self._reset_vertical_head_locked()
                else:
                    if source in {"right_wrist", "hand", "右手"}:
                        source = "hand"
                    elif source in {"head", "头部"}:
                        source = "head"
                    else:
                        raise ValueError("vertical_look_source must be hand, head or off")
                    self.vertical_look["enabled"] = True
                    self.vertical_look["source"] = source
                    self.vertical_look["verticalLookSource"] = source
                    self._reset_vertical_head_locked()
                self._save_general_settings()
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
            # Accept the previous four-hand-circle layout, but run only the
            # two merged hand regions plus the two feet and head-jump region.
            self.fixed_zones = _canonical_fixed_zones(zones)
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
            world_pose=world_pose, hands=self.hand_map_from_message(message),
        )

    def handle_pose_map(
        self,
        source_id: str,
        pose_map: dict[str, dict] | None,
        *,
        width: int = 640,
        height: int = 480,
        world_pose: dict[str, dict] | list[dict] | None = None,
        hands: dict[str, list[dict]] | None = None,
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
            # Read back out inside _process_pose_locked rather than threaded
            # through it: that hook still has callers passing positional
            # arguments only, and this keeps them working untouched.
            self.latest_hands = copy.deepcopy(hands) if hands else None
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
        # Evaluated before anything else in the frame: the zone pass and the
        # final apply() both consult the engaged state, and they run at
        # opposite ends of this function.  Updating it in between would let a
        # zone button fire on the very frame the fist closes.
        self.hand_mouse_controller.update(
            pose_map, now, self._hand_points_for_mouse_locked())
        # Recorded after the hand pass so the saved frames carry the fist
        # reading alongside the skeleton -- that pairing is the point of
        # recording at all when tuning the thresholds.
        if self.pose_recorder.state in {"waiting", "recording"}:
            hand = self.hand_mouse_controller.status()
            self.pose_recorder.capture(
                pose_map, now, width=self.width, height=self.height,
                source=str(self.active_body_source or ""),
                extra={"hand_spread": hand["spread"], "fist": hand["engaged"],
                       "hand": hand["hand"]},
            )
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
        self.body_motion_guard_previous_centers = None
        self.body_motion_guard_early_evidence = False
        self.body_motion_guard_early_until = 0.0
        self.body_motion_guard_early_run = 0
        self.body_motion_guard_early_started_at = 0.0
        self.body_motion_guard_early_last_at = 0.0
        self.body_motion_guard_postburst_budget = 0
        self.body_motion_guard_postburst_until = 0.0
        self.body_motion_guard_distal_runs = {
            "left_arm": 0, "right_arm": 0, "left_leg": 0, "right_leg": 0
        }
        self.body_motion_guard_distal_since = {
            "left_arm": 0.0, "right_arm": 0.0, "left_leg": 0.0, "right_leg": 0.0
        }
        self.body_motion_guard_segment_runs = {
            "left_arm": 0, "right_arm": 0, "left_leg": 0, "right_leg": 0
        }
        self.body_motion_guard_segment_since = {
            "left_arm": 0.0, "right_arm": 0.0, "left_leg": 0.0, "right_leg": 0.0
        }
        self.body_motion_guard_last_at = 0.0
        self.body_motion_guard_hold_until = 0.0
        self.body_motion_guard_settle_frames = 0
        self.body_motion_guard_settle_started_at = 0.0
        self.body_motion_guard_output_blocked = False
        self.body_motion_guard_veto_reason = ""

    def _update_body_motion_guard_locked(self, pose_map: dict[str, dict], now: float) -> None:
        """Measure exercise motion without modifying the selected head algorithm."""
        core = ("left_shoulder", "right_shoulder", "left_hip", "right_hip")
        if not self.body_motion_guard_enabled:
            self._reset_body_motion_guard_locked()
            return
        if not self._points_good(pose_map, core, 0.35):
            # Large body motion can briefly degrade shoulder/hip confidence.
            # Do not drop an already-open transient/persistent guard on the
            # exact frame where tracking quality becomes worst. Preserve its
            # existing timers for a short bounded grace, then reset if the
            # torso really remains unavailable.
            recent_valid = bool(
                self.body_motion_guard_last_at > 0.0
                and now - self.body_motion_guard_last_at <= BODY_MOTION_QUALITY_GRACE_S
            )
            guard_in_flight = bool(
                self.body_motion_guard_active
                or now <= self.body_motion_guard_early_until
                or (self.body_motion_guard_postburst_budget > 0 and now <= self.body_motion_guard_postburst_until)
            )
            if recent_valid and guard_in_flight:
                if self.body_motion_guard_active:
                    self.body_motion_guard_hold_until = max(self.body_motion_guard_hold_until, now + 0.060)
                return
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
        speed_count = 0
        peak_speed = 0.0
        second_speed = 0.0
        coherent_vertical_speed = 0.0
        dt = now - self.body_motion_guard_last_at if self.body_motion_guard_last_at else 0.0
        velocity_by_name: dict[str, tuple[float, float]] = {}
        speed_by_name: dict[str, float] = {}
        if 1.0 / 90.0 <= dt <= 0.12:
            velocity_by_name = {
                name: (
                    (value[0] - self.body_motion_guard_previous[name][0]) / dt,
                    (value[1] - self.body_motion_guard_previous[name][1]) / dt,
                )
                for name, value in current.items()
                if name in self.body_motion_guard_previous
            }
            speed_by_name = {
                name: math.hypot(*velocity) for name, velocity in velocity_by_name.items()
            }
            speeds = list(speed_by_name.values())
            speed_count = len(speeds)
            if speed_count >= 2:
                speeds.sort(reverse=True)
                peak_speed = float(speeds[0])
                second_speed = float(speeds[1])
                fastest_half = speeds[:max(1, len(speeds) // 2)]
                raw = float(statistics.fmean(fastest_half))
            if self.body_motion_guard_previous_centers is not None:
                previous_shoulder_y, previous_hip_y = self.body_motion_guard_previous_centers
                shoulder_vy = (float(shoulder["y"]) - previous_shoulder_y) / torso / dt
                hip_vy = (float(hip["y"]) - previous_hip_y) / torso / dt
                if shoulder_vy * hip_vy > 0.0:
                    coherent_vertical_speed = min(abs(shoulder_vy), abs(hip_vy))
        self.body_motion_guard_previous = current
        self.body_motion_guard_previous_centers = (float(shoulder["y"]), float(hip["y"]))
        self.body_motion_guard_last_at = now
        self.body_motion_guard_raw = raw
        alpha = 1.0 - math.exp(-max(0.0, min(0.12, dt)) / 0.10) if dt > 0.0 else 1.0
        self.body_motion_guard_score += alpha * (raw - self.body_motion_guard_score)

        raw_onset = (
            not self.body_motion_guard_active
            and speed_count >= 8
            and raw >= 2.50
        )
        early_limb_onset = (
            not self.body_motion_guard_active
            and speed_count >= 8
            and peak_speed >= 2.40
            and second_speed >= 0.40
        )
        vertical_body_onset = (
            not self.body_motion_guard_active
            and speed_count >= 8
            and coherent_vertical_speed >= 0.35
        )

        # C2.5: some articulated actions are dominated by one distal joint
        # (wrist/ankle), while the elbow/knee only moves modestly. Requiring the
        # global second-fastest point to be large misses these motions. Accept a
        # distal-chain onset only after two consecutive supported frames, so a
        # single-landmark one-frame spike cannot open the transient suppressor.
        distal_specs = (
            ("left_arm", "left_elbow", "left_wrist", 1.20),
            ("right_arm", "right_elbow", "right_wrist", 1.20),
            ("left_leg", "left_knee", "left_ankle", 1.35),
            ("right_leg", "right_knee", "right_ankle", 1.35),
        )
        distal_chain_onset = False
        for chain_name, proximal_name, distal_name, distal_threshold in distal_specs:
            supported = bool(
                not self.body_motion_guard_active
                and speed_count >= 8
                and speed_by_name.get(distal_name, 0.0) >= distal_threshold
                and speed_by_name.get(proximal_name, 0.0) >= 0.10
            )
            self.body_motion_guard_distal_runs[chain_name] = (
                self.body_motion_guard_distal_runs.get(chain_name, 0) + 1 if supported else 0
            )
            if supported:
                if self.body_motion_guard_distal_since.get(chain_name, 0.0) <= 0.0:
                    self.body_motion_guard_distal_since[chain_name] = now
                if now - self.body_motion_guard_distal_since[chain_name] >= BODY_MOTION_CHAIN_CONFIRM_S:
                    distal_chain_onset = True
            else:
                self.body_motion_guard_distal_since[chain_name] = 0.0

        # C2.6: articulation changes the distal-minus-proximal segment vector,
        # unlike rigid translation of the whole limb. Two consecutive frames
        # are required so single-frame landmark deformation cannot open the
        # transient suppressor. This complements C2.5 when wrist/ankle motion is
        # real but the absolute distal speed stays below its higher threshold.
        segment_specs = (
            ("left_arm", "left_elbow", "left_wrist", 0.80),
            ("right_arm", "right_elbow", "right_wrist", 0.80),
            ("left_leg", "left_knee", "left_ankle", 1.20),
            ("right_leg", "right_knee", "right_ankle", 1.20),
        )
        segment_articulation_onset = False
        for chain_name, proximal_name, distal_name, segment_threshold in segment_specs:
            proximal_velocity = velocity_by_name.get(proximal_name)
            distal_velocity = velocity_by_name.get(distal_name)
            segment_speed = 0.0
            if proximal_velocity is not None and distal_velocity is not None:
                segment_speed = math.hypot(
                    distal_velocity[0] - proximal_velocity[0],
                    distal_velocity[1] - proximal_velocity[1],
                )
            supported = bool(
                not self.body_motion_guard_active
                and speed_count >= 8
                and speed_by_name.get(proximal_name, 0.0) >= 0.10
                and segment_speed >= segment_threshold
            )
            self.body_motion_guard_segment_runs[chain_name] = (
                self.body_motion_guard_segment_runs.get(chain_name, 0) + 1 if supported else 0
            )
            if supported:
                if self.body_motion_guard_segment_since.get(chain_name, 0.0) <= 0.0:
                    self.body_motion_guard_segment_since[chain_name] = now
                if now - self.body_motion_guard_segment_since[chain_name] >= BODY_MOTION_CHAIN_CONFIRM_S:
                    segment_articulation_onset = True
            else:
                self.body_motion_guard_segment_since[chain_name] = 0.0
        # Early evidence is deliberately transient: it can suppress the current
        # horizontal output frame, but it does not own the persistent guard
        # lifecycle. Persistent activation remains restricted to the already
        # validated C1 raw/EMA/action evidence, preventing repeated early
        # triggers from stretching guard occupancy across a whole exercise.
        previous_early = bool(self.body_motion_guard_early_evidence)
        self.body_motion_guard_early_evidence = bool(
            early_limb_onset or vertical_body_onset or distal_chain_onset
            or segment_articulation_onset
        )
        strong_burst_ended = False
        if self.body_motion_guard_early_evidence:
            self.body_motion_guard_early_run += 1
            if not previous_early:
                self.body_motion_guard_early_started_at = now
            self.body_motion_guard_early_last_at = now
            # Bridge the estimator/output phase lag without granting early
            # evidence ownership of the persistent guard lifecycle. 67 ms is
            # time-based and therefore stable across camera frame rates.
            self.body_motion_guard_early_until = max(self.body_motion_guard_early_until, now + 0.067)
        else:
            if previous_early and self.body_motion_guard_early_started_at > 0.0:
                burst_duration = max(0.0, self.body_motion_guard_early_last_at - self.body_motion_guard_early_started_at)
                strong_burst_ended = burst_duration >= BODY_MOTION_STRONG_BURST_CONFIRM_S
            self.body_motion_guard_early_run = 0
            self.body_motion_guard_early_started_at = 0.0
            self.body_motion_guard_early_last_at = 0.0
        if (
            raw_onset
            or self.body_motion_guard_score >= 2.50
            or bool(self.body_motion_action_risk)
        ):
            self.body_motion_guard_active = True
            self.body_motion_guard_hold_until = now + 0.10
            self.body_motion_guard_settle_frames = 0
            self.body_motion_guard_settle_started_at = 0.0
        elif self.body_motion_guard_active and self.body_motion_guard_score >= 1.625:
            self.body_motion_guard_hold_until = now + 0.10
            self.body_motion_guard_settle_frames = 0
            self.body_motion_guard_settle_started_at = 0.0

        if self.body_motion_guard_active:
            # Persistent guard owns this phase; discard any transient tail so it
            # cannot survive a persistent-guard episode and fire on recovery.
            self.body_motion_guard_postburst_budget = 0
            self.body_motion_guard_postburst_until = 0.0
        elif strong_burst_ended:
            # A sustained early-evidence burst can be followed by one or two
            # delayed horizontal spikes after the ordinary 67 ms bridge. Arm a
            # tiny output-only veto budget instead of extending a blanket hold:
            # at most two non-zero frames may be suppressed within 100 ms.
            self.body_motion_guard_postburst_budget = 2
            self.body_motion_guard_postburst_until = now + 0.10

    def _guard_horizontal_output_locked(self, x: float, now: float) -> float:
        self.body_motion_guard_output_blocked = False
        self.body_motion_guard_veto_reason = ""
        x = float(x)
        if not self.body_motion_guard_enabled:
            return x
        if not self.body_motion_guard_active:
            if now <= self.body_motion_guard_early_until:
                if abs(x) > 0.01:
                    self.body_motion_guard_output_blocked = True
                    self.body_motion_guard_veto_reason = "early"
                return 0.0
            if now > self.body_motion_guard_postburst_until:
                self.body_motion_guard_postburst_budget = 0
            if self.body_motion_guard_postburst_budget > 0 and abs(x) > 0.01:
                self.body_motion_guard_postburst_budget -= 1
                self.body_motion_guard_output_blocked = True
                self.body_motion_guard_veto_reason = "postburst"
                return 0.0
            return x
        if now < self.body_motion_guard_hold_until or self.body_motion_guard_score >= 1.625:
            self.body_motion_guard_settle_frames = 0
            self.body_motion_guard_settle_started_at = 0.0
        elif abs(x) <= 0.01:
            self.body_motion_guard_settle_frames += 1
            if self.body_motion_guard_settle_started_at <= 0.0:
                self.body_motion_guard_settle_started_at = now
            if now - self.body_motion_guard_settle_started_at >= BODY_MOTION_SETTLE_S:
                self.body_motion_guard_active = False
                self.body_motion_guard_settle_frames = 0
                self.body_motion_guard_settle_started_at = 0.0
        else:
            self.body_motion_guard_settle_frames = 0
            self.body_motion_guard_settle_started_at = 0.0
        if self.body_motion_guard_active:
            if abs(x) > 0.01:
                self.body_motion_guard_output_blocked = True
                self.body_motion_guard_veto_reason = "persistent"
            return 0.0
        return x

    def _update_head_jump_anchor(self, target: dict, shoulder: dict, hip: dict, now: float) -> dict[str, float]:
        """Track a changed stance without letting a jump carry the target away.

        This measures its own coherent vertical speed rather than reading the
        body-motion guard's: the guard runs after zone evaluation, so its value
        would be one frame stale, and it returns early when the user switches
        the guard off, which would silently disable the jump zone.
        """
        torso_n = _distance(shoulder, hip)
        coherent_vy, dt = 0.0, 0.0
        if self.head_jump_prev is not None and torso_n > 1e-6:
            prev_shoulder_y, prev_hip_y, prev_at = self.head_jump_prev
            dt = now - prev_at
            if 1.0 / 90.0 <= dt <= 0.12:
                shoulder_vy = (shoulder["y"] - prev_shoulder_y) / torso_n / dt
                hip_vy = (hip["y"] - prev_hip_y) / torso_n / dt
                # Matching signs mean the torso translated as one piece.  An arm
                # raised overhead moves neither; a shrug moves them apart.
                if shoulder_vy * hip_vy > 0.0:
                    coherent_vy = min(abs(shoulder_vy), abs(hip_vy))
        self.head_jump_prev = (float(shoulder["y"]), float(hip["y"]), float(now))

        if self.head_jump_anchor is None:
            self.head_jump_anchor = {"x": float(target["x"]), "y": float(target["y"])}
            return self.head_jump_anchor
        anchor = self.head_jump_anchor
        if dt <= 0.0:
            return anchor
        step = min(dt, 0.12)
        anchor["x"] += (1.0 - math.exp(-step / HEAD_JUMP_FOLLOW_X_S)) * (float(target["x"]) - anchor["x"])
        if coherent_vy < HEAD_JUMP_FREEZE_VY:
            anchor["y"] += (1.0 - math.exp(-step / HEAD_JUMP_FOLLOW_Y_S)) * (float(target["y"]) - anchor["y"])
        if torso_n > 1e-6 and abs(float(target["y"]) - anchor["y"]) > HEAD_JUMP_SNAP_TORSO * torso_n:
            anchor["y"] = float(target["y"])
        return anchor

    def _compute_body_zones(self, pose_map: dict[str, dict], now: float) -> dict[str, dict]:
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
            # One broad region per hand, spanning the whole upper corner on its
            # side.  Edges are stated directly instead of as a center plus a
            # size: the outer and top edges belong on the image border, which a
            # centered box can only approximate.
            #
            # The bottom edge is the anti-false-trigger boundary and is the one
            # number that matters here.  It is measured up from the hips because
            # that is what predicts where a relaxed wrist hangs.  Note torso_px
            # is normalized by the image diagonal, as everywhere else in this
            # function, so the coefficient is not a torso fraction: measured on
            # an upright frame it leaves about 0.75 torso between the region and
            # a naturally hanging wrist.  The inner edge clears the head and
            # still leaves a gap to the headJump target beside it.
            hand_bottom = hip["y"] - 0.40 * torso_px / ih
            hand_inset = 0.45 * torso_px / iw
            for name, direction in (("leftHand", left_dir), ("rightHand", right_dir)):
                inner = head_center["x"] + direction * hand_inset
                outer = 1.0 if direction > 0 else 0.0
                next_rect = {
                    "x1": _clamp(min(inner, outer), 0.0, 1.0),
                    "x2": _clamp(max(inner, outer), 0.0, 1.0),
                    "y1": 0.0,
                    "y2": _clamp(hand_bottom, 0.0, 1.0),
                }
                old = self.zone_rects.get(name)
                rects[name] = self._smooth_rect(old, next_rect)

            # A small rise of the head/nose into the space above it is a
            # separate jump trigger.  The nose is the only point used, so an
            # arm passing above the head cannot fire this region by accident.
            jump_anchor = nose if nose and _score(nose) >= 0.35 else head_center
            anchor = self._update_head_jump_anchor(jump_anchor, shoulder, hip, now)
            jump_rect = _rect_at(
                anchor["x"],
                anchor["y"] - 0.30 * torso_px / ih,
                0.52 * torso_px, 0.28 * torso_px, iw, ih,
            )
            rects["headJump"] = self._smooth_rect(self.zone_rects.get("headJump"), jump_rect)

            # Provisional look-gate region for first-run UX. It intentionally
            # exists only while no fixed Scene Layout has been captured. Once
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
            # 自动脚区保留可见边界，稍减离地间隙以容纳浅侧踢。
            # 原地抬脚由下方身体相对的向外伸脚证据排除，不能仅靠离地。
            foot_w, foot_h = 1.00 * torso_px, 0.60 * torso_px
            for name, side_hip, direction in (("leftFoot", lh, left_dir), ("rightFoot", rh, right_dir)):
                next_rect = _rect_at(
                    side_hip["x"] + direction * 0.64 * torso_px / iw,
                    floor_y - 0.335 * torso_px / ih,
                    foot_w, foot_h, iw, ih,
                )
                old = self.zone_rects.get(name)
                rects[name] = self._smooth_rect(old, next_rect)
        # Keep old zone ids visible to older clients/tests, but make each one
        # refer to the exact same merged hand geometry rather than creating a
        # second trigger area.
        for alias, canonical in ZONE_ALIASES.items():
            if canonical in rects:
                rects[alias] = copy.deepcopy(rects[canonical])
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

    def _foot_in_circle(self, pose_map: dict[str, dict], points: tuple[str, ...], circle: dict | None) -> bool:
        if any(self._point_in_circle(pose_map.get(point), circle) for point in points):
            return True
        if not circle:
            return False
        # 圈位于脚踝与脚尖之间时，实际脚段已穿圈，不应漏掉。
        for endpoint in points[1:]:
            a, b = pose_map.get(points[0]), pose_map.get(endpoint)
            if not a or not b or min(_score(a), _score(b)) < .42:
                continue
            dx, dy = b["x"] - a["x"], b["y"] - a["y"]
            length2 = dx * dx + dy * dy
            if length2 <= 1e-10:
                continue
            t = _clamp(((circle["cx"] - a["x"]) * dx + (circle["cy"] - a["y"]) * dy) / length2, 0.0, 1.0)
            if self._point_in_circle({"x": a["x"] + t * dx, "y": a["y"] + t * dy, "score": min(_score(a), _score(b))}, circle):
                return True
        return False

    def _hand_points_for_mouse_locked(self) -> list[dict] | None:
        """The 21 points for whichever hand is steering, if the device sent them."""
        if not self.latest_hands:
            return None
        return self.latest_hands.get(str(self.hand_mouse_controller.config.get("hand", "right")))

    def configure_hand_mouse(self, updates: dict | None) -> dict:
        """Apply a settings change under the kernel lock and report the result."""
        with self._lock:
            status = self.hand_mouse_controller.configure(updates)
            self._save_general_settings()
            if not status["enabled"]:
                # Leaving the pointer mid-drift after a disable would keep the
                # last velocity applied until head control next writes.
                self._safe_output(self.output.apply, 0.0, 0.0)
            return status

    def _hand_mouse_owns_zone(self, name: str) -> bool:
        """True while hand steering has taken that hand away from its zones.

        lookGate is tied to the left wrist, so it belongs to the left hand here
        even though its name does not say so.
        """
        if not self.hand_mouse_controller.engaged:
            return False
        hand = str(self.hand_mouse_controller.config.get("hand", "right"))
        if name == "lookGate":
            return hand == "left"
        # Only that hand's own zones.  A bare startswith(hand) would also catch
        # leftFoot/rightFoot, and the feet are still free to act.
        return name.startswith(f"{hand}Hand")

    def _update_zones_locked(self, pose_map: dict[str, dict], now: float) -> None:
        previous_gate = bool(self.vertical_gate_active)
        self._update_foot_neutral(pose_map)
        if self.fixed_zones_enabled:
            # Fixed zones live in raw camera normalized coordinates and never
            # follow the body. Rects are generated only for legacy clients.
            self.zone_rects = {}
        else:
            self.zone_rects = self._compute_body_zones(pose_map, now)
        changed = False
        gate_available = self._gate_available()
        zone_names = list(RUNTIME_BODY_ZONES) + (["lookGate"] if gate_available else [])
        for name in zone_names:
            state = self.zone_state.setdefault(name, {"inside": 0, "outside": 0, "pressed": False})
            if name == "lookGate":
                points = ("left_wrist",)
            else:
                points = RUNTIME_BODY_ZONES[name]["points"]
            if self.fixed_zones_enabled:
                circle = self.fixed_zones.get(name)
                inside = any(self._point_in_circle(pose_map.get(point), circle) for point in points)
                if name in ("leftFoot", "rightFoot"):
                    inside = self._foot_in_circle(pose_map, points, circle)
            else:
                inside = any(self._point_in_rect(pose_map.get(point), self.zone_rects.get(name)) for point in points)
            if name in ("leftFoot", "rightFoot"):
                # 固定圈与跟随区均须先实际接触，再确认是向外伸脚。
                inside = inside and self._foot_outward(pose_map, "left" if name == "leftFoot" else "right")
            if inside and self._hand_mouse_owns_zone(name):
                # That hand is steering the pointer.  Without this it would also
                # be pressing whatever zone it flies through, so aiming would
                # mash buttons.
                inside = False
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
            RUNTIME_BODY_ZONES[name]["button"]
            for name, state in self.zone_state.items()
            if name in RUNTIME_BODY_ZONES and RUNTIME_BODY_ZONES[name].get("button") and state["pressed"]
        })

    # ---------- four existing motion rules ----------

    def _foot_relative(self, pose_map: dict[str, dict], side: str) -> tuple[float, float] | None:
        names = ("left_shoulder", "right_shoulder", "left_hip", "right_hip", "left_ankle", "right_ankle")
        if not self._points_good(pose_map, names):
            return None
        shoulder = _midpoint(pose_map["left_shoulder"], pose_map["right_shoulder"])
        hip = _midpoint(pose_map["left_hip"], pose_map["right_hip"])
        scale = abs(hip["y"] - shoulder["y"])
        if scale < .025:
            return None
        direction = 1.0 if pose_map[side + "_shoulder"]["x"] > shoulder["x"] else -1.0
        other = "right" if side == "left" else "left"
        ankle, side_hip = pose_map[side + "_ankle"], pose_map[side + "_hip"]
        lateral = direction * (ankle["x"] - side_hip["x"]) * self.width / (scale * self.height)
        rise = (pose_map[other + "_ankle"]["y"] - ankle["y"]) / scale
        return lateral, rise

    def _update_foot_neutral(self, pose_map: dict[str, dict]) -> None:
        # 双脚等高时记录站姿；抬脚期间冻结，避免目标追随侧踢。
        for side in ("left", "right"):
            relative = self._foot_relative(pose_map, side)
            if relative is not None and abs(relative[1]) < .035:
                self.foot_neutral[side] = relative[0]

    def _foot_outward(self, pose_map: dict[str, dict], side: str) -> bool:
        relative = self._foot_relative(pose_map, side)
        if relative is None:
            return False
        lateral, rise = relative
        return rise > .05 and lateral - self.foot_neutral.get(side, 0.0) > .16

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

    def _motion_has_effective_binding_locked(self, ident: str) -> bool:
        """Return whether a motion is allowed to affect gameplay right now.

        Recognition remains available for diagnostics/UI even when a motion is
        disabled. Action-derived head-guard evidence, however, follows the
        same effective mapping decision as game output so an unused detector
        cannot suppress horizontal head control.
        """
        binding = self._effective_binding_locked(f"motion.{ident}")
        if not isinstance(binding, dict):
            return False
        action = binding.get("action")
        return bool(
            isinstance(action, dict)
            and str(action.get("type", "")).strip()
            and str(action.get("target", "")).strip()
        )

    def _prune_body_motion_action_risk_locked(self) -> None:
        """Immediately forget action-risk state for motions that are not mapped."""
        enabled = {
            ident for ident in self.body_motion_action_risk_debounce
            if self._motion_has_effective_binding_locked(ident)
        }
        self.body_motion_action_risk.intersection_update(enabled)
        for ident, state in self.body_motion_action_risk_debounce.items():
            if ident not in enabled:
                state.update({"active": False, "on_since": 0.0, "off_since": 0.0})

    def _set_body_motion_action_risk_timed(
        self, ident: str, raw: bool, now: float, on_s: float, off_s: float
    ) -> bool:
        state = self.body_motion_action_risk_debounce[ident]
        if raw:
            state["off_since"] = 0.0
            if not state["active"]:
                if state["on_since"] <= 0.0:
                    state["on_since"] = now
                if now - state["on_since"] >= on_s:
                    state["active"] = True
        else:
            state["on_since"] = 0.0
            if state["active"]:
                if state["off_since"] <= 0.0:
                    state["off_since"] = now
                if now - state["off_since"] >= off_s:
                    state["active"] = False
            else:
                state["off_since"] = 0.0
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
            def march_lift(side: str, other: str) -> bool:
                # 膝、踝相对各自髋部同时升高；放低门槛后仍需持续和交替。
                knee_rise = ((pose_map[other + "_knee"]["y"] - pose_map[other + "_hip"]["y"])
                             - (pose_map[side + "_knee"]["y"] - pose_map[side + "_hip"]["y"])) / torso
                ankle_rise = ((pose_map[other + "_ankle"]["y"] - pose_map[other + "_hip"]["y"])
                              - (pose_map[side + "_ankle"]["y"] - pose_map[side + "_hip"]["y"])) / torso
                held = self.step[side + "_was"]
                lifted = knee_rise > (.035 if held else .08) and ankle_rise > (.025 if held else .065)
                lifted = lifted and not squat_raw and not calf_raw and not self._foot_outward(pose_map, side)
                since_key = side + "_since"
                if not lifted:
                    self.step.pop(since_key, None)
                    return False
                since = self.step.setdefault(since_key, now)
                return held or now - since >= .035

            left_lift = march_lift("left", "right")
            right_lift = march_lift("right", "left")
            def step_event(side: str) -> None:
                if self.step["last_side"] and side != self.step["last_side"] and 0.10 <= now - self.step["last_at"] <= 1.50:
                    self.step["active_until"] = now + 0.70
                self.step["last_side"], self.step["last_at"] = side, now
            if left_lift and not self.step["left_was"]:
                step_event("L")
            if right_lift and not self.step["right_was"]:
                step_event("R")
            self.step["left_was"], self.step["right_was"] = left_lift, right_lift
            # A jump breaks the stepping rhythm without meaning "stop walking":
            # both feet leave the ground together, so no alternation can be
            # observed and the walk would otherwise expire in mid-air.  Zones
            # are evaluated before motions, so this reads the current frame.
            # Only an already-running walk is held; a standing jump starts none.
            jumping = bool(self.zone_state.get("headJump", {}).get("pressed"))
            if jumping and now < self.step["active_until"]:
                self.step["active_until"] = now + 0.70
            if now - self.step["last_at"] > 1.55 and not jumping:
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
            self.step.clear()
            self.step.update({"left_was": False, "right_was": False, "last_side": "", "last_at": 0.0, "active_until": 0.0})

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
        raw_motion = {
            "march": march_raw,
            "calf_back": calf_raw,
            "squat": squat_raw,
            "hands_up": hands_raw,
            "jumping_jack": jumping_jack_raw,
            "side_step_jack": side_step_jack_raw,
            "cross_knee_elbow": cross_knee_elbow_raw,
        }
        risk = set()
        for ident, raw in raw_motion.items():
            # Keep detection/status independent from output configuration, but
            # only an effectively mapped action may contribute action-derived
            # evidence to the horizontal head-motion guard. Unused detectors
            # therefore cannot suppress Mouse-X / right-stick X.
            if not self._motion_has_effective_binding_locked(ident):
                self.body_motion_action_risk_debounce[ident].update(
                    {"active": False, "on_since": 0.0, "off_since": 0.0}
                )
                continue
            on_s, off_s = BODY_MOTION_ACTION_RISK_TIMING[ident]
            if self._set_body_motion_action_risk_timed(ident, raw, now, on_s, off_s):
                risk.add(ident)
        self.body_motion_action_risk = risk

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
        state = self.pose_debounce.get(ident)
        if state is None:
            # 自定义姿势是运行时才出现的，第一次见到就建一条。
            state = self.pose_debounce[ident] = {"active": False, "on": 0, "off": 0}
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

        self._update_custom_poses_locked(pose_map, now, active, confidence)
        self.pose_active = active
        self.pose_confidence = confidence

    def _update_custom_poses_locked(self, pose_map: dict[str, dict], now: float,
                                    active: set[str], confidence: dict) -> None:
        """把用户录的动作并进同一套 pose_active。

        并进来而不是另开一条通路：这样它们自动获得按游戏映射、冲突检查、绑定界面、
        紧急停止时一起松开——全部已有的行为。

        触发判定全在 store 里。早先的版本把"够不够像"放在 store、把"保持了几帧"
        放在这里的去抖，各管一半；连贯动作一来就站不住了——每一步都有自己的计时，
        还有步与步之间的超时，硬拆成两处等于让两份状态互相猜对方到哪一步。
        所以这里只保留松开方向的去抖：少数几帧的抖动不至于让键闪断。
        """
        store = self.custom_pose_store
        if store is None:
            return
        try:
            results = store.evaluate(pose_map, now)
        except Exception:  # noqa: BLE001 - 一个坏模板不该让整个识别停摆
            return
        scores: dict[str, float] = {}
        for entry in store.poses:
            ident = entry["id"]
            result = results.get(ident)
            if result is None:
                # 被禁用的：去抖归位，免得禁用瞬间那个键卡在按下状态。
                self._set_pose_debounced(ident, False)
                continue
            scores[ident] = round(float(result["score"]), 3)
            confidence[ident] = scores[ident]
            if self._set_pose_debounced(ident, bool(result["hit"]),
                                        on_frames=1, off_frames=2):
                active.add(ident)
        self.custom_pose_scores = scores

    def configure_custom_poses(self, store) -> None:
        """装上（或换掉）自定义姿势的存储。"""
        with self._lock:
            self.custom_pose_store = store
            # 所有自定义姿势的去抖状态清零，不只是被删掉的那些。阈值和停留时间
            # 定义的就是"什么算触发"，改了它们之后还沿用旧状态，等于新设置要等
            # 到下次松开才生效——用户会以为没保存。代价是改完设置要重新摆一下，
            # 那是符合预期的。
            for ident in list(self.pose_debounce):
                if ident != "hands_cross":
                    self.pose_debounce.pop(ident, None)
            self._dispatch_controls_locked(time.monotonic())

    def _effective_binding_locked(self, trigger: str) -> dict | None:
        binding = self.control_bindings.get(trigger)
        if binding is not None:
            if binding.get("disabled"):
                return None
            return binding
        prefix, _, ident = trigger.partition(".")
        if prefix == "zone" and ident in RUNTIME_BODY_ZONES and RUNTIME_BODY_ZONES[ident].get("button"):
            # Prefer the new combined profile id.  If an older profile has no
            # such entry, use its former upper/lower binding deterministically
            # so saved profiles remain usable after the spatial merge.
            for alias, canonical in ZONE_ALIASES.items():
                if canonical != ident:
                    continue
                legacy = self.control_bindings.get(f"zone.{alias}")
                if legacy is not None:
                    return None if legacy.get("disabled") else legacy
            return {"action": {"type": "gamepad", "target": RUNTIME_BODY_ZONES[ident]["button"], "behavior": "hold"}}
        if prefix == "motion":
            for item in self.motion_config:
                if item.get("id") == ident and item.get("enabled") and item.get("target"):
                    return {"action": {"type": item.get("type", "gamepad"), "target": item.get("target"), "behavior": "hold"}}
        return None

    def _dispatch_controls_locked(self, now: float) -> None:
        active = {f"zone.{name}" for name, state in self.zone_state.items() if name in RUNTIME_BODY_ZONES and state.get("pressed")}
        active.update(f"motion.{name}" for name in self.motion_active)
        active.update(f"pose.{name}" for name in self.pose_active)

        holds = []
        for trigger in sorted(active):
            binding = self._effective_binding_locked(trigger)
            if not binding:
                continue
            action = copy.deepcopy(binding.get("action", {}))
            # A pose defaults to a single edge trigger but may ask to be held,
            # exactly like a motion: the recognizer drops it the same way, so a
            # held output is released when the pose ends.  Rewriting it here
            # made the configured behavior unreachable no matter what was saved.
            behavior = str(action.get("behavior", "tap" if trigger.startswith("pose.") else "hold")).lower()
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
        self.head["horizontal_paused_by_body_motion"] = bool(self.body_motion_guard_output_blocked)
        self.head["body_motion_guard_veto_reason"] = str(self.body_motion_guard_veto_reason)
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
        # Hand steering takes both axes while the fist is closed, and hands them
        # straight back when it opens.  Blending the two would mean the pointer
        # drifts with the head while the player is trying to aim, so this is a
        # takeover rather than a sum.
        hand_mouse = self.hand_mouse_controller.status()
        if hand_mouse["engaged"]:
            x = float(hand_mouse["output_x"])
            y = float(hand_mouse["output_y"])
        # Reporting belongs in status_locked, not here: that function rebuilds
        # self.head from head_controller.status(), so anything written to the
        # dict at this point is discarded before a client ever sees it.
        if getattr(self.output, "enabled", True):
            self._safe_output(self.output.apply, x, y)

    # ---------- safety/status ----------

    def _clear_body_outputs_locked(self) -> None:
        for state in self.zone_state.values():
            state.update({"inside": 0, "outside": 0, "pressed": False})
        self.zone_rects = {}
        self.head_jump_anchor = None
        self.head_jump_prev = None
        self.vertical_gate_active = False
        self._reset_body_motion_guard_locked()
        self._reset_vertical_hand_locked()
        self.vertical_head_anchor_samples.clear()
        self._reset_vertical_head_locked()
        self.motion_active.clear()
        for state in self.motion_debounce.values():
            state.update({"active": False, "on": 0, "off": 0})
        self.body_motion_action_risk.clear()
        for state in self.body_motion_action_risk_debounce.values():
            state.update({"active": False, "on_since": 0.0, "off_since": 0.0})
        self.pose_active.clear()
        self.pose_confidence = {}
        for state in self.pose_debounce.values():
            state.update({"active": False, "on": 0, "off": 0})
        self.trigger_previous.clear()
        self.foot_neutral.clear()
        self.step.clear()
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
        self.latest_hands = None
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

    def _gate_available(self) -> bool:
        """上下视角那道闸现在存不存在。

        关掉上下视角时它一个字也不该出现。输出早就被 enabled 挡住了（见
        vertical_look.get("enabled") 那一处），但区域原来照样上报，界面就照样
        画出绿框和「左手放这里」——一个不起作用却还在指挥人的提示，比没有更糟。

        判定循环和上报状态两个地方都要这个答案，规则只写在这里一份。
        """
        if not bool(self.vertical_look.get("enabled")):
            return False
        if self.fixed_zones_enabled:
            return "lookGate" in self.fixed_zones
        return "lookGate" in self.zone_rects

    def status_locked(self, now: float) -> dict:
        pose_age = round(max(0.0, (now - self.body_last_at) * 1000.0)) if self.body_last_at else None
        gate_available = self._gate_available()
        zone_names = list(RUNTIME_BODY_ZONES) + (["lookGate"] if gate_available else [])
        zones = {}
        for name in zone_names:
            if self.fixed_zones_enabled:
                zones[name] = {"circle": copy.deepcopy(self.fixed_zones.get(name)), "pressed": bool(self.zone_state.get(name, {}).get("pressed", False))}
            else:
                zones[name] = {"rect": copy.deepcopy(self.zone_rects.get(name)), "pressed": bool(self.zone_state[name]["pressed"])}
        # Keep the old four identifiers in status for clients that have not yet
        # learned the merged names. They are aliases only; no second trigger is
        # evaluated or dispatched for them.
        for alias, canonical in ZONE_ALIASES.items():
            if canonical in zones:
                zones[alias] = copy.deepcopy(zones[canonical])
        self.head = self.head_controller.status(now)
        self.head["hand_mouse"] = self.hand_mouse_controller.status()
        source = "head" if str(self.vertical_look.get("source", "hand")).lower() == "head" else "hand"
        vertical_output = self.vertical_pitch_norm if source == "head" else self.vertical_wrist_norm
        self.head["vertical_source"] = "head_pitch" if self.vertical_look.get("enabled") and source == "head" else "right_wrist" if self.vertical_look.get("enabled") else "off"
        self.head["vertical_look_source"] = source
        self.head["verticalLookSource"] = source
        self.head["vertical_gate_active"] = bool(self.vertical_gate_active)
        horizontal_paused = bool(self.vertical_gate_active and self.vertical_look.get("exclusive_axes", False))
        self.head["horizontal_paused_by_vertical_gate"] = horizontal_paused
        if horizontal_paused or self.body_motion_guard_output_blocked:
            self.head["normalized_x"] = 0.0
            self.head["output_x"] = 0.0
        self.head["horizontal_paused_by_body_motion"] = bool(self.body_motion_guard_output_blocked)
        self.head["body_motion_guard_veto_reason"] = str(self.body_motion_guard_veto_reason)
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
        self.head["body_motion_guard_version"] = BODY_MOTION_GUARD_VERSION
        self.head["body_motion_guard_score"] = round(float(self.body_motion_guard_score), 4)
        self.head["body_motion_guard_raw"] = round(float(self.body_motion_guard_raw), 4)
        self.head["body_motion_action_risk"] = sorted(self.body_motion_action_risk)
        # Always expose the final output Y, never the diagnostic pitch value.
        self.head["normalized_y"] = round(
            float(vertical_output) if self.vertical_gate_active else 0.0, 4
        )
        self.head["output_y"] = round(
            float(vertical_output) if self.vertical_gate_active else 0.0, 3
        )
        # Last word on both axes, because that is what actually reached the
        # mouse: everything above derives from head control, which the hand
        # takes over from while the fist is closed.  Placed after the vertical
        # block rather than beside hand_mouse above, where output_y would be
        # overwritten a few lines later.
        if self.head["hand_mouse"]["engaged"]:
            self.head["output_x"] = self.head["hand_mouse"]["output_x"]
            self.head["output_y"] = self.head["hand_mouse"]["output_y"]
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
            # 自定义姿势的实时相似度。放进这份状态里，界面就复用已有的轮询，
            # 不用为它再开一路——多一路轮询就多一份和主状态不同步的机会。
            "custom_pose_scores": dict(self.custom_pose_scores),
            "pose_confidence": copy.deepcopy(self.pose_confidence),
            "control_bindings": copy.deepcopy(self.control_bindings),
            "scene_mode": "fixed" if self.fixed_zones_enabled else "body_relative_provisional",
            "vertical_look": copy.deepcopy(self.vertical_look),
            "vertical_gate_active": bool(self.vertical_gate_active),
            "body_motion_guard_enabled": bool(self.body_motion_guard_enabled),
            "body_motion_guard_active": bool(self.body_motion_guard_active),
            "body_motion_guard_version": BODY_MOTION_GUARD_VERSION,
            "body_motion_guard_score": round(float(self.body_motion_guard_score), 4),
            "body_motion_action_risk": sorted(self.body_motion_action_risk),
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
        # The phone is the usual body source, and selecting it costs nothing
        # when absent: the desktop camera is still one click away and no local
        # capture device is opened until a source is actually started.
        self.body_mode = "phone"

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
