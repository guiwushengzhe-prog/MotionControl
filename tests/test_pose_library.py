"""动作库：每个内置动作都有名字、怎么做、火柴人示范；模板动作的示范就是模板。"""

from __future__ import annotations

import json

import pytest

from motioncontrol.control_kernel import ControlKernel
from motioncontrol_shared import describe, motion_conflicts, pose_library
from motioncontrol_shared.pose_library import (
    HANDS_UP, STAND, _figure, _pose_map, library_payload, library_template, match,
)


class Output:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def hands_up(pose_points):
    template = library_template("hands_up")
    return match(template, _pose_map(pose_points), pose_library.default_threshold("hands_up"))


def test_every_built_in_action_has_a_demo_and_a_how_to():
    payload = library_payload()
    ids = {item["id"] for item in payload}
    assert ids == set(describe.MOTION_NAMES) | set(describe.POSE_NAMES)
    for item in payload:
        assert item["how"], item["id"]
        frames = item["demo"]["frames"]
        assert len(frames) >= 2, f"{item['id']} 的示范要能动"
        for frame in frames:
            for x, y in frame["points"].values():
                assert 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0


def test_names_are_written_once():
    """以前 motion_conflicts 叫「双手过头」、界面叫「双手举过头」，各说各的。"""
    assert motion_conflicts.MOTION_DISPLAY_NAMES["hands_up"] == describe.MOTION_NAMES["hands_up"]
    assert describe.MOTION_NAMES["calf_back"] == "小腿向后抬起"


@pytest.mark.parametrize("name, points, expected", [
    ("示范本身", HANDS_UP, True),
    ("V 字", _figure(STAND, left_elbow=(0.66, 0.10), right_elbow=(0.34, 0.10),
                     left_wrist=(0.73, -0.01), right_wrist=(0.27, -0.01)), True),
    ("边踏步边举", _figure(HANDS_UP, left_knee=(0.56, 0.56), left_ankle=(0.565, 0.74)), True),
    ("站着", STAND, False),
    ("侧平举", _figure(STAND, left_elbow=(0.715, 0.225), right_elbow=(0.285, 0.225),
                       left_wrist=(0.85, 0.225), right_wrist=(0.15, 0.225)), False),
    ("单手举", _figure(STAND, left_elbow=(0.62, 0.09), left_wrist=(0.625, -0.04)), False),
    ("抱头", _figure(STAND, left_elbow=(0.68, 0.12), right_elbow=(0.32, 0.12),
                     left_wrist=(0.56, 0.06), right_wrist=(0.44, 0.06)), False),
])
def test_hands_up_template(name, points, expected):
    assert hands_up(points)["hit"] is expected, name


def test_the_kernel_uses_the_template_and_reports_similarity():
    kernel = ControlKernel(Output())
    try:
        for index in range(6):
            with kernel._lock:
                kernel._process_pose_locked(_pose_map(HANDS_UP), 10.0 + index / 30)
        status = kernel.status()
        assert "hands_up" in status["motions"]
        assert status["library_scores"]["hands_up"] > 0.9
    finally:
        kernel.close()


def test_a_personal_threshold_survives_a_restart(isolated_user_data):
    kernel = ControlKernel(Output())
    try:
        kernel.configure_library_threshold("hands_up", 0.7)
    finally:
        kernel.close()
    saved = json.loads((isolated_user_data / "general_settings.json").read_text(encoding="utf-8"))
    assert saved["pose_library"]["hands_up"]["threshold"] == 0.7
    again = ControlKernel(Output())
    try:
        assert again.library_threshold("hands_up") == 0.7
    finally:
        again.close()


def test_only_template_actions_have_a_threshold():
    kernel = ControlKernel(Output())
    try:
        with pytest.raises(ValueError):
            kernel.configure_library_threshold("march", 0.7)
        with pytest.raises(ValueError):
            kernel.configure_library_threshold("hands_up", 1.5)
    finally:
        kernel.close()
