"""自定义姿势的匹配器。

这里测的是"同一个姿势在各种无关变化下仍然算同一个，换一个姿势就明显不是"。无关
变化包括：人站在画面哪里、离镜头多远、身材高矮。相关变化包括：四肢方向、整个人的
倾斜。分不开这两类，用户就永远调不出一个能用的阈值。
"""

from __future__ import annotations

import math

import pytest

from motioncontrol_shared.pose_template import (
    FULL_SCALE_ERROR,
    MIN_SCORE,
    SCHEMA,
    build_template,
    compare,
    weakest_segments,
)


def pose(*, left_arm: float = 0.0, right_arm: float = 0.0,
         left_forearm: float | None = None, right_forearm: float | None = None,
         left_leg: float = 0.0, right_leg: float = 0.0,
         score: float = 0.95) -> dict:
    """摆一个姿势。角度单位是度：0 向下，90 向外侧，180 向上。"""
    body: dict[str, dict] = {}

    def put(name, x, y):
        body[name] = {"x": x, "y": y, "z": 0.0, "score": score}

    put("left_shoulder", 0.55, 0.30)
    put("right_shoulder", 0.45, 0.30)
    put("left_hip", 0.54, 0.55)
    put("right_hip", 0.46, 0.55)

    def limb(root, joint, tip, upper, lower, side):
        x, y = body[root]["x"], body[root]["y"]
        jx = x + side * 0.12 * math.sin(math.radians(upper))
        jy = y + 0.12 * math.cos(math.radians(upper))
        put(joint, jx, jy)
        put(tip, jx + side * 0.12 * math.sin(math.radians(lower)),
            jy + 0.12 * math.cos(math.radians(lower)))

    limb("left_shoulder", "left_elbow", "left_wrist",
         left_arm, left_arm if left_forearm is None else left_forearm, 1)
    limb("right_shoulder", "right_elbow", "right_wrist",
         right_arm, right_arm if right_forearm is None else right_forearm, -1)
    limb("left_hip", "left_knee", "left_ankle", left_leg, left_leg, 1)
    limb("right_hip", "right_knee", "right_ankle", right_leg, right_leg, -1)
    return body


def moved(body: dict, *, shift_x: float = 0.0, shift_y: float = 0.0,
          scale: float = 1.0) -> dict:
    return {name: {**point,
                   "x": point["x"] * scale + shift_x,
                   "y": point["y"] * scale + shift_y}
            for name, point in body.items()}


def rotated(body: dict, degrees: float) -> dict:
    angle = math.radians(degrees)
    out = {}
    for name, point in body.items():
        dx, dy = point["x"] - 0.5, point["y"] - 0.5
        out[name] = {**point,
                     "x": 0.5 + dx * math.cos(angle) - dy * math.sin(angle),
                     "y": 0.5 + dx * math.sin(angle) + dy * math.cos(angle)}
    return out


T_POSE = pose(left_arm=90, right_arm=90)


def score_of(template, body) -> float:
    result = compare(template, body)
    assert result is not None, "本该能比对"
    return result["score"]


# --- 无关的变化必须被消掉 ------------------------------------------------------

def test_the_same_pose_scores_full_marks():
    assert score_of(build_template(T_POSE), T_POSE) == pytest.approx(1.0)


@pytest.mark.parametrize("label, changed", [
    ("走到画面另一侧", moved(T_POSE, shift_x=0.25)),
    ("站远一点（整体缩小）", moved(T_POSE, scale=0.5, shift_x=0.25, shift_y=0.25)),
    ("走近（整体放大）", moved(T_POSE, scale=1.4, shift_x=-0.2, shift_y=-0.2)),
])
def test_position_and_distance_do_not_matter(label, changed):
    """同一个姿势换个站位、换个距离，必须还是同一个姿势。

    这三项如果不被消掉，用户每次站的位置稍有不同就要重新录一遍姿势。
    """
    assert score_of(build_template(T_POSE), changed) == pytest.approx(1.0, abs=1e-6)


def test_a_taller_person_striking_the_same_pose_matches():
    """比对的是方向不是长度，所以肢体长短不影响。"""
    tall = {name: {**point, "y": point["y"] * 1.3} for name, point in T_POSE.items()}
    # 纵向拉伸会同时改变肢体方向，所以不会是满分，但必须远高于换个姿势。
    assert score_of(build_template(T_POSE), tall) > 0.85


# --- 相关的变化必须被保留 ------------------------------------------------------

@pytest.mark.parametrize("label, other, ceiling", [
    ("双手举过头", pose(left_arm=170, right_arm=170), 0.75),
    ("双手自然下垂", pose(), 0.75),
    ("只举左手", pose(left_arm=170, right_arm=90), 0.85),
    ("蹲下", pose(left_arm=90, right_arm=90, left_leg=50, right_leg=50), 0.80),
])
def test_a_different_pose_scores_clearly_lower(label, other, ceiling):
    assert score_of(build_template(T_POSE), other) < ceiling


def test_leaning_is_a_difference_not_noise():
    """在躯干坐标系里，站直举手和整个人歪着举手是一样的——所以倾斜单独记。

    不单独记的话，人一歪就没法区分，而"歪着"在游戏里往往正是一个独立的动作。
    """
    template = build_template(T_POSE)
    assert score_of(template, rotated(T_POSE, 10)) < 0.99
    assert score_of(template, rotated(T_POSE, 30)) < 0.93
    # 但也不能一歪就崩：人站着本来就会有几度的晃动。
    assert score_of(template, rotated(T_POSE, 5)) > 0.95


