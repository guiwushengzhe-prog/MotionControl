"""独立模型左右转头：合成角度验证，不调用相机或真实鼠标。"""

import json
import math

import pytest

from motioncontrol.head_control import HeadController, HeadEstimate
from motioncontrol.control_kernel import ControlKernel
from motioncontrol.intent_library import _NullOutput
from motioncontrol.view_control import configure_view_control


class PitchEstimator:
    pnp_available = True
    pnp_error = ""

    def estimate(self, pose, width, height, algorithm):
        return HeadEstimate(bool(pose), yaw=-70.0, pitch=4.0, confidence=.9, algorithm=algorithm)

    def set_pnp_model(self, model):
        return False

    def reset(self):
        pass


def frame(yaw=0.0, **changes):
    return {"source": "hfhpe", "valid": True, "yaw_deg": yaw,
            "pitch_deg": 1.0, "roll_deg": 2.0, **changes}


def step(c, yaw, now, **changes):
    return c.update({"pitch": 4.0}, 640, 480, now, head_pose=frame(yaw, **changes))


def make_controller(path=None, center=0.0):
    c = HeadController(path)
    c.estimator = PitchEstimator()
    c.configure(horizontal_algorithm="hfhpe")
    if center is not None:
        c.start_center(10.0)
        for i in range(100):
            step(c, center + (.05 if i % 2 else -.05), 10.0 + i / 30)
        assert c.status(14.0)["calibrated"]
    return c


def drive(c, *, sign=1.0, start=20.0):
    values = []
    for i in range(30):
        values.append(step(c, sign * i * .5, start + i / 30)[0])
    return values


@pytest.mark.parametrize("sign", [-1.0, 1.0])
def test_model_angles_drive_correct_direction_stop_on_hold_and_do_not_reverse_on_return(sign):
    c = make_controller()
    values = drive(c, sign=sign)
    assert any(x * sign > .01 for x in values)
    assert all(x * sign >= 0 for x in values)
    for i in range(30):
        x, y = step(c, sign * 14.5, 21.0 + i / 30)
    assert x == y == 0.0
    returned = [step(c, sign * (14.5 - .5 * i), 22.0 + i / 30)[0] for i in range(30)]
    assert all(x * sign >= 0 for x in returned)


@pytest.mark.parametrize("bad", [None, frame(valid=False, error="未找到头部"),
                                 frame(yaw_deg=math.nan), frame(confidence=.1),
                                 frame(source="pnp"), frame(roll_deg=True)])
def test_invalid_model_stops_immediately_and_never_uses_legacy_pnp(bad):
    c = make_controller()
    assert any(drive(c))
    assert c.update({"pitch": 4.0}, 640, 480, 21.0, head_pose=bad) == (0.0, 0.0)
    state = c.status(21.0)
    assert not state["horizontal_frame_valid"]
    assert not state["estimate_valid"]
    assert state["output_x"] == 0.0
    assert state["horizontal_signal_source"] == "hfhpe"
    assert c.signal_pitch == 4.0
    # 重获一个偏头角度，不能把失联间的跳变当作转头动作。
    assert step(c, -30.0, 21.1) == (0.0, 0.0)


def test_legacy_calibration_is_not_reused_and_survives_switching_and_reload(tmp_path):
    path = tmp_path / "head.json"
    c = HeadController(path)
    c.estimator = PitchEstimator()
    c.calibrated = True
    c._calibration_algorithm = "pnp"
    c._generic_center = (12.0, .2, 4.0, .1)
    c.center_yaw, c.noise_yaw, c.center_pitch, c.noise_pitch = c._generic_center
    c.center_tilt = 0.0
    c.center_quality = "良好"
    legacy = c.profile_document()["calibration"]
    c.configure(horizontal_algorithm="hfhpe")
    assert not c.status()["calibrated"]
    assert step(c, 40.0, 1.0) == (0.0, 0.0)
    assert c.profile_document()["calibration"] == legacy
    c.start_center(10.0)
    for i in range(100):
        step(c, -9.0, 10.0 + i / 30)
    reloaded = HeadController(path)
    assert reloaded.status()["hfhpe_center_yaw_deg"] == -9.0
    assert reloaded.status()["calibration_from_last_session"]
    reloaded.configure(horizontal_algorithm="roll_tilt")
    assert reloaded.status()["calibrated"]
    assert reloaded.center_yaw == 12.0
    reloaded.configure(horizontal_algorithm="hfhpe")
    assert reloaded.status()["calibrated"]
    assert reloaded.status()["center_yaw"] == -9.0


