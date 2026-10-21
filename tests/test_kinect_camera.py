"""Kinect 的设备保存、人体匹配、深度踏步及失效回退。全部使用隔离配置。"""
import copy
import math
from types import SimpleNamespace

import pytest

from motioncontrol.control_kernel import ControlKernel, NativeCameraService, CameraUnavailable
from motioncontrol.depth_march import DepthMarchLifts
from motioncontrol.kinect_camera import (
    DEPTH_POSE_NAMES, depth_hand_states, matching_depth_body, merge_depth_pose, rotate_point,
)
from motioncontrol.responsive_march import ResponsiveMarch
from test_camera_source_default import Output


def body(left=0, right=0, *, z_left=2, z_right=2):
    points = {
        "left_shoulder": {"x": -.15, "y": 1.5, "z": 2},
        "right_shoulder": {"x": .15, "y": 1.5, "z": 2},
        "left_hip": {"x": -.1, "y": 1, "z": 2},
        "right_hip": {"x": .1, "y": 1, "z": 2},
        "left_ankle": {"x": -.1, "y": .1 + left, "z": z_left},
        "right_ankle": {"x": .1, "y": .1 + right, "z": z_right},
    }
    for point in points.values():
        point.update(score=1, color_x=.5 + point["x"], color_y=.8 - point["y"] * .3)
    return {"tracking_id": "person-one", "points": points, "floor": [0, 1, 0, 0]}


def neutral(adapter):
    for i in range(15):
        adapter.update(body(), {"left": 0, "right": 0}, i / 30)
    assert adapter.source == "depth"


def test_depth_camera_default_and_per_device_choice_survive_restart():
    kernel = ControlKernel(Output())
    try:
        camera = NativeCameraService(kernel)
        camera.set_camera_device("kinect2:camera-one")
        assert camera.depth_enabled
        camera.configure_depth(False)
        camera.set_camera_device("kinect2:camera-two")
        assert camera.depth_enabled
        camera.set_camera_device("kinect2:camera-one")
        assert not camera.depth_enabled
        reopened = ControlKernel(Output())
        try:
            again = NativeCameraService(reopened)
            assert again.camera_device == "kinect2:camera-one"
            assert not again.depth_enabled
        finally:
            reopened.close()
        camera.set_camera_index(0)
        assert not camera.depth_supported
        assert not camera.depth_enabled
    finally:
        kernel.close()


def test_numeric_selection_remains_compatible_and_explicit_index_wins():
    kernel = ControlKernel(Output())
    try:
        camera = NativeCameraService(kernel)
        camera.set_camera_device("kinect2:camera-one")
        assert not NativeCameraService(kernel, camera_index=0).depth_supported
        camera.set_camera_device("2")
        assert camera.camera_index == 2 and camera.camera_device == "2"
    finally:
        kernel.close()


def test_no_depth_setting_for_regular_camera():
    camera = NativeCameraService(SimpleNamespace())
    with pytest.raises(CameraUnavailable):
        camera.configure_depth(True)


def test_sensor_timing_is_reported_separately_from_host_capture_rate():
    camera = NativeCameraService(SimpleNamespace())
    camera.selected_backend = "kinect"
    camera._latest_depth = {"color_frame_interval_ms": 1000 / 15,
                            "color_exposure_ms": 65, "color_gain": 2}
    assert camera.performance()["camera_timing"] == {
        "color_frame_interval_ms": 66.67, "color_exposure_ms": 65, "color_gain": 2,
    }
    camera.selected_backend = "dshow"
    assert camera.performance()["camera_timing"] == {}


def test_no_depth_is_exactly_the_current_image_lifts():
    adapter = DepthMarchLifts()
    for i, lift in enumerate((0, .08, 0, -.09, 0, .025, 0)):
        image = {"left": lift, "right": -lift}
        assert adapter.update(None, image, i / 30) == image
        assert adapter.source == "image"


def test_forward_movement_is_not_real_lift():
    adapter = DepthMarchLifts()
    neutral(adapter)
    # Image perspective says "raised"; the depth ankle is only closer to the lens.
    result = adapter.update(body(z_left=1.6), {"left": .12, "right": -.12}, .6)
    assert result["left"] == pytest.approx(0)
    assert adapter.source == "depth"


def test_floor_compensates_for_camera_tilt():
    adapter = DepthMarchLifts()
    neutral(adapter)
    sample = body(z_left=1.6)
    angle = math.radians(25)
    for p in sample["points"].values():
        y, z = p["y"], p["z"]
        p["y"], p["z"] = y * math.cos(angle) - z * math.sin(angle), y * math.sin(angle) + z * math.cos(angle)
    sample["floor"] = [0, math.cos(angle), math.sin(angle), 0]
    result = adapter.update(sample, {"left": .12, "right": -.12}, .6)
    assert result["left"] == pytest.approx(0, abs=1e-6)


