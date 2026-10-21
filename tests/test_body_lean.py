"""身体倾斜实验：两路独立站姿、方向、缺失与部分可用；不调用设备。"""

import math

import pytest

from motioncontrol.body_lean import BodyLeanController


def poses(side=0., back=0., *, mirror=False):
    """正面人物左肩位于图像右边；深度坐标向上，模型坐标向下。"""
    side_x = .3 * math.tan(math.radians(side))
    back_z = .5 * math.tan(math.radians(back))
    image, world, depth = {}, {}, {}
    for name, x in (("left_shoulder", .6), ("right_shoulder", .4), ("left_hip", .57), ("right_hip", .43)):
        shoulder = "shoulder" in name
        px = x + (side_x if shoulder else 0.)
        image[name] = {"x": 1 - px if mirror else px, "y": .3 if shoulder else .6, "score": .95}
        world[name] = {"x": (x - .5) * 2, "y": -.5 if shoulder else 0.,
                       "z": back_z if shoulder else 0., "score": .95}
        depth[name] = {"x": (x - .5) * 2 + (side_x * 2 if shoulder else 0.),
                       "y": .5 if shoulder else 0., "z": 2. + (back_z if shoulder else 0.), "score": .95}
    return image, world, {"tracking_id": 1, "points": depth}


def feed(controller, pose, start, count=40, **options):
    result = set()
    for index in range(count):
        result = controller.update(*pose, 1000, 1000, start + index / 30, **options)
    return result


def test_missing_model_3d_does_not_block_calibration_and_control_of_2d_side():
    c = BodyLeanController()
    image, _, depth = poses()
    feed(c, (image, None, depth), 10., strafe=True, back=True)
    tilted, _, depth = poses(side=10.)
    assert feed(c, (tilted, None, depth), 11.4, count=8, strafe=True, back=True) == {"lean_left"}
    state = c.status(11.4 + 7 / 30)["sources"]["model"]
    assert state["side_valid"] and state["side_calibrated"]
    assert not state["back_valid"] and not state["back_calibrated"]
    assert "三维" in state["reason"]
    assert c.neutral_snapshot()["sources"]["model"].keys() == {"side"}


@pytest.mark.parametrize("mirror", [False, True])
def test_model_lean_left_right_and_back_have_correct_direction(mirror):
    c = BodyLeanController()
    feed(c, poses(mirror=mirror), 10., strafe=True, back=True)
    assert feed(c, poses(side=10., back=12., mirror=mirror), 11.4, count=9, strafe=True, back=True) == {"lean_left", "lean_back"}
    assert feed(c, poses(side=-10., back=-12., mirror=mirror), 11.7, count=15, strafe=True, back=True) == {"lean_right"}
    assert feed(c, poses(mirror=mirror), 12.2, count=15, strafe=True, back=True) == set()


def test_depth_stays_independent_and_missing_depth_never_falls_back_to_model():
    c = BodyLeanController()
    # 深度端初始后倾8度，模型端中立；各自采样，不能共用一个中立角。
    image, world, _ = poses()
    depth = poses(back=8.)[2]
    feed(c, (image, world, depth), 10., strafe=True, back=True, compare=True)
    image, world, _ = poses(side=12., back=12.)
    depth = poses(back=8.)[2]
    actions = feed(c, (image, world, depth), 11.4, count=9, strafe=True, back=True, compare=True)
    assert actions == {"lean_left", "lean_back"}
    assert c.update(image, world, depth, 1000, 1000, 11.7, source="depth", strafe=True, back=True, compare=True) == set()
    assert feed(c, (image, world, depth), 11.74, count=8, source="depth", strafe=True, back=True, compare=True) == set()
    assert c.update(image, world, None, 1000, 1000, 12.02, source="depth", strafe=True, back=True, compare=True) == set()
    state = c.status(12.02)["sources"]
    assert not state["depth"]["valid"] and state["depth"]["active"] == []
    assert "深度" in state["depth"]["reason"]
    assert feed(c, (image, world, poses(back=20.)[2]), 12.06, count=9,
                source="depth", strafe=True, back=True, compare=True) == {"lean_back"}


def test_loaded_neutral_first_frame_gap_and_recovery_are_silent():
    original = BodyLeanController()
    feed(original, poses(), 10., strafe=True, back=True)
    c = BodyLeanController(original.neutral_snapshot())
    tilted = poses(side=12., back=12.)
    assert c.update(*tilted, 1000, 1000, 20., strafe=True, back=True) == set()
    assert c.update(*tilted, 1000, 1000, 20.04, strafe=True, back=True) == {"lean_left", "lean_back"}
    assert c.update(*tilted, 1000, 1000, 21., strafe=True, back=True) == set()
    assert c.update(*tilted, 1000, 1000, 21.04, strafe=True, back=True) == {"lean_left", "lean_back"}
    image, world, depth = tilted
    image["left_shoulder"]["score"] = .1
    assert c.update(image, world, depth, 1000, 1000, 21.08, strafe=True, back=True) == {"lean_back"}
    world["left_shoulder"]["z"] = math.nan
    assert c.update(image, world, depth, 1000, 1000, 21.12, strafe=True, back=True) == set()
    assert c.status(22.)["active"] == []


