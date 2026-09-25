"""内核只认下载了的动作。

程序自带原地踏步和小腿向后抬起，别的动作要从云端官方动作库下载——识别规则跟着
动作文件一起来。没下载的动作做得再标准也认不出来；下载了当场就认；删掉的那一刻
它要是正在做，也当场放开，不能留一个按住的键。
"""

from __future__ import annotations

import json

from conftest import official_pose_docs
from motioncontrol.control_kernel import ControlKernel
from motioncontrol_shared import pose_library
from pose_rule_frames import OFFICIAL
from test_minimal_controls import KernelOutput


def demo_pose(ident: str, index: int) -> dict:
    doc = json.loads((OFFICIAL / f"{ident}.json").read_text(encoding="utf-8"))
    frame = doc["demo"]["frames"][index]
    pose = {name: {"x": x, "y": y, "score": 0.95} for name, (x, y) in frame.items()}
    for side in ("left", "right"):
        ankle = pose[side + "_ankle"]
        pose[side + "_heel"] = {"x": ankle["x"], "y": ankle["y"] + 0.02, "score": 0.95}
        pose[side + "_foot_index"] = {"x": ankle["x"], "y": ankle["y"] + 0.03, "score": 0.95}
    return pose


def feed(kernel: ControlKernel, pose: dict, frames: int) -> None:
    for _ in range(frames):
        with kernel._lock:
            kernel.width, kernel.height = 640, 480
            kernel._update_motion_locked(pose, 1.0)
            kernel._update_cross_poses_locked(pose, 1.0)


def test_an_action_that_was_not_downloaded_is_not_recognised(no_downloaded_actions):
    kernel = ControlKernel(KernelOutput())
    try:
        # 开合跳示范里跳开的那一帧：两脚分开、两手举过头。
        feed(kernel, demo_pose("jumping_jack", 1), 6)
        assert "jumping_jack" not in kernel.motion_active
        assert "jumping_jack" not in kernel.motion_raw
        feed(kernel, demo_pose("hands_cross", 1), 6)
        assert "hands_cross" not in kernel.pose_active
    finally:
        kernel.close()


def test_a_downloaded_action_is_recognised_right_away(no_downloaded_actions):
    kernel = ControlKernel(KernelOutput())
    try:
        kernel.configure_pose_actions([doc for doc in official_pose_docs() if doc["id"] == "jumping_jack"])
        feed(kernel, demo_pose("jumping_jack", 1), 6)
        assert "jumping_jack" in kernel.motion_active
        # 只装了开合跳：别的照样不认。
        feed(kernel, demo_pose("hands_cross", 1), 6)
        assert "hands_cross" not in kernel.pose_active
    finally:
        kernel.close()


def test_removing_an_action_while_it_is_held_lets_go_at_once():
    kernel = ControlKernel(KernelOutput())
    try:
        feed(kernel, demo_pose("hands_cross", 1), 6)
        assert "hands_cross" in kernel.pose_active
        kernel.configure_pose_actions([doc for doc in official_pose_docs() if doc["id"] != "hands_cross"])
        assert "hands_cross" not in kernel.pose_active
        assert "hands_cross" not in kernel.pose_debounce
        feed(kernel, demo_pose("hands_cross", 1), 6)
        assert "hands_cross" not in kernel.pose_active
    finally:
        kernel.close()


def test_a_kernel_picks_up_what_the_library_has_registered(no_downloaded_actions):
    """电脑端启动时先登记本机下载过的，再建内核：内核一建好就认得它们。"""
    pose_library.register([doc for doc in official_pose_docs() if doc["id"] == "squat"])
    kernel = ControlKernel(KernelOutput())
    try:
        assert set(kernel.pose_actions) == {"squat"}
    finally:
        kernel.close()
