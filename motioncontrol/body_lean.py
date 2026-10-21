"""身体走位实验：复用当前帧的模型或可靠深度骨骼，不额外推理。"""
from __future__ import annotations

import copy
import math
import statistics


SOURCES = ("model", "depth")
NAMES = ("left_shoulder", "right_shoulder", "left_hip", "right_hip")
INDICES = (11, 12, 23, 24)
NEUTRAL_SECONDS = 1.0
NEUTRAL_TIMEOUT = 6.0
STALE_SECONDS = .25
FILTER_TAU = .06
SIDE_ON, SIDE_OFF = 6.0, 4.0
BACK_ON, BACK_OFF = 7.0, 5.0


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _unit(vector):
    length = math.sqrt(_dot(vector, vector))
    return tuple(v / length for v in vector) if length > 1e-6 else None


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _points(value, dimensions, *, image=False, depth=False):
    if isinstance(value, list) and len(value) == 33:
        value = {name: value[index] for name, index in zip(NAMES, INDICES)}
    if not isinstance(value, dict):
        return None
    result = []
    try:
        for name in NAMES:
            point = value[name]
            if not isinstance(point, dict):
                return None
            score = float(point.get("score", point.get("visibility", point.get("presence", 0))))
            vector = tuple(float(point[key]) for key in ("x", "y", "z")[:dimensions])
            if not .6 <= score <= 1 or not all(math.isfinite(v) for v in vector):
                return None
            # 模型对照必须保留原始图像点，不能混入深度融合结果。
            if image and point.get("position_source") == "kinect":
                return None
            if depth and vector[2] <= 0:
                return None
            result.append(vector)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return result


def _torso(points):
    up = tuple((points[0][i] + points[1][i] - points[2][i] - points[3][i]) / 2
               for i in range(len(points[0])))
    left = tuple(points[0][i] - points[1][i] for i in range(len(up)))
    return (up, left) if _unit(up) and _unit(left) else None


def _basis(measured):
    """保存中立躯干；左右方向按人物具名关节确定，兼容镜像画面。"""
    up, left = measured
    up = _unit(up)
    left = _unit(tuple(left[i] - _dot(left, up) * up[i] for i in range(len(up))))
    if left is None:
        return None
    if len(up) == 2:
        return {"up": list(up), "axis": list(left)}
    back = _unit(_cross(left, up))
    if back is None or abs(back[2]) < .25:
        return None
    # 两种骨骼坐标的上下轴不同，统一以远离相机的方向作为后倾。
    if back[2] < 0:
        back = tuple(-v for v in back)
    return {"up": list(up), "axis": list(back)}


def _angle(measured, basis):
    if measured is None or basis is None:
        return None
    up = measured[0]
    return math.degrees(math.atan2(_dot(up, basis["axis"]), _dot(up, basis["up"])))


