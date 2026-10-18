"""彩色头姿推理契约与人体定位复用；不访问真实摄像头或鼠标。"""
from types import SimpleNamespace

import numpy as np
import pytest

from motioncontrol.hfhpe import HfhpeEstimator, decode_pose, head_box, prepare_crop
from motioncontrol.control_kernel import ControlKernel, NativeCameraService, LocalControlRuntime
from motioncontrol.intent_library import _NullOutput


def pose():
    return {name: {"x": x, "y": y, "score": .99} for name, x, y in (
        ("nose", .5, .25), ("left_ear", .56, .23), ("right_ear", .44, .23),
        ("left_eye", .52, .21), ("right_eye", .48, .21),
        ("left_shoulder", .65, .4), ("right_shoulder", .35, .4))}


def test_whole_head_box_uses_existing_pose_and_scales_without_depth():
    box = head_box(pose(), 640, 480)
    assert box is not None
    x1, y1, x2, y2 = box
    assert y1 < .21 * 480 and y2 > .25 * 480
    assert x1 < .44 * 640 and x2 > .56 * 640
    assert head_box(pose(), 1280, 960) == pytest.approx(tuple(v * 2 for v in box))
    points = pose()
    for name in ("nose", "left_ear", "right_ear", "left_eye", "right_eye"):
        points[name]["score"] = .1
    assert head_box(points, 640, 480) is None


def test_crop_rgb_normalization_border_padding_and_channel_order():
    image = np.zeros((60, 90, 3), dtype=np.uint8)
    image[:] = (0, 127, 255)
    tensor = prepare_crop(image, (10, 10, 40, 40), 64)
    assert tensor.shape == (1, 3, 64, 64) and tensor.dtype == np.float32
    assert tensor[0, :, 32, 32] == pytest.approx([1, 127 / 127.5 - 1, -1])
    padded = prepare_crop(image, (-80, -40, 160, 130), 64)
    assert padded.shape == (1, 3, 64, 64) and np.all(np.isfinite(padded))


def test_angle_order_sign_and_concentration_are_not_fake_probabilities():
    result = decode_pose([[0, 1, 1, 0, 0, -1]], [[100, 90, 80]])
    assert result["yaw_deg"] == 90 and result["pitch_deg"] == 0 and result["roll_deg"] == -90
    assert result["kappa"] == [100, 90, 80] and "confidence" not in result
    with pytest.raises(ValueError):
        decode_pose([[np.nan, 0, 1, 0, 1, 0]], [[1, 1, 1]])
    with pytest.raises(ValueError):
        decode_pose([[0, 0, 1, 0, 1, 0]], [[1, 1, 1]])


class Session:
    def __init__(self, path, **kwargs):
        self.calls = []

    def get_inputs(self):
        return [SimpleNamespace(name="images", shape=[1, 3, 128, 128])]

    def get_outputs(self):
        return [SimpleNamespace(name=n) for n in ("cos_sin", "kappa")]

    def get_modelmeta(self):
        return SimpleNamespace(custom_metadata_map={})

    def run(self, names, values):
        self.calls.append((names, values))
        return np.array([[1, 0, 1, 0, 1, 0]]), np.array([[10, 10, 10]])


def test_only_selected_mode_loads_model_and_missing_head_never_reuses_a_crop(tmp_path):
    model = tmp_path / "head.onnx"
    model.write_bytes(b"model fixture")
    sessions = []

    def create(*args, **kwargs):
        session = Session(*args, **kwargs)
        sessions.append(session)
        return session

    estimator = HfhpeEstimator(model, session_factory=create)
    image = np.zeros((480, 640, 3), dtype=np.uint8)
    assert estimator.process(image, pose(), enabled=False) is None and not sessions
    result = estimator.process(image, pose(), enabled=True)
    assert result["valid"]
    assert result["model"] == "vitt128"
    assert result["inference_ms"] >= 0
    assert result["head_box"] == estimator.status()["head_box"]
    assert len(sessions) == 1 and len(sessions[0].calls) == 1
    assert not estimator.process(image, {}, enabled=True)["valid"]
    assert len(sessions[0].calls) == 1
    assert estimator.status()["state"] == "unavailable"
    assert estimator.process(image, pose(), enabled=False) is None
    assert estimator.session is None and estimator.status()["state"] == "inactive"


def test_missing_model_is_reported_and_does_not_break_other_algorithms(tmp_path):
    estimator = HfhpeEstimator(tmp_path / "missing.onnx")
    result = estimator.process(np.zeros((480, 640, 3), dtype=np.uint8), pose(), enabled=True)
    assert not result["valid"] and "下载" in result["error"]
    assert estimator.status()["state"] == "error"
    assert estimator.process(None, None, enabled=False) is None


@pytest.mark.parametrize("mode", ["hfhpe", "hfhpe_simple"])
def test_phone_or_stopped_camera_has_actionable_experimental_status(mode):
    kernel = ControlKernel(_NullOutput(), persist=False)
    try:
        runtime = LocalControlRuntime(kernel, NativeCameraService(kernel))
        kernel.head_controller.configure(horizontal_algorithm=mode)
        runtime.body_mode = "phone"
        state = runtime.status()["camera"]["hfhpe"]
        assert state["state"] == "unavailable" and "切到电脑" in state["message"]
        runtime.body_mode = "computer"
        state = runtime.status()["camera"]["hfhpe"]
        assert state["state"] == "unavailable" and "启动" in state["message"]
    finally:
        kernel.close()


@pytest.mark.parametrize("mode", ["hfhpe", "hfhpe_simple"])
def test_kernel_passes_native_model_signal_and_clears_it_for_phone(monkeypatch, mode):
    kernel = ControlKernel(_NullOutput(), persist=False)
    try:
        kernel.head_controller.configure(horizontal_algorithm=mode)
        captured = []

        def update(*args, **kwargs):
            captured.append(kwargs.get("head_pose"))
            return 0., 0.

        monkeypatch.setattr(kernel.head_controller, "update", update)
        model_pose = {"source": "hfhpe", "valid": True, "yaw_deg": 3., "pitch_deg": 0., "roll_deg": 0.}
        kernel.handle_pose_map("computer_camera", pose(), head_pose=model_pose)
        assert captured[-1] == model_pose
        kernel.handle_pose_map("phone-one", pose(), head_pose=model_pose)
        assert captured[-1] is None and kernel.latest_head_pose is None
        kernel._clear_body_locked()
        assert kernel.latest_head_pose is None
        assert NativeCameraService(kernel).status()["hfhpe"]["state"] == "inactive"
    finally:
        kernel.close()