@pytest.mark.parametrize("bad", ["missing", "inferred", "nan"])
def test_unreliable_feet_fall_back_to_image(bad):
    adapter = DepthMarchLifts()
    neutral(adapter)
    sample = body()
    if bad == "missing":
        del sample["points"]["left_ankle"]
    elif bad == "inferred":
        sample["points"]["left_ankle"]["score"] = .2
    else:
        sample["points"]["left_ankle"]["z"] = math.nan
    image = {"left": 0, "right": 0}
    assert adapter.update(sample, image, .6) == image
    assert adapter.source == "depth"
    adapter.update(sample, image, .8)
    assert adapter.source == "image"


def test_source_switch_cannot_create_a_step_from_a_height_jump():
    adapter = DepthMarchLifts()
    neutral(adapter)
    before = adapter.update(body(left=.04), {"left": .08, "right": -.08}, .6)
    # A depth dropout must not introduce a discontinuity from different perspective.
    after = adapter.update(None, {"left": -.12, "right": .12}, .633)
    assert after == pytest.approx(before)


def test_depth_preserves_alternation_and_small_continuing_steps():
    adapter, detector = DepthMarchLifts(), ResponsiveMarch()
    neutral(adapter)
    now = .5
    def feed(sample, image, count):
        nonlocal now
        for _ in range(count):
            now += 1 / 30
            active = detector.update(adapter.update(sample, image, now), now)
        return active
    assert not feed(body(left=.04), {"left": .08, "right": -.08}, 3)
    feed(body(), {"left": 0, "right": 0}, 2)
    assert feed(body(right=.04), {"left": -.08, "right": .08}, 2)
    feed(body(), {"left": 0, "right": 0}, 2)
    assert feed(body(left=.006), {"left": .012, "right": -.012}, 2)
    feed(body(), {"left": 0, "right": 0}, 2)
    assert feed(body(right=.006), {"left": -.012, "right": .012}, 2)
    assert not feed(body(), {"left": 0, "right": 0}, 15)


def test_one_leg_never_starts_march_even_with_depth():
    adapter, detector = DepthMarchLifts(), ResponsiveMarch()
    neutral(adapter)
    now = .5
    for _ in range(4):
        for sample, lift in ((body(left=.04), .08), (body(), 0)):
            for _ in range(5):
                now += 1 / 30
                assert not detector.update(adapter.update(sample, {"left": lift, "right": -lift}, now), now)


@pytest.mark.parametrize("rotation", ["none", "cw", "ccw", "180"])
def test_depth_matches_the_same_person_after_rotation(rotation):
    native = body()
    pose = {}
    for name in ("left_hip", "right_hip", "left_shoulder", "right_shoulder"):
        p = native["points"][name]
        x, y = rotate_point(p["color_x"], p["color_y"], rotation)
        pose[name] = {"x": x, "y": y, "score": 1}
    metadata = {"depth_valid": True, "bodies": [native], "floor": native["floor"]}
    assert matching_depth_body(metadata, pose, rotation)["tracking_id"] == native["tracking_id"]
    other = copy.deepcopy(native)
    for p in other["points"].values():
        p["color_x"] += .3
    metadata["bodies"] = [other]
    assert matching_depth_body(metadata, pose, rotation) is None


def test_missing_depth_and_overlapping_people_do_not_supply_depth():
    native = body()
    pose = {name: {"x": p["color_x"], "y": p["color_y"], "score": 1} for name, p in native["points"].items()}
    assert matching_depth_body({"depth_valid": False, "bodies": [native]}, pose) is None
    assert matching_depth_body({"depth_valid": True, "bodies": [native, copy.deepcopy(native)]}, pose) is None


def test_matched_depth_keeps_supervision_frame_age_and_person_provenance():
    native = body()
    pose = {name: {"x": p["color_x"], "y": p["color_y"], "score": 1} for name, p in native["points"].items()}
    metadata = {"depth_valid": True, "body_valid": True, "bodies": [native],
                "sample_at": 23.5, "body_age_ms": 12., "device_id": "kinect2:test", "body_frames": 7}
    matched = matching_depth_body(metadata, pose)
    assert matched["matched_to_model"] and matched["body_valid"]
    assert matched["match_error"] == 0
    assert matched["sample_at"] == 23.5 and matched["body_age_ms"] == 12.
    assert matched["device_id"] == "kinect2:test" and matched["body_frames"] == 7
    assert "matched_to_model" not in native