def test_holding_a_pose_stays_above_switching_to_another_one():
    """真人握住一个姿势，帧间抖动大约三到八度。那个区间必须和"换个姿势"分得开。

    分不开，阈值就无从定起——这是整个功能能不能用的前提。
    """
    template = build_template(T_POSE)
    held = min(score_of(template, pose(left_arm=90 + d, right_arm=90 - d))
               for d in (3, 5, 8))
    switched = max(score_of(template, other) for other in (
        pose(left_arm=170, right_arm=170), pose(), pose(left_arm=170, right_arm=90)))
    assert held > switched + 0.08, f"握住 {held:.2f} 与切换 {switched:.2f} 挨得太近"


# --- 看不清的时候要说看不清 ----------------------------------------------------

def test_a_missing_torso_gives_no_template():
    """没有肩和髋就没有坐标系。这时要返回 None，不能给一个像模像样的数字。"""
    broken = {name: point for name, point in T_POSE.items() if name != "left_hip"}
    assert build_template(broken) is None
    assert compare(build_template(T_POSE), broken) is None


def test_low_confidence_points_are_not_used():
    faint = {name: {**point, "score": MIN_SCORE - 0.01} for name, point in T_POSE.items()}
    assert build_template(faint) is None


def test_a_partly_hidden_body_still_matches_on_what_is_visible():
    """腿被桌子挡住时，手臂的姿势仍然该能认出来。"""
    hidden = {name: ({**point, "score": 0.1} if "knee" in name or "ankle" in name else point)
              for name, point in T_POSE.items()}
    result = compare(build_template(T_POSE), hidden)
    assert result is not None
    assert result["matched"] == 4, "应当只比对四条手臂段"
    assert result["score"] == pytest.approx(1.0)


def test_too_little_visible_gives_up():
    """只剩一两段时，能对上的姿势太多，报分数是误导。"""
    mostly_hidden = {
        name: ({**point, "score": 0.1}
               if name not in {"left_shoulder", "right_shoulder", "left_hip",
                               "right_hip", "left_elbow"} else point)
        for name, point in T_POSE.items()
    }
    assert build_template(mostly_hidden) is None


# --- 杂项 ---------------------------------------------------------------------

def test_a_template_from_another_schema_is_refused():
    template = build_template(T_POSE)
    template["schema"] = "motioncontrol.pose_template.v99"
    assert compare(template, T_POSE) is None


def test_weakest_segments_points_at_what_is_wrong():
    """只给一个 62% 用户没法调整，得说出是哪里不像。"""
    result = compare(build_template(T_POSE), pose(left_arm=170, right_arm=90))
    worst = dict(weakest_segments(result, limit=2))
    assert "left_upper_arm" in worst and "left_forearm" in worst
    assert all(value < 0.5 for value in worst.values())


def test_template_is_json_safe():
    """要存进用户目录、也要能传到云端，所以只能有基本类型。"""
    import json
    template = build_template(T_POSE)
    assert json.loads(json.dumps(template)) == template
    assert template["schema"] == SCHEMA


def test_full_scale_is_documented_as_sixty_degrees():
    """改了这个值等于改了所有人已经调好的阈值，不该是随手改的。"""
    assert FULL_SCALE_ERROR == pytest.approx(math.radians(60.0))


# --- 录制瞬间的骨架预览 --------------------------------------------------------

def test_preview_frames_the_body_the_same_way_wherever_it_stood():
    """人站画面哪个角落、占多大，缩略图里都该一样大。

    不归一化的话，站远时缩略图里就是一个几像素的小点，认不出是什么姿势。
    """
    from motioncontrol_shared.pose_template import build_preview

    near = build_preview(T_POSE)
    far = build_preview(moved(T_POSE, scale=0.4, shift_x=0.5, shift_y=0.4))
    assert near is not None and far is not None
    for name, point in near["points"].items():
        assert far["points"][name] == pytest.approx(point, abs=1e-3)


def test_preview_keeps_the_aspect_ratio():
    """不等比缩放会把站远的人拉成一条细线。"""
    from motioncontrol_shared.pose_template import build_preview

    preview = build_preview(T_POSE)
    xs = [x for x, _ in preview["points"].values()]
    ys = [y for _, y in preview["points"].values()]
    # T 字比人高要宽，所以横向铺满、纵向居中留白。
    assert max(xs) - min(xs) == pytest.approx(1.0, abs=0.02)
    assert max(ys) - min(ys) < 0.98


def test_preview_only_draws_bones_whose_ends_are_visible():
    """腿看不见时不该画出两条连到画面角落的线。"""
    from motioncontrol_shared.pose_template import build_preview

    no_legs = {name: ({**point, "score": 0.1} if "knee" in name or "ankle" in name else point)
               for name, point in T_POSE.items()}
    preview = build_preview(no_legs)
    joined = {name for bone in preview["bones"] for name in bone}
    assert not any("knee" in name or "ankle" in name for name in joined)


def test_preview_gives_up_when_there_is_almost_nothing_to_draw():
    from motioncontrol_shared.pose_template import build_preview

    assert build_preview({name: {**p, "score": 0.1} for name, p in T_POSE.items()}) is None


def test_preview_is_json_safe_and_small():
    """要存进配置文件，也要跟着配置走到别的机器上。"""
    import json
    from motioncontrol_shared.pose_template import build_preview

    preview = build_preview(T_POSE)
    encoded = json.dumps(preview)
    assert json.loads(encoded) == preview
    assert len(encoded) < 1200, f"预览 {len(encoded)} 字节，太大了"
