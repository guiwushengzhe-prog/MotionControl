"""实测全身参考：验证、短窗合成、受限侧举扇形和录制文件提取。"""
from __future__ import annotations

import copy
import json
import math
import statistics
import time
from pathlib import Path

REQUIRED_POINTS = ("nose",) + tuple(
    f"{side}_{part}" for side in ("left", "right")
    for part in ("shoulder", "elbow", "wrist", "hip", "knee", "ankle"))


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _point(raw):
    if not isinstance(raw, dict):
        return None
    x, y = raw.get("x"), raw.get("y")
    score = raw.get("score", raw.get("visibility", raw.get("presence", 0)))
    if not all(_finite(v) for v in (x, y, score)) or score < .42 or not (0 <= x <= 1 and 0 <= y <= 1):
        return None
    return {"x": float(x), "y": float(y), "score": float(score)}


def visible_pose(pose):
    """只返回真实可见的画面内点，不夹值、不推断画外关节。"""
    if not isinstance(pose, dict):
        return {}
    result = {}
    for name, raw in pose.items():
        point = _point(raw)
        if point is not None:
            result[name] = {**point, "source": "current"}
    return result


def complete_pose(pose):
    visible = visible_pose(pose)
    return all(name in visible for name in REQUIRED_POINTS)


def _dimensions(width, height):
    return all(_finite(value) and value > 0 for value in (width, height))


def make_reference(samples, width, height, source):
    """完整有效帧逐点取中位数；没有有效全身样本则不生成参考。"""
    if not _dimensions(width, height) or not isinstance(samples, (list, tuple)):
        return None
    poses = [visible_pose(pose) for pose in samples if complete_pose(pose)]
    if not poses:
        return None
    names = set.intersection(*(set(pose) for pose in poses))
    pose = {name: {key: statistics.median(item[name][key] for item in poses)
                   for key in ("x", "y", "score")} for name in sorted(names)}
    for point in pose.values():
        point["source"] = "recorded"
    return {"version": 1, "width": width, "height": height, "pose": pose,
            "source": copy.deepcopy(source) if isinstance(source, dict) else {"kind": str(source)},
            "captured_at": time.time()}


def validate_reference(raw):
    """损坏或不完整的存盘资料返回 None；绝不通过夹坐标修成有效资料。"""
    if (not isinstance(raw, dict) or raw.get("version") != 1
            or not _dimensions(raw.get("width"), raw.get("height"))
            or not isinstance(raw.get("pose"), dict) or not complete_pose(raw["pose"])):
        return None
    if any(_point(point) is None for point in raw["pose"].values()):
        return None
    captured = raw.get("captured_at")
    if not _finite(captured) or captured < 0 or not isinstance(raw.get("source"), dict):
        return None
    result = copy.deepcopy(raw)
    result["pose"] = {name: {**_point(point), "source": "recorded"}
                      for name, point in raw["pose"].items()}
    return result


def reach_sectors(reference):
    """由保存骨链和侧举样本补成外侧扇形；既不画整圆，也不改变实测骨骼。"""
    reference = validate_reference(reference)
    if reference is None:
        return []
    pose, width, height = reference["pose"], reference["width"], reference["height"]
    reaches = reference.get("reaches")
    if not isinstance(reaches, dict):
        return []
    shoulder_delta = pose["left_shoulder"]["x"] - pose["right_shoulder"]["x"]
    if abs(shoulder_delta) * width < 1:
        return []
    result = []
    for side in ("left", "right"):
        outward = (1 if shoulder_delta > 0 else -1) * (1 if side == "left" else -1)
        for kind, suffix, chain, maximum in (
            ("hand", "Hand", ("shoulder", "elbow", "wrist"), 160.),
            ("foot", "Foot", ("hip", "knee", "ankle"), 85.),
        ):
            samples = reaches.get(side + suffix)
            if not isinstance(samples, list):
                continue
            samples = [point for point in samples if isinstance(point, dict)
                       and all(_finite(point.get(axis)) and -5 <= point[axis] <= 5 for axis in ("x", "y"))]
            if len(samples) < 5:
                continue
            joints = [pose[f"{side}_{part}"] for part in chain]
            lengths = [math.hypot((a["x"] - b["x"]) * width, (a["y"] - b["y"]) * height)
                       for a, b in zip(joints, joints[1:])]
            if min(lengths) < 1:
                continue
            center = joints[0]
            samples = [point for point in samples if outward * (point["x"] - center["x"]) * width >= -1.]
            if len(samples) < 5:
                continue

            def angle(point):
                # 零度向下，九十度水平外举；身体内侧的样本不扩到另一侧。
                dx = outward * (point["x"] - center["x"]) * width
                dy = (point["y"] - center["y"]) * height
                return math.degrees(math.atan2(max(0., dx), dy))

            start = min(maximum, max(0., angle(joints[-1])))
            angles = sorted(angle(point) for point in samples)
            peak = angles[math.ceil(.9*len(angles))-1]
            end = min(maximum, max(start, peak) + 10.)
            if end - start < 1:
                continue
            radius = sum(lengths)
            rx, ry = radius / width, radius / height
            steps = max(2, math.ceil((end - start) / 8))
            points = [{"x": center["x"], "y": center["y"]}]
            for index in range(steps + 1):
                theta = math.radians(start + (end - start) * index / steps)
                points.append({"x": center["x"] + outward * rx * math.sin(theta),
                               "y": center["y"] + ry * math.cos(theta)})
            result.append({"side": side, "kind": kind, "cx": center["x"], "cy": center["y"],
                           "rx": rx, "ry": ry, "start_angle": start, "end_angle": end,
                           "points": points})
    return result


