import math

import pytest

from motioncontrol import head_control as head_module
from motioncontrol.head_control import HeadController, HeadEstimate


def test_v188_policy_is_selectable_and_requires_a_fresh_center(tmp_path):
    controller = HeadController(tmp_path / "head.json")
    controller.calibrated = True
    controller.configure(horizontal_algorithm="gesture_v188")
    state = controller.status(1.0)
    assert state["horizontal_algorithm"] == "gesture_v188"
    assert state["horizontal_algorithm_version"] == "v5.1-consensus-shared-calibration"
    assert controller.calibrated is False


# The v5.1 consensus gate is driven end to end through update(): the pose
# carries the PnP yaw/pitch, and the two auxiliary yaw cues (fixed-22 matched
# yaw and 3D world-rigid yaw) are supplied per frame by the helpers below.

class NumericEstimator:
    pnp_available = True
    pnp_error = ""

    def reset(self):
        pass

    def estimate(self, pose, width, height, algorithm):
        return HeadEstimate(
            True,
            yaw=float(pose["yaw"]),
            pitch=float(pose["pitch"]),
            roll=0.0,
            confidence=1.0,
            algorithm=algorithm,
        )


@pytest.fixture
def evidence(monkeypatch):
    cues = {"frozen22": 0.0, "world": 0.0}
    monkeypatch.setattr(head_module, "_head11_local_feature", lambda pose, width, height: ((0.0,) * 22, 100.0))
    monkeypatch.setattr(head_module, "_personal22_matched_yaw", lambda *args: float(cues["frozen22"]))
    monkeypatch.setattr(head_module, "_world_face_xyz", lambda world_pose: ((0.0, 0.0, 0.0),))
    monkeypatch.setattr(head_module, "_world_rigid_yaw", lambda center, current: (float(cues["world"]), 0.0))
    return cues


def _controller(tmp_path, monkeypatch):
    controller = HeadController(tmp_path / "head.json")
    controller.config.update({"algorithm": "pnp", "horizontal_algorithm": "gesture_v188", "enabled": True})
    controller.calibrated = True
    controller.center_yaw = 0.0
    controller.center_pitch = 0.0
    controller.estimator = NumericEstimator()
    controller.center_world_face_template = ((0.0, 0.0, 0.0),)
    controller.frozen22_calibration_valid = True
    controller.frozen22_cal_sigma_deg = 0.30
    controller.noise_world_rigid_yaw = 0.30
    controller.noise_yaw = 0.30
    monkeypatch.setattr(controller._head_point_filter, "apply", lambda pose, names, now: pose)
    monkeypatch.setattr(controller, "_diagnostic_yaw", lambda estimate: estimate.yaw)
    return controller


def _run(tmp_path, monkeypatch, evidence, fps, yaw, pitch, frozen22, world, duration=1.5):
    controller = _controller(tmp_path, monkeypatch)
    frames = []
    for index in range(int(duration * fps)):
        t = index / fps
        evidence["frozen22"] = frozen22(t)
        evidence["world"] = world(t)
        output_x, _ = controller.update({"yaw": yaw(t), "pitch": pitch(t)}, 640, 480, now=1.0 + t, world_pose={})
        frames.append((t, output_x, controller.frozen22_gate_source))
    return controller, frames


def _ramp(rate, limit):
    return lambda t: math.copysign(min(abs(limit), abs(rate) * t), rate)


FPS = (20, 30, 45, 60)


@pytest.mark.parametrize("fps", FPS)
def test_v188_gate_stays_shut_for_static_jitter(tmp_path, monkeypatch, evidence, fps):
    _, frames = _run(
        tmp_path, monkeypatch, evidence, fps,
        yaw=lambda t: 0.4 * math.sin(9 * t),
        pitch=lambda t: 0.3 * math.sin(7 * t),
        frozen22=lambda t: 0.2 * math.sin(5 * t),
        world=lambda t: 0.2 * math.sin(6 * t),
    )
    assert all(output == 0.0 for _, output, _ in frames)


