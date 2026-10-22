"""复用当前帧的人体位置，取固定像素窗口；不再执行人体检测，也不逐帧缩放取景。"""
from __future__ import annotations

import math


def person_rect(pose, width, height):
    if not isinstance(pose, dict):
        return None
    torso = [pose.get(name) for name in ("left_shoulder", "right_shoulder", "left_hip", "right_hip")]
    if any(not isinstance(p, dict) or p.get("score", 0) < .6
           or not 0 <= p.get("x", -1) <= 1 or not 0 <= p.get("y", -1) <= 1 for p in torso):
        return None
    visible = [p for p in pose.values() if isinstance(p, dict) and p.get("score", 0) >= .6
               and 0 <= p.get("x", -1) <= 1 and 0 <= p.get("y", -1) <= 1]
    if len(visible) < 8:
        return None
    xs, ys = [p["x"] * width for p in visible], [p["y"] * height for p in visible]
    side = math.ceil(max(max(xs) - min(xs), max(ys) - min(ys)) * 1.2)
    if side < max(32, min(width, height) * .2):
        return None
    w, h = min(width, side), min(height, side)
    x = min(width - w, max(0, round((max(xs) + min(xs) - w) / 2)))
    y = min(height - h, max(0, round((max(ys) + min(ys) - h) / 2)))
    return x, y, w, h


def crop_references(references, rect, width, height):
    x, y, w, h = rect
    result = []
    for point in references:
        px, py = (point["x"] * width - x) / w, (point["y"] * height - y) / h
        if 0 <= px <= 1 and 0 <= py <= 1:
            result.append({**point, "x": px, "y": py})
    return result
