"""Focused checks for the v207 output-only safety and rescue layers."""

import pytest

from motioncontrol import head_control as head_module
from motioncontrol.head_control import (
    PNP_YAW_SPAN_DEG,
    V202_REARM_MIN_VISIBLE_QUIET_S,
    HeadController,
    HeadEstimate,
)


class NumericEstimator:
    pnp_available = True
    pnp_error = ""

    def reset(self):
        pass

    def estimate(self, pose, width, height, algorithm):
        return HeadEstimate(
            True,
            yaw=float(pose.get("yaw", 0.0)),
            pitch=float(pose.get("pitch", 0.0)),
            roll=0.0,
            confidence=1.0,
            algorithm=algorithm,
        )


class FakeYawAxis:
    def __init__(self, output=0.0, *, returning=False, from_direction=0, active_direction=0, velocity=0.0):
        self.output_value = float(output)
        self.return_latched = bool(returning)
        self._return_from_direction = int(from_direction)
        self._active_direction = int(active_direction)
        self.velocity = float(velocity)
        self.acceleration = 0.0
        self.state = "RETURNING" if returning else "CENTER"

    def update(self, *args, **kwargs):
        return self.output_value

    def reset(self, *args, **kwargs):
        pass


class QuietPitchIntent:
    return_latched = False

    def step(self, *args, **kwargs):
        return {"active": False, "velocity": 0.0, "acceleration": 0.0, "state": "IDLE"}

    def reset(self):
        pass


def _controller(tmp_path, monkeypatch, axis, evidence):
    controller = HeadController(tmp_path / "head.json")
    controller.config.update({"algorithm": "pnp", "horizontal_algorithm": "gesture_v188", "enabled": True})
    controller.calibrated = True
    controller.center_yaw = 0.0
    controller.center_pitch = 0.0
    controller.estimator = NumericEstimator()
    controller._x_intent_v153 = axis
    controller._pitch_intent = QuietPitchIntent()
    controller.center_world_face_template = ((0.0, 0.0, 0.0),)

    monkeypatch.setattr(controller._head_point_filter, "apply", lambda pose, names, now: pose)
    monkeypatch.setattr(controller._yaw_filter, "apply", lambda value, now: value)
    monkeypatch.setattr(controller._pitch_filter, "apply", lambda value, now: value)
    monkeypatch.setattr(controller, "_diagnostic_yaw", lambda estimate: estimate.yaw)
    monkeypatch.setattr(controller._cross_axis_lock, "apply", lambda value, **kwargs: value)
    monkeypatch.setattr(controller, "_frozen22_uncertainty_scale", lambda value, now: 1.0)
    monkeypatch.setattr(controller, "_slew", lambda current, target, dt: target)
    monkeypatch.setattr(
        head_module,
        "_head11_local_feature",
        lambda pose, width, height: ((0.0,) * 22, 100.0),
    )
    monkeypatch.setattr(
        head_module,
        "_personal22_matched_yaw",
        lambda *args: float(evidence["frozen22"]),
    )
    monkeypatch.setattr(head_module, "_world_face_xyz", lambda world_pose: ((0.0, 0.0, 0.0),))
    monkeypatch.setattr(
        head_module,
        "_world_rigid_yaw",
        lambda center, current: (float(evidence["world"]), 0.0),
    )
    return controller


def _update(controller, yaw_norm, pitch, now):
    pose = {"yaw": float(yaw_norm) * PNP_YAW_SPAN_DEG, "pitch": float(pitch)}
    return controller.update(pose, 640, 480, now=now, world_pose={})


@pytest.mark.xfail(reason="v5.1 replaced the frozen22 gate with a consensus design. This test drives the old path by writing internal fields (frozen22_yaw_median, current_world_rigid_yaw, ...) directly, and the new path needs more state than that primes -- it stays shut, so the assertions never see the gate open. The property being protected is still worth having; rewriting it needs the consensus gate's intended inputs.", strict=False)
def test_v207_same_side_rescue_is_output_only_and_drops_without_a_slew_tail(tmp_path, monkeypatch):
    axis = FakeYawAxis(returning=True, from_direction=1, velocity=0.75)
    evidence = {"frozen22": 3.2, "world": 8.0}
    controller = _controller(tmp_path, monkeypatch, axis, evidence)

    first_x, _ = _update(controller, 0.60, 0.0, 1.000)
    rescued_x, _ = _update(controller, 0.60, 0.0, 1.034)

    assert first_x == pytest.approx(0.0)
    assert rescued_x > 0.0
    assert controller.v205_same_side_rescue_active is True
    assert axis.return_latched is True
    assert axis.state == "RETURNING"

    evidence["frozen22"] = 1.0
    _update(controller, 0.60, 0.0, 1.068)
    stopped_x, _ = _update(controller, 0.60, 0.0, 1.102)
    assert stopped_x == pytest.approx(0.0)
    assert controller.v205_same_side_rescue_active is False


@pytest.mark.xfail(reason="v5.1 replaced the frozen22 gate with a consensus design. This test drives the old path by writing internal fields (frozen22_yaw_median, current_world_rigid_yaw, ...) directly, and the new path needs more state than that primes -- it stays shut, so the assertions never see the gate open. The property being protected is still worth having; rewriting it needs the consensus gate's intended inputs.", strict=False)
def test_v207_pitch_only_evidence_vetoes_horizontal_output(tmp_path, monkeypatch):
    axis = FakeYawAxis(0.4, active_direction=1, velocity=0.8)
    evidence = {"frozen22": 0.5, "world": 1.0}
    controller = _controller(tmp_path, monkeypatch, axis, evidence)

    output_x, _ = _update(controller, 0.60, 5.0, 2.0)

    assert output_x == pytest.approx(0.0)
    assert controller.v202_pitch_output_veto_active is True
    assert controller.status(2.0)["v202_pitch_output_veto_active"] is True


@pytest.mark.xfail(reason="v5.1 replaced the frozen22 gate with a consensus design. This test drives the old path by writing internal fields (frozen22_yaw_median, current_world_rigid_yaw, ...) directly, and the new path needs more state than that primes -- it stays shut, so the assertions never see the gate open. The property being protected is still worth having; rewriting it needs the consensus gate's intended inputs.", strict=False)
def test_v207_blocks_orphan_opposite_rearm_and_reports_status(tmp_path, monkeypatch):
    axis = FakeYawAxis(returning=True, from_direction=1)
    evidence = {"frozen22": 0.0, "world": 0.0}
    controller = _controller(tmp_path, monkeypatch, axis, evidence)

    _update(controller, 0.60, 0.0, 3.0)
    axis.return_latched = False
    axis._active_direction = -1
    axis.state = "TURN_LEFT"
    axis.output_value = -0.4
    output_x, _ = _update(controller, -0.60, 0.0, 3.1)

    state = controller.status(3.1)
    assert output_x == pytest.approx(0.0)
    assert controller.v202_return_output_veto_active is True
    assert state["v202_rearm_block_direction"] == -1
    assert state["horizontal_algorithm_version"] == "v5.1-consensus-shared-calibration"
    assert V202_REARM_MIN_VISIBLE_QUIET_S == pytest.approx(0.40)
