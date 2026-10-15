"""区域编辑的身体比例参考；估算点只用于画图和布局，不作为动作识别点。"""
from __future__ import annotations

import copy
import math

from motioncontrol.zone_fit import body_frame


def normalize_anchor(raw):
    if not isinstance(raw, dict):
        raise ValueError("缺少区域编辑的身体参照")
    out = {}
    for key in ("x", "y", "ux", "uy"):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("身体参照必须是有限数值")
        out[key] = float(value)
    if not (-5 <= out["x"] <= 5 and -5 <= out["y"] <= 5 and
            .005 <= out["ux"] <= 5 and .005 <= out["uy"] <= 5):
        raise ValueError("身体参照超出合理范围")
    return out


def body_anchor(pose, width, height):
    pose = pose or {}
    frame = body_frame(pose, width, height)
    if frame:
        return {"x": frame["hip"]["x"], "y": frame["hip"]["y"],
                "ux": frame["ux"], "uy": frame["uy"]}
    # 只看到上半身时按肩宽估计躯干，不能把画外的模型髋点当作实测。
    ls, rs = pose.get("left_shoulder"), pose.get("right_shoulder")
    def good(point):
        return isinstance(point, dict) and point.get("score", point.get("visibility", 0)) >= .42
    if not (good(ls) and good(rs)):
        return None
    width, height = max(1, width), max(1, height)
    span = math.hypot((ls["x"] - rs["x"]) * width, (ls["y"] - rs["y"]) * height)
    if not math.isfinite(span) or span < 12:
        return None
    torso = span * 1.25
    return {"x": (ls["x"] + rs["x"]) / 2,
            "y": (ls["y"] + rs["y"]) / 2 + torso / height,
            "ux": torso / width, "uy": torso / height}


def relative_rect(rect, anchor):
    anchor = normalize_anchor(anchor)
    return {key: (float(rect[key]) - anchor[axis]) / anchor[unit]
            for key, axis, unit in (("x1", "x", "ux"), ("x2", "x", "ux"),
                                    ("y1", "y", "uy"), ("y2", "y", "uy"))}


def screen_rect(rect, anchor):
    """允许框越过画面边界；跟随时不压缩成零宽，也不让贴边影响身体比例。"""
    return {key: anchor[axis] + float(rect[key]) * anchor[unit]
            for key, axis, unit in (("x1", "x", "ux"), ("x2", "x", "ux"),
                                    ("y1", "y", "uy"), ("y2", "y", "uy"))}


def editor_rect(rect):
    result = {}
    for first, last in (("x1", "x2"), ("y1", "y2")):
        low, high = sorted(max(0.0, min(1.0, float(rect[key]))) for key in (first, last))
        if high - low < .02:
            center = max(.01, min(.99, (low + high) / 2))
            low, high = center - .01, center + .01
        result[first], result[last] = low, high
    return result


def editor_reference(pose, width, height, fit):
    source_pose = copy.deepcopy(pose or {})
    anchor = body_anchor(source_pose, width, height)
    source = "current" if body_frame(source_pose, width, height) else "estimated"
    if anchor is None:
        anchor = {"x": .5, "y": .52, "ux": .20, "uy": .23}
        source = "default"
    ls, rs = source_pose.get("left_shoulder", {}), source_pose.get("right_shoulder", {})
    direction = 1 if ls.get("x", 1) >= rs.get("x", 0) else -1
    def at(x, y):
        return {"x": anchor["x"] + x * anchor["ux"],
                "y": anchor["y"] + y * anchor["uy"], "score": .5, "estimated": True}
    result = {"nose": at(0, -1.45)}
    ranges = []
    for side, sign in (("left", direction), ("right", -direction)):
        for part, x, y in (("ear", .16, -1.44), ("shoulder", .40, -1),
                           ("elbow", .56, -.40), ("wrist", .57, .18),
                           ("hip", .24, 0), ("knee", .25, .85), ("ankle", .26, 1.68)):
            result[f"{side}_{part}"] = at(sign * x, y)
        for kind, x, y, rx, ry in (("hand", .4, -1, 1.15, 1.15), ("foot", .24, 0, 1.1, 1.75)):
            center = at(sign * x, y)
            chain = ("shoulder", "elbow", "wrist") if kind == "hand" else ("hip", "knee", "ankle")
            joints = [source_pose.get(f"{side}_{part}") for part in chain]
            visible = all(isinstance(point, dict) and point.get("score", point.get("visibility", 0)) >= .42
                          and 0 <= point.get("x", -1) <= 1 and 0 <= point.get("y", -1) <= 1 for point in joints)
            if visible:
                length = sum(math.hypot((a["x"] - b["x"]) * width, (a["y"] - b["y"]) * height)
                             for a, b in zip(joints, joints[1:]))
                if length > 10:
                    rx = length / max(1, width) / anchor["ux"]
                    ry = length / max(1, height) / anchor["uy"]
            ranges.append({"side": side, "kind": kind, "cx": center["x"], "cy": center["y"],
                           "rx": rx * anchor["ux"], "ry": ry * anchor["uy"], "source": "estimated",
                           "basis": "landmarks" if visible else "body_proportions"})
    for name, point in source_pose.items():
        if (isinstance(point, dict) and point.get("score", point.get("visibility", 0)) >= .42
                and all(isinstance(point.get(key), (int, float)) and math.isfinite(point[key]) for key in ("x", "y"))
                and 0 <= point["x"] <= 1 and 0 <= point["y"] <= 1):
            result[name] = {**point, "estimated": False}
    return {"anchor": anchor, "pose": result, "source": source, "ranges": ranges,
            "measured_at_unix": fit.get("measured_at_unix")}
