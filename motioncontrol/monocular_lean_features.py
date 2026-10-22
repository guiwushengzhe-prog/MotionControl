"""从已有躯干位置内缩取胸腹区域；只读取单目深度，不接收原厂骨骼或距离。"""
from __future__ import annotations

import math

import numpy as np

NAMES = ("left_shoulder", "right_shoulder", "left_hip", "right_hip")
LEVELS = np.linspace(.2, .85, 6)
FEATURE_VERSION = 1


def torso_features(depth, pose, *, inverse=False, source_height=None):
    if not isinstance(pose, dict) or depth.ndim != 2:
        return None
    try:
        points = [pose[name] for name in NAMES]
        if any(p.get("score", 0) < .6 for p in points):
            return None
        xy = np.asarray([[p["x"], p["y"]] for p in points], dtype=float)
    except (KeyError, TypeError, ValueError):
        return None
    if not np.isfinite(xy).all() or (xy < 0).any() or (xy > 1).any():
        return None
    h, w = depth.shape
    xy *= [w, h]
    shoulder, hip = xy[:2].mean(axis=0), xy[2:].mean(axis=0)
    height = float(np.linalg.norm(hip - shoulder))
    width = float((np.linalg.norm(xy[0] - xy[1]) + np.linalg.norm(xy[2] - xy[3])) / 2)
    if height < 24 or width < 12:
        return None
    rx, ry = max(2, round(width * .045)), max(2, round(height * .025))
    profile, regions = [], []
    for v in LEVELS:
        left, right = xy[0] * (1 - v) + xy[2] * v, xy[1] * (1 - v) + xy[3] * v
        values = []
        for u in (.25, .375, .5, .625, .75):
            px, py = left * (1 - u) + right * u
            x, y = round(float(px)), round(float(py))
            if x - rx < 0 or x + rx >= w or y - ry < 0 or y + ry >= h:
                return None
            patch = depth[y-ry:y+ry+1, x-rx:x+rx+1]
            valid = patch[np.isfinite(patch) & (patch > 0)]
            if valid.size < patch.size * .8:
                return None
            value = float(np.median(valid))
            values.append(1 / value if inverse else value)
            regions.append({"x": float(px / w), "y": float(py / h), "value": value})
        profile.append(float(np.median(values)))
    centre = float(np.median(profile))
    relative = np.asarray(profile) / centre - 1
    slope = float(np.polyfit(LEVELS, relative, 1)[0])
    tangent = -slope * (source_height or h) / height
    return {"version": FEATURE_VERSION, "features": [*map(float, relative), height / width],
            "geometric_deg": math.degrees(math.atan(tangent)), "profile": profile,
            "regions": regions, "quality": 1., "sampled_regions": len(regions)}
