"""Focused v0.9.7 head-intent and dual vertical-mode regressions."""

from __future__ import annotations


import pytest

from motioncontrol.control_kernel import ControlKernel
from motioncontrol.head_control import HeadController, HeadEstimate, IntentAxis


class NumericEstimator:
    pnp_available = True
    pnp_error = ""

    def reset(self):
        pass

    def estimate(self, pose, width, height, algorithm):
        pose = pose or {}
        if not pose.get("valid", True):
            return HeadEstimate(False, algorithm=algorithm, error="invalid")
        return HeadEstimate(
            True,
            float(pose.get("yaw", 0.0)),
            float(pose.get("pitch", 0.0)),
            0.0,
            1.0,
            algorithm,
        )


class Output:
    enabled = True

    def __init__(self):
        self.axes = []

    def apply(self, x, y):
        self.axes.append((float(x), float(y)))

    def set_buttons(self, *args, **kwargs):
        pass

    def set_holds(self, *args, **kwargs):
        pass

    def clear_source(self, *args, **kwargs):
        pass


def _gate_pose(*, yaw=0.0, pitch=0.0, left_x=0.2):
    return {
        "yaw": yaw,
        "pitch": pitch,
        "left_wrist": {"x": left_x, "y": 0.2, "score": 0.98},
        "right_wrist": {"x": 0.7, "y": 0.5, "score": 0.98},
        "right_shoulder": {"x": 0.6, "y": 0.32, "score": 0.98},
    }


def _ready_controller(tmp_path):
    c = HeadController(tmp_path / "head.json")
    c.estimator = NumericEstimator()
    c.center_pending = False
    c.calibrated = True
    c.center_yaw = 0.0
    c.center_pitch = 0.0
    c._recompute_deadzone()
    c._reset_filters()
    return c


def test_intent_axis_stops_held_off_center_signal():
    axis = IntentAxis("yaw")
    kwargs = dict(angle_threshold=.05, start_velocity=.12, stop_velocity=.045, release_threshold=.06)
    # The axis measures a turn, so it needs a baseline sample before the first
    # real one: a single step straight to .55 has no velocity and stays IDLE.
    axis.step(0.0, 1.00, **kwargs)
    for index, signal in enumerate((.18, .36, .55)):
        first = axis.step(signal, 1.00 + (index + 1) * .04, **kwargs)
    assert first["active"]
    axis.step(.55, 1.16, **kwargs)
    stopped = axis.step(.55, 1.20, **kwargs)
    assert stopped["state"] == "HOLD"
    assert not stopped["active"]


def test_intent_axis_reversal_enters_returning_immediately():
    axis = IntentAxis("yaw")
    axis.step(.55, 1.0, angle_threshold=.05, start_velocity=.12, stop_velocity=.045, release_threshold=.06)
    axis.step(.65, 1.04, angle_threshold=.05, start_velocity=.12, stop_velocity=.045, release_threshold=.06)
    result = axis.step(.50, 1.08, angle_threshold=.05, start_velocity=.12, stop_velocity=.045, release_threshold=.06)
    assert result["state"] == "RETURNING"
    assert not result["active"]


def test_intent_axis_neutral_jitter_is_idle():
    axis = IntentAxis("yaw")
    states = [axis.step(v, 1.0 + i * .033, angle_threshold=.05, start_velocity=.12, stop_velocity=.045, release_threshold=.06) for i, v in enumerate((0.0, .012, -.010, .008, -.006))]
    assert all(item["state"] == "IDLE" and not item["active"] for item in states)
def test_head_vertical_mode_uses_gate_temporary_pitch_center(tmp_path):
    output = Output()
    kernel = ControlKernel(output)
    try:
        kernel.head_controller = _ready_controller(tmp_path)
        kernel.configure_scene_layout({
            "zones": {"lookGate": {"cx": .2, "cy": .2, "r": .12}},
            "vertical_look": {"enabled": True, "source": "head", "deadzone": .05},
        })
        for i in range(4):
            kernel.handle_pose_map("camera", _gate_pose(pitch=0.0), width=640, height=480)
        assert kernel.vertical_gate_active
        assert kernel.vertical_head_anchor_pitch == pytest.approx(0.0)
        # Canonical positive pitch is down, so a negative pitch movement is up.
        for i in range(1, 5):
            kernel.handle_pose_map("camera", _gate_pose(pitch=-3.0 * i), width=640, height=480)
        assert output.axes[-1][1] < 0.0
        # While the clutch remains held, a stable nod is view velocity rather
        # than a one-shot gesture; RETURNING/HOLD diagnostics must not mute it.
        for _ in range(5):
            kernel.handle_pose_map("camera", _gate_pose(pitch=-12.0), width=640, height=480)
        assert output.axes[-1][1] < 0.0
    finally:
        kernel.close()


def test_head_vertical_gate_exit_clears_center_and_output(tmp_path):
    output = Output()
    kernel = ControlKernel(output)
    try:
        kernel.head_controller = _ready_controller(tmp_path)
        kernel.configure_scene_layout({
            "zones": {"lookGate": {"cx": .2, "cy": .2, "r": .12}},
            "vertical_look": {"enabled": True, "source": "head", "deadzone": .05},
        })
        for _ in range(4):
            kernel.handle_pose_map("camera", _gate_pose(), width=640, height=480)
            kernel.handle_pose_map("camera", _gate_pose(pitch=-12.0), width=640, height=480)
        kernel.handle_pose_map("camera", _gate_pose(pitch=-12.0, left_x=.9), width=640, height=480)
        assert not kernel.vertical_gate_active
        assert kernel.vertical_head_anchor_pitch is None
        assert output.axes[-1][1] == 0.0
    finally:
        kernel.close()


def test_head_vertical_reentry_establishes_new_center(tmp_path):
    output = Output()
    kernel = ControlKernel(output)
    try:
        kernel.head_controller = _ready_controller(tmp_path)
        kernel.configure_scene_layout({
            "zones": {"lookGate": {"cx": .2, "cy": .2, "r": .12}},
            "vertical_look": {"enabled": True, "source": "head", "deadzone": .05},
        })
        for _ in range(4):
            kernel.handle_pose_map("camera", _gate_pose(pitch=2.0), width=640, height=480)
        first_center = kernel.vertical_head_anchor_pitch
        kernel.handle_pose_map("camera", _gate_pose(pitch=2.0, left_x=.9), width=640, height=480)
        for _ in range(4):
            kernel.handle_pose_map("camera", _gate_pose(pitch=5.0), width=640, height=480)
        assert kernel.vertical_head_anchor_pitch != first_center
        assert kernel.vertical_pitch_intent_state in {"IDLE", "HOLD"}
    finally:
        kernel.close()
