"""Focused product checks for the selectable frozen22 horizontal source."""

from __future__ import annotations

import math

from head_control import FROZEN22_SIGNATURE, HeadController


def _pose(*, nose_x: float = 0.50, omit_inner: bool = False) -> dict[str, dict]:
    names = (
        "nose", "left_eye_inner", "left_eye", "left_eye_outer",
        "right_eye_inner", "right_eye", "right_eye_outer",
        "left_ear", "right_ear", "mouth_left", "mouth_right",
    )
    xy = {
        "nose": (nose_x, 0.40),
        "left_eye_inner": (0.45, 0.35),
        "left_eye": (0.46, 0.35),
        "left_eye_outer": (0.47, 0.35),
        "right_eye_inner": (0.53, 0.35),
        "right_eye": (0.54, 0.35),
        "right_eye_outer": (0.55, 0.35),
        "left_ear": (0.42, 0.36),
        "right_ear": (0.58, 0.36),
        "mouth_left": (0.47, 0.47),
        "mouth_right": (0.53, 0.47),
    }
    pose = {
        name: {"x": x, "y": y, "z": 0.0, "score": 0.98}
        for name, (x, y) in xy.items()
    }
    if omit_inner:
        pose.pop("left_eye_inner")
    return pose


def test_frozen22_is_explicit_and_switch_requires_new_center(tmp_path):
    controller = HeadController(tmp_path / "head.json")
    controller.configure(algorithm="ratio", horizontal_algorithm="frozen22")
    state = controller.status()
    assert state["horizontal_algorithm"] == "frozen22"
    assert state["horizontal_algorithm_version"] == "real-ab-equalmean-20260830-v1"
    assert state["frozen22_controls_mouse"] is True
    assert state["calibrated"] is False
    assert state["frozen22_calibration_valid"] is False


def test_frozen22_calibration_uses_neutral_center_and_fixed_signature(tmp_path):
    controller = HeadController(tmp_path / "head.json")
    controller.configure(algorithm="ratio", horizontal_algorithm="frozen22")
    pose = _pose()
    controller.start_center(now=0.0, kind="test")
    for index in range(40):
        controller.update(pose, 640, 480, now=1.05 + index * 0.08)
        if not controller.calibrating:
            break
    assert controller.calibrated
    assert controller.frozen22_calibration_valid
    assert controller.frozen22_center is not None
    assert controller.frozen22_sigma is not None
    assert len(FROZEN22_SIGNATURE) == 22
    controller.update(pose, 640, 480, now=5.0)
    state = controller.status()
    assert state["frozen22_calibration_valid"] is True
    assert state["frozen22_frame_valid"] is True
    assert math.isfinite(float(state["frozen22_yaw_median_deg"]))


def test_frozen22_missing_11_point_neutralizes_horizontal_output(tmp_path):
    controller = HeadController(tmp_path / "head.json")
    controller.configure(algorithm="ratio", horizontal_algorithm="frozen22")
    pose = _pose()
    controller.start_center(now=0.0, kind="test")
    for index in range(40):
        controller.update(pose, 640, 480, now=1.05 + index * 0.08)
        if not controller.calibrating:
            break
    assert controller.frozen22_calibration_valid
    controller.output_x = 42.0
    controller.update(_pose(omit_inner=True), 640, 480, now=5.0)
    assert controller.output_x == 0.0
    assert controller.status()["frozen22_frame_valid"] is False