@pytest.mark.parametrize("fps", FPS)
@pytest.mark.parametrize("side", (1, -1))
def test_v188_gate_opens_promptly_for_corroborated_yaw(tmp_path, monkeypatch, evidence, fps, side):
    _, frames = _run(
        tmp_path, monkeypatch, evidence, fps,
        yaw=_ramp(side * 25.0, 20.0),
        pitch=lambda t: 0.0,
        frozen22=_ramp(side * 22.0, 18.0),
        world=_ramp(side * 22.0, 18.0),
    )
    opened = [(t, output, source) for t, output, source in frames if output != 0.0]
    assert opened, "a clear, corroborated turn must reach the output"
    first_t, first_output, first_source = opened[0]
    assert first_t <= 0.20
    assert first_source == "CONSENSUS_WORLD"
    # One direction throughout the turn: no sign flip leaks out.
    signs = {math.copysign(1.0, output) for _, output, _ in opened}
    assert len(signs) == 1


def test_v188_gate_output_direction_follows_the_turn(tmp_path, monkeypatch, evidence):
    def first_output(side):
        _, frames = _run(
            tmp_path, monkeypatch, evidence, 30,
            yaw=_ramp(side * 25.0, 20.0),
            pitch=lambda t: 0.0,
            frozen22=_ramp(side * 22.0, 18.0),
            world=_ramp(side * 22.0, 18.0),
        )
        return next(output for _, output, _ in frames if output != 0.0)

    assert first_output(1) * first_output(-1) < 0.0


@pytest.mark.parametrize("fps", FPS)
def test_v188_pnp_yaw_alone_cannot_open_the_gate(tmp_path, monkeypatch, evidence, fps):
    # Same PnP yaw ramp as the corroborated turn, but no auxiliary cue agrees.
    _, frames = _run(
        tmp_path, monkeypatch, evidence, fps,
        yaw=_ramp(25.0, 20.0),
        pitch=lambda t: 0.0,
        frozen22=lambda t: 0.0,
        world=lambda t: 0.0,
    )
    assert all(output == 0.0 for _, output, _ in frames)
    assert "AUX_UNCONFIRMED" in {source for _, _, source in frames}


@pytest.mark.parametrize("fps", FPS)
def test_v188_pitch_only_motion_is_vetoed_even_when_pnp_yaw_leaks(tmp_path, monkeypatch, evidence, fps):
    # Same PnP yaw ramp as the corroborated turn above, but the head is
    # nodding and neither 3D nor fixed-22 yaw confirms a turn.
    controller, frames = _run(
        tmp_path, monkeypatch, evidence, fps,
        yaw=_ramp(25.0, 20.0),
        pitch=_ramp(20.0, 8.0),
        frozen22=lambda t: 0.3,
        world=lambda t: 0.2,
    )
    assert all(output == 0.0 for _, output, _ in frames)
    assert "PITCH_UNCONFIRMED" in {source for _, _, source in frames}
    assert controller.v188_pitch_guard_active is True
    assert controller.status(2.5)["v202_pitch_output_veto_active"] is True


@pytest.mark.parametrize("fps", FPS)
def test_v188_gate_blocks_when_3d_yaw_points_the_other_way(tmp_path, monkeypatch, evidence, fps):
    _, frames = _run(
        tmp_path, monkeypatch, evidence, fps,
        yaw=_ramp(25.0, 20.0),
        pitch=lambda t: 0.0,
        frozen22=_ramp(22.0, 18.0),
        world=_ramp(-22.0, 18.0),
    )
    assert all(output == 0.0 for _, output, _ in frames)
    assert "DIRECTION_CONFLICT" in {source for _, _, source in frames}


def test_v188_gate_mutes_output_while_the_yaw_axis_is_returning(tmp_path, monkeypatch, evidence):
    controller = _controller(tmp_path, monkeypatch)
    controller._x_intent_v153._return_latched = True
    controller._x_intent_v153._return_from_direction = 1
    controller.update({"yaw": 0.0, "pitch": 0.0}, 640, 480, now=1.0, world_pose={})
    controller.control_yaw = 15.0
    evidence["frozen22"] = evidence["world"] = 15.0
    assert controller._horizontal_evidence_scale(0.5, 1.05, "gesture_v188") == 0.0
    assert controller.frozen22_gate_source == "RETURNING"
