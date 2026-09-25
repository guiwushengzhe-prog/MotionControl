"""手区防误触：擦边不按、进够深待一下才按、两只手一起扫进来要待更久。

真人录像里手区的误按大多是这两种：手臂擦着框边过去，和举双手时两只手从两边一起
扫过手区。挥手时手是一只一只、实实在在伸进去的。
"""

from __future__ import annotations

from motioncontrol.control_kernel import HAND_BOTH_DWELL_S, HAND_DWELL_S, HAND_ENTER_DEPTH, ControlKernel
from motioncontrol.zone_fit import body_frame
from test_minimal_controls import KernelOutput, _standing_pose, _zone_feeder

FPS = 30


def with_elbows(pose):
    pose["left_elbow"] = {"x": .40, "y": .52, "score": .95}
    pose["right_elbow"] = {"x": .60, "y": .52, "score": .95}
    return pose


def into(kernel, pose, zone, depth):
    """把这只手的手腕放进手区，越过下沿 depth、越过里沿更多（量身那把尺）。"""
    frame = body_frame(pose, 640, 480)
    rect = kernel.zone_rects[zone]
    direction = frame["left_dir"] if zone == "leftHand" else frame["right_dir"]
    inner = rect["x1"] if direction > 0 else rect["x2"]
    wrist = "left_wrist" if zone == "leftHand" else "right_wrist"
    pose[wrist] = {"x": inner + direction * (depth + .15) * frame["ux"],
                   "y": rect["y2"] - depth * frame["uy"], "score": .95}
    return pose


def start(monkeypatch):
    kernel = ControlKernel(KernelOutput())
    feed = _zone_feeder(kernel, monkeypatch)
    feed(with_elbows(_standing_pose()), 10)
    return kernel, feed


def test_one_hand_reaching_in_presses_after_a_moment(monkeypatch):
    kernel, feed = start(monkeypatch)
    try:
        pose = into(kernel, with_elbows(_standing_pose()), "leftHand", .12)
        feed(pose, 2)
        assert not kernel.zone_state["leftHand"]["pressed"], "进来的头一两帧还不按"
        feed(pose, int(HAND_DWELL_S * FPS) + 1)
        assert kernel.zone_state["leftHand"]["pressed"]
    finally:
        kernel.close()


def test_grazing_the_edge_does_not_press(monkeypatch):
    """手臂擦着框边、骨架抖到边上：进去不到 HAND_ENTER_DEPTH，待多久都不按。"""
    kernel, feed = start(monkeypatch)
    try:
        pose = into(kernel, with_elbows(_standing_pose()), "rightHand", HAND_ENTER_DEPTH / 2)
        feed(pose, 20)
        assert kernel._point_in_rect(pose["right_wrist"], kernel.zone_rects["rightHand"])
        assert not kernel.zone_state["rightHand"]["pressed"]
    finally:
        kernel.close()


def test_both_hands_sweeping_in_together_need_to_stay(monkeypatch):
    """举双手时两只手从两边一起扫过手区：一扫而过不按，真停在里面才按。"""
    kernel, feed = start(monkeypatch)
    try:
        pose = with_elbows(_standing_pose())
        pose = into(kernel, pose, "leftHand", .12)
        pose = into(kernel, pose, "rightHand", .12)
        feed(pose, int(HAND_DWELL_S * FPS) + 3)
        assert not kernel.zone_state["leftHand"]["pressed"]
        assert not kernel.zone_state["rightHand"]["pressed"]
        feed(pose, int(HAND_BOTH_DWELL_S * FPS))
        assert kernel.zone_state["leftHand"]["pressed"]
        assert kernel.zone_state["rightHand"]["pressed"]
    finally:
        kernel.close()


def test_leaving_is_as_quick_as_before_so_waving_still_taps(monkeypatch):
    """松开没有变慢：挥手时一进一出，每进一次按一下。"""
    kernel, feed = start(monkeypatch)
    try:
        rest = with_elbows(_standing_pose())
        inside = into(kernel, with_elbows(_standing_pose()), "leftHand", .12)
        presses = 0
        for _ in range(3):
            feed(inside, int(HAND_DWELL_S * FPS) + 2)
            presses += kernel.zone_state["leftHand"]["pressed"]
            feed(rest, 2)
            assert not kernel.zone_state["leftHand"]["pressed"]
        assert presses == 3
    finally:
        kernel.close()