def test_failed_reset_keeps_saved_neutral_and_stable_tilt_is_not_an_outward_motion():
    c = BodyLeanController()
    feed(c, poses(side=8., back=8.), 10., strafe=True, back=True)
    saved = c.neutral_snapshot()
    assert feed(c, poses(side=8., back=8.), 11.4, count=10, strafe=True, back=True) == set()
    c.start_neutral(12.)
    for t in (12., 12.1, 18.1):
        assert c.update(None, None, None, 1000, 1000, t, strafe=True, back=True) == set()
    assert c.neutral_snapshot() == saved
    assert not c.status(18.1)["sources"]["model"]["calibrating"]
    assert "保留" in c.status(18.1)["sources"]["model"]["reason"]


def test_off_has_no_skeleton_work_and_fused_points_are_not_used_as_model(monkeypatch):
    c = BodyLeanController()
    monkeypatch.setattr(c, "_measure", lambda *args: pytest.fail("关闭实验时不应计算骨骼"))
    assert c.update(*poses(), 1000, 1000, 1.) == set()
    c = BodyLeanController()
    image, world, depth = poses()
    for point in image.values():
        point["position_source"] = "kinect"
    feed(c, (image, world, depth), 10., strafe=True)
    state = c.status(11.3)["sources"]["model"]
    assert not state["side_valid"] and not state["side_calibrated"]


@pytest.mark.parametrize("source", ["model", "depth"])
def test_forward_hold_back_switch_center_and_lost_data_release(source):
    c = BodyLeanController()
    options = {"source": source, "forward": True, "back": True}
    feed(c, poses(), 10., **options)
    assert feed(c, poses(back=-12.), 11.4, count=10, **options) == {"lean_forward"}
    assert feed(c, poses(back=-12.), 11.74, count=10, **options) == {"lean_forward"}
    assert feed(c, poses(back=12.), 12.08, count=12, **options) == {"lean_back"}
    assert feed(c, poses(), 12.49, count=12, **options) == set()
    assert feed(c, poses(back=-12.), 12.90, count=10, **options) == {"lean_forward"}
    assert c.update(None, None, None, 1000, 1000, 13.24, **options) == set()
    assert c.status(13.24)["active"] == []


def test_forward_uses_saved_back_axis_and_is_independent_of_back_switch():
    c = BodyLeanController()
    feed(c, poses(back=3.), 10., back=True)
    neutral = c.neutral_snapshot()
    assert feed(c, poses(back=-10.), 11.4, count=10, forward=True) == {"lean_forward"}
    assert c.neutral_snapshot() == neutral
    assert feed(c, poses(back=16.), 11.74, count=12, forward=True) == set()
    assert feed(c, poses(back=-10.), 12.15, count=12, forward=True) == {"lean_forward"}
    assert c.update(*poses(back=-10.), 1000, 1000, 12.56) == set()


def test_forward_has_no_depth_fallback_and_stale_gap_starts_silent():
    c = BodyLeanController()
    feed(c, poses(), 10., source="depth", forward=True)
    image, world, depth = poses(back=-12.)
    assert feed(c, (image, world, None), 11.4, count=10, source="depth", forward=True) == set()
    assert c.update(image, world, depth, 1000, 1000, 12., source="depth", forward=True) == set()
    assert c.update(image, world, depth, 1000, 1000, 12.04, source="depth", forward=True) == {"lean_forward"}
    assert c.status(12.5)["active"] == []


def test_split_sources_use_model_side_and_depth_fore_aft_without_cross_driving():
    c = BodyLeanController()
    options = {"strafe": True, "back": True, "forward": True,
               "side_source": "model", "fore_aft_source": "depth"}
    feed(c, poses(), 10., **options)
    image, world, _ = poses(side=12., back=-12.)
    depth = poses(side=-12., back=12.)[2]
    assert feed(c, (image, world, depth), 11.4, count=10, **options) == {"lean_left", "lean_back"}
    state = c.status(11.7)
    assert state["source"] == "mixed"
    assert state["side_source"] == "model" and state["fore_aft_source"] == "depth"
    assert state["sources"]["model"]["control_axes"] == ["side"]
    assert state["sources"]["depth"]["control_axes"] == ["back"]
    assert c.neutral_snapshot()["sources"]["model"].keys() == {"side"}
    assert c.neutral_snapshot()["sources"]["depth"].keys() == {"back"}
    # 深度前后倾丢失后不借用模型前倾，模型侧倾仍可独立输出。
    assert feed(c, (image, world, None), 11.74, count=10, **options) == {"lean_left"}
    # 手选交换来源后先静默，再用深度侧倾与模型前倾；共用各自保存的站姿。
    c.start_neutral(12.1)
    reversed_options = {**options, "side_source": "depth", "fore_aft_source": "model"}
    feed(c, poses(), 12.1, **reversed_options)
    assert feed(c, (image, world, depth), 13.5, count=10, **reversed_options) == {"lean_right", "lean_forward"}