def test_circular_center_has_small_noise_and_small_cross_boundary_delta():
    c = make_controller(center=None)
    c.start_center(10.0)
    for i in range(100):
        step(c, 179.8 if i % 2 else -179.8, 10.0 + i / 30)
    state = c.status(14.0)
    assert abs(state["hfhpe_center_yaw_deg"]) > 179.0
    assert state["hfhpe_noise_yaw_deg"] < 1.0
    for i in range(20):
        x, _ = step(c, 179.8 if i % 2 else -179.8, 20.0 + i / 30)
        assert x == 0.0
        assert abs(c.norm_x) < .1


def test_failed_or_cancelled_calibration_keeps_saved_model_center(tmp_path):
    path = tmp_path / "head.json"
    c = make_controller(path, center=7.0)
    saved = json.loads(path.read_text(encoding="utf-8"))["hfhpe_calibration"]
    c.start_center(20.0)
    step(c, 25.0, 21.1)
    c.configure(sensitivity_x=90.0)
    assert json.loads(path.read_text(encoding="utf-8"))["hfhpe_calibration"] == saved
    c.cancel_center()
    assert c.status()["hfhpe_center_yaw_deg"] == pytest.approx(7.0, abs=.1)
    c.start_center(30.0)
    c.update({}, 640, 480, 37.0, head_pose=frame(valid=False))
    assert not c.calibrating
    assert c.status()["hfhpe_center_yaw_deg"] == pytest.approx(7.0, abs=.1)
    assert json.loads(path.read_text(encoding="utf-8"))["hfhpe_calibration"] == saved


def test_real_deadzone_status_and_missing_confidence():
    c = make_controller(center=None)
    assert c.status()["hfhpe_deadzone_deg"] is None
    c._hfhpe_calibration = {"version": 1, "center_yaw_deg": 0.0, "noise_yaw_deg": 1.5, "sample_count": 32}
    c.configure(deadzone=.25)
    c.update({}, 640, 480, 20.0, head_pose=frame(), motion_deadzone_scale=1.1)
    state = c.status(20.0)
    assert state["hfhpe_requested_deadzone_deg"] == 4.5
    assert state["hfhpe_adaptive_deadzone_deg"] == pytest.approx(4.95)
    assert state["hfhpe_deadzone_deg"] == pytest.approx(4.95)
    assert state["confidence"] is None
    assert state["estimate_valid"]
    assert math.isnan(c.signal_pitch)


@pytest.mark.parametrize("angle", [-30.0, -4.0, 4.0, 30.0])
def test_tracking_gap_and_reacquisition_do_not_invent_a_turn(angle):
    c = make_controller()
    step(c, 0.0, 20.0)
    # 间隔超过有效帧上限；一个新的偏头静止姿态也不能凭空获得速度。
    for i in range(20):
        assert step(c, angle, 22.0 + i / 30) == (0.0, 0.0)


def test_direction_inversion_and_sensitivity_are_applied_to_model_output():
    c = make_controller()
    c.configure(invert_x=True, sensitivity_x=80.0)
    values = drive(c)
    assert min(values) < -.01
    assert max(values) <= 0.0
    assert abs(c.status()["target_output_x"]) <= 80.0


def test_new_view_source_is_saved_without_enabling_hand_control(tmp_path, monkeypatch):
    monkeypatch.setenv("MOTIONCONTROL_USER_DIR", str(tmp_path))
    k = ControlKernel(_NullOutput())
    try:
        result = configure_view_control(k, "hfhpe", "head")
        assert result["kernel"]["head"]["horizontal_algorithm"] == "hfhpe"
        assert result["kernel"]["head"]["enabled"]
        assert not result["hand_mouse"]["enabled"]
        assert k.vertical_look["enabled"]
    finally:
        k.close()
