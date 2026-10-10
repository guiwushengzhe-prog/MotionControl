"""Steer the mouse with one hand, gated by a closed fist.

There are two ways to read the fist, and which one is available depends on
what the camera device sends.

*Finger joints, when the device sends them.*  A phone running the hand model
reports 21 points per hand.  Curl is then measured directly: each fingertip's
distance from the wrist against its own knuckle's distance from the wrist.
Extended, a tip sits about twice as far out as its knuckle; curled, it comes
back level with it or nearer.  Both distances start at the wrist, so the ratio
survives the hand rotating, and both scale together, so it survives the player
standing closer to or further from the camera.

*Fingertip spread, otherwise.*  The pose model gives four points per hand --
wrist, thumb, index, pinky -- and no finger joints at all, so "is this a fist"
has to come from how far those three tips sit from the wrist.  That is much
coarser.  It is the fallback, not the plan: it is what a device without the
hand model can still manage.

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
not measured against a population of hands.  ``status()`` therefore reports
the live ``spread`` and ``curl`` readings, and which of the two drove the gate,
so a user whose hand does not match can watch the number and set their own.
"""

from __future__ import annotations

import math

HANDS = ("left", "right")

DEFAULT_CONFIG = {
    # 默认就开着。第一次打开这个软件的人，能自己验证"它真的在动"的动作只有
    # 一个：握拳，看桌面鼠标跟不跟着走。手柄摇杆得先有游戏在前台才看得见,
    # 而鼠标在哪儿都看得见。把它默认关掉，等于要求新手先找到一个开关，才能
    # 知道自己装对了没有——那个开关他不知道存在。
    #
    # 代价是手机端多跑一次手部识别，帧率大约降一成。用手柄玩的人可以关掉，
    # 而且关掉之后会存盘，不会自己开回来。
    "enabled": True,
    "hand": "right",
    # Fraction of forearm length.  Below `fist_close` counts as closed, above
    # `fist_open` as open; between them the previous state persists.  Used only
    # when the device sends no finger joints.
    "fist_close": 0.30,
    "fist_open": 0.40,
    # Fingertip distance from the wrist as a multiple of that finger's own
    # knuckle distance, averaged over the four fingers.  A flat hand measures
    # around 2.0 and a fist around 1.0, so the pair below straddles the
    # midpoint with the same kind of hysteresis gap as the spread pair.
    "curl_close": 1.35,
    "curl_open": 1.60,
    # Offset from the anchor, as a fraction of forearm length, that produces
    # full-speed movement.
    "range": 0.55,
    "deadzone": 0.12,
    "sensitivity": 70.0,
    # 常开，不再是一个开关。摄像头看到的左右和玩家感觉到的左右本来就是相反的,
    # 每个人都要去勾一下的"选项"说明默认值就是错的，不是一个偏好。
    "invert_x": True,
    # A fingertip below this visibility is ignored; too few usable tips means
    # "unknown", not "open".
    "min_visibility": 0.35,
    "min_tips": 2,
}

# How far outside the picture a landmark may sit and still be believed.
#
# This is the check that visibility alone does not give.  Measured on a live
# phone feed: a hand held outside the frame still came back at 0.68 visibility,
# with x around -0.15 -- MediaPipe extrapolates the wrist and fingers from the
# arm rather than reporting that it cannot see them.  Those guessed tips land
# close to the guessed wrist, so the spread reads about 0.24, which is under
# fist_close, and the hand is reported as a permanently clenched fist.
#
# A coordinate outside the picture is not an observation at any confidence, so
# this is a hard geometric test rather than another tunable threshold.  The
# small margin keeps a fingertip resting exactly on the edge usable.
_FRAME_MARGIN = 0.03

_TIPS = ("thumb", "index", "pinky")

# 手部模型时不时漏掉一帧手（动得快、模糊、裁切没跟上）。漏的那帧改看人体指尖间距
# 不妥：那是另一把尺子、另一组门槛，握着拳也可能读成张开——松开再握，起点重定，
# 鼠标断一下；张着手也可能读成握拳，鼠标被拽一下。2026-10-07 Kinect 录制里手腕
# 在画面内时有 29% 的帧没认出手。刚才还有手指读数的话，这么久之内维持上一帧的
# 握拳状态（照常按手腕移动），过了就当看不到手、松开。
FINGER_GAP_HOLD_S = 0.5
# 这么久之内手部模型认出过这只手，就说明它在工作：认不出时不改用指尖间距。
# 2026-10-10 两段真人握拳录制里，握拳状态的来回切换几乎都出在退回指尖间距的那些
# 帧：指尖间距读数 0.24～0.6 连成一片，和握拳门槛搅在一起；10 秒握放 4～5 次切了
# 24 次，改成保持 0.5 秒后松开是 16 次，踏步时张着的手误判握拳从 22% 降到 17%。
# 超过这么久都没认出过，才当作没有手部模型，照旧用指尖间距。
FINGER_SOURCE_S = 3.0