class BodyLeanController:
    """两路分别量中立姿态；只有手选来源能产生方向动作。"""

    def __init__(self, neutral=None):
        self._neutral = {}
        self._capture = {source: {axis: None for axis in ("side", "back")} for source in SOURCES}
        self._states = {}
        self._source = "model"
        self._strafe = self._back = self._compare = False
        self.load_neutral(neutral)
        self.reset_tracking()

    def neutral_snapshot(self):
        return {"version": 1, "sources": copy.deepcopy(self._neutral)}

    def load_neutral(self, neutral):
        result = {}
        if isinstance(neutral, dict) and neutral.get("version") == 1:
            for source, axes in (neutral.get("sources") if isinstance(neutral.get("sources"), dict) else {}).items():
                if source not in SOURCES or not isinstance(axes, dict):
                    continue
                accepted = {}
                for name, dimensions in (("side", 2), ("back", 3)):
                    basis = axes.get(name)
                    if not isinstance(basis, dict):
                        continue
                    try:
                        up, axis = (tuple(float(v) for v in basis[key]) for key in ("up", "axis"))
                        if (len(up) != dimensions or len(axis) != dimensions
                                or not all(math.isfinite(v) for v in up + axis)
                                or not .99 <= _dot(up, up) <= 1.01
                                or not .99 <= _dot(axis, axis) <= 1.01
                                or abs(_dot(up, axis)) > .01):
                            continue
                        accepted[name] = {"up": list(up), "axis": list(axis)}
                    except (KeyError, TypeError, ValueError, OverflowError):
                        continue
                if accepted:
                    result[source] = accepted
        self._neutral = result

    def reset_tracking(self):
        self._states = {source: {"valid": False, "reason": "等待身体数据", "side_valid": False,
            "back_valid": False, "side_deg": None, "back_deg": None, "active": [],
            "last_at": None, "remaining_s": NEUTRAL_SECONDS} for source in SOURCES}
        for source in SOURCES:
            for capture in self._capture[source].values():
                if capture is not None:
                    capture["samples"].clear()

    def start_neutral(self, now=None, source=None):
        for name in SOURCES if source is None else (source,):
            if name not in SOURCES:
                raise ValueError("身体数据来源无效")
            self._capture[name] = {axis: {"started": now, "samples": []} for axis in ("side", "back")}
            self._states[name].update(active=[], side_deg=None, back_deg=None, remaining_s=NEUTRAL_SECONDS)

    @staticmethod
    def _measure(image_pose, world_pose, depth_body, width, height):
        result = {source: {} for source in SOURCES}
        image = _points(image_pose, 2, image=True)
        if image and width > 0 and height > 0:
            result["model"]["side"] = _torso([(p[0] * width, p[1] * height) for p in image])
        world = _points(world_pose, 3)
        if world:
            result["model"]["back"] = _torso(world)
        points = _points(depth_body.get("points") if isinstance(depth_body, dict) else None, 3, depth=True)
        if points:
            # 原生深度使用米制空间；侧倾也投影到相机正面，避免图像比例混入深度。
            result["depth"]["side"] = _torso([(p[0], -p[1]) for p in points])
            result["depth"]["back"] = _torso(points)
        return result

    def update(self, image_pose, world_pose, depth_body, width, height, now, *,
               source="model", strafe=False, back=False, compare=False):
        if source not in SOURCES:
            raise ValueError("身体数据来源无效")
        if not isinstance(now, (int, float)) or not math.isfinite(now):
            self.reset_tracking()
            return set()
        if source != self._source or (bool(strafe), bool(back)) != (self._strafe, self._back):
            self.reset_tracking()
        self._source, self._strafe, self._back, self._compare = source, bool(strafe), bool(back), bool(compare)
        capturing = any(value is not None for axes in self._capture.values() for value in axes.values())
        if not (strafe or back or compare or capturing):
            for state in self._states.values():
                state.update(valid=False, side_valid=False, back_valid=False, side_deg=None,
                             back_deg=None, active=[], last_at=None, reason="身体走位未开启")
            return set()
        measurements = self._measure(image_pose, world_pose, depth_body, width, height)
        needed = (["side"] if strafe else []) + (["back"] if back else [])
        for name in SOURCES:
            state, measured = self._states[name], measurements[name]
            previous_at = state["last_at"]
            fresh = previous_at is None or not 0 < now - previous_at <= STALE_SECONDS
            if fresh:
                state.update(side_deg=None, back_deg=None, active=[])
                for capture in self._capture[name].values():
                    if capture is not None:
                        capture["samples"].clear()
            state["last_at"] = now
            required = needed or (["side", "back"] if compare else [])
            selected = name == source or compare
            dt = min(.1, now - previous_at) if previous_at is not None and now > previous_at else 0
            active = set(state["active"])
            fresh_axes = set()
            timed_out = False
            for axis in ("side", "back"):
                valid = measured.get(axis) is not None and _basis(measured[axis]) is not None
                if not state[axis + "_valid"]:
                    fresh_axes.add(axis)
                state[axis + "_valid"] = valid
                neutral = self._neutral.get(name, {})
                if selected and axis in required and axis not in neutral and valid and self._capture[name][axis] is None:
                    self._capture[name][axis] = {"started": now, "samples": []}
                capture = self._capture[name][axis]
                if capture is not None:
                    if capture["started"] is None:
                        capture["started"] = now
                    if now - capture["started"] > NEUTRAL_TIMEOUT:
                        self._capture[name][axis] = None
                        timed_out = True
                        fresh_axes.add(axis)
                    else:
                        self._collect(name, axis, measured.get(axis), now)
                neutral = self._neutral.get(name, {})
                value = (_angle(measured.get(axis), neutral.get(axis))
                         if valid and self._capture[name][axis] is None else None)
                old = state[axis + "_deg"]
                state[axis + "_deg"] = (old + (1 - math.exp(-dt / FILTER_TAU)) * (value - old)
                                         if value is not None and old is not None and dt > 0 else value)
            side, backward = state["side_deg"], state["back_deg"]
            side_ready = not fresh and "side" not in fresh_axes and side is not None
            back_ready = not fresh and "back" not in fresh_axes and backward is not None
            left_on = side_ready and side >= (SIDE_OFF if "lean_left" in active else SIDE_ON)
            right_on = side_ready and side <= -(SIDE_OFF if "lean_right" in active else SIDE_ON)
            back_on = back_ready and backward >= (BACK_OFF if "lean_back" in active else BACK_ON)
            state["active"] = (["lean_left"] if left_on else ["lean_right"] if right_on else []) + (["lean_back"] if back_on else [])
            state["valid"] = bool(required) and all(state[axis + "_valid"] for axis in required)
            missing = [axis for axis in required if not state[axis + "_valid"]]
            pending = [capture for capture in self._capture[name].values() if capture is not None]
            state["remaining_s"] = max((max(0., NEUTRAL_SECONDS - (now - capture["samples"][0][0]))
                                        if capture["samples"] else NEUTRAL_SECONDS for capture in pending), default=0.)
            state["reason"] = ("未取得与当前人物匹配的可靠深度骨骼" if missing and name == "depth" else
                               "后倾需要可靠的三维肩髋；侧倾可单独使用" if missing == ["back"] else
                               "需要可靠的双肩和双髋" if missing else
                               "自然站直，保持约一秒" if pending else "")
            if timed_out:
                state["reason"] = "站姿采集超时，保留上次有效基准"
        return {action for action in self._states[source]["active"]
                if (strafe and action in ("lean_left", "lean_right")) or (back and action == "lean_back")}

    def _collect(self, source, axis, measured, now):
        samples = self._capture[source][axis]["samples"]
        basis = _basis(measured) if measured is not None else None
        if basis is None:
            samples.clear()
            return
        if samples and abs(_angle(measured, samples[0][1])) > 2.0:
            samples.clear()
        samples.append((now, basis))
        elapsed = now - samples[0][0]
        if elapsed < NEUTRAL_SECONDS or len(samples) < 8:
            return
        up = _unit(tuple(statistics.median(sample[1]["up"][i] for sample in samples)
                         for i in range(len(basis["up"]))))
        direction = tuple(statistics.median(sample[1]["axis"][i] for sample in samples) for i in range(len(up)))
        direction = _unit(tuple(direction[i] - _dot(direction, up) * up[i] for i in range(len(up))))
        self._neutral.setdefault(source, {})[axis] = {"up": list(up), "axis": list(direction)}
        self._capture[source][axis] = None
        self._states[source][axis + "_deg"] = None

    def status(self, now):
        states = copy.deepcopy(self._states)
        needed = (["side"] if self._strafe else []) + (["back"] if self._back else [])
        for source, state in states.items():
            last_at = state.pop("last_at")
            if last_at is None or now - last_at > STALE_SECONDS:
                state.update(valid=False, side_valid=False, back_valid=False, side_deg=None,
                             back_deg=None, active=[], reason=("身体走位未开启" if state["reason"] == "身体走位未开启"
                                                              else "等待当前帧身体数据"))
            neutral = self._neutral.get(source, {})
            state.update(calibrating=any(value is not None for value in self._capture[source].values()),
                         side_calibrating=self._capture[source]["side"] is not None,
                         back_calibrating=self._capture[source]["back"] is not None,
                         side_calibrated="side" in neutral, back_calibrated="back" in neutral,
                         calibrated=bool(neutral) and all(axis in neutral for axis in needed))
        active = [action for action in states[self._source]["active"]
                  if (self._strafe and action in ("lean_left", "lean_right")) or (self._back and action == "lean_back")]
        return {"source": self._source, "sources": states, "active": active,
                "thresholds": {"side_on_deg": SIDE_ON, "side_off_deg": SIDE_OFF,
                               "back_on_deg": BACK_ON, "back_off_deg": BACK_OFF}}
