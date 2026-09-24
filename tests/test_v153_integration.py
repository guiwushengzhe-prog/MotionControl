"""Small deterministic checks for the v153 head-control integration.

These tests exercise the boundary between the 33-point input protocol and the
existing head controller.  They intentionally do not claim real-person or
game accuracy; the latter still needs a live camera and a human.
"""

from __future__ import annotations

import math

import pytest

from motioncontrol.control_kernel import ControlKernel, MP_NAMES
from motioncontrol.head_control import HeadController, HeadPoseEstimator, _RelativeYawAxisV153


def _point(x: float, y: float, z: float = 0.0, score: float = 0.98) -> dict:
    return {"x": float(x), "y": float(y), "z": float(z), "score": float(score)}


def _project_pose(estimator: HeadPoseEstimator, *, yaw: float = 0.0) -> dict[str, dict]:
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    model = np.asarray(estimator._MODEL, dtype=np.float64)
    radians = math.radians(float(yaw))
    rotation = np.asarray(
        [
            [math.cos(radians), 0.0, math.sin(radians)],
            [0.0, 1.0, 0.0],
            [-math.sin(radians), 0.0, math.cos(radians)],
        ],
        dtype=np.float64,
    )
    rvec, _ = cv2.Rodrigues(rotation)
    tvec = np.asarray([[0.0], [0.0], [600.0]], dtype=np.float64)
    camera = np.asarray(
        [[640.0, 0.0, 320.0], [0.0, 640.0, 240.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    projected, _ = cv2.projectPoints(
        model, rvec, tvec, camera, np.zeros((4, 1), dtype=np.float64)
    )
    pose = {name: _point(0.5, 0.5) for name in MP_NAMES}
    for name, xy in zip(estimator._PNP_NAMES, projected.reshape(-1, 2)):
        pose[name] = _point(xy[0] / 640.0, xy[1] / 480.0)
    return pose


def _world_face(estimator: HeadPoseEstimator, *, bad_ear: bool = False) -> dict[str, dict]:
    world = {}
    for name, xyz in zip(estimator._PNP_NAMES, estimator._MODEL):
        x, y, z = (float(value) / 1000.0 for value in xyz)
        if bad_ear and name == "left_ear":
            z += 0.05
        world[name] = _point(x, y, z)
    return world


def _calibrate(controller: HeadController, *, world: dict[str, dict], base_pose: dict[str, dict]) -> None:
    controller.configure(horizontal_algorithm="gesture_v153")
    controller.start_center(now=0.0, kind="test")
    for index in range(48):
        controller.update(base_pose, 640, 480, now=1.05 + index * 0.07, world_pose=world)
        if not controller.calibrating:
            break
    assert controller.calibrated
    assert not controller.calibrating


def test_v153_drive_uses_frozen_minimum_without_name_error():
    axis = _RelativeYawAxisV153()

    assert axis._drive(1, 0.20) == pytest.approx(0.22)
    assert axis._drive(-1, -0.20) == pytest.approx(-0.22)


def test_full_pose_message_keeps_world_pose_separate_and_unmirrored():
    pose = [_point(0.25, 0.75, 0.1) for _ in range(33)]
    world = [_point(-0.12, 0.03, 0.42) for _ in range(33)]
    message = {
        "poses": [{"pose": pose, "world_pose": world}],
        "coordinates_mirrored": True,
    }

    image_map = ControlKernel.pose_map_from_message(message)
    world_map = ControlKernel.world_pose_map_from_message(message)

    assert image_map is not None and world_map is not None
    assert image_map["nose"]["x"] == pytest.approx(0.75)
    # World coordinates are metric/model coordinates, not display coordinates.
    assert world_map["nose"]["x"] == pytest.approx(-0.12)
    assert world_map["nose"]["z"] == pytest.approx(0.42)


def test_kernel_forwards_optional_world_pose_without_replacing_image_pose(tmp_path, monkeypatch):
    class Output:
        def apply(self, *args, **kwargs):
            pass

        def set_buttons(self, *args, **kwargs):
            pass

        def set_holds(self, *args, **kwargs):
            pass

        def set_action_holds(self, *args, **kwargs):
            pass

        def clear_source(self, *args, **kwargs):
            pass

        def set_sensor_state(self, *args, **kwargs):
            pass

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    estimator = HeadPoseEstimator()
    pose_map = _project_pose(estimator)
    pose = [_point(0.5, 0.5) for _ in range(33)]
    for index, name in enumerate(MP_NAMES):
        pose[index] = pose_map.get(name, pose[index])
    world = [_point(0.0, 0.0, 0.0) for _ in range(33)]
    for index, name in enumerate(MP_NAMES):
        if name in dict(zip(estimator._PNP_NAMES, estimator._MODEL)):
            xyz = dict(zip(estimator._PNP_NAMES, estimator._MODEL))[name]
            world[index] = _point(xyz[0] / 1000.0, xyz[1] / 1000.0, xyz[2] / 1000.0)
    message = {
        "type": "pose_frame_v2", "role": "camera", "device_id": "test",
        "sequence": 1, "captured_at_ms": 1.0, "width": 640, "height": 480,
        "camera_facing": "environment", "orientation_degrees": 0,
        "preview_mirrored": False, "coordinates_mirrored": False,
        "poses": [{"pose": pose, "world_pose": world}], "hands": [],
    }
    kernel = ControlKernel(Output())
    try:
        state = kernel.handle_pose_message("mobile_pose:test", message)
        assert state["world_pose_available"] is True
        assert state["pose"]["nose"]["x"] == pytest.approx(pose[0]["x"])
        assert state["head"]["world_yaw_proxy"] is not None
    finally:
        kernel.close()


def test_v153_personal_pnp_activates_from_world_pose_and_exposes_diagnostics(tmp_path):
    estimator = HeadPoseEstimator()
    if not estimator.pnp_available:
        pytest.skip(estimator.pnp_error)
    pose = _project_pose(estimator)
    controller = HeadController(tmp_path / "head.json")
    _calibrate(controller, world=_world_face(estimator), base_pose=pose)

    state = controller.status()
    assert state["horizontal_algorithm"] == "gesture_v153"
    assert state["personal_pnp_active"] is True
    assert state["personal_pnp_template_quality"] == "active"
    assert state["personal_pnp_rejection_reason"] is None
    assert state["personal_pnp_valid_samples"] >= 20
    assert state["personal_pnp_center_depth"] is not None


def test_v153_runtime_world_pose_dropout_does_not_change_horizontal_output(tmp_path):
    estimator = HeadPoseEstimator()
    if not estimator.pnp_available:
        pytest.skip(estimator.pnp_error)
    neutral = _project_pose(estimator)
    turned = _project_pose(estimator, yaw=12.0)
    world = _world_face(estimator)
    with_world = HeadController(tmp_path / "with-world.json")
    without_world = HeadController(tmp_path / "without-world.json")
    _calibrate(with_world, world=world, base_pose=neutral)
    _calibrate(without_world, world=world, base_pose=neutral)
    assert with_world.personal_pnp_active and without_world.personal_pnp_active

    outputs_with = []
    outputs_without = []
    for index in range(8):
        now = 4.5 + index * 0.04
        outputs_with.append(with_world.update(turned, 640, 480, now=now, world_pose=world)[0])
        outputs_without.append(without_world.update(turned, 640, 480, now=now, world_pose=None)[0])
    assert outputs_with == pytest.approx(outputs_without, abs=1e-9)


def test_v153_personal_model_comes_back_after_restart(tmp_path):
    """个人脸模型也是校准的一部分：下次打开要连它一起装回来，转头的输出和重启前一样。"""
    estimator = HeadPoseEstimator()
    if not estimator.pnp_available:
        pytest.skip(estimator.pnp_error)
    neutral = _project_pose(estimator)
    turned = _project_pose(estimator, yaw=12.0)
    before = HeadController(tmp_path / "head.json")
    _calibrate(before, world=_world_face(estimator), base_pose=neutral)
    assert before.personal_pnp_active

    after = HeadController(tmp_path / "head.json")
    assert after.calibrated and after.personal_pnp_active
    assert after.status()["horizontal_calibrated"]
    assert (after.center_yaw, after.center_pitch) == pytest.approx((before.center_yaw, before.center_pitch))
    before.reset_tracking()
    outputs_before, outputs_after = [], []
    for index in range(8):
        now = 4.5 + index * 0.04
        outputs_before.append(before.update(turned, 640, 480, now=now)[0])
        outputs_after.append(after.update(turned, 640, 480, now=now)[0])
    assert outputs_after == pytest.approx(outputs_before, abs=1e-9)


def test_bad_world_template_falls_back_to_generic_pnp(tmp_path):
    estimator = HeadPoseEstimator()
    if not estimator.pnp_available:
        pytest.skip(estimator.pnp_error)
    controller = HeadController(tmp_path / "bad-world.json")
    _calibrate(controller, world=_world_face(estimator, bad_ear=True), base_pose=_project_pose(estimator))

    state = controller.status()
    assert controller.calibrated
    assert state["personal_pnp_active"] is False
    assert state["personal_pnp_template_quality"] == "rejected"
    assert state["personal_pnp_rejection_reason"] == "世界脸部左右点不一致"
