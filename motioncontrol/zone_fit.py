"""量身：跟随身体的那几个框放在哪、多大，握拳怎么算握紧，照这个人自己量出来。

跟随区域原来全是写死的比例：手区下沿在胯上方 0.40 个躯干，头顶区要鼻子抬高
0.16 个躯干，脚区在胯外 0.64 个躯干……这是照一个人定的。个子矮、手短的够不着，
动作幅度大的站着不动都会碰到。

这里让人站到平时玩的位置，把真正会做的动作做一遍——两手往两边挥、左右脚各往
外伸一下、原地跳一下——量出他站着不动时手脚在哪、做动作时到哪，框就放在两者
之间：站着碰不到，做动作一定进得去。

单位和 control_kernel._compute_body_zones 完全一样：横向是 torso_px / 画面宽，
纵向是 torso_px / 画面高（torso_px 是肩中点到胯中点的距离乘画面对角线）。所以
一套数在远近、横竖屏下都成立。参考系只在 body_frame 里算一次，量的和用的是同
一把尺子。

最后一步是握拳：开着握拳控制的那只手先张开、再握紧，两个阈值放在两次读数之间。
设置里原来要人自己看读数拖滑块，没人会去弄。两只手共用一对阈值，所以取对两只
手都成立的那一段。

量的过程每一帧都要跑（跳起来最高那一下只有一两帧），所以放在内核这边，网页只
管提示人做什么。
"""

from __future__ import annotations

import copy
import math
import statistics
import time
from typing import Any

ZONE_FIT_VERSION = 1

# 手区：下沿在胯上方多高（bottom），里沿离头中心多远（inset）。外沿和上沿是画面边。
# 头顶区：鼻子要比站着时高出多少才算跳（rise）。
# 脚区：中心在同侧胯外多远（out）、离地多高（lift），半宽（half_w）、半高（half_h）。
DEFAULT_ZONE_FIT: dict[str, dict[str, float]] = {
    "leftHand": {"bottom": 0.40, "inset": 0.45},
    "rightHand": {"bottom": 0.40, "inset": 0.45},
    "headJump": {"rise": 0.16},
    "leftFoot": {"out": 0.64, "half_w": 0.50, "lift": 0.335, "half_h": 0.30},
    "rightFoot": {"out": 0.64, "half_w": 0.50, "lift": 0.335, "half_h": 0.30},
}

# 量出来的数再怎么离谱也不出这个范围。inset 的下限让手区和头顶区之间留一道缝，
# bottom 的下限让垂着的手碰不到手区。
LIMITS = {
    "bottom": (0.20, 1.10),
    "inset": (0.30, 0.85),
    "rise": (0.10, 0.40),
    "out": (0.20, 1.40),
    "half_w": (0.20, 0.70),
    "lift": (0.10, 0.70),
    "half_h": (0.15, 0.50),
}

# 头顶区的半高，对应 _compute_body_zones 里 0.28 个躯干的框高，不参与量。
HEAD_JUMP_HALF_H = 0.14

# 量的顺序，也是教学里提示的顺序。握拳那几步接在后面，只给开着握拳控制的手：
# leftOpen、leftFist、rightOpen、rightFist。
BODY_PHASES = ("stand", "hands", "leftFoot", "rightFoot", "jump")
GRIP_STEPS = ("Open", "Fist")

# 握拳读数：手机传了手指关节时是弯曲度（伸直约 2.0、握紧约 1.0），否则是指尖
# 张开度（前臂长的几分之几）。两种数量级不同，各有各的一对阈值和门槛。键名就是
# hand_mouse_control.DEFAULT_CONFIG 里的。
GRIP_KEYS = {"hand": ("curl_close", "curl_open"), "pose": ("fist_close", "fist_open")}
GRIP_STEADY = {"hand": 0.20, "pose": 0.06}   # 稳住的这段里读数最多差多少
GRIP_GAP_MIN = {"hand": 0.25, "pose": 0.06}  # 握紧要比张开小这么多才算真握了
GRIP_REACT_S = 0.8     # 提示出来之后先等人反应过来
GRIP_HOLD_S = 0.8      # 稳住多久算数
GRIP_CLOSE_AT = 0.40   # 握拳阈值：从握紧的读数往张开走 40%
GRIP_OPEN_AT = 0.60    # 松开阈值：走到 60%，中间留一段不来回跳

