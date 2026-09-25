"""造一大批骨架，专门用来对照两套识别（见 tests/test_pose_rules.py）。

随机均匀撒点几乎永远落不到"动作成立"的区域，对照不出什么。所以以各个动作的示范
帧和站立姿势为中心加扰动：扰动小时多半成立，扰动大时多半不成立，门槛附近两边都
有。另外随机镜像、随机把一些点的分数压到门槛上下、随机换画面尺寸——那几个门槛
（0.36、0.38、0.42、0.44、0.45）和宽高比都是规则里真会用到的。
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from motioncontrol_shared.pose_rules import JOINTS

OFFICIAL = Path(__file__).resolve().parent.parent / "cloud" / "official_poses"
SIZES = ((640, 480), (480, 640), (1280, 720), (720, 1280), (960, 960))
SCORE_EDGES = (0.35, 0.36, 0.37, 0.38, 0.39, 0.40, 0.41, 0.42, 0.43, 0.44, 0.45, 0.46)


def demo_frames() -> list[dict]:
    """官方动作示范里的每一帧，坐标还是原始的画面坐标。"""
    frames = []
    for path in sorted(OFFICIAL.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        frames.extend(doc["demo"]["frames"])
    return frames


def _complete(frame: dict) -> dict:
    """示范只画了 13 个点。脚跟、脚尖这些补在脚踝旁边，缺的点规则不会用到。"""
    points = {name: list(xy) for name, xy in frame.items()}
    for side in ("left", "right"):
        ax, ay = points[side + "_ankle"]
        points.setdefault(side + "_heel", [ax, ay + 0.02])
        points.setdefault(side + "_foot_index", [ax, ay + 0.03])
    return points


def frames(count: int, seed: int = 20260925) -> list[tuple[dict, int, int]]:
    rng = random.Random(seed)
    bases = [_complete(frame) for frame in demo_frames()]
    out = []
    for _ in range(count):
        base = rng.choice(bases)
        spread = rng.choice((0.0, 0.005, 0.015, 0.03, 0.06, 0.12))
        mirrored = rng.random() < 0.5
        pose = {}
        for name, (x, y) in base.items():
            x += rng.gauss(0.0, spread)
            y += rng.gauss(0.0, spread)
            if mirrored:
                x = 1.0 - x
            score = rng.uniform(0.85, 0.99)
            roll = rng.random()
            if roll < 0.10:
                score = rng.choice(SCORE_EDGES)
            elif roll < 0.13:
                score = rng.uniform(0.0, 0.3)
            pose[name] = {"x": x, "y": y, "score": score}
        if rng.random() < 0.05:
            # 偶尔整个点都没有：模型那一帧没给。
            pose.pop(rng.choice(sorted(pose)), None)
        if rng.random() < 0.02:
            # 偶尔两点完全重合：角度的分母是 0。
            pose["left_knee"] = dict(pose.get("left_hip", {"x": 0.5, "y": 0.5, "score": 0.9}))
        width, height = rng.choice(SIZES)
        out.append((pose, width, height))
    return out


__all__ = ["frames", "demo_frames", "JOINTS"]