# MediaPipe hand landmark order: 0 is the wrist, then each finger runs from
# knuckle to tip.  Only the four fingers count towards curl.  The thumb folds
# across the palm rather than back towards the wrist, so its tip barely moves
# closer when the hand closes; including it only blunts the signal.
HAND_LANDMARK_COUNT = 21
_HAND_WRIST = 0
_FINGER_KNUCKLES = (5, 9, 13, 17)
_FINGER_TIPS = (8, 12, 16, 20)
_MIN_FINGERS = 3


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


def _point(pose_map: dict, name: str, min_visibility: float,
           *, require_in_frame: bool = True) -> dict | None:
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
    # Out of the picture: extrapolated, not seen. See _FRAME_MARGIN.
    if require_in_frame:
        for axis in ("x", "y"):
            value = float(point[axis])
            if value < -_FRAME_MARGIN or value > 1.0 + _FRAME_MARGIN:
                return None
    return point


def _distance(a: dict, b: dict) -> float:
    return math.hypot(float(a["x"]) - float(b["x"]), float(a["y"]) - float(b["y"]))


def _in_frame(point: dict) -> bool:
    for axis in ("x", "y"):
        value = point.get(axis)
        if not isinstance(value, (int, float)):
            return False
        if value < -_FRAME_MARGIN or value > 1.0 + _FRAME_MARGIN:
            return False
    return True


def _reach(a: dict, b: dict, frame_size: tuple[float, float] | None) -> float:
    """手部两点的距离。知道画面尺寸时按像素算，并带上手部模型给的深度 z。

    归一化坐标里 x 按画面宽、y 按画面高，16:9 画面上 y 的一格比 x 长 1.78 倍，
    同一只手转个方向读数就变。z 和 x 一样按画面宽归一（电脑和手机两边都这样打
    包），乘宽度就是像素。手指朝镜头伸直时，平面上指尖贴着指根，看着像拳；带上
    深度才量得出这段长度。
    """
    if frame_size is None:
        return _distance(a, b)
    width, height = frame_size
    dz = 0.0
    if all(isinstance(point.get("z"), (int, float)) and math.isfinite(point["z"]) for point in (a, b)):
        dz = float(a["z"]) - float(b["z"])
    return math.sqrt(((float(a["x"]) - float(b["x"])) * width) ** 2
                     + ((float(a["y"]) - float(b["y"])) * height) ** 2 + (dz * width) ** 2)