STAND_S = 1.2           # 站定多久取基准
SETTLE_S = 0.6          # 做到之后再收一会儿，峰值才收全
PHASE_TIMEOUT_S = 15.0  # 一项迟迟做不到就跳过，那一项用原来的
LOST_ISSUE_S = 1.5      # 看不到人（或看不到脚、看不到手）多久才提示

# 点下「站好了，开始」后留给人走回镜头前的时间。准备阶段不采姿态，
# 所以人从电脑前走开、回到正常游戏位置的这几秒不会混进站定基准。
ZONE_FIT_PREPARE_S = 3.0

# 什么算"真做了这个动作"，而不是站着晃了一下。单位同上。
HAND_RAISE_MIN = 0.35
FOOT_REACH_MIN = 0.20
JUMP_RISE_MIN = 0.14

# 框放在"站着"和"做动作"之间的哪儿。
HAND_BOTTOM_AT = 0.65   # 手区下沿：从垂手的高度往上走到挥手高度的 65%
HAND_INSET_AT = 0.45    # 手区里沿：挥手时手离头中心距离的 45%
FOOT_INNER_AT = 0.45    # 脚区里沿：从站立到伸脚最远的 45%
FOOT_OUTER_PAST = 0.35  # 脚区外沿：伸脚最远处再往外留出伸脚距离的 35%
JUMP_RISE_AT = 0.60     # 头顶区下沿：跳起高度的 60%。下限 0.10 个躯干（约 8 厘米），踮脚够不着

PEAK_SHARE = 0.30       # 取做动作时最高/最远的那三成帧的中位数当"到了哪"
MIN_POINT_SCORE = 0.42  # 和 _point_in_rect 用的一样：判定用不上的点，量也不用


def _finite(value: Any, default: float = math.nan) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _score(point: dict | None) -> float:
    if not isinstance(point, dict):
        return 0.0
    return _finite(point.get("score", point.get("visibility", point.get("presence", 0.0))), 0.0)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _midpoint(a: dict, b: dict) -> dict:
    return {"x": (a["x"] + b["x"]) / 2.0, "y": (a["y"] + b["y"]) / 2.0}


def body_frame(pose_map: dict[str, dict] | None, width: int, height: int) -> dict | None:
    """跟随区域的参考系：胯、肩、头中心、左右朝向和两个方向上的尺子。

    _compute_body_zones 和量身的过程都从这里取，规则只写一份。
    """
    pose_map = pose_map or {}
    ls, rs = pose_map.get("left_shoulder"), pose_map.get("right_shoulder")
    lh, rh = pose_map.get("left_hip"), pose_map.get("right_hip")
    if not all((ls, rs, lh, rh)) or min(_score(ls), _score(rs), _score(lh), _score(rh)) < 0.4:
        return None
    iw, ih = max(1, int(width)), max(1, int(height))
    shoulder, hip = _midpoint(ls, rs), _midpoint(lh, rh)
    torso_px = math.hypot(shoulder["x"] - hip["x"], shoulder["y"] - hip["y"]) * math.hypot(iw, ih)
    if not math.isfinite(torso_px) or torso_px < 35:
        return None
    left_dir = 1.0 if (ls["x"] - shoulder["x"]) >= 0 else -1.0
    right_dir = 1.0 if (rs["x"] - shoulder["x"]) >= 0 else -left_dir
    le, re, nose = pose_map.get("left_ear"), pose_map.get("right_ear"), pose_map.get("nose")
    head_center = None
    if le and re and min(_score(le), _score(re)) >= 0.35:
        head_center = _midpoint(le, re)
    elif nose and _score(nose) >= 0.35:
        head_center = nose
    return {
        "shoulder": shoulder, "hip": hip, "torso_px": torso_px,
        "ux": torso_px / iw, "uy": torso_px / ih,
        "left_dir": left_dir, "right_dir": right_dir,
        "head_center": head_center,
        "nose": nose if nose and _score(nose) >= 0.35 else None,
    }