def _standing(pose, width, height):
    if not complete_pose(pose):
        return False
    pose = visible_pose(pose)
    if abs(pose["left_ankle"]["y"] - pose["right_ankle"]["y"]) >= .025:
        return False
    for side in ("left", "right"):
        hip, knee, ankle = (pose[f"{side}_{part}"] for part in ("hip", "knee", "ankle"))
        a = ((hip["x"] - knee["x"]) * width, (hip["y"] - knee["y"]) * height)
        b = ((ankle["x"] - knee["x"]) * width, (ankle["y"] - knee["y"]) * height)
        length = math.hypot(*a) * math.hypot(*b)
        if length <= 1e-9:
            return False
        cosine = max(-1., min(1., (a[0] * b[0] + a[1] * b[1]) / length))
        if math.degrees(math.acos(cosine)) <= 155:
            return False
    return True


def _stability(frames):
    diagonal = math.hypot(frames[0]["width"], frames[0]["height"])
    return sum(statistics.pvariance(frame["pose"][name][axis] * frame[dimension] / diagonal
                                    for frame in frames)
               for name in REQUIRED_POINTS for axis, dimension in (("x", "width"), ("y", "height")))


def extract_recording_reference(path):
    """只读指定逐行录制文件，从连续站姿中选位置最稳定的短窗。"""
    path = Path(path)
    best, best_score, run = None, math.inf, []

    def consider():
        nonlocal best, best_score
        if len(run) < 10:
            return
        size = min(15, len(run))
        for start in range(len(run) - size + 1):
            window = run[start:start + size]
            score = _stability(window)
            if score < best_score:
                best, best_score = window, score

    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            for line in stream:
                try:
                    frame = json.loads(line)
                except (ValueError, TypeError):
                    consider(); run = []
                    continue
                if isinstance(frame, dict) and "schema" in frame and "pose" not in frame:
                    continue
                if not isinstance(frame, dict) or "pose" not in frame:
                    consider(); run = []
                    continue
                stamp = frame.get("t", frame.get("sample_at", frame.get("at")))
                valid = (_dimensions(frame.get("width"), frame.get("height")) and _finite(stamp)
                         and _standing(frame["pose"], frame["width"], frame["height"]))
                if not valid:
                    consider(); run = []
                    continue
                frame = {"pose": visible_pose(frame["pose"]), "width": frame["width"],
                         "height": frame["height"], "t": stamp}
                if run and ((frame["width"], frame["height"]) != (run[-1]["width"], run[-1]["height"])
                            or frame["t"] <= run[-1]["t"]):
                    consider(); run = []
                run.append(frame)
        consider()
    except (OSError, UnicodeError):
        return None
    if best is None:
        return None
    return make_reference([frame["pose"] for frame in best], best[0]["width"], best[0]["height"],
                          {"kind": "recording", "path": str(path.resolve()), "t_start": best[0]["t"],
                           "t_end": best[-1]["t"], "frames": len(best)})