def test_seated_person_matches_by_visible_shoulders_but_one_point_is_not_enough():
    native = body()
    pose = {name: {"x": p["color_x"], "y": p["color_y"], "score": 1}
            for name, p in native["points"].items()}
    pose["left_hip"]["score"] = pose["right_hip"]["score"] = .1
    metadata = {"depth_valid": True, "bodies": [native]}
    assert matching_depth_body(metadata, pose)["tracking_id"] == "person-one"
    pose["left_hip"]["score"] = pose["right_hip"]["score"] = 1
    native["points"]["left_hip"]["score"] = native["points"]["right_hip"]["score"] = .1
    assert matching_depth_body(metadata, pose)["tracking_id"] == "person-one"
    pose["left_shoulder"]["score"] = .1
    assert matching_depth_body(metadata, pose) is None


@pytest.mark.parametrize("rotation", ["none", "cw", "ccw", "180"])
@pytest.mark.parametrize("mirrored", [False, True])
def test_native_points_replace_only_anatomical_matches_in_current_frame(rotation, mirrored):
    from motioncontrol_shared.pose_points import MP_NAMES
    original = {name: {"x": .2, "y": .3, "z": -.15, "score": .7} for name in MP_NAMES}
    native = body()
    for name in DEPTH_POSE_NAMES:
        native["points"][name] = {"x": .1, "y": .6, "z": 2., "score": 1.,
                                   "color_x": .62, "color_y": .44}
    native["points"]["head"] = {"x": 0, "y": 1.7, "z": 2, "score": 1,
                                    "color_x": .9, "color_y": .8}
    merged = merge_depth_pose(original, native, rotation, mirrored=mirrored)
    x, y = rotate_point(.62, .44, rotation)
    if mirrored:
        x = 1 - x
    assert len(merged) == 33
    assert len(DEPTH_POSE_NAMES) == 14
    for name in MP_NAMES:
        if name in DEPTH_POSE_NAMES:
            assert merged[name]["x"] == pytest.approx(x)
            assert merged[name]["y"] == pytest.approx(y)
            assert merged[name]["score"] == 1
            assert merged[name]["position_source"] == "kinect"
            assert merged[name]["camera_space"]["z"] == 2
            assert merged[name]["z"] == -.15  # 实测米制坐标不能混入图像相对 z。
        else:
            assert merged[name] == original[name]
    assert original["left_wrist"]["x"] == .2  # 不污染同帧原始人物匹配数据。


@pytest.mark.parametrize("field,value", [("score", .2), ("score", math.nan),
                                        ("x", math.nan), ("z", -1), ("color_x", math.inf)])
def test_invalid_native_joint_falls_back_point_by_point(field, value):
    native = body()
    native["points"]["left_shoulder"][field] = value
    pose = {name: {"x": .2, "y": .3, "z": -.1, "score": .9}
            for name in native["points"]}
    merged = merge_depth_pose(pose, native)
    assert merged["left_shoulder"] == pose["left_shoulder"]
    assert merged["right_shoulder"]["position_source"] == "kinect"
    assert merge_depth_pose(pose, None) is pose
    assert merge_depth_pose(None, native) is None


@pytest.mark.parametrize("swapped", [False, True])
def test_native_hand_states_follow_the_matched_body_side(swapped):
    native = body()
    native["points"]["left_wrist"] = {**native["points"]["left_shoulder"], "color_y": .5}
    native["points"]["right_wrist"] = {**native["points"]["right_shoulder"], "color_y": .5}
    native["hand_states"] = {"left": {"state": "closed", "score": 1},
                             "right": {"state": "open", "score": 1}}
    pose = {}
    for name in ("left_hip", "right_hip", "left_shoulder", "right_shoulder"):
        source = (("right_" if name.startswith("left_") else "left_") + name.split("_", 1)[1]) if swapped else name
        point = native["points"][source]
        pose[name] = {"x": 1 - point["color_x"], "y": point["color_y"], "score": 1}
    matched = matching_depth_body({"depth_valid": True, "image_mirrored": True, "bodies": [native]}, pose)
    states = depth_hand_states(matched)
    assert states["left"]["state"] == ("open" if swapped else "closed")
    assert states["right"]["state"] == ("closed" if swapped else "open")


@pytest.mark.parametrize("state,score", [("unknown", 1), ("lasso", 1), ("nottracked", 1),
                                        ("closed", .1), ("open", math.nan)])
def test_uncertain_native_hand_state_keeps_the_image_fallback(state, score):
    native = body()
    native["points"]["left_wrist"] = native["points"]["left_shoulder"]
    native["hand_states"] = {"left": {"state": state, "score": score}}
    assert depth_hand_states(native) == {}