def normalize_zone_fit(raw: Any) -> dict:
    """存盘的那份整理成能直接用的样子。缺的、坏的一律回到默认值，不报错：
    这份数丢了最坏也就是框回到默认大小，不该让程序起不来。"""
    zones = copy.deepcopy(DEFAULT_ZONE_FIT)
    stamps: dict[str, float | None] = {"measured_at_unix": None, "grip_measured_at_unix": None}
    if isinstance(raw, dict) and raw.get("version") == ZONE_FIT_VERSION:
        source = raw.get("zones") if isinstance(raw.get("zones"), dict) else {}
        for zone, params in zones.items():
            given = source.get(zone) if isinstance(source.get(zone), dict) else {}
            for key in params:
                value = _finite(given.get(key))
                if math.isfinite(value):
                    params[key] = round(_clamp(value, *LIMITS[key]), 4)
        for key in stamps:
            stamp = _finite(raw.get(key))
            stamps[key] = stamp if math.isfinite(stamp) else None
    # grip_measured_at_unix：握拳阈值是哪天量的。阈值本身存在握拳控制那份设置里，
    # 这里只记"量过没有"，界面靠它决定要不要提示「量一下握拳」。
    return {"version": ZONE_FIT_VERSION, **stamps, "zones": zones}


def is_default(fit: dict) -> bool:
    return fit.get("zones") == DEFAULT_ZONE_FIT


def _peak(samples: list[tuple[float, ...]], key: int) -> list[tuple[float, ...]]:
    """按第 key 项排序，取最大的那三成。"""
    ranked = sorted(samples, key=lambda item: item[key], reverse=True)
    return ranked[:max(1, int(math.ceil(len(ranked) * PEAK_SHARE)))]


def fit_hand(rest: tuple[float, float], samples: list[tuple[float, float]]) -> dict | None:
    """rest 和 samples 都是 (高出胯多少, 离头中心往外多远)。"""
    rest_y = rest[0]
    raised = [item for item in samples if item[0] - rest_y >= HAND_RAISE_MIN]
    if not raised:
        return None
    peak = _peak(raised, 0)
    peak_y = statistics.median(item[0] for item in peak)
    peak_dx = statistics.median(item[1] for item in peak)
    bottom = min(rest_y + HAND_BOTTOM_AT * (peak_y - rest_y), peak_y - 0.05)
    return {
        "bottom": round(_clamp(bottom, *LIMITS["bottom"]), 4),
        "inset": round(_clamp(HAND_INSET_AT * peak_dx, *LIMITS["inset"]), 4),
    }


def fit_foot(rest_out: float, samples: list[tuple[float, float]]) -> dict | None:
    """samples 是 (往外伸了多远, 离地多高)，都从同侧胯和站着那只脚量。"""
    reached = [item for item in samples if item[0] - rest_out >= FOOT_REACH_MIN]
    if not reached:
        return None
    peak = _peak(reached, 0)
    peak_out = statistics.median(item[0] for item in peak)
    peak_lift = max(0.0, statistics.median(item[1] for item in peak))
    reach = peak_out - rest_out
    inner = rest_out + FOOT_INNER_AT * reach
    outer = peak_out + FOOT_OUTER_PAST * reach
    half_w = _clamp((outer - inner) / 2.0, *LIMITS["half_w"])
    out = _clamp((inner + outer) / 2.0, *LIMITS["out"])
    # 夹过之后里沿可能又贴回站着的脚上，往外推回去。
    if out - half_w < rest_out + 0.10:
        out = _clamp(rest_out + 0.10 + half_w, *LIMITS["out"])
    # 高度一般不用量：默认那一段（离地 0.035 到 0.635）已经够宽。只在踢得特别
    # 高、或者几乎贴地伸出去的时候撑开。
    low = min(0.035, 0.5 * peak_lift)
    high = max(0.635, peak_lift + 0.15)
    return {
        "out": round(out, 4),
        "half_w": round(half_w, 4),
        "lift": round(_clamp((low + high) / 2.0, *LIMITS["lift"]), 4),
        "half_h": round(_clamp((high - low) / 2.0, *LIMITS["half_h"]), 4),
    }


