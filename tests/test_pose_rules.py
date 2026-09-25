"""官方动作的识别规则：写成数据之后，和以前写死在内核里的代码逐帧一致。

动作库里除了原地踏步、小腿向后抬起，其余动作的识别规则都跟着动作从云端下载。
规则因此是数据、由 motioncontrol_shared.pose_rules 解释。这个文件管两件事：

* 结果不变。tests/pose_rule_reference.py 是改之前内核里那几段代码的原样拷贝；同一批
  骨架喂两边，每一帧都要相同，包括提膝碰肘左右两边各自的"碰到了""差一点"，以及
  双手交叉的置信度。
* 规则只能是数据。认不得的运算、关节、写得太长太深的，校验时就拒绝。
"""

from __future__ import annotations

import json
import math

import pytest

import pose_rule_reference as reference
from pose_rule_frames import OFFICIAL, frames
from motioncontrol_shared import pose_rules
from motioncontrol_shared.pose_rules import RuleError, evaluate_rules, normalize_rule


def official_rules() -> dict[str, dict]:
    rules = {}
    for path in sorted(OFFICIAL.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        rules[doc["id"]] = normalize_rule(doc["rule"])
    return rules


RULES = official_rules()
ORDER = pose_rules.evaluation_order(RULES)
FRAMES = frames(20000)


def test_every_official_action_has_a_rule():
    assert set(RULES) == {"squat", "hands_up", "jumping_jack", "side_step_jack", "cross_knee_elbow", "hands_cross"}


@pytest.mark.parametrize("ident", ["squat", "hands_up", "jumping_jack", "side_step_jack", "cross_knee_elbow"])
def test_motion_rules_match_the_old_kernel_code_frame_by_frame(ident):
    positives = 0
    for index, (pose, width, height) in enumerate(FRAMES):
        expected = reference.motions(pose, width, height)
        got = evaluate_rules(RULES, pose, width, height, ORDER)[ident]
        assert got["raw"] == expected[ident], f"第 {index} 帧不一致"
        positives += expected[ident]
    # 对照组里成立的帧要够多，否则"全都不成立"也能通过。
    assert positives > 200


def test_cross_knee_elbow_reports_each_side_like_the_old_code():
    """原地踏步要用每一边"碰到了""差一点"来判断这一下抬腿算不算踏步。"""
    for index, (pose, width, height) in enumerate(FRAMES):
        expected = reference.motions(pose, width, height)
        got = evaluate_rules(RULES, pose, width, height, ORDER)["cross_knee_elbow"]
        assert got["sides"] == expected["knee_meets_elbow"], f"第 {index} 帧不一致"
        assert got["attempts"] == expected["knee_near_elbow"], f"第 {index} 帧不一致"


def test_hands_cross_and_its_confidence_match_the_old_code():
    positives = 0
    for index, (pose, width, height) in enumerate(FRAMES):
        raw, confidence = reference.hands_cross(pose)
        got = evaluate_rules(RULES, pose, width, height, ORDER)["hands_cross"]
        assert (got["raw"], got["score"]) == (raw, confidence), f"第 {index} 帧不一致"
        positives += raw
    assert positives > 100


# --- 示范和规则对得上 --------------------------------------------------------

def _pose(frame: dict) -> dict:
    return {name: {"x": x, "y": y, "score": 0.95} for name, (x, y) in frame.items()}


@pytest.mark.parametrize("path", sorted(OFFICIAL.glob("*.json")), ids=lambda path: path.stem)
def test_the_demo_shows_a_pose_the_rule_accepts(path):
    """示范图至少有一帧是规则认得出来的，也至少有一帧认不出来（起始姿势）。

    示范画错了这里就会红：以前提膝碰对侧肘的示范，手肘根本没碰到膝盖。
    """
    doc = json.loads(path.read_text(encoding="utf-8"))
    results = [evaluate_rules(RULES, _pose(frame), 640, 480, ORDER)[doc["id"]]["raw"]
               for frame in doc["demo"]["frames"]]
    assert any(results), "示范里没有一帧能被自己的规则认出来"
    assert not all(results), "示范里每一帧都成立，看不出动作是从哪里开始的"


# --- 规则只能是数据 ----------------------------------------------------------

@pytest.mark.parametrize("bad, message", [
    (["eval", "1"], "不认识的运算"),
    (["<", ["y", "left_toe"], 1], "不认识的关节"),
    (["<", ["y", "{side}_knee"], 1], "只有左右对称"),
    (["<", 1], "参数个数"),
    (True, "true/false"),
    ("left_knee", "看不懂"),
    (["<", float("inf"), 1], "有限"),
])
def test_anything_but_the_known_operations_is_refused(bad, message):
    with pytest.raises(RuleError, match=message):
        normalize_rule({"when": bad})


def test_a_rule_that_is_too_big_is_refused():
    huge = ["+"] + [1.0] * (pose_rules.MAX_NODES + 1)
    with pytest.raises(RuleError, match="太长"):
        normalize_rule({"when": ["<", huge, 1]})
    deep = 1.0
    for _ in range(pose_rules.MAX_DEPTH + 2):
        deep = ["abs", deep]
    with pytest.raises(RuleError, match="太深"):
        normalize_rule({"when": ["<", deep, 1]})


def test_a_rule_for_a_newer_engine_asks_for_an_update():
    with pytest.raises(RuleError, match="请先更新电脑端"):
        normalize_rule({"engine": pose_rules.ENGINE_VERSION + 1, "when": ["<", 1, 2]})


def test_rules_that_reference_each_other_in_a_circle_are_refused():
    rules = {"a": normalize_rule({"when": ["rule", "b"]}), "b": normalize_rule({"when": ["rule", "a"]})}
    with pytest.raises(RuleError, match="转圈"):
        pose_rules.evaluation_order(rules)


def test_a_referenced_rule_that_is_not_installed_counts_as_not_done():
    rules = {"a": normalize_rule({"when": ["not", ["rule", "missing"]]})}
    pose = _pose({"left_shoulder": (0.6, 0.2), "right_shoulder": (0.4, 0.2),
                  "left_hip": (0.55, 0.5), "right_hip": (0.45, 0.5)})
    assert evaluate_rules(rules, pose, 640, 480)["a"]["raw"] is True


def test_without_a_visible_torso_nothing_is_recognised():
    """躯干是所有长度的单位。看不见躯干，就不该有任何动作成立。"""
    rules = {"a": normalize_rule({"when": ["<", 0, 1]})}
    assert evaluate_rules(rules, {}, 640, 480)["a"]["raw"] is False


def test_division_by_zero_and_nan_never_make_a_rule_true():
    rules = {"a": normalize_rule({"when": ["<", ["/", 1, 0], 5]}),
             "b": normalize_rule({"when": [">", ["/", 1, 0], 5]})}
    pose = _pose({"left_shoulder": (0.6, 0.2), "right_shoulder": (0.4, 0.2),
                  "left_hip": (0.55, 0.5), "right_hip": (0.45, 0.5)})
    result = evaluate_rules(rules, pose, 640, 480)
    assert result["a"]["raw"] is False and result["b"]["raw"] is False
    assert math.isnan(float("nan"))
