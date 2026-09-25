"""官方动作改成数据规则之前，control_kernel 里写死的那几段判断，原样抄在这里。

它是 tests/test_pose_rules.py 的对照组：同一批骨架喂给这里和规则引擎，每一帧的
结果必须完全相同。改成数据规则是为了让识别规则能跟着动作从云端下载，识别效果
本身一点都不该变——用户下载回来的开合跳，必须和以前内置的那个一模一样。

这里的代码只做了两处机械替换：``self._points_good`` / ``self._angle_at`` /
``self._lateral_coordinate`` 换成同名的模块函数，``self.width`` / ``self.height``
换成参数。别的一个字都没改，常量也照抄。
"""

from __future__ import annotations

import math

SQUAT_KNEE_ANGLE = 145
FEET_WIDE_SPAN = 1.0
CROSS_KNEE_ELBOW_REACH = 0.95
CROSS_KNEE_ELBOW_ATTEMPT_REACH = CROSS_KNEE_ELBOW_REACH + 0.18


def _finite(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _score(point) -> float:
    if not isinstance(point, dict):
        return 0.0
    return _finite(point.get("score", point.get("visibility", point.get("presence", 0.0))))


def _midpoint(a: dict, b: dict) -> dict:
    return {"x": (a["x"] + b["x"]) / 2.0, "y": (a["y"] + b["y"]) / 2.0}


def _clamp(value, low, high):
    return max(low, min(high, value))


def points_good(pose_map, names, minimum=0.42):
    return all(name in pose_map and _score(pose_map[name]) >= minimum for name in names)


def angle_at(a, b, c, width, height):
    if min(_score(a), _score(b), _score(c)) < 0.4:
        return math.nan
    ux, uy = (a["x"] - b["x"]) * width, (a["y"] - b["y"]) * height
    vx, vy = (c["x"] - b["x"]) * width, (c["y"] - b["y"]) * height
    denominator = math.hypot(ux, uy) * math.hypot(vx, vy)
    if denominator < 1e-6:
        return math.nan
    return math.degrees(math.acos(_clamp((ux * vx + uy * vy) / denominator, -1.0, 1.0)))


def lateral_coordinate(point, left_ref, right_ref):
    span = float(right_ref["x"]) - float(left_ref["x"])
    width = max(1e-5, abs(span))
    sign = 1.0 if span >= 0.0 else -1.0
    mid = (float(left_ref["x"]) + float(right_ref["x"])) * 0.5
    return (float(point["x"]) - mid) * sign / width


def motions(pose_map, width, height) -> dict:
    """_update_motion_locked 里和踏步、小腿后抬无关的那几段。"""
    shoulder = _midpoint(pose_map["left_shoulder"], pose_map["right_shoulder"]) if points_good(pose_map, ("left_shoulder", "right_shoulder")) else None
    hip = _midpoint(pose_map["left_hip"], pose_map["right_hip"]) if points_good(pose_map, ("left_hip", "right_hip")) else None
    torso = max(0.025, abs(hip["y"] - shoulder["y"])) if shoulder and hip else math.nan
    hands_raw = squat_raw = False
    jumping_jack_raw = side_step_jack_raw = cross_knee_elbow_raw = False
    if math.isfinite(torso) and points_good(pose_map, ("nose", "left_shoulder", "right_shoulder", "left_elbow", "right_elbow", "left_wrist", "right_wrist")):
        hands_raw = (
            pose_map["left_wrist"]["y"] < pose_map["nose"]["y"] - 0.06 * torso
            and pose_map["right_wrist"]["y"] < pose_map["nose"]["y"] - 0.06 * torso
            and pose_map["left_elbow"]["y"] < pose_map["left_shoulder"]["y"] + 0.08 * torso
            and pose_map["right_elbow"]["y"] < pose_map["right_shoulder"]["y"] + 0.08 * torso
        )
    knee_meets_elbow = {"left": False, "right": False}
    knee_near_elbow = {"left": False, "right": False}
    elbow_knee_good = math.isfinite(torso) and points_good(
        pose_map,
        ("left_hip", "right_hip", "left_elbow", "right_elbow", "left_knee", "right_knee"),
        0.36,
    )
    if elbow_knee_good:
        def body_distance(a: dict, b: dict) -> float:
            dx = (float(a["x"]) - float(b["x"])) * width
            dy = (float(a["y"]) - float(b["y"])) * height
            return math.hypot(dx, dy) / max(1e-6, torso * height)

        for side, other in (("left", "right"), ("right", "left")):
            raised = float(pose_map[side + "_knee"]["y"]) < float(pose_map[side + "_hip"]["y"]) + 0.58 * torso
            distance = body_distance(pose_map[side + "_knee"], pose_map[other + "_elbow"])
            knee_meets_elbow[side] = raised and distance < CROSS_KNEE_ELBOW_REACH
            knee_near_elbow[side] = raised and distance < CROSS_KNEE_ELBOW_ATTEMPT_REACH
        cross_knee_elbow_raw = any(knee_meets_elbow.values())

    leg_good = math.isfinite(torso) and points_good(pose_map, ("left_hip", "right_hip", "left_knee", "right_knee", "left_ankle", "right_ankle"))
    if leg_good:
        left_angle = angle_at(pose_map["left_hip"], pose_map["left_knee"], pose_map["left_ankle"], width, height)
        right_angle = angle_at(pose_map["right_hip"], pose_map["right_knee"], pose_map["right_ankle"], width, height)
        hip_knee = ((pose_map["left_knee"]["y"] - pose_map["left_hip"]["y"]) + (pose_map["right_knee"]["y"] - pose_map["right_hip"]["y"])) / 2.0
        squat_raw = left_angle < SQUAT_KNEE_ANGLE and right_angle < SQUAT_KNEE_ANGLE and hip_knee < 0.84 * torso

        upper_good = points_good(
            pose_map,
            ("left_shoulder", "right_shoulder", "left_elbow", "right_elbow", "left_wrist", "right_wrist"),
            0.38,
        )
        if upper_good:
            ls, rs = pose_map["left_shoulder"], pose_map["right_shoulder"]
            la, ra = pose_map["left_ankle"], pose_map["right_ankle"]
            lw, rw = pose_map["left_wrist"], pose_map["right_wrist"]
            left_ankle_lat = lateral_coordinate(la, ls, rs)
            right_ankle_lat = lateral_coordinate(ra, ls, rs)
            foot_span = right_ankle_lat - left_ankle_lat
            left_wrist_lat = lateral_coordinate(lw, ls, rs)
            right_wrist_lat = lateral_coordinate(rw, ls, rs)
            wrist_span = right_wrist_lat - left_wrist_lat
            feet_wide = foot_span > FEET_WIDE_SPAN
            arms_overhead = (
                float(lw["y"]) < float(ls["y"]) - 0.28 * torso
                and float(rw["y"]) < float(rs["y"]) - 0.28 * torso
            )
            arms_sideways = (
                wrist_span > 1.72
                and max(float(lw["y"]), float(rw["y"])) < float(hip["y"]) - 0.08 * torso
                and min(float(lw["y"]), float(rw["y"])) > float(shoulder["y"]) - 0.48 * torso
            )
            jumping_jack_raw = feet_wide and arms_overhead
            side_step_jack_raw = feet_wide and arms_sideways and not jumping_jack_raw

    return {
        "squat": bool(squat_raw),
        "hands_up": bool(hands_raw),
        "jumping_jack": bool(jumping_jack_raw),
        "side_step_jack": bool(side_step_jack_raw),
        "cross_knee_elbow": bool(cross_knee_elbow_raw),
        "knee_meets_elbow": dict(knee_meets_elbow),
        "knee_near_elbow": dict(knee_near_elbow),
    }


def hands_cross(pose_map) -> tuple[bool, float]:
    """_update_cross_poses_locked 里双手交叉那一段：(成不成立, 置信度)。"""
    confidence = 0.0
    hands_raw = False
    torso_good = points_good(pose_map, ("left_shoulder", "right_shoulder", "left_hip", "right_hip"), 0.45)
    if torso_good:
        ls, rs = pose_map["left_shoulder"], pose_map["right_shoulder"]
        lh, rh = pose_map["left_hip"], pose_map["right_hip"]
        shoulder_mid = _midpoint(ls, rs)
        hip_mid = _midpoint(lh, rh)
        torso = max(0.025, abs(float(hip_mid["y"]) - float(shoulder_mid["y"])))
        hands_good = points_good(pose_map, ("left_elbow", "right_elbow", "left_wrist", "right_wrist"), 0.44)
        if hands_good:
            le, re = pose_map["left_elbow"], pose_map["right_elbow"]
            lw, rw = pose_map["left_wrist"], pose_map["right_wrist"]
            left_lat = lateral_coordinate(lw, ls, rs)
            right_lat = lateral_coordinate(rw, ls, rs)
            le_lat = lateral_coordinate(le, ls, rs)
            re_lat = lateral_coordinate(re, ls, rs)
            y_mid = (float(lw["y"]) + float(rw["y"])) * 0.5
            chest_low = float(hip_mid["y"]) + 0.10 * torso
            chest_high = float(shoulder_mid["y"]) - 0.18 * torso
            vertical_close = abs(float(lw["y"]) - float(rw["y"])) <= 0.55 * torso
            wrist_gap = abs(left_lat - right_lat)
            forearms_point_inward = (left_lat - le_lat) > 0.10 and (right_lat - re_lat) < -0.10
            crossed_order = left_lat > right_lat + 0.07
            near_center = abs(left_lat) < 0.72 and abs(right_lat) < 0.72
            hands_raw = (
                forearms_point_inward
                and crossed_order
                and near_center
                and wrist_gap < 0.72
                and chest_high <= y_mid <= chest_low
                and vertical_close
            )
            cross_depth = max(0.0, min(1.0, (left_lat - right_lat - 0.07) / 0.52))
            confidence = round(0.58 + 0.38 * cross_depth, 3) if hands_raw else round(0.30 * cross_depth, 3)
    return bool(hands_raw), confidence