def fit_jump(rises: list[float]) -> dict | None:
    """rises 是鼻子比站着时高出多少。取最高那一下：跳是一瞬间的事。"""
    best = max(rises, default=0.0)
    if best < JUMP_RISE_MIN:
        return None
    return {"rise": round(_clamp(JUMP_RISE_AT * best, *LIMITS["rise"]), 4)}


def fit_grip(readings: dict[str, tuple[str, float, float]]) -> dict:
    """readings 是 {手: (读数种类, 张开时, 握紧时)}，给出要改的那几个阈值。

    两只手共用一对阈值：握紧取两只手里大的那个、张开取小的那个，阈值放在这一段
    里——对两只手都成立。这一段太窄（两只手差太多）就不改，免得改完哪只都不准。
    """
    updates: dict[str, float] = {}
    for kind, (close_key, open_key) in GRIP_KEYS.items():
        pairs = [(opened, fisted) for got, opened, fisted in readings.values() if got == kind]
        if not pairs:
            continue
        fisted = max(item[1] for item in pairs)
        opened = min(item[0] for item in pairs)
        gap = opened - fisted
        if gap < GRIP_GAP_MIN[kind]:
            continue
        updates[close_key] = round(fisted + GRIP_CLOSE_AT * gap, 4)
        updates[open_key] = round(fisted + GRIP_OPEN_AT * gap, 4)
    return updates


