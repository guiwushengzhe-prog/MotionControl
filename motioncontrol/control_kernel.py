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

from motioncontrol.head_control import (
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
from motioncontrol.hand_mouse_control import HANDS
from motioncontrol.axis_hand_mouse import AxisHandMouseController as HandMouseController
from motioncontrol.pose_recorder import PoseRecorder
from motioncontrol.intent_recording import (
    IntentRecordingSession, IntentRecordingStore, build_steps,
)
from motioncontrol.zone_arbiter import (
    CLOCK_MAX_LAG_S, JUMP_ZONES, TAIL_S, CaptureClock, Kinematics, SnippetBank, ZoneArbiter, ZoneInput,
    fresh_zone_state, zone_phase_for_display,
)
from motioncontrol.hold_chain import HoldChain, DEFAULT_ACTION_CHAIN
from motioncontrol.responsive_march import ResponsiveMarch
from motioncontrol.zone_fit import (
    HEAD_JUMP_HALF_H, ZONE_FIT_PREPARE_S, ZoneFitSession, body_frame, foot_bottom_y,
    foot_floor_y, foot_out, is_default, normalize_zone_fit,
)
from motioncontrol_shared import pose_library, pose_rules


def _user_intent_dir():
    """「录我的动作」录下来的东西。和游戏无关，见 intent_recording。"""
    from motioncontrol.user_paths import user_data_root

    return user_data_root() / "intent_recordings"


def _user_recordings_dir():
    """Where skeleton recordings land: beside the user's other data.

    Not in the program folder -- recordings are the user's, and since 2.0.x the
    program folder is treated as read-only so an upgrade can replace it.
    """
    from motioncontrol.user_paths import user_data_root

    return user_data_root() / "recordings"
from motioncontrol.vertical_hand_control import VerticalHandController


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
# 从云端下载的动作各自在动作文件里带着这两个数（risk_timing）。
BODY_MOTION_ACTION_RISK_TIMING = {
    "march": (0.000, 0.030),
    "calf_back": (0.060, 0.095),
}

# Head-jump anchor tuning.  The anchor exists so the target above the head can
# track a changed stance without also riding up with a jump.  Lateral drift is
# followed promptly; vertical drift is followed slowly and stops entirely above
# the freeze speed.  A crouch also shortens the shoulder-to-hip span, so while
# the span is short the target is not pulled *down* after the head: a target
# that followed a squat would sit low, and the nose would cross it on the way
# back up.  Following *up* stays allowed, since that only moves the target away
# from the nose.
#
# The upright span it compares with has to forget.  It used to be an all-time
# maximum, and leaning in to tap the phone makes the body bigger: after sitting
# back every frame looked like a crouch, and the target stayed wherever the
# lean had left it -- under the chin, which is where the phone showed it.  Now
# it forgets slowly, so a squat held for more than about ten seconds is taken as
# the new stance and standing up from it can brush the target once.
HEAD_JUMP_FREEZE_VY = 0.35
HEAD_JUMP_FOLLOW_X_S = 0.35
HEAD_JUMP_FOLLOW_Y_S = 1.50
HEAD_JUMP_CROUCH_RATIO = 0.90
HEAD_JUMP_UPRIGHT_RISE_S = 0.30
HEAD_JUMP_UPRIGHT_FORGET_S = 20.0
# A jump spans roughly 0.3-0.5 torso, so this only fires when the player truly
# relocated or the camera was re-aimed.
HEAD_JUMP_SNAP_TORSO = 1.20
# 任一膝角弯过这个角度，头顶区就不往下跟（站直时 165° 以上）。
HEAD_JUMP_KNEE_BENT = 160

# 下面这几个数都是照 2026-09-25 那批真人录像定的（手机竖屏放在人正前方）。

# 小腿向后抬起：脚踝比另一只高出 0.40 个躯干（抬到膝盖那么高），膝盖升得不到脚
# 踝的四成、也不超过 0.15。录像里小腿后抬每次 0.57~0.90、膝盖最多 0.07，踏步最多
# 0.29；提膝时膝盖会升一大截，不算。
CALF_LIFT_ANKLE_RISE = 0.40
CALF_LIFT_KNEE_SHARE = 0.40
CALF_LIFT_KNEE_MAX = 0.15
# 还要脚踝抬到离膝盖不到 0.15 个躯干。小腿后抬抬到头时脚踝高过膝盖（录像里 +0.11
# 到 -0.34），提膝时小腿垂在膝盖下面（+0.28 到 +0.41）。
CALF_LIFT_SHIN_MAX = 0.15

# 原地踏步：一只脚比另一只高出 0.07 个躯干算抬起来了，落到 0.04 以下算放下。录像
# 里小的那几步 0.07~0.09、大的到 0.29，站着晃最多 0.055。走完一步之后 0.8 秒内
# 没有下一步就停。高出多少按脚的下缘直接比（见 _update_feet_locked），不再各自
# 相对自己那边的胯量：侧踢、换重心时骨盆一歪，站着那只脚相对胯就"抬高"了，
# 真人录像里左脚往外伸，右脚被算成迈了一步。
MARCH_LIFT_START = 0.07
MARCH_LIFT_END = 0.04
MARCH_HOLD_S = 0.80

# 两只脚站着时的基准（见 _update_feet_locked）：高低差、各自往外多远。手机斜着
# 放时，站着的两只脚在画面上本来就差一截（录像里右脚高 0.05），不扣掉的话一只脚
# 的每一步都显得小。基准只在两脚着地、0.3 秒里没怎么动时往现在的样子挪：侧踢
# 前脚贴地往外滑的那一段不能挪，挪了伸脚就够不着门槛。换了站位，一秒左右跟上。
FOOT_STILL_S = 0.30
FOOT_STILL_OUT = 0.03       # 这段时间里每只脚横向最多晃多少（量身那把横向尺子）
FOOT_STILL_LIFT = 0.03      # 两脚高低差最多变多少（躯干）
FOOT_GROUNDED_LIFT = 0.035  # 离基准这么近算两脚着地
FOOT_FIRST_BASE_LIFT = 0.10 # 还没有基准时，高低差在这以内才当是站着
FOOT_FIRST_BASE_S = 1.0     # 一直没站稳过，就拿这么长一段的中位数先当基准
FOOT_BASE_FOLLOW_S = 0.8
# 脚区要"确实往外抬了脚"：下缘离地至少这么多（躯干）、往外离站着的位置至少这么
# 多（横向尺子，和量身的 out 同一把）。只贴地往外滑、站宽一点、另一只脚在动，
# 都不算。
FOOT_ZONE_LIFT = 0.04
FOOT_ZONE_OUT = 0.12

# 区域怎么算按下。smart（智能）：进框先判断是故意伸进来的还是做动作时扫过，见
# zone_arbiter。simple（进去就按）：关节进框那一帧就按、出框那一帧就松，脚不用往外
# 抬、框不给动作让路，只留一条——握拳控制鼠标的那只手，飞过哪个区域都不按。
# 以前的 guarded（防误触）是一组写死的等待时间，已经被 smart 取代，存盘里读到就当 smart。
ZONE_TRIGGER_MODES = ("smart", "simple")
DEFAULT_ZONE_TRIGGER_MODE = "smart"
LEGACY_ZONE_TRIGGER_MODES = {"guarded": "smart"}
# 会扫过这个框的动作，录的时候几次里至少有这么多次真的扫过，才算冲突。
ZONE_CONFLICT_MIN_RATE = 0.2
# 按下之后这么久之内认出了一个绑了键的动作，这一下记成「疑似误按」。多了就提示去录。
MISFIRE_WINDOW_S = 0.8
MISFIRE_MEMORY_S = 600.0
MISFIRE_HINT_COUNT = 3

# 定住的跟随框：哪几个能定、编辑时最小多大（画面宽高的比例）。
FROZEN_ZONE_IDS = ("leftHand", "rightHand", "leftFoot", "rightFoot", "headJump", "lookGate")
FROZEN_ZONE_MIN_SIZE = 0.02

# 下蹲、开合跳、提膝碰对侧肘这些动作的门槛跟着动作文件从云端下载，
# 写在 cloud/official_poses/ 里，每个数字怎么来的见那里的 README。


def _fresh_step() -> dict:
    """踏步的状态：两只脚各自正在进行的那一下抬起，和最近一步是什么时候。"""
    return {"left_lift": None, "right_lift": None, "last_side": "", "last_at": 0.0, "active_until": 0.0}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _normalize_frozen_rects(raw) -> dict[str, dict]:
    """定住的框整理成能直接用的样子：只认 FROZEN_ZONE_IDS，坐标夹进画面，太小的撑开。

    存盘读回来的、界面上拖完送过来的都走这里。坏的那一个丢掉，别的照用。
    """
    out: dict[str, dict] = {}
    if not isinstance(raw, dict):
        return out
    for name in FROZEN_ZONE_IDS:
        rect = raw.get(name)
        if not isinstance(rect, dict):
            continue
        try:
            xs = sorted(_clamp(rect[key], 0.0, 1.0) for key in ("x1", "x2"))
            ys = sorted(_clamp(rect[key], 0.0, 1.0) for key in ("y1", "y2"))
        except (KeyError, TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for value in (*xs, *ys)):
            continue
        box = {}
        for (low, high), (key_low, key_high) in ((xs, ("x1", "x2")), (ys, ("y1", "y2"))):
            if high - low < FROZEN_ZONE_MIN_SIZE:
                middle = _clamp((low + high) / 2, FROZEN_ZONE_MIN_SIZE / 2, 1 - FROZEN_ZONE_MIN_SIZE / 2)
                low, high = middle - FROZEN_ZONE_MIN_SIZE / 2, middle + FROZEN_ZONE_MIN_SIZE / 2
            box[key_low], box[key_high] = round(low, 4), round(high, 4)
        out[name] = box
    return out


def _body_anchor(pose_map) -> dict | None:
    """定住的框和人对齐用的参照：胯中点，和肩中点到胯中点的长度（画面比例）。

    定住那一刻记一份；「区域挪到我这里」时按现在的人再量一份，两份一比就知道框该
    平移多少、放大缩小多少。肩、胯看不清就是 None。
    """
    if not isinstance(pose_map, dict):
        return None
    points = [pose_map.get(name) for name in ("left_shoulder", "right_shoulder", "left_hip", "right_hip")]
    if any(not isinstance(point, dict) or _score(point) < 0.42 for point in points):
        return None
    try:
        ls, rs, lh, rh = ({"x": float(p["x"]), "y": float(p["y"])} for p in points)
    except (KeyError, TypeError, ValueError):
        return None
    hip_x, hip_y = (lh["x"] + rh["x"]) / 2, (lh["y"] + rh["y"]) / 2
    scale = math.hypot(hip_x - (ls["x"] + rs["x"]) / 2, hip_y - (ls["y"] + rs["y"]) / 2)
    if not math.isfinite(scale) or scale < 0.02:
        return None
    return {"x": round(hip_x, 4), "y": round(hip_y, 4), "scale": round(scale, 4)}


def _normalize_anchor(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    try:
        anchor = {key: float(raw[key]) for key in ("x", "y", "scale")}
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in anchor.values()) or anchor["scale"] < 0.02:
        return None
    return anchor


def _move_rects(rects: dict[str, dict], old: dict, new: dict) -> dict[str, dict]:
    """把一组定住的框从 old 那个人搬到 new 那个人身上：平移、按躯干长度缩放。

    贴着画面边的那条边（手区的外沿、上沿）还贴着画面边，不跟着挪进来。
    """
    ratio = new["scale"] / old["scale"]
    moved: dict[str, dict] = {}
    for name, rect in rects.items():
        box = {}
        for key, axis in (("x1", "x"), ("x2", "x"), ("y1", "y"), ("y2", "y")):
            value = float(rect[key])
            pinned = value <= 0.001 or value >= 0.999
            box[key] = value if pinned else new[axis] + (value - old[axis]) * ratio
        moved[name] = box
    return _normalize_frozen_rects(moved)


def legacy_scene_to_frozen(layout) -> tuple[dict[str, dict], dict, dict | None]:
    """旧版「参考场景」（scene_layout.json）换成定住的框：(框, 上下视角设置, 身体参照)。

    参考场景已经删了：它把六个圆圈钉在画面上，和「定住跟随框」是同一件事，还要拍
    参考照片、做背景匹配。记录过的人升级后不能突然变回跟着走，所以圆圈换成外接的
    方框、照样定住；文件里存着记录那一刻的骨架，拿来当身体参照，「区域挪到我这里」
    照样能用。
    """
    data = layout if isinstance(layout, dict) else {}
    rects: dict[str, dict] = {}
    for name, circle in _canonical_fixed_zones(data.get("zones")).items():
        try:
            cx, cy, radius = float(circle["cx"]), float(circle["cy"]), float(circle["r"])
        except (KeyError, TypeError, ValueError):
            continue
        rects[name] = {"x1": cx - radius, "x2": cx + radius, "y1": cy - radius, "y2": cy + radius}
    vertical = data.get("vertical_look")
    return (_normalize_frozen_rects(rects), dict(vertical) if isinstance(vertical, dict) else {},
            _body_anchor(data.get("pose")))


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


def _enclose_circles(circles: list[dict]) -> dict | None:
    """Return one circle containing old per-side circles for migration.

    只给 legacy_scene_to_frozen 用：旧版「参考场景」存的是圆圈，升级时换成定住的框。
    """
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
    """Normalize old six-zone layouts to the merged hand/head-jump schema.

    同上，只在把旧场景文件换成定住的框时用。
    """
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

    def __init__(self, output, *, watchdog_timeout: float = 0.30, persist: bool = True) -> None:
        """persist=False：不读也不写用户的设置文件。回放录的动作时临时建的那个内核
        用它（intent_library），设置由调用方一项一项装进去，怎么折腾都不碰盘。"""
        self.output = output
        self._persist = bool(persist)
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
        # 跟随区域放在哪、多大。默认是写死的那组比例，量过身就换成量出来的，
        # 存在 general_settings.json 里，见 zone_fit.py。
        self.zone_fit = normalize_zone_fit(None)
        self.zone_fit_session: ZoneFitSession | None = None
        # 区域触发方式，见 ZONE_TRIGGER_MODES。存在 general_settings.json 里。
        self.zone_trigger_mode = DEFAULT_ZONE_TRIGGER_MODE
        # 定住的跟随框：定住那一刻的框，之后不再跟着人走，可以在界面上拖。存盘，
        # 重启还是定住的。只管跟随框；记录过参考场景的固定圆圈是另一套，不受它影响。
        self.zones_frozen = False
        self.frozen_rects: dict[str, dict] = {}
        # 定住那一刻人站在哪（见 _body_anchor），「区域挪到我这里」按它搬框。
        self.frozen_anchor: dict | None = None
        # Provisional head-jump target anchor.  It deliberately does not track
        # the nose frame by frame: a jump lifts the whole body, so a fast
        # follower carries the target upward and the nose can never enter it.
        self.head_jump_anchor: dict[str, float] | None = None
        self.head_jump_prev: tuple[float, float, float] | None = None
        self.head_jump_torso_ref: float | None = None
        self.vertical_look = {
            # lookGate 和别的区域一样跟着人走，定住时一起定住。
            # 默认关。上下视角这道闸抢的是右手，而手控鼠标默认就在用右手——两个
            # 一起开着，第一屏就会弹一条"绿框白放"的提示，而第一次打开的人根本
            # 不知道那个绿框是什么。要用它的人去打开，开了会存盘。
            "enabled": False, "gate_zone_id": "lookGate", "point": "right_wrist",
            "source": "hand", "verticalLookSource": "hand",
            # Optional axis exclusivity: entering the left-hand gate may pause
            # horizontal head output while vertical view control is active.
            "exclusive_axes": False,
            "body_motion_guard": False,
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
        self.zone_state = {name: fresh_zone_state() for name in RUNTIME_BODY_ZONES}
        self.zone_state["lookGate"] = fresh_zone_state()
        self.last_zone_emit = 0.0
        # 「智能」判定：最近一小段手脚动得多快、每个框的判断状态机、录的数据。
        self.zone_kin = Kinematics()
        self.zone_arbiter = ZoneArbiter()
        # 这一帧是什么时候认出来的（电脑的钟）。手机来的帧照手机自己的时间换算，
        # 不用收到的时刻：WiFi 一抖，几帧挤在一起到，速度就算错了。只给算速度和录制
        # 用；判断等了多久、按下的时机仍然看收到的时刻。电脑自己的摄像头是 None。
        self.capture_clock = CaptureClock()
        self.pose_sample_at: float | None = None
        # 录的动作算出来的冲突：{触发名: {框: {"hits": 扫过几次, "reps": 做了几次}}}。
        # 录过的动作以它为准（哪怕一次都没扫过），没录过的才看动作文件的 passes_zones。
        # 由 intent_library 在后台算好装进来，见 configure_zone_learning。
        self.zone_conflict_rates: dict[str, dict[str, dict]] = {}
        # 每个触发最近一次「正在做」是什么时候，框用它判断动作是不是刚做完。
        self.trigger_busy_at: dict[str, float] = {}
        # 疑似误按：[(时刻, 框, 触发名)]。某个没录过的动作攒够了就提示去录。
        self.zone_misfires: deque = deque(maxlen=64)
        # 「录我的动作」：正在录的这一轮，存盘的地方，录过哪几项（存盘后刷新，不每帧读盘）。
        self.intent_session: IntentRecordingSession | None = None
        self.intent_store = IntentRecordingStore(_user_intent_dir())
        self.intent_recorded: set[str] = self.intent_store.recorded_keys() if persist else set()
        self.intent_saving = False
        self.intent_last_error = ""
        # 后台对着现在的框算录的东西（intent_library.ZoneLearner）：算到哪了、体检报告。
        self.zone_learning: dict = {"state": "idle", "error": "", "report": None, "took_s": None, "snippets": 0}
        # 录完存好之后叫一声（server 装上，拿去重新算冲突和体检）。在存盘线程里调。
        self._intent_listener = None

        self.motion_config: list[dict] = []
        self.motion_active: set[str] = set()
        self.motion_debounce = {
            key: {"active": False, "on": 0, "off": 0} for key in ("march", "calf_back")
        }
        self.body_motion_action_risk: set[str] = set()
        self.body_motion_action_risk_debounce = {
            key: {"active": False, "on_since": 0.0, "off_since": 0.0} for key in ("march", "calf_back")
        }
        # 从云端下载的动作：编号 → 动作文件（含识别规则）。原地踏步、小腿向后抬起是
        # 写在下面的代码，不在这里。见 configure_pose_actions。
        self.pose_actions: dict[str, dict] = {}
        self._motion_rules: dict[str, dict] = {}
        self._motion_rule_order: list[str] = []
        self._pose_rules: dict[str, dict] = {}
        self._pose_rule_order: list[str] = []
        self.step = _fresh_step()
        self.march_algorithm = "legacy"
        self._responsive_march = ResponsiveMarch()
        # 脚：站着时的基准、最近 0.3 秒的样子、这一帧算出来的离地和往外。见 _update_feet_locked。
        self.foot_base: dict[str, float] | None = None
        self.foot_history: list[tuple[float, float, float, float]] = []
        self.feet: dict[str, dict[str, float]] | None = None
        self.last_motion_emit = 0.0

        # Head estimation keeps observing frames, but strong exercise motion
        # must not move the in-game camera. This guard uses body-normalized
        # limb velocity because action labels can be intermittent or absent.
        self.body_motion_guard_enabled = False
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
        # 下载的姿势（双手交叉……）装上时建条目，自己录的姿势第一次见到时建。
        self.pose_debounce: dict[str, dict] = {}
        # 默认装上动作库里已经登记的（电脑端启动时登记本机下载过的）。
        self._install_pose_actions_locked(pose_library.registered().values())
        # 用户自己录的姿势。id 是运行时才知道的，所以去抖条目按需建。
        # 存取在 custom_poses.CustomPoseStore 里，由 server.py 装进来——内核不碰
        # 文件，这样测试里可以直接塞一个假的。
        self.custom_pose_store = None
        self.custom_pose_scores: dict[str, float] = {}
        # 上一帧各个动作的原始判定（去抖之前）。圈在动作之前算，要让圈的时候看的
        # 是它——见 _zone_yield_locked。
        self.motion_raw: dict[str, bool] = {}
        # 用户自己建的键盘宏。和上面一样，文件不归内核管，由 server.py 装进来。
        self.macro_store = None
        # 最近触发过什么。做一个动作、摆一个姿势、说一句口令，到底有没有生效、按的
        # 是哪个键——这些都是"发生一下就没了"的事，靠轮询状态根本看不见：区域按下
        # 十几毫秒就松开，姿势是边沿触发，语音更是说完就完。没有这份记录，人只能
        # 反复做动作然后盯着游戏猜。
        #
        # 只留最近这些条，按时间顺序。它是给人看的，不是日志。
        self.recent_triggers: deque = deque(maxlen=24)
        # 触发集合一变就通知一次（不是每帧）。手机靠它显示"刚才按了什么"——打游戏
        # 时人看不到电脑屏幕，只看得见手机。
        #
        # 回调必须是"放下就走"的：它在控制线程、而且在锁里被调用，里面做任何可能
        # 阻塞的事（比如往 socket 写）都会卡住识别，表现出来是掉帧。
        self._trigger_listener = None
        # 映射表里绑的「系统功能」里不归内核管的那些（开始/停止输出、视角回正）交给
        # 它，server 装上。在单独的线程里调，不占控制线程、不在锁里。
        self._system_action_handler = None

        # Head control is intentionally isolated from body actions.  The clean
        # engine owns its estimator, center capture, filtering and compact
        # profile.  Legacy five-stage/head-face state is no longer part of the
        # runtime path.
        self.head_controller = HeadController(self._head_profile_path())
        # Experimental action chains are opt-in.  With no user setting the
        # legacy headJump binding remains the only behavior.
        self.action_chain = HoldChain(DEFAULT_ACTION_CHAIN)
        self._general_raw: dict = {}
        if self._persist:
            self._load_general_settings()
            self._migrate_scene_layout_locked()
        # 新玩家使用侧倾左右配左手上下；初次校准只保存头控档案时，重启仍保留该组合。
        if not isinstance(self._general_raw.get("hand_mouse"), dict) and (
            not self._head_profile_path().exists() or self.head_controller.config["horizontal_algorithm"] == "roll_tilt"
        ):
            self.hand_mouse_controller.configure({"horizontal_hand": "off", "vertical_hand": "left"})
        self.action_chain_result = self.action_chain.result()
        self.head = self.head_controller.status(time.monotonic())
        self.sensor_sources: dict[str, dict] = {}
        self._thread.start()

    def _user_file(self, key: str) -> Path:
        """用户数据文件的位置，走 user_paths 而不是自己拼。

        自己读 LOCALAPPDATA 会绕过 MOTIONCONTROL_USER_DIR，而那正是测试用来
        避开开发者真实数据的开关——绕过它，跑一次测试就可能覆盖掉你自己的
        头控校准或者设置。文件名也只在 motioncontrol_shared 里写一次。
        """
        from motioncontrol.user_paths import user_path

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
        # 摄像头来源和第几个摄像头也存在这份文件里，但它们不属于内核——存它们
        # 的是 LocalControlRuntime 和 NativeCameraService。原样留着，别的地方
        # 通过 remember_general_setting 存的东西才不会被下一次写盘抹掉。
        self._general_raw = dict(data)
        if data.get("march_algorithm") in {"legacy", "responsive"}:
            self.march_algorithm = data["march_algorithm"]
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
            # 这几项原来只存在参考场景文件里，没记录过场景的人每次重启都回到默认。
            self._apply_vertical_extras_locked(vertical)

        # Invalid or absent action-chain settings safely retain the disabled
        # default; user data is never written into the program directory.
        self.action_chain.configure(data.get("action_chain", DEFAULT_ACTION_CHAIN))
        # 量过身的区域。坏了、缺了都回到默认大小，不影响启动。
        self.zone_fit = normalize_zone_fit(data.get("zone_fit"))
        mode = str(data.get("zone_trigger_mode", "")).strip().lower()
        mode = LEGACY_ZONE_TRIGGER_MODES.get(mode, mode)
        if mode in ZONE_TRIGGER_MODES:
            self.zone_trigger_mode = mode
        frozen = data.get("zone_freeze")
        if isinstance(frozen, dict):
            rects = _normalize_frozen_rects(frozen.get("rects"))
            self.frozen_rects = rects
            # 存着"定住"却一个框都没有（文件被改坏了），当没定住：定住一堆空框等于
            # 区域全部失灵，而界面上看不出为什么。
            self.zones_frozen = bool(frozen.get("frozen")) and bool(rects)
            self.frozen_anchor = _normalize_anchor(frozen.get("anchor"))
            if self.zones_frozen:
                # 人还没进画面，框就已经在那儿了。
                self.zone_rects = self._frozen_zone_rects_locked()

    def _apply_vertical_extras_locked(self, vertical: dict) -> None:
        """上下视角里除了开关、来源以外的那几项：暂停左右、身体动作保护、范围、死区。"""
        if "exclusive_axes" in vertical:
            self.vertical_look["exclusive_axes"] = bool(vertical["exclusive_axes"])
        if "body_motion_guard" in vertical:
            self.body_motion_guard_enabled = bool(vertical["body_motion_guard"])
            self.vertical_look["body_motion_guard"] = self.body_motion_guard_enabled
        for key, low, high in (("range_y", 0.06, 0.40), ("deadzone", 0.0, 0.35)):
            if key in vertical:
                try:
                    self.vertical_look[key] = _clamp(float(vertical[key]), low, high)
                except (TypeError, ValueError):
                    pass

    def _migrate_scene_layout_locked(self) -> None:
        """记录过旧版「参考场景」的人：固定圆圈换成定住的框，只做一次。

        参考场景已经删了（见 legacy_scene_to_frozen）。不迁移的话，这些人升级后区域
        会悄悄变回跟着人走。已经有定住设置的、迁移过的都不动；参考照片不在了的，
        旧版本身也不会加载那份布局，这里同样跳过。旧文件原样留着，不删用户的东西。
        """
        raw = getattr(self, "_general_raw", {})
        if raw.get("scene_layout_migrated") or "zone_freeze" in raw:
            return
        layout_path, photo_path = self._user_file("scene_layout"), self._user_file("scene_reference")
        try:
            if not photo_path.is_file():
                return
            layout = json.loads(layout_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        rects, vertical, anchor = legacy_scene_to_frozen(layout)
        self._general_raw["scene_layout_migrated"] = True
        if rects:
            self.frozen_rects, self.frozen_anchor, self.zones_frozen = rects, anchor, True
            self.zone_rects = self._frozen_zone_rects_locked()
        if vertical:
            self._apply_vertical_extras_locked(vertical)
        self._save_general_settings()

    def general_setting(self, key: str, default=None):
        """读一项不归内核管、但和它存在同一份文件里的设置。

        摄像头来源、用第几个摄像头，都是"重启之后必须还在"的东西，和手控鼠标
        是同一类；但它们归 LocalControlRuntime 和 NativeCameraService 管。与其
        再开一份文件、再写一遍"坏了不要崩"的读盘代码，不如共用这一份。
        """
        return getattr(self, "_general_raw", {}).get(key, default)

    def remember_general_setting(self, key: str, value) -> None:
        if not hasattr(self, "_general_raw"):
            self._general_raw = {}
        self._general_raw[key] = value
        self._save_general_settings()

    def _save_general_settings(self) -> None:
        """写盘。失败不抛：存不下设置也不该打断正在进行的游戏。"""
        if not self._persist:
            return
        path = self._general_settings_path()
        payload = {
            **getattr(self, "_general_raw", {}),
            "saved_at_unix": time.time(),
            "hand_mouse": dict(self.hand_mouse_controller.config),
            "vertical_look": {
                "enabled": bool(self.vertical_look.get("enabled", True)),
                "source": str(self.vertical_look.get("source", "hand")),
                "exclusive_axes": bool(self.vertical_look.get("exclusive_axes", False)),
                "body_motion_guard": bool(self.body_motion_guard_enabled),
                "range_y": float(self.vertical_look.get("range_y", 0.18)),
                "deadzone": float(self.vertical_look.get("deadzone", 0.10)),
            },
            "action_chain": self.action_chain.config,
            "zone_trigger_mode": self.zone_trigger_mode,
            "march_algorithm": self.march_algorithm,
            "zone_freeze": {"frozen": bool(self.zones_frozen),
                            "rects": copy.deepcopy(self.frozen_rects),
                            "anchor": copy.deepcopy(self.frozen_anchor)},
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

    def _apply_macro_behavior_locked(self, bindings: dict) -> dict:
        """让指向宏的绑定，"跑一遍还是循环"跟着那条宏自己的设定走。

        这件事是宏的属性，不是每条绑定各选一次的东西——用户在宏库里选一次，所有用
        到它的地方都照办。绑定里存着的那个 behavior 只是一份副本，宏改了它就旧了，
        所以每次装配置都按宏库重算一遍。不重算的话，界面上写着"循环"而实际只跑一
        遍，那种不一致查起来最费劲。
        """
        store = self.macro_store
        if store is None:
            return bindings
        for binding in bindings.values():
            action = binding.get("action")
            if not isinstance(action, dict) or action.get("type") != "macro":
                continue
            # 语音的"松开"是一条停止指令，和宏本身循环不循环无关，不能被改掉。
            if action.get("behavior") == "release":
                continue
            try:
                action["behavior"] = "hold" if store.repeats(action.get("target", "")) else "tap"
            except Exception:  # noqa: BLE001 - 宏库出问题时保持原样，不影响别的绑定
                continue
        return bindings

    def configure_macros(self, store) -> None:
        """装上（或换掉）键盘宏库。"""
        with self._lock:
            self.macro_store = store
            setter = getattr(self.output, "configure_macros", None)
            if setter is not None:
                self._safe_output(setter, store)
            self.control_bindings = self._apply_macro_behavior_locked(self.control_bindings)
            self._dispatch_controls_locked(time.monotonic())

    def configure_bindings(self, bindings: dict | None) -> None:
        """Install one effective Game Profile without touching recognition thresholds."""
        with self._lock:
            self.control_bindings = self._apply_macro_behavior_locked(flatten_bindings(bindings))
            self.trigger_previous.clear()
            self.action_chain.reset()
            self.action_chain_result = self.action_chain.result()
            # A profile switch can disable a motion while its guard-risk debounce
            # is still active. Drop only now-unmapped action-derived evidence;
            # raw/EMA body-motion evidence continues to own the safety guard.
            self._prune_body_motion_action_risk_locked()
            # Release any output contributed by the previous profile immediately.
            setter = getattr(self.output, "set_action_holds", None)
            if setter is not None:
                self._safe_output(setter, [], source_group="controls")

    def configure_action_chain(self, config: dict | None) -> dict:
        """Apply and persist the opt-in declarative action-chain experiment."""
        with self._lock:
            result = self.action_chain.configure(config)
            self.action_chain_result = self.action_chain.result()
            self._save_general_settings()
            # Re-dispatch immediately so a running chain cannot leave a stale
            # hold after configuration changes.
            self._dispatch_controls_locked(time.monotonic())
            return self.status_locked(time.monotonic())

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
            if vertical_exclusive is not None or body_motion_guard is not None:
                # 原来只存进参考场景文件，没记录过场景的人重启就丢。
                self._save_general_settings()
            self.head = self.head_controller.status(time.monotonic())
            return self.status_locked(time.monotonic())

    def configure_vertical_look(self, updates: dict | None) -> dict:
        """上下视角的设置：开关、右手还是头部、暂停左右、身体动作保护、范围、死区。

        原来这几项跟着参考场景存，参考场景删了以后都在通用设置里。只改送来的那几项。
        """
        updates = updates if isinstance(updates, dict) else {}
        with self._lock:
            if "enabled" in updates:
                self.vertical_look["enabled"] = bool(updates["enabled"])
            raw_source = str(updates.get("source", updates.get("verticalLookSource", ""))).strip().lower()
            if raw_source in {"hand", "head", "头部", "右手", "right_wrist"}:
                source = "head" if raw_source in {"head", "头部"} else "hand"
                self.vertical_look["source"] = source
                self.vertical_look["verticalLookSource"] = source
            self._apply_vertical_extras_locked(updates)
            if not self.body_motion_guard_enabled:
                self._reset_body_motion_guard_locked()
            self.vertical_gate_active = False
            self._reset_vertical_hand_locked()
            self.vertical_head_anchor_samples.clear()
            self._reset_vertical_head_locked()
            self._save_general_settings()
            return self.status_locked(time.monotonic())

    def handle_pose_message(self, source_id: str, message: dict) -> dict:
        pose_map = self.pose_map_from_message(message)
        world_pose = self.world_pose_map_from_message(message)
        width = int(message.get("width") or 640)
        height = int(message.get("height") or 480)
        captured = message.get("captured_at_ms")
        return self.handle_pose_map(
            source_id, pose_map, width=width, height=height,
            world_pose=world_pose, hands=self.hand_map_from_message(message),
            captured_at_ms=captured if isinstance(captured, (int, float)) and not isinstance(captured, bool) else None,
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
        captured_at_ms: float | None = None,
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
                self.capture_clock.reset()
            if captured_at_ms is not None and math.isfinite(float(captured_at_ms)):
                self.pose_sample_at = self.capture_clock.map(float(captured_at_ms) / 1000.0, now)
            else:
                self.pose_sample_at = None
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

    # ---------- 录我的动作 ----------

    def _intent_actions_locked(self) -> list[tuple[str, str]]:
        """这台电脑上有的全部动作（不管这个游戏绑没绑）：录的东西和游戏无关。"""
        out = []
        for ident in ("march", "calf_back", *self._motion_rules):
            entry = pose_library.get(ident)
            out.append((f"motion.{ident}", entry["name"] if entry else ident))
        for ident in self._pose_rules:
            entry = pose_library.get(ident)
            out.append((f"pose.{ident}", entry["name"] if entry else ident))
        store = self.custom_pose_store
        for entry in getattr(store, "poses", ()) if store is not None else ():
            if isinstance(entry, dict) and entry.get("id"):
                out.append((f"pose.{entry['id']}", str(entry.get("name") or entry["id"])))
        return list(dict.fromkeys(out))

    def intent_items_locked(self) -> dict:
        """要录的全部项目，和其中还没录过的。界面靠 missing 提示「新动作还没录」。"""
        steps = build_steps(self._intent_actions_locked())
        return {
            "all": [{"key": step["key"], "kind": step["kind"], "name": step["name"]} for step in steps],
            "recorded": sorted(self.intent_recorded),
            "missing": [step["key"] for step in steps if step["key"] not in self.intent_recorded],
        }

    def start_intent_recording(self, keys=None) -> dict:
        """开始录。keys 给了就只录这几项（补录新动作、重录某一项）。"""
        with self._lock:
            now = time.monotonic()
            if self.intent_saving:
                raise ValueError("上一轮还在存，等一下再录")
            steps = build_steps(self._intent_actions_locked(), keys)
            if not steps:
                raise ValueError("没有要录的项目")
            self.intent_session = IntentRecordingSession(steps, now)
            self.intent_last_error = ""
            return self.status_locked(now)

    def skip_intent_step(self) -> dict:
        with self._lock:
            now = time.monotonic()
            if self.intent_session is not None:
                self.intent_session.skip(now)
                self._finish_intent_if_done_locked()
            return self.status_locked(now)

    def cancel_intent_recording(self) -> dict:
        with self._lock:
            if self.intent_session is not None:
                self.intent_session.cancel()
            return self.status_locked(time.monotonic())

    def forget_intent_items(self, keys) -> dict:
        """删掉这几项录的东西（比如删了一个自定义动作）。"""
        with self._lock:
            self.intent_store.forget(keys)
            self.intent_recorded = self.intent_store.recorded_keys()
            listener = self._intent_listener
        if listener is not None:
            listener()
        return self.status()

    def configure_intent_listener(self, listener) -> None:
        with self._lock:
            self._intent_listener = listener

    def _pose_sample_time_locked(self, now: float) -> float:
        """这一帧算速度用的时刻：手机来的用换算好的认出时刻，别的就是现在。"""
        sample = self.pose_sample_at
        if sample is None or not now - CLOCK_MAX_LAG_S <= sample <= now:
            return now
        return sample

    def _update_intent_recording_locked(self, pose_map, now: float) -> None:
        session = self.intent_session
        if session is None or not session.active:
            return
        zone_inside = {name: bool(self.zone_state.get(name, {}).get("raw_inside")) for name in RUNTIME_BODY_ZONES}
        active = {f"motion.{ident}" for ident in self.motion_active} | {f"pose.{ident}" for ident in self.pose_active}
        session.update(now, pose_map, self.width, self.height, zone_inside, active,
                       sample_at=self._pose_sample_time_locked(now))
        self._finish_intent_if_done_locked()

    def _finish_intent_if_done_locked(self) -> None:
        session = self.intent_session
        if session is None or session.state != "done" or self.intent_saving:
            return
        self.intent_saving = True

        def save() -> None:
            error = ""
            try:
                self.intent_store.save(session)
            except OSError as exc:
                error = f"保存失败：{exc}"
            with self._lock:
                self.intent_saving = False
                self.intent_last_error = error
                self.intent_recorded = self.intent_store.recorded_keys()
                session.frames = []  # 存完就不占内存了
                listener = self._intent_listener
            if listener is not None and not error:
                try:
                    listener()
                except Exception as exc:  # noqa: BLE001 - 算不出来不该拖垮录制
                    self.last_error = str(exc)

        threading.Thread(target=save, name="intent-save", daemon=True).start()

    def _intent_status_locked(self, now: float) -> dict:
        session = self.intent_session
        status = session.status(now) if session is not None else {"active": False, "state": "idle", "steps": []}
        status["saving"] = bool(self.intent_saving)
        status["error"] = self.intent_last_error
        return status

    # ---------- 量身定区域 ----------

    def start_zone_fit(self, *, body: bool = True) -> dict:
        """开始量身。body=False 只量握拳。

        握拳那几步只给现在开着握拳控制的手：手机只给在用的手跑手指识别，另一只手
        量不到手指关节。两只手共用一对阈值，同一个人两只手读数差不多，换手不用重量。
        """
        with self._lock:
            now = time.monotonic()
            tracking = self.hand_mouse_controller.tracking_request()
            grip_hands = tuple(tracking["hands"]) if tracking["enabled"] else ()
            self.zone_fit_session = ZoneFitSession(
                self.zone_fit, now, grip_hands=grip_hands, body=body,
                prepare_s=ZONE_FIT_PREPARE_S,
            )
            return self.status_locked(now)

    def skip_zone_fit_phase(self) -> dict:
        with self._lock:
            now = time.monotonic()
            if self.zone_fit_session is not None:
                self.zone_fit_session.skip(now)
                if self.zone_fit_session.state == "done":
                    self._apply_zone_fit_locked()
            return self.status_locked(now)

    def cancel_zone_fit(self) -> dict:
        """中途不量了：什么都不改。"""
        with self._lock:
            if self.zone_fit_session is not None:
                self.zone_fit_session.cancel()
            return self.status_locked(time.monotonic())

    def reset_zone_fit(self) -> dict:
        """区域回到默认大小。握拳阈值不动：它在设置里有自己的滑块。"""
        with self._lock:
            if self.zone_fit_session is not None:
                self.zone_fit_session.cancel()
            grip_at = self.zone_fit.get("grip_measured_at_unix")
            self.zone_fit = normalize_zone_fit(None)
            self.zone_fit["grip_measured_at_unix"] = grip_at
            self._general_raw["zone_fit"] = self.zone_fit
            self._save_general_settings()
            return self.status_locked(time.monotonic())

    def _apply_zone_fit_locked(self) -> None:
        """量完了：新区域和握拳阈值装上、存盘。量到几项就换几项。"""
        session = self.zone_fit_session
        fit = session.result()
        if session.values:
            fit["measured_at_unix"] = round(time.time(), 3)
        self.zone_fit = normalize_zone_fit(fit)
        self._general_raw["zone_fit"] = self.zone_fit
        grip = session.grip_updates()
        if grip:
            try:
                self.hand_mouse_controller.configure(grip)
                self.zone_fit["grip_measured_at_unix"] = round(time.time(), 3)
            except ValueError:
                grip = {}
        session.applied_grip = grip
        self._save_general_settings()

    def _zone_fit_status_locked(self, now: float | None = None) -> dict:
        session = self.zone_fit_session
        state = session.status(now=now) if session is not None else {
            "active": False, "state": "idle", "preparing": False, "remaining_s": 0.0,
            "phase": "idle", "phase_index": 0, "phases": [],
            "issue": "", "hands_reached": {}, "measured": [], "skipped": [],
        }
        state["grip_applied"] = sorted(session.applied_grip) if session is not None else []
        state["custom"] = not is_default(self.zone_fit)
        state["measured_at_unix"] = self.zone_fit.get("measured_at_unix")
        state["grip_measured_at_unix"] = self.zone_fit.get("grip_measured_at_unix")
        state["zones"] = copy.deepcopy(self.zone_fit["zones"])
        return state

    # ---------- 区域触发方式、定住跟随框 ----------

    def configure_zone_trigger_mode(self, mode: str) -> dict:
        mode = str(mode or "").strip().lower()
        mode = LEGACY_ZONE_TRIGGER_MODES.get(mode, mode)
        if mode not in ZONE_TRIGGER_MODES:
            raise ValueError("区域触发方式只能是「智能」或「进去就按」")
        with self._lock:
            if mode != self.zone_trigger_mode:
                self.zone_trigger_mode = mode
                # 换方式时各区域的判断状态清零，免得带着上一种方式的半截状态。按着
                # 的键保留：人手还在框里，换个方式不该让键闪断。
                for state in self.zone_state.values():
                    pressed = state["pressed"]
                    state.update(fresh_zone_state())
                    if pressed:
                        state.update(pressed=True, phase="pressed", inside=1)
                self._save_general_settings()
            return self.status_locked(time.monotonic())

    def configure_march_algorithm(self, algorithm: str) -> dict:
        if algorithm not in {"legacy", "responsive"}:
            raise ValueError("请选择旧版踏步或新版灵敏踏步")
        with self._lock:
            self.march_algorithm = algorithm
            self.step = _fresh_step()
            self._responsive_march.reset()
            self.motion_active.discard("march")
            self.motion_raw["march"] = False
            self.motion_debounce["march"].update(active=False, on=0, off=0)
            self._save_general_settings()
            now = time.monotonic()
            self._dispatch_controls_locked(now)
            return self.status_locked(now)

    def _frozen_zone_rects_locked(self) -> dict[str, dict]:
        rects = copy.deepcopy(self.frozen_rects)
        for alias, canonical in ZONE_ALIASES.items():
            if canonical in rects:
                rects[alias] = copy.deepcopy(rects[canonical])
        return rects

    def _freeze_zones_locked(self) -> dict:
        """把跟随框定在现在的位置。已经定住了就什么都不改——之后拖过的不能被冲掉。"""
        if self.zones_frozen:
            return {"executed": True, "frozen": True}
        rects = _normalize_frozen_rects(self.zone_rects)
        if not rects:
            return {"executed": False, "reason": "还没看到人，没有框可以定住：先让头和双肩入镜"}
        self.frozen_rects = rects
        self.frozen_anchor = _body_anchor(self.latest_pose)
        self.zones_frozen = True
        self._save_general_settings()
        return {"executed": True, "frozen": True, "zones": sorted(rects)}

    def _unfreeze_zones_locked(self) -> dict:
        if self.zones_frozen or self.frozen_rects:
            self.zones_frozen = False
            self.frozen_rects = {}
            self.frozen_anchor = None
            self._save_general_settings()
        return {"executed": True, "frozen": False}

    def _move_zones_here_locked(self) -> dict:
        """「区域挪到我这里」：不用鼠标就能挪区域。

        还跟着人走的时候，就是在现在的位置定住。已经定住的，整组框按人现在站的位置
        平移、按远近缩放，拖过的大小和相对位置都保留——摄像头碰歪了、人换了站位，
        站好说一声就对上了。
        """
        if not self.zones_frozen:
            return self._freeze_zones_locked()
        here = _body_anchor(self.latest_pose)
        if here is None:
            return {"executed": False, "reason": "没看清你站在哪：让头、双肩和胯都入镜再试"}
        if self.frozen_anchor is None:
            # 定住那一刻没看清人（只露了半身）：没有参照可比，只好从现在的位置重新定。
            self.zones_frozen, self.frozen_rects = False, {}
            self.zone_rects = {}
            return {"executed": False, "reason": "定住时没看清你站在哪，已恢复跟随；站好后再说一次就定在这里"}
        self.frozen_rects = _move_rects(self.frozen_rects, self.frozen_anchor, here)
        self.frozen_anchor = here
        self.zone_rects = self._frozen_zone_rects_locked()
        self._save_general_settings()
        return {"executed": True, "frozen": True, "moved": True}

    def _toggle_zone_freeze_locked(self) -> dict:
        return self._unfreeze_zones_locked() if self.zones_frozen else self._freeze_zones_locked()

    def freeze_zones(self, frozen: bool = True) -> dict:
        with self._lock:
            result = self._freeze_zones_locked() if frozen else self._unfreeze_zones_locked()
            return {**result, "status": self.status_locked(time.monotonic())}

    def move_zones_here(self) -> dict:
        with self._lock:
            result = self._move_zones_here_locked()
            return {**result, "status": self.status_locked(time.monotonic())}

    def set_frozen_zones(self, rects, anchor=None) -> dict:
        """直接换一组定住的框（旧场景迁移、测试用）。一个能用的框都没有就是恢复跟随。"""
        with self._lock:
            normalized = _normalize_frozen_rects(rects)
            if not normalized:
                self._unfreeze_zones_locked()
            else:
                self.frozen_rects, self.zones_frozen = normalized, True
                self.frozen_anchor = _normalize_anchor(anchor)
                self.zone_rects = self._frozen_zone_rects_locked()
                self._save_general_settings()
            return self.status_locked(time.monotonic())

    def update_frozen_zones(self, rects) -> dict:
        """界面上拖完的框。只改送来的那几个，别的不动。"""
        if not isinstance(rects, dict):
            raise ValueError("rects 必须是对象")
        with self._lock:
            if not self.zones_frozen:
                raise ValueError("区域没有定住：先定住，再拖")
            merged = {**self.frozen_rects, **_normalize_frozen_rects(rects)}
            self.frozen_rects = _normalize_frozen_rects(merged)
            self.zone_rects = self._frozen_zone_rects_locked()
            self._save_general_settings()
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
            self._update_intent_recording_locked(None, now)
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
                       "hand": hand["hand"], "hands": hand["hands"],
                       "axes": hand["axes"]},
            )
        if self.zone_fit_session is not None and self.zone_fit_session.active:
            grips = {hand: {"curl": controller.curl, "spread": controller.spread}
                     for hand, controller in self.hand_mouse_controller.hands.items()}
            self.zone_fit_session.update(pose_map, self.width, self.height, now, grips)
            if self.zone_fit_session.state == "done":
                self._apply_zone_fit_locked()
        self._update_zones_locked(pose_map, now)
        self._update_motion_locked(pose_map, now)
        self._update_cross_poses_locked(pose_map, now)
        self._update_intent_recording_locked(pose_map, now)
        self._update_action_chain_locked(now)
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

    def _update_head_jump_anchor(self, target: dict, shoulder: dict, hip: dict, now: float,
                                 knees_bent: bool = False) -> dict[str, float]:
        """Track a changed stance without letting a jump carry the target away.

        This measures its own coherent vertical speed rather than reading the
        body-motion guard's: the guard runs after zone evaluation, so its value
        would be one frame stale, and it returns early when the user switches
        the guard off, which would silently disable the jump zone.

        ``knees_bent``：膝盖弯着的时候也当成蹲着，头顶区不往下跟。光看"躯干变短"
        不够——正对镜头下蹲时躯干在画面上几乎不变短，头顶区就跟着人往下走，站起
        来的那一下鼻子穿过它，等于按了一下跳（真人录像里按住了 0.6~0.9 秒）。
        """
        torso_n = _distance(shoulder, hip)
        if self.head_jump_torso_ref is None:
            self.head_jump_torso_ref = torso_n
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
        # The upright span rises within a fraction of a second, so a camera or
        # stance change that makes the body larger is accepted, but a single
        # stray hip estimate is not.  It falls slowly, so a held squat keeps
        # being recognised while backing away from the camera does not read as
        # a crouch for good.
        ref = self.head_jump_torso_ref
        settle_s = HEAD_JUMP_UPRIGHT_RISE_S if torso_n > ref else HEAD_JUMP_UPRIGHT_FORGET_S
        ref += (1.0 - math.exp(-step / settle_s)) * (torso_n - ref)
        self.head_jump_torso_ref = ref
        crouched = knees_bent or (ref > 1e-6 and torso_n < HEAD_JUMP_CROUCH_RATIO * ref)
        anchor["x"] += (1.0 - math.exp(-step / HEAD_JUMP_FOLLOW_X_S)) * (float(target["x"]) - anchor["x"])
        rising = float(target["y"]) < anchor["y"]
        if coherent_vy < HEAD_JUMP_FREEZE_VY and (rising or not crouched):
            anchor["y"] += (1.0 - math.exp(-step / HEAD_JUMP_FOLLOW_Y_S)) * (float(target["y"]) - anchor["y"])
        if torso_n > 1e-6 and abs(float(target["y"]) - anchor["y"]) > HEAD_JUMP_SNAP_TORSO * torso_n:
            anchor["y"] = float(target["y"])
        return anchor

    def _compute_body_zones(self, pose_map: dict[str, dict], now: float) -> dict[str, dict]:
        # 参考系（胯、肩、头中心、左右朝向、尺子）和量身用的是同一份，见 zone_fit.py。
        frame = body_frame(pose_map, self.width, self.height)
        if frame is None:
            return {}
        iw, ih = self.width, self.height
        lh, rh, nose = pose_map.get("left_hip"), pose_map.get("right_hip"), pose_map.get("nose")
        shoulder, hip, torso_px = frame["shoulder"], frame["hip"], frame["torso_px"]
        left_dir, right_dir = frame["left_dir"], frame["right_dir"]
        head_center = frame["head_center"]
        # 各区放在哪、多大：默认是下面注释里说的那组比例，量过身就是量出来的。
        fit = self.zone_fit["zones"]
        rects: dict[str, dict] = {}
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
            for name, direction in (("leftHand", left_dir), ("rightHand", right_dir)):
                hand_bottom = hip["y"] - fit[name]["bottom"] * torso_px / ih
                inner = head_center["x"] + direction * fit[name]["inset"] * torso_px / iw
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
            knees_bent = self._points_good(pose_map, (
                "left_hip", "left_knee", "left_ankle", "right_hip", "right_knee", "right_ankle")) and min(
                self._angle_at(pose_map["left_hip"], pose_map["left_knee"], pose_map["left_ankle"]),
                self._angle_at(pose_map["right_hip"], pose_map["right_knee"], pose_map["right_ankle"]),
            ) < HEAD_JUMP_KNEE_BENT
            anchor = self._update_head_jump_anchor(jump_anchor, shoulder, hip, now, knees_bent)
            # 框的下沿比站着时的鼻子高 rise 个躯干（默认 0.16），框高 0.28 个躯干。
            jump_rect = _rect_at(
                anchor["x"],
                anchor["y"] - (fit["headJump"]["rise"] + HEAD_JUMP_HALF_H) * torso_px / ih,
                0.52 * torso_px, 2 * HEAD_JUMP_HALF_H * torso_px, iw, ih,
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
        # 地面线按脚的下缘（脚踝、脚跟、脚尖里最低的点），和量身量离地高度用的是同一个。
        floor_y = foot_floor_y(pose_map)
        if floor_y is not None:
            # 原地抬脚由 _foot_outward 的向外伸脚证据排除，不能仅靠离地。
            # 默认：中心在胯外 0.64、离地 0.335 个躯干，宽 1.00、高 0.60 个躯干。
            for name, side_hip, direction in (("leftFoot", lh, left_dir), ("rightFoot", rh, right_dir)):
                foot = fit[name]
                next_rect = _rect_at(
                    side_hip["x"] + direction * foot["out"] * torso_px / iw,
                    floor_y - foot["lift"] * torso_px / ih,
                    2 * foot["half_w"] * torso_px, 2 * foot["half_h"] * torso_px, iw, ih,
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

    def _zone_depth_locked(self, pose_map: dict[str, dict], name: str, frame: dict | None) -> float:
        """这个框的那只手（脚、鼻子）进了框多深，量身那把尺；几个点取最深的。不在框里是 -1。

        看越过每条边多少，取最小的。贴着画面边的那几条不算：跟随的手区外沿、上沿
        就是画面边，手不可能从那边进来。定住后拖离了画面边的框，四条边都算。
        """
        rect = self.zone_rects.get(name)
        points = ("left_wrist",) if name == "lookGate" else RUNTIME_BODY_ZONES[name]["points"]
        best = -1.0
        for point_name in points:
            point = pose_map.get(point_name)
            if not self._point_in_rect(point, rect):
                continue
            if frame is None:
                return math.inf
            depths = []
            if rect["y2"] < 0.999:
                depths.append((rect["y2"] - point["y"]) / frame["uy"])
            if rect["y1"] > 0.001:
                depths.append((point["y"] - rect["y1"]) / frame["uy"])
            if rect["x1"] > 0.001:
                depths.append((point["x"] - rect["x1"]) / frame["ux"])
            if rect["x2"] < 0.999:
                depths.append((rect["x2"] - point["x"]) / frame["ux"])
            best = max(best, min(depths) if depths else math.inf)
        return best

    def _hand_points_for_mouse_locked(self) -> dict | None:
        """两只手各自使用自己的关节点，缺失时分别退回人体指尖判断。"""
        return self.latest_hands

    def configure_hand_mouse(self, updates: dict | None) -> dict:
        """Apply a settings change under the kernel lock and report the result."""
        with self._lock:
            status = self.hand_mouse_controller.configure(updates)
            self._save_general_settings()
            # 换手或调参都废弃旧起点，立即清掉上一帧速度。
            self._safe_output(self.output.apply, 0.0, 0.0)
            return status

    def _hand_mouse_owns_zone(self, name: str) -> bool:
        """True while hand steering has taken that hand away from its zones.

        lookGate is tied to the left wrist, so it belongs to the left hand here
        even though its name does not say so.
        """
        if name == "lookGate":
            return self.hand_mouse_controller.owns_hand("left")
        return any(name.startswith(f"{hand}Hand") and
                   self.hand_mouse_controller.owns_hand(hand) for hand in HANDS)

    def _update_zones_locked(self, pose_map: dict[str, dict], now: float) -> None:
        previous_gate = bool(self.vertical_gate_active)
        self._update_feet_locked(pose_map, now)
        if self.zones_frozen:
            # 定住了：用定住那一刻（或者之后在界面上拖过）的框，不再跟着人算。
            self.zone_rects = self._frozen_zone_rects_locked()
        else:
            self.zone_rects = self._compute_body_zones(pose_map, now)
        changed = False
        simple = self.zone_trigger_mode == "simple"
        gate_available = self._gate_available()
        zone_names = list(RUNTIME_BODY_ZONES) + (["lookGate"] if gate_available else [])
        frame = body_frame(pose_map, self.width, self.height)
        self.zone_kin.update(pose_map, frame, self._pose_sample_time_locked(now))
        actions = self._bound_action_triggers_locked()
        conflicts = {} if simple else self.zone_conflicts_locked(actions)
        busy = {trigger for trigger in actions if self._motion_busy_locked(trigger)}
        self._note_busy_locked(busy, conflicts, now)
        for name in zone_names:
            state = self.zone_state.setdefault(name, fresh_zone_state())
            depth = self._zone_depth_locked(pose_map, name, frame)
            inside = depth >= 0.0
            if name in ("leftFoot", "rightFoot") and not simple:
                # 脚区要确实往外抬了脚（见 FOOT_ZONE_LIFT）：站宽一点、脚贴地滑一下
                # 都会碰到框，那不是按。
                inside = inside and self._foot_outward(pose_map, "left" if name == "leftFoot" else "right")
            if inside and self._hand_mouse_owns_zone(name):
                # That hand is steering the pointer.  Without this it would also
                # be pressing whatever zone it flies through, so aiming would
                # mash buttons.
                inside = False
            # 身体确实在框里没有，不管判定按不按。「录我的动作」数进框次数用它。
            state["raw_inside"] = inside
            # The look gate is a safety arm, so leaving it must cut vertical
            # output on the very first missing frame.  Body action zones
            # retain their normal two-frame hysteresis.
            exit_frames = 1 if name == "lookGate" else 2
            if simple:
                # 进去就按：在框里就按着，出来就松，下面那些判断一条都不走。
                state["inside"] = state["inside"] + 1 if inside else 0
                state["outside"] = 0 if inside else state["outside"] + 1
                if state["pressed"] != inside:
                    state.update(pressed=inside, phase="pressed" if inside else "idle",
                                 pressed_at=now if inside else None, reason="simple")
                    if inside:
                        state["last_pressed_at"] = now
                    changed = True
                continue
            info = conflicts.get(name)
            yielding = tuple(info["triggers"]) if info and info["yields"] else ()
            binding = self._effective_binding_locked(f"zone.{name}") if name in RUNTIME_BODY_ZONES else None
            deliberate = str(((binding or {}).get("action") or {}).get("type", "")) == "system"
            # 系统功能：有任何绑了键的动作正在做都不按。游戏按键：只看会扫过它的那几个。
            watched = actions if deliberate else yielding
            zone_busy = any(trigger in busy for trigger in watched)
            tail = not zone_busy and any(now - self.trigger_busy_at.get(trigger, -math.inf) < TAIL_S
                                         for trigger in watched)
            given = ZoneInput(
                inside=inside, depth=depth if inside else -1.0, conflict=bool(yielding),
                busy=zone_busy, tail=tail, deliberate=deliberate, jump=name in JUMP_ZONES,
                exit_frames=exit_frames, sweep_tags=yielding,
            )
            if self.zone_arbiter.update(name, state, now, given, self.zone_kin):
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

    def _bound_action_triggers_locked(self) -> list[str]:
        """绑了键的动作和姿势（内置、下载的、自己录的），框要不要让路只看这些。"""
        idents = [f"motion.{ident}" for ident in ("march", "calf_back", *self._motion_rules)]
        idents += [f"pose.{ident}" for ident in self._pose_rules]
        store = self.custom_pose_store
        if store is not None:
            idents += [f"pose.{entry['id']}" for entry in getattr(store, "poses", ())
                       if isinstance(entry, dict) and entry.get("id")]
        out = []
        for trigger in dict.fromkeys(idents):
            action = (self._effective_binding_locked(trigger) or {}).get("action")
            if isinstance(action, dict) and action.get("type") and action.get("target"):
                out.append(trigger)
        return out

    def _zone_with_motion_locked(self, zone: str, binding: dict | None) -> bool:
        """这个框设成了「做动作时也要按」没有。没设过的：要跳才碰得到的框默认是，
        别的默认不是——人在空中只停一瞬间，等不起。"""
        if isinstance(binding, dict) and "with_motion" in binding:
            return bool(binding["with_motion"])
        return zone in JUMP_ZONES

    def zone_conflicts_locked(self, actions: list[str] | None = None) -> dict[str, dict]:
        """绑了键的框里，哪些会被同样绑了键的动作扫过，录的时候几次里扫过几次。

        录过的动作按录的算（intent_library 对着现在的框在后台算好装进来），哪怕一次
        都没扫过也以它为准；没录过的才看动作文件里写的 passes_zones。

        ``yields``：框要让路，也就是没设成「做动作时也要按」。设成了的照样列出来，
        界面上提醒「做这个动作时会一起按到」。
        """
        out: dict[str, dict] = {}
        actions = self._bound_action_triggers_locked() if actions is None else actions
        # 动作文件里写的，一次读完：这个函数每帧都跑。
        declared: dict[str, set[str]] = {}
        for entry in pose_library.entries():
            for zone in entry["passes_zones"]:
                declared.setdefault(zone, set()).add(pose_library.trigger_of(entry))
        for zone in RUNTIME_BODY_ZONES:
            binding = self._effective_binding_locked(f"zone.{zone}")
            if not binding:
                continue
            rates: dict[str, dict] = {}
            for trigger in actions:
                recorded = self.zone_conflict_rates.get(trigger)
                if recorded is not None:
                    rate = recorded.get(zone) or {}
                    hits, reps = int(rate.get("hits", 0)), int(rate.get("reps", 0))
                    if reps > 0 and hits / reps >= ZONE_CONFLICT_MIN_RATE:
                        rates[trigger] = {"hits": hits, "reps": reps, "source": "recorded"}
                elif trigger in declared.get(zone, ()):
                    rates[trigger] = {"source": "declared"}
            if rates:
                with_motion = self._zone_with_motion_locked(zone, binding)
                out[zone] = {"triggers": list(rates), "rates": rates, "with_motion": with_motion,
                             "yields": not with_motion and self.zone_trigger_mode != "simple"}
        return out

    def _note_busy_locked(self, busy: set[str], conflicts: dict, now: float) -> None:
        """记下哪些动作正在做；一个动作刚开始做时，看看有没有框在这之前刚按下——
        那一下多半是做这个动作时扫过去误按的。"""
        started = busy - getattr(self, "_busy_previous", set())
        self._busy_previous = set(busy)
        for trigger in busy:
            self.trigger_busy_at[trigger] = now
        for trigger in started:
            for zone in RUNTIME_BODY_ZONES:
                state = self.zone_state.get(zone) or {}
                pressed_at = state.get("last_pressed_at")
                if pressed_at is None or not 0.0 <= now - pressed_at <= MISFIRE_WINDOW_S:
                    continue
                info = conflicts.get(zone)
                if info and trigger in info["triggers"] and not info["yields"]:
                    continue  # 设成了「做动作时也要按」：本来就要一起按
                if self._zone_with_motion_locked(zone, self._effective_binding_locked(f"zone.{zone}")):
                    continue
                recent = [item for item in self.zone_misfires
                          if item[1] == zone and item[2] == trigger and now - item[0] < 1.5]
                if not recent:
                    self.zone_misfires.append((now, zone, trigger))

    def zone_misfire_hint_locked(self, now: float) -> dict | None:
        """没录过的动作误按框攒够了次数，就提示去录一下。录过的不提示：录过还误按，
        要调的是判断本身，录第二遍没用。"""
        counts: dict[str, list[str]] = {}
        for at, zone, trigger in self.zone_misfires:
            if now - at <= MISFIRE_MEMORY_S and trigger not in self.zone_conflict_rates:
                counts.setdefault(trigger, []).append(zone)
        best = max(counts.items(), key=lambda item: len(item[1]), default=None)
        if not best or len(best[1]) < MISFIRE_HINT_COUNT:
            return None
        return {"trigger": best[0], "zones": sorted(set(best[1])), "count": len(best[1])}

    def configure_zone_learning(self, rates: dict | None, bank: SnippetBank | None) -> None:
        """装上从录的动作算出来的东西：各动作扫过各框的次数、每次进框的样子。"""
        with self._lock:
            self.zone_conflict_rates = copy.deepcopy(rates or {})
            self.zone_arbiter.bank = bank or SnippetBank()

    def set_zone_learning_state(self, state: str, *, report=None, error: str = "", took_s=None) -> None:
        """后台算的进度和结果（体检报告），界面上显示。"""
        with self._lock:
            self.zone_learning = {"state": state, "error": error,
                                  "report": report if state == "ready" else self.zone_learning.get("report"),
                                  "took_s": took_s, "snippets": len(self.zone_arbiter.bank)}

    def replay_snapshot(self) -> dict | None:
        """回放录的动作要照着的那些设置（intent_library.make_replay_kernel）。正在录就不给。"""
        with self._lock:
            if self.intent_session is not None and self.intent_session.active:
                return None
            store = self.custom_pose_store
            return copy.deepcopy({
                "control_bindings": self.control_bindings,
                "motion_config": self.motion_config,
                "pose_actions": list(self.pose_actions.values()),
                "custom_poses": list(getattr(store, "poses", []) or []) if store is not None else [],
                "zone_fit": self.zone_fit,
                "zones_frozen": self.zones_frozen,
                "frozen_rects": self.frozen_rects,
                "frozen_anchor": self.frozen_anchor,
                "zone_trigger_mode": self.zone_trigger_mode,
                "march_algorithm": self.march_algorithm,
                "vertical_look": self.vertical_look,
                "hand_mouse": dict(self.hand_mouse_controller.config),
                "action_chain": self.action_chain.config,
            })

    def _motion_busy_locked(self, trigger: str) -> bool:
        """这个动作正在做：已经认出来了，或者上一帧的原始判定已经成立（还在去抖）。"""
        ident = trigger.split(".", 1)[-1]
        return bool(self.motion_raw.get(ident)) or ident in self.motion_active or ident in self.pose_active

    def _pressed_keys_locked(self) -> list[str]:
        return sorted({
            RUNTIME_BODY_ZONES[name]["button"]
            for name, state in self.zone_state.items()
            if name in RUNTIME_BODY_ZONES and RUNTIME_BODY_ZONES[name].get("button") and state["pressed"]
        })

    # ---------- four existing motion rules ----------

    def _update_feet_locked(self, pose_map: dict[str, dict], now: float) -> None:
        """这一帧两只脚各离地多高、往外离站着的位置多远，存进 self.feet。

        离地：两只脚的下缘直接比高低（躯干），再扣掉站着时本来就有的那点差。往外：
        和量身同一把横向尺子，减去站着时的位置。踏步和脚区都读这一份。

        站着的基准只在两脚着地、最近 FOOT_STILL_S 里没怎么动时才往现在挪（见
        FOOT_STILL_*）。以前是第一次站好时记一下就再也不动：人挪了位置、站宽了，
        基准就一直是错的。
        """
        self.feet = None
        frame = body_frame(pose_map, self.width, self.height)
        if frame is None:
            return
        torso = abs(frame["hip"]["y"] - frame["shoulder"]["y"])
        bottoms = {side: foot_bottom_y(pose_map, side) for side in ("left", "right")}
        outs = {side: foot_out(pose_map, frame, side) for side in ("left", "right")}
        if torso < .025 or None in bottoms.values() or None in outs.values():
            return
        # 左脚比右脚高多少。右脚的就是它的相反数。
        diff = (bottoms["right"] - bottoms["left"]) / torso
        history = self.foot_history
        history.append((now, diff, outs["left"], outs["right"]))
        while now - history[0][0] > FOOT_FIRST_BASE_S:
            history.pop(0)
        recent = [item for item in history if now - item[0] <= FOOT_STILL_S]
        still = (
            now - recent[0][0] >= FOOT_STILL_S * 0.6
            and max(item[1] for item in recent) - min(item[1] for item in recent) <= FOOT_STILL_LIFT
            and all(max(item[k] for item in recent) - min(item[k] for item in recent) <= FOOT_STILL_OUT
                    for k in (2, 3))
        )
        base = self.foot_base
        if base is None and still and abs(diff) <= FOOT_FIRST_BASE_LIFT:
            base = self.foot_base = {"diff": diff, "left": outs["left"], "right": outs["right"], "at": now}
        elif base is None and now - history[0][0] >= FOOT_FIRST_BASE_S * 0.8:
            # 一上来就在踏步，一直没站稳：左右交替抬脚时两边各抬一半，最近一秒的
            # 中位数就落在站着的地方。之后一站稳就按上面那条慢慢修正。
            base = self.foot_base = {
                "diff": statistics.median(item[1] for item in history),
                "left": statistics.median(item[2] for item in history),
                "right": statistics.median(item[3] for item in history),
                "at": now,
            }
        elif still and base is not None and abs(diff - base["diff"]) <= FOOT_GROUNDED_LIFT:
            follow = _clamp((now - base["at"]) / FOOT_BASE_FOLLOW_S, 0.0, 1.0)
            for key, value in (("diff", diff), ("left", outs["left"]), ("right", outs["right"])):
                base[key] += follow * (value - base[key])
        # 还没站稳过一次就不报：没有基准时，手机斜放带来的那点高低差会被当成抬脚，
        # 真人录像里一开头就凭空多出一步。
        if base is None:
            return
        base["at"] = now
        lift = diff - base["diff"]
        self.feet = {
            "lift": {"left": lift, "right": -lift},
            "out": {side: outs[side] - base[side] for side in ("left", "right")},
        }

    def _foot_lift(self, side: str) -> float | None:
        return self.feet["lift"][side] if self.feet else None

    def _foot_outward(self, pose_map: dict[str, dict], side: str) -> bool:
        """这只脚确实往外抬起来了：离地、而且往外离开了站着的位置。"""
        if not self.feet:
            return False
        return self.feet["lift"][side] >= FOOT_ZONE_LIFT and self.feet["out"][side] >= FOOT_ZONE_OUT

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

    # ---------- 从云端下载的动作 ----------

    def configure_pose_actions(self, docs) -> None:
        """换成这一批下载过的动作（校验过的动作文件）。没在这里面的就认不出来。"""
        with self._lock:
            self._install_pose_actions_locked(docs)
            self._dispatch_controls_locked(time.monotonic())

    def _install_pose_actions_locked(self, docs) -> None:
        actions = {doc["id"]: doc for doc in docs}
        motion_rules = {ident: doc["rule"] for ident, doc in actions.items() if doc["group"] == "motion"}
        pose_rules_ = {ident: doc["rule"] for ident, doc in actions.items() if doc["group"] == "pose"}
        # 先排好顺序：转圈的规则在这里就报错，不会等到某一帧才炸。
        motion_order = pose_rules.evaluation_order(motion_rules)
        pose_order = pose_rules.evaluation_order(pose_rules_)
        removed = set(self.pose_actions) - set(actions)
        self.pose_actions = actions
        self._motion_rules, self._motion_rule_order = motion_rules, motion_order
        self._pose_rules, self._pose_rule_order = pose_rules_, pose_order
        for ident in removed:
            # 删掉的动作：状态全部清掉，免得删的那一刻它正按着键。
            self.motion_debounce.pop(ident, None)
            self.body_motion_action_risk_debounce.pop(ident, None)
            self.body_motion_action_risk.discard(ident)
            self.motion_active.discard(ident)
            self.motion_raw.pop(ident, None)
            self.pose_debounce.pop(ident, None)
            self.pose_active.discard(ident)
            self.pose_confidence.pop(ident, None)
        for ident in motion_rules:
            self.motion_debounce.setdefault(ident, {"active": False, "on": 0, "off": 0})
            self.body_motion_action_risk_debounce.setdefault(
                ident, {"active": False, "on_since": 0.0, "off_since": 0.0})
        for ident in pose_rules_:
            self.pose_debounce.setdefault(ident, {"active": False, "on": 0, "off": 0})

    def _motion_risk_timing(self, ident: str) -> tuple[float, float]:
        if ident in BODY_MOTION_ACTION_RISK_TIMING:
            return BODY_MOTION_ACTION_RISK_TIMING[ident]
        on_s, off_s = self.pose_actions[ident]["risk_timing"]
        return float(on_s), float(off_s)

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
        calf_raw = march_raw = False
        # 从云端下载的动作（下蹲、双手举过头、开合跳……）：规则是数据，交给规则引擎。
        # 以前写死在这里的判断原样搬进了各自的规则，逐帧一致（tests/test_pose_rules.py）。
        rules = pose_rules.evaluate_rules(self._motion_rules, pose_map, self.width, self.height,
                                          self._motion_rule_order)
        # 下面踏步那段要问两件事，动作文件自己声明：
        #   blocks_steps —— 做着这个动作时不算踏步、不算小腿后抬（下蹲）；
        #   claims_lift  —— 这一下抬腿要是碰上了它，就只算它（提膝碰对侧肘）。
        # 它们不看手腕和脚踝——做的时候那两个常被挡住，抬起的膝盖和对侧手肘却看得清。
        steps_blocked = any(rules[ident]["raw"] for ident in rules
                            if self.pose_actions[ident].get("blocks_steps"))
        lift_claimers = [ident for ident in rules if self.pose_actions[ident].get("claims_lift")]
        lift_claimers_bound = [ident for ident in lift_claimers if self._motion_has_effective_binding_locked(ident)]
        knee_meets_elbow = {side: any(rules[ident]["sides"][side] for ident in lift_claimers)
                            for side in ("left", "right")}
        knee_near_elbow = {side: any(rules[ident].get("attempts", {}).get(side, False) for ident in lift_claimers_bound)
                           for side in ("left", "right")}

        leg_good = math.isfinite(torso) and self._points_good(pose_map, ("left_hip", "right_hip", "left_knee", "right_knee", "left_ankle", "right_ankle"))
        if leg_good:
            def rise(side: str, other: str, joint: str) -> float:
                """这一侧的膝或踝比另一侧高出多少个躯干，各自相对自己那边的髋量。"""
                return ((pose_map[other + "_" + joint]["y"] - pose_map[other + "_hip"]["y"])
                        - (pose_map[side + "_" + joint]["y"] - pose_map[side + "_hip"]["y"])) / torso

            def shin(side: str) -> float:
                """脚踝在膝盖下面多少个躯干。小腿往后抬到头时是负的（脚踝高过膝盖）。"""
                return (pose_map[side + "_ankle"]["y"] - pose_map[side + "_knee"]["y"]) / torso

            def calf_like(ankle: float, knee: float, shin_len: float) -> bool:
                # 小腿向后抬起：脚踝抬到膝盖那么高（高出另一只 0.40 个躯干以上、离膝盖
                # 不到 0.15），膝盖几乎不动。真人录像里小腿后抬每次都在 0.57 以上、膝盖
                # 最多升 0.07、脚踝高过膝盖；踏步最多到 0.29——正对镜头时两个动作都是
                # "脚上来、膝盖不太动"，能分开它们的就是抬多高。提膝时膝盖朝镜头来，
                # 画面上也几乎不升，但小腿垂在膝盖下面 0.28 以上，靠最后这条分开。
                return (ankle > CALF_LIFT_ANKLE_RISE and shin_len < CALF_LIFT_SHIN_MAX
                        and knee < min(CALF_LIFT_KNEE_SHARE * ankle, CALF_LIFT_KNEE_MAX))

            # 小腿后抬和提膝碰肘都没绑键的话，一步不用等脚抬到最高：抬起来稳住一下
            # 就算。等到最高是为了和那两个分开，没绑的动作不需要分。
            wait_for_peak = (self._motion_has_effective_binding_locked("calf_back")
                             or bool(lift_claimers_bound))

            def step_done(side: str, other: str) -> bool:
                """这只脚是不是刚走完一步。

                正对镜头时，踏步看到的主要是脚踝抬高，膝盖是朝镜头来的，画面上几乎
                不往上走（真人录像：脚踝每步抬 0.07~0.29 个躯干，膝盖只有 0.01~0.07）。
                以前要求膝盖抬 0.08，所以一步都认不出来。

                每一下抬起只算一次，落回去之前不会再算。要和小腿后抬、提膝碰肘分开
                时，等脚踝抬到最高、不再往上的那一刻再定：这一下碰到了对侧手肘就是
                提膝碰肘，抬到膝盖那么高就是小腿后抬，都不是才算一步。
                """
                # 抬没抬起、落没落下看脚的下缘（见 MARCH_LIFT_START）；抬得像不像小腿
                # 后抬，还是看相对胯的脚踝和膝盖，那几个门槛是照它量的。
                ankle = rise(side, other, "ankle")
                knee = rise(side, other, "knee")
                raised = self._foot_lift(side)
                key = side + "_lift"
                if raised is None:
                    # 脚看不清，或者人还没站稳过一次：这一帧不算抬也不算落。
                    self.step[key] = None
                    return False
                lift = self.step.get(key)
                if lift is None:
                    if raised < MARCH_LIFT_START:
                        return False
                    lift = self.step[key] = {"peak_ankle": ankle, "peak_knee": knee, "min_shin": shin(side),
                                             "prev": raised, "since": now, "done": False,
                                             "crossed": False, "cross_attempt": False}
                    rising = True
                else:
                    if raised < MARCH_LIFT_END:
                        self.step[key] = None
                        return False
                    rising = raised > lift["prev"] + 0.01
                lift["peak_ankle"] = max(lift["peak_ankle"], ankle)
                lift["peak_knee"] = max(lift["peak_knee"], knee)
                lift["min_shin"] = min(lift["min_shin"], shin(side))
                lift["prev"] = raised
                lift["crossed"] = lift["crossed"] or knee_meets_elbow[side]
                if lift_claimers_bound:
                    # 记录“正在做提膝碰肘但还没碰到”的整次抬腿，避免它在峰值
                    # 处落入踏步兜底。普通踏步的肘部距离超过这层余量，不受影响。
                    lift["cross_attempt"] = lift.get("cross_attempt", False) or knee_near_elbow[side]
                if lift["done"] or now - lift["since"] < 0.035 or (wait_for_peak and rising):
                    return False
                lift["done"] = True
                if (lift["crossed"] or lift.get("cross_attempt", False)
                        or calf_like(lift["peak_ankle"], lift["peak_knee"], lift["min_shin"])):
                    return False
                return not steps_blocked and not self._foot_outward(pose_map, side)

            left_step = step_done("left", "right")
            right_step = step_done("right", "left")

            def calf_side(side: str, other: str) -> bool:
                lift = self.step.get(side + "_lift")
                return (calf_like(rise(side, other, "ankle"), rise(side, other, "knee"), shin(side))
                        and not (lift and lift["crossed"]))

            calf_raw = not steps_blocked and (calf_side("left", "right") or calf_side("right", "left"))

            def step_event(side: str) -> None:
                # 恢复旧版的交替门控：单腿挪动只记住这一侧，不直接开始前进；
                # 只有相反脚在 0.10~1.50 秒内也完成一次抬脚，才续上踏步。
                # 脚下缘的测量、站姿基准和峰值分类仍沿用当前版本，避免把已修好的
                # 斜手机位、脚跟脚尖和提膝碰肘问题一起回退。
                previous = self.step["last_side"]
                gap = now - self.step["last_at"]
                if previous and side != previous and 0.10 <= gap <= 1.50:
                    self.step["active_until"] = now + MARCH_HOLD_S
                self.step["last_side"], self.step["last_at"] = side, now
            if left_step:
                step_event("L")
            if right_step:
                step_event("R")
            # A jump breaks the stepping rhythm without meaning "stop walking":
            # both feet leave the ground together, so no alternation can be
            # observed and the walk would otherwise expire in mid-air.  Zones
            # are evaluated before motions, so this reads the current frame.
            # Only an already-running walk is held; a standing jump starts none.
            jumping = bool(self.zone_state.get("headJump", {}).get("pressed"))
            if jumping and now < self.step["active_until"]:
                self.step["active_until"] = now + MARCH_HOLD_S
            if now - self.step["last_at"] > 1.55 and not jumping:
                self.step["last_side"], self.step["active_until"] = "", 0.0
            cross_attempting = any(
                self.step.get(side + "_lift") and self.step[side + "_lift"].get("cross_attempt", False)
                for side in ("left", "right")
            )
            if cross_attempting:
                # 一次未完成碰肘动作也要取消已有的短暂踏步保持，不能在放腿后又补出
                # 一个踏步状态。
                self.step["active_until"] = 0.0
            march_raw = (not steps_blocked and not calf_raw and not cross_attempting
                         and now < self.step["active_until"])
            if self.march_algorithm == "responsive":
                calf_in_progress = any(
                    # 新版不等峰值，因此对已经明显接近小腿后抬的轨迹提前让路。
                    # 真实踏步的脚踝峰值低于 0.30；该记录中的未完整后抬为 0.40。
                    (calf_like(lift["peak_ankle"], lift["peak_knee"], lift["min_shin"])
                     or (lift["peak_ankle"] > .35 and lift["min_shin"] < .22
                         and lift["peak_knee"] < min(.35 * lift["peak_ankle"], .10)))
                    for side in ("left", "right")
                    if (lift := self.step.get(side + "_lift"))
                )
                excluded = {side for side in ("left", "right")
                            if self._foot_outward(pose_map, side) or knee_meets_elbow[side] or knee_near_elbow[side]}
                march_raw = self._responsive_march.update(
                    self.feet["lift"] if self.feet else None, now,
                    excluded=excluded, blocked=steps_blocked or calf_raw or calf_in_progress or cross_attempting,
                    jumping=jumping,
                )
        else:
            self.step.clear()
            self.step.update(_fresh_step())
            self._responsive_march.reset()

        raw_motion = {"march": march_raw, "calf_back": calf_raw}
        raw_motion.update({ident: result["raw"] for ident, result in rules.items()})
        self.motion_raw = dict(raw_motion)
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
            on_s, off_s = self._motion_risk_timing(ident)
            if self._set_body_motion_action_risk_timed(ident, raw, now, on_s, off_s):
                risk.add(ident)
        self.body_motion_action_risk = risk

        active = set()
        march_off_frames = 1 if self.march_algorithm == "responsive" else 2
        if self._set_motion_debounced("march", march_raw, 1, march_off_frames): active.add("march")
        if self._set_motion_debounced("calf_back", calf_raw, 3, 4): active.add("calf_back")
        for ident, result in rules.items():
            on_frames, off_frames = self.pose_actions[ident]["debounce"]
            if self._set_motion_debounced(ident, result["raw"], on_frames, off_frames):
                active.add(ident)
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
        confidence = {ident: 0.0 for ident in self._pose_rules}

        torso_good = self._points_good(pose_map, ("left_shoulder", "right_shoulder", "left_hip", "right_hip"), 0.45)
        if torso_good:
            # 从云端下载的姿势（双手交叉……）。规则是数据，交给规则引擎；以前写死在这
            # 里的双手交叉原样搬进了它的规则，连置信度都逐帧一致。
            rules = pose_rules.evaluate_rules(self._pose_rules, pose_map, self.width, self.height,
                                              self._pose_rule_order)
            for ident, result in rules.items():
                confidence[ident] = float(result.get("score", float(result["raw"])))
                on_frames, off_frames = self.pose_actions[ident]["debounce"]
                if self._set_pose_debounced(ident, result["raw"], on_frames, off_frames):
                    active.add(ident)
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
                if ident not in self._pose_rules:
                    self.pose_debounce.pop(ident, None)
            self._dispatch_controls_locked(time.monotonic())

    def _effective_bindings_locked(self) -> dict[str, dict]:
        """每个触发器**真正会按下去的**那一份，界面照这个显示。

        为什么不能直接给 control_bindings：区域和动作有一层内置的兜底。配置里没有
        zone.headJump 这一条时，它照样按 A——兜底在 _effective_binding_locked 里。
        于是界面读配置读出个空，写「未映射」，而人在游戏里明明被按了一个键。

        这种不一致最难查：界面说没绑，实际有反应，两边都"没报错"。所以真正会生效的
        那份必须由内核算好报出来，不能让界面自己再猜一遍——猜就一定会有第二套规则。
        """
        triggers = set(self.control_bindings)
        triggers.update(f"zone.{name}" for name in RUNTIME_BODY_ZONES)
        triggers.update(f"motion.{item.get('id')}" for item in self.motion_config
                        if item.get("id"))
        out: dict[str, dict] = {}
        for trigger in triggers:
            binding = self._effective_binding_locked(trigger)
            if binding:
                out[trigger] = copy.deepcopy(binding)
        return out

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

    def effective_bindings(self) -> dict[str, dict]:
        """真正会生效的那份，给手机用。见 _effective_bindings_locked。"""
        with self._lock:
            return self._effective_bindings_locked()

    def configure_trigger_listener(self, listener) -> None:
        """装上"触发集合变了"的回调。见 _trigger_listener 的说明。"""
        with self._lock:
            self._trigger_listener = listener

    def configure_system_action_handler(self, handler) -> None:
        """装上执行系统功能的回调：handler(target, trigger)。见 _system_action_handler。"""
        with self._lock:
            self._system_action_handler = handler

    def _run_system_action_locked(self, target, trigger: str) -> None:
        """绑在区域、动作、姿势上的「系统功能」，触发那一下执行一次。

        定住 / 恢复跟随就在这里做：定住的是这一帧刚算出来的框，放到别的线程去做就
        晚了一两帧。别的交给 server。系统功能不看游戏控制开没开——「开始输出」
        本来就是在没开的时候用的。
        """
        target = str(target or "").strip().upper()
        zones = {"ZONES.FREEZE": self._freeze_zones_locked,
                 "ZONES.FOLLOW": self._unfreeze_zones_locked,
                 "ZONES.FREEZE_TOGGLE": self._toggle_zone_freeze_locked,
                 "ZONES.MOVE_HERE": self._move_zones_here_locked}
        if target in zones:
            result = zones[target]()
            if not result.get("executed"):
                self.last_error = str(result.get("reason") or "")
            return
        handler = self._system_action_handler
        if handler is None:
            return

        def run() -> None:
            try:
                handler(target, trigger)
            except Exception as exc:  # noqa: BLE001 - 系统功能失败不该拖垮控制
                self.last_error = str(exc)

        threading.Thread(target=run, name="system-action", daemon=True).start()

    def _trigger_brief_locked(self, trigger: str) -> dict:
        binding = self._effective_binding_locked(trigger)
        return {"id": trigger, "action": copy.deepcopy((binding or {}).get("action"))}

    def _mapped_triggers_locked(self, triggers) -> set[str]:
        """其中真的绑了键的那些。手机上只显示这些。

        没绑键的动作做了也不按任何键，手机头顶再写一个「下蹲 → 未映射」、框再亮一
        下，人只会以为它起作用了。要看它认没认出来，去电脑上的「动作测试」页——
        那一页照样全列，靠的是 recent_triggers 和区域的 recognized。
        """
        return {trigger for trigger in triggers if self._effective_binding_locked(trigger)}

    def note_trigger(self, trigger: str, action: dict | None, label: str | None = None) -> None:
        """记一次触发。语音走的不是内核这条路，所以由 server 调进来。

        一份记录、一个时钟。分两份存的话，界面上要把两串时间戳对齐，而它们来自
        不同的地方，早晚差开。

        ``label`` 是人说出口的那句话。通用口令和内置口令不在映射表里，界面上
        没有名字可查，不带它的话只能显示成 voice.M 这种代号。
        """
        with self._lock:
            now = time.monotonic()
            self._note_trigger_locked(trigger, action, now, label)
            # 语音也要推给手机。它是"说一句就完"的那种，不会出现在按住的集合里，
            # 所以 held 照旧、fired 只有这一条。没有动作的不推，理由同
            # _mapped_triggers_locked。
            mapped = isinstance(action, dict) and bool(action.get("type")) and bool(action.get("target"))
            if mapped and self._trigger_listener is not None:
                fired = {"id": str(trigger),
                         "action": copy.deepcopy(action) if isinstance(action, dict) else None}
                if label:
                    fired["name"] = str(label)
                try:
                    self._trigger_listener({
                        "held": [self._trigger_brief_locked(item)
                                 for item in sorted(self._mapped_triggers_locked(self.trigger_previous))],
                        "fired": [fired],
                        "at": round(now, 3),
                    })
                except Exception as exc:  # noqa: BLE001 - 同上，显示不能拖垮控制
                    self.last_error = str(exc)

    def _note_trigger_locked(self, trigger: str, action: dict | None, now: float,
                             label: str | None = None) -> None:
        record = {
            "at": round(now, 3),
            "trigger": str(trigger),
            # 动作原样带上，不在这里翻译成"Y 键"。名字和写法归界面管，内核翻一遍
            # 就成了第二套说法，和映射表那边迟早不一致。
            "action": copy.deepcopy(action) if isinstance(action, dict) else None,
        }
        if label:
            record["label"] = str(label)
        self.recent_triggers.append(record)

    def _update_action_chain_locked(self, now: float) -> None:
        squat = "squat" in self.motion_active
        signals = {
            "zone.headJump": bool(self.zone_state.get("headJump", {}).get("pressed")),
            "motion.squat": squat,
            "motion.stand": not squat,
        }
        self.action_chain_result = self.action_chain.update(now, signals)

    def release_voice_hold(self, command_id) -> dict:
        """停住一条或多条口令当初"持续按住"的那些键（或循环宏）。"""
        with self._lock:
            return self._release_voice_hold_locked(command_id)

    def _release_voice_hold_locked(self, command_id) -> dict:
        # 按那条口令现在的绑定去找要松的键，不在停止这一侧另存一份——口令改了键，
        # 停止跟着就对，不会去松一个早就不按的键。
        if isinstance(command_id, set):
            raw_ids = sorted(command_id, key=str)
        elif isinstance(command_id, (list, tuple)):
            raw_ids = list(command_id)
        else:
            raw_ids = [command_id]
        identifiers = []
        for raw_id in raw_ids:
            ident = str(raw_id or "").strip().lower()
            if ident.startswith("voice."):
                ident = ident[len("voice."):]
            if ident and ident not in identifiers:
                identifiers.append(ident)
        if not identifiers:
            return {"executed": False, "reason": "要停的那条口令不存在或没有按住任何东西"}
        releaser = getattr(self.output, "release_voice_hold", None)
        if releaser is None:
            return {"executed": False, "reason": "输出不支持停住语音按住"}
        released = []
        missing = []
        for ident in identifiers:
            binding = self.control_bindings.get(f"voice.{ident}")
            action = binding.get("action") if isinstance(binding, dict) and not binding.get("disabled") else None
            if not isinstance(action, dict) or action.get("type") in {"system", "voice_release"}:
                missing.append(ident)
                continue
            self._safe_output(releaser, action)
            released.append(ident)
        if not released:
            return {"executed": False, "reason": "要停的那条口令不存在或没有按住任何东西"}
        result = {"executed": True, "action": f"voice_release:{','.join(released)}"}
        if missing:
            result["missing"] = missing
        return result

    def _dispatch_controls_locked(self, now: float) -> None:
        managed = self.action_chain.managed_triggers if self.action_chain.enabled else set()
        active = {
            f"zone.{name}" for name, state in self.zone_state.items()
            if name in RUNTIME_BODY_ZONES and state.get("pressed")
            and f"zone.{name}" not in managed
        }
        active.update(f"motion.{name}" for name in self.motion_active)
        active.update(f"pose.{name}" for name in self.pose_active)

        chain_hold = self.action_chain_result if hasattr(self, "action_chain_result") else self.action_chain.result()
        if chain_hold.hold and chain_hold.hold_action:
            active.add(chain_hold.hold_action)

        holds = []
        for trigger in sorted(active):
            binding = self._effective_binding_locked(trigger)
            if not binding:
                continue
            action = copy.deepcopy(binding.get("action", {}))
            if trigger == chain_hold.hold_action and chain_hold.hold:
                # A chain is a hold lifecycle even if the profile's ordinary
                # headJump binding was saved as a tap.
                action["behavior"] = "hold"
            # A pose defaults to a single edge trigger but may ask to be held,
            # exactly like a motion: the recognizer drops it the same way, so a
            # held output is released when the pose ends.  Rewriting it here
            # made the configured behavior unreachable no matter what was saved.
            behavior = str(action.get("behavior", "tap" if trigger.startswith("pose.") else "hold")).lower()
            if action.get("type") == "voice_release":
                # 停住一条语音按住：只在刚触发那一下做一次，本身不按任何键。
                if trigger not in self.trigger_previous:
                    self._release_voice_hold_locked(action.get("target", ""))
            elif action.get("type") == "system":
                # 系统功能也是只在刚触发那一下做一次，不按游戏里的任何键。
                if trigger not in self.trigger_previous:
                    self._run_system_action_locked(action.get("target", ""), trigger)
            elif behavior == "tap":
                if trigger not in self.trigger_previous:
                    action["source"] = f"trigger:{trigger}:{time.monotonic_ns()}"
                    action["nonblocking"] = True
                    executor = getattr(self.output, "execute_action", None)
                    if executor is not None:
                        self._safe_output(executor, action)
            else:
                holds.append({"id": trigger, "action": action})

        # 新按下的那些记一笔。只记上升沿：一直按着的话每帧记一条，几秒就把这份
        # 记录冲光了，真正有用的那几条反而看不见。
        for trigger in sorted(active - self.trigger_previous):
            binding = self._effective_binding_locked(trigger)
            self._note_trigger_locked(trigger, (binding or {}).get("action"), now)

        # 集合变了才推给手机。每帧推一次的话，按住不放的那几秒就是每秒三十条一模
        # 一样的消息——手机那边什么都不会变，网络和电池却在一直烧。
        #
        # 只看绑了键的那部分：没绑键的动作手机上不显示，它变了也就不用推。
        shown = self._mapped_triggers_locked(active)
        shown_before = self._mapped_triggers_locked(self.trigger_previous)
        if shown != shown_before and self._trigger_listener is not None:
            payload = {
                "held": [self._trigger_brief_locked(item) for item in sorted(shown)],
                "fired": [self._trigger_brief_locked(item)
                          for item in sorted(shown - shown_before)],
                "at": round(now, 3),
            }
            try:
                self._trigger_listener(payload)
            except Exception as exc:  # noqa: BLE001 - 显示用的东西不该拖垮控制
                self.last_error = str(exc)

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
        # 每个方向只由一个来源输出；垂直握拳不阻断头部左右转向。
        x, y = self.hand_mouse_controller.compose_output(x, y)
        # Reporting belongs in status_locked, not here: that function rebuilds
        # self.head from head_controller.status(), so anything written to the
        # dict at this point is discarded before a client ever sees it.
        if getattr(self.output, "enabled", True):
            self._safe_output(self.output.apply, x, y)

    # ---------- safety/status ----------

    def _clear_body_outputs_locked(self) -> None:
        self.hand_mouse_controller.reset()
        for state in self.zone_state.values():
            last = state.get("last_pressed_at")
            state.update(fresh_zone_state(), last_pressed_at=last)
        self.zone_kin.reset()
        # 定住的框不靠人算，人走开了也还在原地，画面上照样画出来、照样能拖。
        self.zone_rects = self._frozen_zone_rects_locked() if self.zones_frozen else {}
        self.head_jump_anchor = None
        self.head_jump_prev = None
        self.head_jump_torso_ref = None
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
        self.foot_base = None
        self._responsive_march.reset()
        self.foot_history.clear()
        self.feet = None
        self.step.clear()
        self.step.update(_fresh_step())
        self.head_controller.reset_tracking()
        self.head = self.head_controller.status(time.monotonic())
        self._safe_output(self.output.set_buttons, [], source="zones")
        self._safe_output(self.output.set_holds, [], source_group="motions")
        setter = getattr(self.output, "set_action_holds", None)
        if setter is not None:
            self._safe_output(setter, [], source_group="controls")
        self._safe_output(self.output.apply, 0.0, 0.0)
        self.action_chain.reset()
        self.action_chain_result = self.action_chain.result()

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
        return "lookGate" in self.zone_rects

    def runtime_zones_locked(self) -> dict:
        """Return only display geometry and pressed state for the phone overlay.

        ``pressed`` 是"这个框现在按着一个键"：没绑键的框人伸进去也是 False，
        手机和网页上就不会亮。``recognized`` 是"身体确实在框里"，不管绑没绑键，
        只给「动作测试」页用。lookGate 不是键而是一道闸，它两个值一样。
        """
        gate_available = self._gate_available()
        zone_names = list(RUNTIME_BODY_ZONES) + (["lookGate"] if gate_available else [])
        now = time.monotonic()
        zones = {}
        for name in zone_names:
            raw = self.zone_state.get(name, {})
            recognized = bool(raw.get("pressed", False))
            mapped = name not in RUNTIME_BODY_ZONES or bool(self._effective_binding_locked(f"zone.{name}"))
            state = {"pressed": recognized and mapped, "recognized": recognized}
            # 画成什么样：idle 灰、pending 判断中（黄）、pressed 亮、swept 判定是扫过（闪红）。
            # 没绑键的框不判断也不亮，一律 idle。progress 是系统功能框「稳住」走到哪了。
            phase = zone_phase_for_display(raw, now) if mapped and raw else "idle"
            state["phase"] = phase
            if phase == "pending" and raw.get("progress"):
                state["progress"] = round(float(raw["progress"]), 2)
            zones[name] = {"rect": copy.deepcopy(self.zone_rects.get(name)), **state}
        # Keep the old four identifiers in status for clients that have not yet
        # learned the merged names. They are aliases only; no second trigger is
        # evaluated or dispatched for them.
        for alias, canonical in ZONE_ALIASES.items():
            if canonical in zones:
                zones[alias] = copy.deepcopy(zones[canonical])
        return zones

    def runtime_zones(self) -> dict:
        """Small lock-protected snapshot; avoids copying full kernel diagnostics."""
        with self._lock:
            return self.runtime_zones_locked()

    def status_locked(self, now: float) -> dict:
        pose_age = round(max(0.0, (now - self.body_last_at) * 1000.0)) if self.body_last_at else None
        zones = self.runtime_zones_locked()
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
        # 状态与实际输出共用方向归属，不能把头部左右误报成手部的零值。
        self.head["output_x"], self.head["output_y"] = self.hand_mouse_controller.compose_output(
            self.head["output_x"], self.head["output_y"], self.head["hand_mouse"])
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
            # 界面要显示"按的是哪个键"时用这一份，见 _effective_bindings_locked。
            "effective_bindings": self._effective_bindings_locked(),
            "recent_triggers": list(self.recent_triggers),
            # 界面要靠它把 at 换算成"几秒前"。用服务端自己的钟，省得和浏览器对时。
            "now": round(now, 3),
            "action_chain": self.action_chain.status(),
            "zone_fit": self._zone_fit_status_locked(now),
            "intent_recording": self._intent_status_locked(now),
            "intent_items": self.intent_items_locked(),
            "zone_learning": copy.deepcopy(self.zone_learning),
            # 哪些框会被哪些动作扫过、让不让路。界面在绑键的地方照这个提醒。
            "zone_overlaps": self.zone_conflicts_locked(),
            # 没录过的动作误按框攒够了次数：界面提示去「录我的动作」。
            "zone_misfire_hint": self.zone_misfire_hint_locked(now),
            "zone_trigger_mode": self.zone_trigger_mode,
            "march_algorithm": self.march_algorithm,
            # 跟随框定住了没有；zones_anchor_known：定住时看清了人站在哪，「区域挪到我这里」
            # 能按它把整组框搬过来。
            "zones_frozen": bool(self.zones_frozen),
            "zones_anchor_known": self.frozen_anchor is not None,
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
    # 扫到第几个为止。笔记本最多见的是"内置 + 外接"两个，留到 5 已经很宽；
    # 每个打不开的序号都要等系统超时，扫太多只会让人干等。
    MAX_CAMERA_INDEX = 5

    def __init__(self, kernel: ControlKernel, model_path=None, camera_index: int | None = None) -> None:
        self.kernel = kernel
        self.model_path = model_path
        # 不写死 0。一台电脑上可以有好几个摄像头（内置的、外接的、虚拟的），
        # 而 0 号未必是对着人的那个——以前这个数字没有任何地方能改，插了采集卡
        # 或者装了 OBS 虚拟摄像头的人就只能对着一块黑屏，没有别的办法。
        if camera_index is None:
            camera_index = self._remembered("camera_index", 0)
        self.camera_index = max(0, min(self.MAX_CAMERA_INDEX, int(camera_index)))
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

    def _remembered(self, key: str, default):
        """内核那份 general_settings.json。拿不到就用默认值。

        测试里的内核可能是个假的，缺这两个方法很正常；存不下一个摄像头序号
        也不该让摄像头开不起来。
        """
        try:
            value = self.kernel.general_setting(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _remember(self, key: str, value) -> None:
        try:
            self.kernel.remember_general_setting(key, value)
        except Exception:
            pass

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

    def set_camera_index(self, index: int) -> dict:
        """换用第几个摄像头。运行中不给换，和换采集后端一样。

        换了之后要把已选后端清掉：那套后端/格式是上一个镜头探出来的，新镜头
        未必吃同一套，留着会让它带着一份不属于自己的参数去开。
        """
        try:
            index = int(index)
        except (TypeError, ValueError):
            raise CameraUnavailable("摄像头序号必须是数字") from None
        if not 0 <= index <= self.MAX_CAMERA_INDEX:
            raise CameraUnavailable(f"摄像头序号只能是 0 到 {self.MAX_CAMERA_INDEX}")
        with self._lock:
            if self.running and index != self.camera_index:
                raise CameraUnavailable("摄像头运行中不能换摄像头，请先停止识别")
            if index != self.camera_index:
                self.camera_index = index
                self.selected_backend = None
                self.selected_backend_name = None
                self.selected_fourcc = None
                self._last_probe_results = []
                self._remember("camera_index", index)
            return self.backend_config()

    def list_cameras(self, limit: int | None = None) -> dict:
        """挨个序号试着打开，看哪几个是真的在。

        OpenCV 给不出摄像头的名字，所以这里只能报序号和分辨率——名字要靠
        Windows 那边另外一套接口，为一个下拉框不值得。分辨率加上界面里那块
        实时画面，已经够人认出哪个是对着自己的：选一个、开一下、看画面。

        正在用的那一个不去开第二遍：设备多半是独占的，第二次打开会失败，
        于是"正在用的摄像头"反而会被报成不存在。
        """
        limit = self.MAX_CAMERA_INDEX if limit is None else max(0, min(self.MAX_CAMERA_INDEX, int(limit)))
        with self._lock:
            running, current = self.running, self.camera_index
        try:
            import cv2
        except Exception as exc:
            raise CameraUnavailable("本地 Python 未安装 opencv-python，无法列出摄像头") from exc

        # DSHOW 打不开的序号失败得快；MSMF 会在不存在的设备上等很久。列表要
        # 人站在那里等结果，所以这里选快的那个。
        api = self._backend_api(cv2, self.BACKEND_DSHOW)
        devices: list[dict] = []
        for index in range(limit + 1):
            if running and index == current:
                devices.append({"index": index, "width": self.capture_width,
                                "height": self.capture_height, "in_use": True})
                continue
            capture = None
            try:
                capture = cv2.VideoCapture(index, api)
                if not capture.isOpened():
                    continue
                ok, frame = capture.read()
                if not ok or not self._valid_frame(frame):
                    continue
                height, width = int(frame.shape[0]), int(frame.shape[1])
                devices.append({"index": index, "width": width, "height": height, "in_use": False})
            except Exception:
                continue
            finally:
                if capture is not None:
                    try:
                        capture.release()
                    except Exception:
                        pass
        return {"devices": devices, "camera_index": current, "scanned_to": limit}

    def backend_config(self) -> dict:
        with self._lock:
            cache = self._load_backend_cache()
            return {
                "camera_index": self.camera_index,
                "max_camera_index": self.MAX_CAMERA_INDEX,
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
        # 默认电脑摄像头，并且记住上次选的那个。
        #
        # 以前默认手机。这对第一次打开的人是错的：他手机上还没装 APK，而电脑
        # 摄像头是现成的——默认值对着的是少数情况，多数人一进来就得先改一个
        # 自己还不知道含义的下拉框。
        #
        # 但这台电脑可能根本没有摄像头。那种人改成手机之后必须一直是手机，不
        # 能每次启动又被推回一个打不开的东西，所以这里存盘。默认只在"从来没
        # 选过"的时候才生效。
        remembered = str(kernel.general_setting("body_source", "") or "").strip().lower()
        self.body_mode = remembered if remembered in {"computer", "phone"} else "computer"

    def configure_model(self, model_path) -> None:
        self.camera.configure_model(model_path)

    def configure_camera_backend(self, preference: str | None) -> dict:
        return self.camera.configure_backend(preference)

    def camera_backend_config(self) -> dict:
        return self.camera.backend_config()

    def configure_camera_index(self, index) -> dict:
        return self.camera.set_camera_index(index)

    def list_cameras(self) -> dict:
        return self.camera.list_cameras()

    def set_source(self, source: str, *, start_computer: bool = True) -> dict:
        source = str(source).strip().lower()
        if source not in {"computer", "phone"}:
            raise ValueError("source must be computer or phone")
        with self._lock:
            self.camera.stop()
            self.kernel.clear_body()
            self.body_mode = source
            # 先记下来再开摄像头：开不起来也是一次有效的选择——没有摄像头的人
            # 正是要靠这一步把"手机"钉住的。
            self.kernel.remember_general_setting("body_source", source)
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
from motioncontrol.hand_anchor import install_hand_anchor_adapter

install_hand_anchor_adapter(ControlKernel)
