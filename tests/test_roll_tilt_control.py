import math

import pytest

from motioncontrol.head_control import HeadController, HeadEstimate
from motioncontrol.roll_tilt_control import RollTiltControl, eye_line_tilt, gentle_turn_rate


def eyes(angle, width=640, height=480, mirror=False, offset_y=0.):
    dy = math.tan(math.radians(angle)) * (.2 * width) / height
    return {
        'left_eye': {'x': .4 if mirror else .6, 'y': .5 - dy / 2 + offset_y, 'score': 1.},
        'right_eye': {'x': .6 if mirror else .4, 'y': .5 + dy / 2 + offset_y, 'score': 1.},
    }


@pytest.mark.parametrize('width,height', [(640, 480), (720, 1280)])
@pytest.mark.parametrize('angle', [-12., 12.])
@pytest.mark.parametrize('mirror', [False, True])
def test_eye_line_sign_and_pixel_aspect(width, height, angle, mirror):
    assert eye_line_tilt(eyes(angle, width, height, mirror), width, height) == pytest.approx(angle)


@pytest.mark.parametrize('direction', [-1, 1])
def test_held_tilt_keeps_turning_and_neutral_stops_immediately(direction):
    c = RollTiltControl()
    values = [c.update(direction * 10., 1 + i * .04, center=0., noise=.1, deadzone=.1) for i in range(100)]
    assert all(direction * v > 0 for v in values[4:])
    assert c.update(0., 5., center=0., noise=.1, deadzone=.1) == 0.
    assert c.state == 'CENTER'


def test_noise_missing_face_gap_and_reversal_do_not_keep_old_output():
    c = RollTiltControl()
    for i in range(50):
        assert c.update(3. + (-1.) ** i * .4, 1 + i * .04, center=3., noise=.2, deadzone=.1) == 0.
    for i in range(10):
        value = c.update(13., 3 + i * .04, center=3., noise=.2, deadzone=.1)
    assert value > 0
    assert c.update(-10., 3.4, center=3., noise=.2, deadzone=.1) == 0.
    assert c.update(math.nan, 3.44, center=3., noise=.2, deadzone=.1) == 0.
    assert c.update(-10., 5., center=3., noise=.2, deadzone=.1) == 0.


def controller(tmp_path, monkeypatch):
    c = HeadController(tmp_path / 'head.json')
    c.configure(horizontal_algorithm='roll_tilt')
    c.calibrated = True
    c.center_tilt, c.noise_tilt = 0., .1
    monkeypatch.setattr(c.estimator, 'estimate', lambda *args: HeadEstimate(True, yaw=0., pitch=0., roll=0., confidence=1.))
    return c


def test_full_controller_ignores_yaw_return_lock_and_stops_on_eye_loss(tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch)
    c._x_intent_v153._return_latched = True
    for i in range(20):
        output = c.update(eyes(10.), 640, 480, now=1 + i * .04)[0]
    assert output > 0
    assert c.status()['horizontal_algorithm'] == 'roll_tilt'
    assert not c.status()['yaw_return_latched']
    assert c.update(eyes(0.), 640, 480, now=1.8)[0] == 0.
    for i in range(20):
        output = c.update(eyes(-10.), 640, 480, now=2 + i * .04)[0]
    assert output < 0
    assert c.update({}, 640, 480, now=2.8)[0] == 0.


def test_nod_translation_and_yaw_estimator_do_not_steer_roll(tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch)
    monkeypatch.setattr(c.estimator, 'estimate', lambda *args: HeadEstimate(True, yaw=25., pitch=15., roll=12., confidence=1.))
    for i in range(30):
        assert c.update(eyes(0., offset_y=.03 * math.sin(i)), 640, 480, now=1 + i * .04)[0] == 0.


def test_calibration_records_tilt_and_mode_switch_preserves_original_yaw(tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch)
    c.start_center(1.)
    for i in range(100):
        c.update(eyes(4.), 640, 480, now=1 + i * .04)
    assert c.calibrated
    assert c.center_tilt == pytest.approx(4.)
    saved_yaw = c.center_yaw
    c.configure(horizontal_algorithm='gesture_v188')
    assert c.calibrated
    assert c.center_yaw == saved_yaw
    c.configure(horizontal_algorithm='roll_tilt')
    assert c.status()['horizontal_calibrated']
    assert c.center_tilt == pytest.approx(4.)


def test_tilt_center_comes_back_after_restart(tmp_path, monkeypatch):
    """新玩家默认就是侧倾。它的中心也要记住，不然下次打开还是得先校准。"""
    c = controller(tmp_path, monkeypatch)
    c.start_center(1.)
    for i in range(100):
        c.update(eyes(4.), 640, 480, now=1 + i * .04)
    assert c.calibrated
    reloaded = HeadController(tmp_path / 'head.json')
    assert reloaded.config['horizontal_algorithm'] == 'roll_tilt'
    assert reloaded.status()['horizontal_calibrated']
    assert reloaded.center_tilt == pytest.approx(4.)
    assert reloaded.center_pending is False


def test_tilt_does_not_require_yaw_estimator_to_be_valid_after_calibration(tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch)
    monkeypatch.setattr(c.estimator, 'estimate', lambda *args: HeadEstimate(False, error='yaw unavailable'))
    for i in range(20):
        output = c.update(eyes(10.), 640, 480, now=1 + i * .04)[0]
    assert output > 0
    assert c.status()['horizontal_frame_valid']


def test_gentle_turn_curve_is_continuous_progressive_and_has_no_speed_jump():
    values = [gentle_turn_rate(index / 1000) for index in range(1001)]
    assert values[0] == 0 and values[-1] == 1
    assert all(0 < b - a < .002 for a, b in zip(values, values[1:]))
    assert values[100] == pytest.approx(.01)
    assert values[500] == pytest.approx(.25)
    assert values[750] == pytest.approx(.5625)
    assert all(value <= index / 1000 for index, value in enumerate(values))


@pytest.mark.parametrize('direction', [-1, 1])
def test_real_tilt_angle_controls_rate_and_keeps_original_start_stop(direction):
    values = []
    for angle in (4., 8., 12., 18., 25.):
        c = RollTiltControl()
        for frame in range(12):
            value = c.update(direction * angle, 1. + frame / 30,
                             center=0., noise=.1, deadzone=.1)
        values.append(direction * value)
        assert c.update(0., 1.4, center=0., noise=.1, deadzone=.1) == 0.
    assert 0 < values[0] < .1
    assert values[0] < values[1] < values[2] < values[3]
    assert values[3:] == [1., 1.]