class ZoneFitSession:
    """一次量身。站定 → 挥手 → 左脚 → 右脚 → 跳 → 握拳，一项一项往下走。

    每一项做到了、再收半秒峰值就进下一项；迟迟做不到就跳过，那一项用原来的。
    全走完 result() 给出新的区域，grip_updates() 给出新的握拳阈值，由内核装上
    并存盘。
    """

    def __init__(self, current: dict, now: float, *, grip_hands: tuple[str, ...] = (),
                 body: bool = True, prepare_s: float = 0.0) -> None:
        """body=False 是「只量握拳」：换了只手、或者握拳认不准时用，不用再挥手跳。"""
        self.base = normalize_zone_fit(current)
        self.grip_hands = tuple(hand for hand in ("left", "right") if hand in grip_hands)
        self.phases = (BODY_PHASES if body else ()) + tuple(
            f"{hand}{step}" for hand in self.grip_hands for step in GRIP_STEPS)
        if not self.phases:
            raise ValueError("没有要量的：握拳控制关着，又只量握拳")
        self.phase_index = 0
        self.prepare_s = max(0.0, float(prepare_s))
        self.prepare_until = now + self.prepare_s
        self.phase_started = now
        self.reached_at: float | None = None
        self.state = "preparing" if self.prepare_s > 0.0 else "measuring"
        self.issue = ""
        self.lost_since: float | None = None
        self.measured: list[str] = []
        self.skipped: list[str] = []
        self.values: dict[str, dict] = {}
        self.rest: dict[str, Any] = {}
        self._stand: list[dict] = []
        self._hands: dict[str, list[tuple[float, float]]] = {"leftHand": [], "rightHand": []}
        self._feet: list[tuple[float, float]] = []
        self._rises: list[float] = []
        # 鼻子的高度和尺子。跳之前那一小段拿来当"站着"的基准：人可能量到一半
        # 往前挪了一步，用开头站定时的数就不准了。
        self._recent_nose: list[tuple[float, float, float]] = []
        self._jump_base: tuple[float, float] | None = None
        self._grip: list[tuple[float, str, float]] = []
        self._grip_open: dict[str, tuple[str, float]] = {}
        self.grips: dict[str, tuple[str, float, float]] = {}
        # 内核装上了哪几个握拳阈值（量完之后由内核填）。
        self.applied_grip: dict[str, float] = {}

    @property
    def phase(self) -> str:
        return self.phases[self.phase_index] if self.state == "measuring" else self.state

    @property
    def active(self) -> bool:
        return self.state in {"preparing", "measuring"}

    @property
    def measuring(self) -> bool:
        return self.state == "measuring"

    def _start_measuring_if_ready(self, now: float) -> None:
        if self.state == "preparing" and now >= self.prepare_until:
            self.state = "measuring"
            self.phase_started = now

    # ---------- 每一帧 ----------

    def update(self, pose_map: dict[str, dict] | None, width: int, height: int, now: float,
               grips: dict[str, dict] | None = None) -> None:
        """grips 是握拳控制那边这一帧的读数：{手: {"curl": …, "spread": …}}。"""
        if not self.active:
            return
        self._start_measuring_if_ready(now)
        if not self.measuring:
            return
        frame = body_frame(pose_map, width, height)
        if frame is not None and frame["nose"] is not None:
            self._recent_nose.append((now, frame["nose"]["y"], frame["uy"]))
            self._recent_nose = [item for item in self._recent_nose if now - item[0] <= 2.0]
        phase = self.phases[self.phase_index]
        grip = self._grip_reading(phase, grips) if phase not in BODY_PHASES else None
        seen = grip is not None if phase not in BODY_PHASES else (
            frame is not None and self._phase_can_see(phase, pose_map or {}, frame))
        if not seen:
            self.lost_since = self.lost_since if self.lost_since is not None else now
            if now - self.lost_since >= LOST_ISSUE_S:
                if phase not in BODY_PHASES:
                    self.issue = "hand_hidden"
                elif frame is not None and phase in ("leftFoot", "rightFoot"):
                    self.issue = "feet_hidden"
                else:
                    self.issue = "not_visible"
        else:
            self.lost_since = None
            self.issue = ""
            if phase in BODY_PHASES:
                getattr(self, f"_measure_{phase}")(pose_map, frame, now)
            else:
                self._measure_grip(phase, grip, now)
        if not self.active or self.phases[self.phase_index] != phase:
            return
        settle = SETTLE_S if phase in ("hands", "leftFoot", "rightFoot", "jump") else 0.0
        if self.reached_at is not None and now - self.reached_at >= settle:
            self._finish_phase(now)
        elif phase != "stand" and now - self.phase_started >= PHASE_TIMEOUT_S:
            self.skip(now)

    @staticmethod
    def _point(pose_map: dict, name: str) -> dict | None:
        point = pose_map.get(name)
        return point if point and _score(point) >= MIN_POINT_SCORE else None

    def _ankle(self, pose_map: dict, side: str) -> dict | None:
        return self._point(pose_map, f"{side}_ankle") or self._point(pose_map, f"{side}_heel")

    def _phase_can_see(self, phase: str, pose_map: dict, frame: dict) -> bool:
        if phase == "stand":
            return frame["head_center"] is not None and frame["nose"] is not None
        if phase == "hands":
            return frame["head_center"] is not None
        if phase in ("leftFoot", "rightFoot"):
            return self._ankle(pose_map, "left") is not None and self._ankle(pose_map, "right") is not None
        return frame["nose"] is not None

    def _hand_sample(self, pose_map: dict, frame: dict, zone: str) -> tuple[float, float] | None:
        side = "left" if zone == "leftHand" else "right"
        wrist = self._point(pose_map, f"{side}_wrist")
        head = frame["head_center"]
        if wrist is None or head is None:
            return None
        direction = frame["left_dir"] if side == "left" else frame["right_dir"]
        return (
            (frame["hip"]["y"] - wrist["y"]) / frame["uy"],
            direction * (wrist["x"] - head["x"]) / frame["ux"],
        )

    def _foot_sample(self, pose_map: dict, frame: dict, zone: str) -> tuple[float, float] | None:
        side = "left" if zone == "leftFoot" else "right"
        ankle = self._ankle(pose_map, side)
        feet = [point for point in (self._ankle(pose_map, "left"), self._ankle(pose_map, "right")) if point]
        hip = pose_map.get(f"{side}_hip")
        if ankle is None or not feet or not hip:
            return None
        floor_y = max(point["y"] for point in feet)
        direction = frame["left_dir"] if side == "left" else frame["right_dir"]
        return (
            direction * (ankle["x"] - hip["x"]) / frame["ux"],
            (floor_y - ankle["y"]) / frame["uy"],
        )

    def _measure_stand(self, pose_map: dict, frame: dict, now: float) -> None:
        sample = {"at": now, "nose_y": frame["nose"]["y"], "uy": frame["uy"]}
        for zone in ("leftHand", "rightHand"):
            sample[zone] = self._hand_sample(pose_map, frame, zone)
        for zone in ("leftFoot", "rightFoot"):
            sample[zone] = self._foot_sample(pose_map, frame, zone)
        self._stand.append(sample)
        if now - self._stand[0]["at"] >= STAND_S and len(self._stand) >= 5:
            self.reached_at = now

    def _measure_hands(self, pose_map: dict, frame: dict, now: float) -> None:
        for zone in ("leftHand", "rightHand"):
            sample = self._hand_sample(pose_map, frame, zone)
            if sample is not None:
                self._hands[zone].append(sample)
        if self.reached_at is None and all(self.hand_reached(zone) for zone in self._hands):
            self.reached_at = now

    def hand_reached(self, zone: str) -> bool:
        rest = self.rest.get(zone)
        if rest is None:
            return False
        return any(item[0] - rest[0] >= HAND_RAISE_MIN for item in self._hands[zone])

    def _measure_leftFoot(self, pose_map: dict, frame: dict, now: float) -> None:  # noqa: N802 - 名字跟着区域 id
        self._measure_foot("leftFoot", pose_map, frame, now)

    def _measure_rightFoot(self, pose_map: dict, frame: dict, now: float) -> None:  # noqa: N802
        self._measure_foot("rightFoot", pose_map, frame, now)

    def _foot_rest(self, zone: str) -> float | None:
        rest = self.rest.get(zone)
        if rest is not None:
            return rest
        # 站定时没拍到脚：这一项里脚大半时间是站着的，取伸得最近的那一成当站着。
        if len(self._feet) < 5:
            return None
        ranked = sorted(item[0] for item in self._feet)
        return ranked[max(0, len(ranked) // 10)]

    def _measure_foot(self, zone: str, pose_map: dict, frame: dict, now: float) -> None:
        sample = self._foot_sample(pose_map, frame, zone)
        if sample is not None:
            self._feet.append(sample)
        rest = self._foot_rest(zone)
        if self.reached_at is None and rest is not None and any(item[0] - rest >= FOOT_REACH_MIN for item in self._feet):
            self.reached_at = now

    def _measure_jump(self, pose_map: dict, frame: dict, now: float) -> None:
        if self._jump_base is None:
            return
        base_y, uy = self._jump_base
        self._rises.append((base_y - frame["nose"]["y"]) / uy)
        if self.reached_at is None and max(self._rises) >= JUMP_RISE_MIN:
            self.reached_at = now

    @staticmethod
    def _grip_reading(phase: str, grips: dict[str, dict] | None) -> tuple[str, float] | None:
        """这一步那只手这一帧的读数。手指关节优先，和握拳判定用的是同一个。"""
        hand = "left" if phase.startswith("left") else "right"
        reading = (grips or {}).get(hand) or {}
        curl, spread = _finite(reading.get("curl")), _finite(reading.get("spread"))
        if math.isfinite(curl):
            return "hand", curl
        if math.isfinite(spread):
            return "pose", spread
        return None

    def _measure_grip(self, phase: str, reading: tuple[str, float], now: float) -> None:
        """先等人反应过来，再等读数稳住一小段，取那一段的中位数。

        握紧那一步还要比张开时小出一截才算：人还没握下去的时候读数也是稳的。
        """
        kind, value = reading
        self._grip.append((now, kind, value))
        if self.reached_at is not None or now - self.phase_started < GRIP_REACT_S:
            return
        window = [item for item in self._grip if now - item[0] <= GRIP_HOLD_S]
        if (
            len(window) < 5 or now - window[0][0] < GRIP_HOLD_S * 0.75
            or any(item[1] != kind for item in window)
            or max(item[2] for item in window) - min(item[2] for item in window) > GRIP_STEADY[kind]
        ):
            return
        steady = statistics.median(item[2] for item in window)
        hand = "left" if phase.startswith("left") else "right"
        if phase.endswith("Open"):
            self._grip_open[hand] = (kind, steady)
            self.reached_at = now
            return
        opened = self._grip_open.get(hand)
        if opened is None or opened[0] != kind or opened[1] - steady < GRIP_GAP_MIN[kind]:
            return
        self.grips[hand] = (kind, opened[1], steady)
        self.reached_at = now

    # ---------- 一项做完 ----------

    def _finish_phase(self, now: float) -> None:
        phase = self.phases[self.phase_index]
        if phase == "stand":
            self._settle_rest()
        elif phase == "hands":
            for zone, samples in self._hands.items():
                fitted = fit_hand(self.rest[zone], samples) if self.rest.get(zone) else None
                self._record(zone, fitted)
        elif phase in ("leftFoot", "rightFoot"):
            rest = self._foot_rest(phase)
            self._record(phase, fit_foot(rest, self._feet) if rest is not None else None)
        elif phase == "jump":
            self._record("headJump", fit_jump(self._rises))
        elif phase.endswith("Fist"):
            hand = "left" if phase.startswith("left") else "right"
            self._note(f"{hand}Grip", hand in self.grips)
        self._advance(now)

    def _settle_rest(self) -> None:
        def median_of(zone: str) -> tuple[float, float] | None:
            items = [sample[zone] for sample in self._stand if sample.get(zone)]
            if len(items) < 3:
                return None
            return (statistics.median(item[0] for item in items), statistics.median(item[1] for item in items))

        for zone in ("leftHand", "rightHand"):
            self.rest[zone] = median_of(zone)
        for zone in ("leftFoot", "rightFoot"):
            both = median_of(zone)
            self.rest[zone] = both[0] if both else None

    def _note(self, name: str, ok: bool) -> None:
        target = self.measured if ok else self.skipped
        if name not in target:
            target.append(name)

    def _record(self, zone: str, fitted: dict | None) -> None:
        if fitted is not None:
            self.values[zone] = fitted
        self._note(zone, fitted is not None)

    def _advance(self, now: float, steps: int = 1) -> None:
        self.phase_index += steps
        self.phase_started = now
        self.reached_at = None
        self.lost_since = None
        self.issue = ""
        self._feet = []
        self._grip = []
        if self.phase_index >= len(self.phases):
            self.state = "done"
            return
        if self.phases[self.phase_index] == "jump":
            recent = [item for item in self._recent_nose if now - item[0] <= 0.8]
            if len(recent) >= 3:
                self._jump_base = (statistics.median(item[1] for item in recent),
                                   statistics.median(item[2] for item in recent))
            elif self._stand:
                self._jump_base = (statistics.median(item["nose_y"] for item in self._stand),
                                   statistics.median(item["uy"] for item in self._stand))

    def skip(self, now: float) -> None:
        """这一项不量了，用原来的。站定那一步跳不过去：后面全靠它当基准。"""
        if not self.measuring:
            return
        phase = self.phases[self.phase_index]
        if phase == "stand":
            return
        if phase == "hands":
            for zone in ("leftHand", "rightHand"):
                self._record(zone, None)
        elif phase == "jump":
            self._record("headJump", None)
        elif phase in ("leftFoot", "rightFoot"):
            self._record(phase, None)
        else:
            # 张开和握紧是一对，跳过哪一步都是这只手不量了。
            hand = "left" if phase.startswith("left") else "right"
            self.grips.pop(hand, None)
            self._note(f"{hand}Grip", False)
            self._advance(now, 2 if phase.endswith("Open") else 1)
            return
        self._advance(now)

    def cancel(self) -> None:
        if self.active:
            self.state = "cancelled"

    def result(self) -> dict:
        """量完的新区域：量到的换掉，没量到的留原来的。"""
        fit = copy.deepcopy(self.base)
        for zone, values in self.values.items():
            fit["zones"][zone].update(values)
        return fit

    def grip_updates(self) -> dict[str, float]:
        return fit_grip(self.grips)

    def status(self, *, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else now
        self._start_measuring_if_ready(now)
        return {
            "active": self.active,
            "state": self.state,
            "preparing": self.state == "preparing",
            "remaining_s": round(max(0.0, self.prepare_until - now), 2)
            if self.state == "preparing" else 0.0,
            "phase": self.phase,
            "phase_index": self.phase_index,
            "phases": list(self.phases),
            "issue": self.issue,
            "hands_reached": {zone: self.hand_reached(zone) for zone in ("leftHand", "rightHand")},
            "measured": list(self.measured),
            "skipped": list(self.skipped),
        }
