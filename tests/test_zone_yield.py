"""圈给动作让路：动作和它会扫过的圈都绑了键时，圈不能被动作带着误按。

举双手时手从两侧往上抬，正好扫过两边的手区——不让的话每举一次就误按一次 X、B。
只绑了其中一边的人完全不受影响：圈照样两帧就按。
"""

from __future__ import annotations

from motioncontrol.control_kernel import ZONE_YIELD_S, ControlKernel
from test_minimal_controls import KernelOutput, _standing_pose, _zone_feeder

HANDS_UP = [{"id": "hands_up", "enabled": True, "type": "gamepad", "target": "Y"}]
CALF = [{"id": "calf_back", "enabled": True, "type": "gamepad", "target": "B"}]


def with_elbows(pose, left=(.40, .52), right=(.60, .52)):
    pose["left_elbow"] = {"x": left[0], "y": left[1], "score": .95}
    pose["right_elbow"] = {"x": right[0], "y": right[1], "score": .95}
    return pose


def standing():
    return with_elbows(_standing_pose())


def reach_left():
    """左手伸进左手区（画面左上角），另一只手垂着。"""
    return with_elbows(_standing_pose(left_wrist=(.25, .36)), left=(.33, .42))


def test_without_the_motion_bound_the_zone_is_as_fast_as_before(monkeypatch):
    kernel = ControlKernel(KernelOutput())
    try:
        feed = _zone_feeder(kernel, monkeypatch)
        feed(standing(), 10)
        # 不用等让路那 ZONE_YIELD_S；只是手区本身要待够 HAND_DWELL_S（30 帧/秒约 3 帧）。
        feed(reach_left(), 4)
        assert kernel.zone_state["leftHand"]["pressed"]
        assert kernel.status()["zone_overlaps"] == {}
    finally:
        kernel.close()


def test_a_hand_zone_waits_a_moment_when_raising_both_hands_is_bound(monkeypatch):
    kernel = ControlKernel(KernelOutput())
    try:
        kernel.configure_motions(HANDS_UP)
        overlaps = kernel.status()["zone_overlaps"]
        assert overlaps["leftHand"] == {"triggers": ["motion.hands_up"], "yields": True, "delay": True}
        feed = _zone_feeder(kernel, monkeypatch)
        feed(standing(), 10)
        feed(reach_left(), 4)
        assert not kernel.zone_state["leftHand"]["pressed"], "扫过去的那一下不能按"
        feed(reach_left(), int(ZONE_YIELD_S * 30) + 2)
        assert kernel.zone_state["leftHand"]["pressed"], "真伸进去待住了还是要按"
    finally:
        kernel.close()


def test_raising_both_hands_does_not_press_the_hand_zones(monkeypatch):
    kernel = ControlKernel(KernelOutput())
    try:
        kernel.configure_motions(HANDS_UP)
        feed = _zone_feeder(kernel, monkeypatch)
        feed(standing(), 10)
        # 两手从两侧往上抬：先扫过两边的手区，再举到头顶。
        path = [((.36, .60), (.64, .60), (.40, .52), (.60, .52)),
                ((.28, .46), (.72, .46), (.35, .45), (.65, .45)),
                ((.26, .34), (.74, .34), (.34, .38), (.66, .38)),
                ((.32, .24), (.68, .24), (.36, .31), (.64, .31)),
                ((.42, .18), (.58, .18), (.39, .30), (.61, .30))]
        pressed = []
        for lw, rw, le, re in path:
            pose = with_elbows(_standing_pose(left_wrist=lw, right_wrist=rw), left=le, right=re)
            for _ in range(3):
                feed(pose)
                pressed += [z for z in ("leftHand", "rightHand") if kernel.zone_state[z]["pressed"]]
        top = path[-1]
        for _ in range(12):
            feed(with_elbows(_standing_pose(left_wrist=top[0], right_wrist=top[1]), left=top[2], right=top[3]))
            pressed += [z for z in ("leftHand", "rightHand") if kernel.zone_state[z]["pressed"]]
        assert "hands_up" in kernel.motion_active
        assert not pressed, f"举双手时误按了 {sorted(set(pressed))}"
    finally:
        kernel.close()


def test_the_head_zone_never_waits(monkeypatch):
    """开合跳本身就在跳，头顶区晚按等于跳不起来：只提醒，不让。"""
    kernel = ControlKernel(KernelOutput())
    try:
        kernel.configure_motions([{"id": "jumping_jack", "enabled": True, "type": "gamepad", "target": "Y"}])
        overlaps = kernel.status()["zone_overlaps"]
        assert overlaps["headJump"]["yields"] is False
        assert overlaps["leftHand"]["delay"] is True
    finally:
        kernel.close()


def test_a_zone_touched_only_after_the_motion_is_recognised_is_not_delayed(monkeypatch):
    """小腿后抬的脚是认出来之后才碰到脚区的：做着的时候不按就够了，平时不用晚按。"""
    kernel = ControlKernel(KernelOutput())
    try:
        kernel.configure_motions(CALF)
        overlaps = kernel.status()["zone_overlaps"]
        assert overlaps["leftFoot"] == {"triggers": ["motion.calf_back"], "yields": True, "delay": False}
    finally:
        kernel.close()


def test_standing_up_from_a_front_facing_squat_is_not_a_jump(monkeypatch):
    """正对镜头下蹲时躯干在画面上几乎不变短，以前靠"躯干变短"判断蹲着，头顶区就
    跟着人往下走，站起来的那一下鼻子穿过它——真人录像里按住了 0.6~0.9 秒。"""
    def squat(depth):
        pose = _standing_pose()
        for name in ("nose", "left_ear", "right_ear", "left_shoulder", "right_shoulder",
                     "left_wrist", "right_wrist", "left_hip", "right_hip"):
            pose[name]["y"] += depth
        for name in ("left_knee", "right_knee"):
            pose[name]["y"] += .20 * depth
        pose["left_knee"]["x"] -= .50 * depth
        pose["right_knee"]["x"] += .50 * depth
        return pose

    kernel = ControlKernel(KernelOutput())
    try:
        feed = _zone_feeder(kernel, monkeypatch)
        feed(_standing_pose(), 40)
        poses = ([squat(.12 * (frame + 1) / 12) for frame in range(12)]
                 + [squat(.12)] * 45
                 + [squat(.12 * (1 - (frame + 1) / 8)) for frame in range(8)]
                 + [_standing_pose()] * 10)
        pressed = []
        for frame, pose in enumerate(poses):
            feed(pose)
            if kernel.zone_state["headJump"]["pressed"]:
                pressed.append(frame)
        assert not pressed, f"站起来按到了跳：第 {pressed} 帧"
    finally:
        kernel.close()