def test_prolonged_dropout_restarts_alternation_and_does_not_inherit_first_leg():
    adapter, detector = DepthMarchLifts(), ResponsiveMarch()
    neutral(adapter)
    detector.last_side = "left"
    detector.last_event_at = .55
    now = .6
    for i in range(15):
        now += 1 / 30
        lifts = adapter.update(None, {"left": -.12, "right": .12}, now)
        if adapter.source_changed:
            detector.reset()
        assert not detector.update(lifts, now)
    assert adapter.source == "image"

@pytest.mark.parametrize("algorithm", ["legacy", "responsive"])
def test_depth_reaches_the_real_kernel_with_no_visible_image_lift(monkeypatch, algorithm):
    from test_minimal_controls import KernelOutput, _standing_pose
    clock = [10.0]
    monkeypatch.setattr("motioncontrol.control_kernel.time.monotonic", lambda: clock[0])
    kernel = ControlKernel(KernelOutput())
    try:
        kernel.configure_march_algorithm(algorithm)
        pose = _standing_pose()
        def feed(sample, count):
            for _ in range(count):
                clock[0] += 1 / 30
                kernel.handle_pose_map("computer_camera", pose, depth_body=sample)
        feed(body(), 20)
        assert kernel.march_depth_source == "depth"
        feed(body(left=.04), 4)
        assert "march" not in kernel.motion_active
        feed(body(), 2)
        feed(body(right=.04), 4)
        assert "march" in kernel.motion_active
        # Both algorithms must release a held walk when the source is cleared.
        kernel.clear_source("computer_camera")
        assert "march" not in kernel.motion_active
        assert kernel.march_depth_source == "image"
    finally:
        kernel.close()


@pytest.mark.parametrize("rotation", ["none", "cw", "ccw", "180"])
@pytest.mark.parametrize("swapped", [False, True])
def test_flipped_video_still_matches_depth_feet(rotation, swapped):
    native = body(left=.04, right=.01)
    pose = {}
    for name in ("left_hip", "right_hip", "left_shoulder", "right_shoulder"):
        native_name = (("right_" if name.startswith("left_") else "left_") +
                       name.split("_", 1)[1]) if swapped else name
        point = native["points"][native_name]
        x, y = rotate_point(point["color_x"], point["color_y"], rotation)
        pose[name] = {"x": 1 - x, "y": y, "score": 1}
    metadata = {"depth_valid": True, "image_mirrored": True,
                "bodies": [native], "floor": native["floor"]}
    matched = matching_depth_body(metadata, pose, rotation)
    assert matched["tracking_id"] == native["tracking_id"]
    side = "right_ankle" if swapped else "left_ankle"
    assert matched["points"]["left_ankle"] == native["points"][side]
    assert matched["floor"] == native["floor"]
    metadata["bodies"].append(copy.deepcopy(native))
    assert matching_depth_body(metadata, pose, rotation) is None


@pytest.mark.parametrize("kinect,rotation", [
    (True, "none"), (True, "cw"), (True, "ccw"), (True, "180"), (False, "none")])
def test_source_flip_is_shared_by_inference_and_preview(kinect, rotation, monkeypatch):
    import cv2
    import numpy as np
    import threading
    from motioncontrol import kinect_camera

    raw = np.arange(4 * 6 * 3, dtype=np.uint8).reshape(4, 6, 3)
    class Capture:
        metadata = {"sample_at": 10., "depth_valid": True}
        def __init__(self):
            self.once = True
        def release(self):
            pass
        def read(self):
            if self.once:
                self.once = False
                return True, raw
            return False, None
    class Kinect(Capture):
        pass
    monkeypatch.setattr(kinect_camera, "KinectCapture", Kinect)
    kernel = ControlKernel(Output())
    try:
        camera = NativeCameraService(kernel)
        camera.applied_rotation = rotation
        session = SimpleNamespace(stop=threading.Event(), capture=Kinect() if kinect else Capture(),
                                  capture_released=False)
        camera._session = session
        monkeypatch.setattr(camera, "_fail_session", lambda session, error: session.stop.set())
        monkeypatch.setattr(camera, "_worker_finished", lambda session: None)
        camera._capture_loop(session)
        expected = raw
        codes = {"cw": cv2.ROTATE_90_CLOCKWISE, "ccw": cv2.ROTATE_90_COUNTERCLOCKWISE,
                 "180": cv2.ROTATE_180}
        if rotation in codes:
            expected = cv2.rotate(expected, codes[rotation])
        if kinect:
            expected = expected[:, ::-1]
            assert camera._latest_depth["image_mirrored"] is True
            assert "image_mirrored" not in session.capture.metadata
        else:
            assert camera._latest_depth is None
        np.testing.assert_array_equal(camera._latest_frame, expected)
    finally:
        kernel.close()
