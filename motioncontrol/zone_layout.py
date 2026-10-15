"""区域编辑的身体比例参考；估算点只用于画图和布局，不作为动作识别点。"""
from __future__ import annotations

import copy
import math

from motioncontrol.zone_fit import body_frame
from motioncontrol.body_reference import reach_sectors


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


def editor_reference(pose, width, height, fit, saved=None):
    source_pose = copy.deepcopy(pose or {})
    anchor = body_anchor(source_pose, width, height)
    if anchor is None:
        anchor = {"x": .5, "y": .52, "ux": .20, "uy": .23}
    result = copy.deepcopy(saved["pose"]) if saved else {}
    return {"anchor": anchor, "pose": result, "source": "measured" if saved else "none",
            "ready": saved is not None, "ranges": reach_sectors(saved) if saved else [],
            "recording": copy.deepcopy(saved.get("source")) if saved else None,
            "measured_at_unix": fit.get("measured_at_unix")}


def layout_reference_pose(pose, anchor, width, height):
    # 仅为无人时提供可编辑的默认框位置，不绘制或输入识别。
    if body_frame(pose or {}, width, height):
        return pose
    result = {}
    for side, sign in (("left", 1), ("right", -1)):
        for part, x, y in (("ear", .16, -1.44), ("shoulder", .40, -1),
                           ("hip", .24, 0), ("ankle", .26, 1.68)):
            result[f"{side}_{part}"] = {"x": anchor["x"] + sign*x*anchor["ux"],
                                       "y": anchor["y"] + y*anchor["uy"], "score": .5}
    result["nose"] = {"x": anchor["x"], "y": anchor["y"] - 1.45*anchor["uy"], "score": .5}
    return result