def measure_curl(points: object, frame_size: tuple[float, float] | None = None) -> float | None:
    """Mean fingertip reach in knuckle-distances, or None if unusable.

    Extended fingers read near 2.0 and a closed fist near 1.0.  Every distance
    starts at the wrist, which is what makes the number independent of how the
    hand is rotated and how far away the player is standing.

    ``frame_size`` (宽, 高) 给了就按像素加深度量，见 _reach。2026-10-07 两段
    Kinect 640×360 录制里 1457 只手，新旧读数中位数 1.26/1.28，判断跨过整段
    门槛翻转的只有 2 只，原来量好的握拳门槛照用；变化主要在两头，平面上读到
    0.3~0.5 这种指尖比指根还近一半的拳，带深度后在 0.8~0.9。
    """
    if not isinstance(points, list) or len(points) != HAND_LANDMARK_COUNT:
        return None
    if not all(isinstance(point, dict) for point in points):
        return None
    wrist = points[_HAND_WRIST]
    # Same rule as the pose path: a landmark outside the picture was inferred
    # from the ones inside it, not seen.  See _FRAME_MARGIN.
    if not _in_frame(wrist):
        return None
    ratios = []
    for knuckle_index, tip_index in zip(_FINGER_KNUCKLES, _FINGER_TIPS):
        knuckle = points[knuckle_index]
        tip = points[tip_index]
        if not _in_frame(knuckle) or not _in_frame(tip):
            continue
        base = _reach(wrist, knuckle, frame_size)
        if base <= 1e-6:
            continue
        ratios.append(_reach(wrist, tip, frame_size) / base)
    if len(ratios) < _MIN_FINGERS:
        return None
    return sum(ratios) / len(ratios)


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
    if config["curl_open"] <= config["curl_close"]:
        raise ValueError("手指伸开阈值必须大于手指弯曲阈值，否则握拳状态会抖动")
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
        self.anchor_source = None
        self.spread: float | None = None
        self.curl: float | None = None
        self.grip_source = "none"
        self.output = (0.0, 0.0)
        self.offset = (0.0, 0.0)
        self.tips_seen = 0
        self.reason = "idle"
        self.finger_seen_at: float | None = None

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
        # 手肘允许在画面外：它只是长度基准，而"握没握拳"由指尖相对手腕的位置决定。
        # 两个方向的失败代价也不同——手肘估偏会让比值偏大、误判成"张开"，那只是
        # 不响应；手腕和指尖是猜出来的则会误判成"握拳"，鼠标会一直卡在按下状态。
        # 手举到画面下缘外时手肘出画是常事，为此整只手作废太苛刻。
        elbow = _point(pose_map, f"{hand}_elbow", min_visibility, require_in_frame=False)
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

    def update(self, pose_map: dict, now: float,
               hand_points: list | None = None, hand_state: dict | None = None,
               frame_size: tuple[float, float] | None = None) -> dict:
        config = self.config
        if not config["enabled"]:
            self.reset()
            self.reason = "disabled"
            return self.status()

        hand = str(config["hand"])
        spread, tips = self.measure_spread(pose_map, hand)
        self.spread = spread
        self.tips_seen = tips
        curl = measure_curl(hand_points, frame_size)
        self.curl = curl
        if curl is not None:
            self.finger_seen_at = now
        held = False

        # Both readings shrink as the hand closes, so the gate below is the
        # same shape either way; only the number and its thresholds change.
        # 原厂可靠张合状态优先，其次是手指关节，最后是人体模型的指尖间距。
        if (isinstance(hand_state, dict) and hand_state.get("state") in ("open", "closed")
                and isinstance(hand_state.get("score"), (int, float)) and .6 <= hand_state["score"] <= 1):
            # 原厂已计算的张合状态优先，未知状态仍走下方原有手指/人体判断。
            grip = 0.0 if hand_state["state"] == "closed" else 1.0
            close_at, open_at = .25, .75
            self.grip_source = "kinect"
        elif curl is not None:
            grip = curl
            close_at = float(config["curl_close"])
            open_at = float(config["curl_open"])
            self.grip_source = "hand"
        elif (self.finger_seen_at is not None
              and 0.0 <= now - self.finger_seen_at <= FINGER_GAP_HOLD_S):
            # 手指读数只断了一下：维持上一帧的状态，见 FINGER_GAP_HOLD_S。
            grip, held = None, True
            self.grip_source = "hand"
        elif (self.finger_seen_at is not None
              and 0.0 <= now - self.finger_seen_at <= FINGER_SOURCE_S):
            # 手部模型在工作，只是这会儿认不出手：当看不到手，不换指尖间距。
            grip = None
            self.grip_source = "none"
        elif spread is not None:
            grip = spread
            close_at = float(config["fist_close"])
            open_at = float(config["fist_open"])
            self.grip_source = "pose"
        else:
            grip = None
            self.grip_source = "none"

        if grip is None and not held:
            # Losing sight of the hand must release, not freeze: a stuck
            # engagement would keep driving the pointer from a stale anchor.
            if self.engaged:
                self._release("lost")
            else:
                self.reason = "lost"
            return self.status()

        was_engaged = self.engaged
        if held:
            pass
        elif self.engaged:
            if grip > open_at:
                self._release("opened")
                return self.status()
        elif grip < close_at:
            self.engaged = True

        if not self.engaged:
            self.output = (0.0, 0.0)
            self.offset = (0.0, 0.0)
            self.reason = "open"
            return self.status()

        wrist = _point(pose_map, f"{hand}_wrist", float(config["min_visibility"]))
        elbow = _point(pose_map, f"{hand}_elbow", float(config["min_visibility"]),
                       require_in_frame=False)
        if wrist is None or elbow is None:
            self._release("lost")
            return self.status()
        forearm = max(1e-6, _distance(elbow, wrist))

        source = wrist.get("position_source", "image")
        if not was_engaged or self.anchor is None or source != self.anchor_source:
            self.anchor = (float(wrist["x"]), float(wrist["y"]))
            self.anchor_source = source
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
        self.anchor_source = None
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
            # 来源：原厂张合 kinect、手指关节 hand、人体指尖 pose 或无有效数据 none。
            "grip_source": str(self.grip_source),
            "curl": None if self.curl is None else round(float(self.curl), 4),
            "tips_seen": int(self.tips_seen),
            "offset_x": self.offset[0],
            "offset_y": self.offset[1],
            "output_x": self.output[0],
            "output_y": self.output[1],
            "config": dict(self.config),
        }
