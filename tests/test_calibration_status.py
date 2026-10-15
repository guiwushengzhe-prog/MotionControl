"""校准来源独立于噪声补偿，提示必须与实际控制门槛一致。"""
import pytest

from motioncontrol.head_control import HeadController
from test_head_control_clean import NumericEstimator, _calibrate_by_samples, _ready_controller
from test_roll_tilt_control import controller, eyes


def test_loaded_calibration_reports_actual_tilt_noise_compensation(tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch)
    c.configure(deadzone=.07)
    c.noise_tilt = 1.5750164131757338
    c.center_quality = '噪声较大'
    c.calibration_from_last_session = True
    c.update(eyes(8.), 640, 480, now=1.)
    status = c.status()
    assert status['quality'] == '已载入上次校准 · 噪声补偿：起转 2° → 4.7°'
    assert status['tilt_deadzone_deg'] == pytest.approx(4.725049239527202)
    assert status['deadzone'] == .07
    assert status['calibration_from_last_session']


def test_legacy_minimum_threshold_does_not_falsely_claim_expansion(tmp_path):
    c = HeadController(tmp_path / 'head.json')
    c.configure(horizontal_algorithm='gesture_v188', deadzone=.07)
    c.effective_deadzone_x = .09
    assert '已扩大' not in c._calibration_noise_hint()
    c.effective_deadzone_x = .2
    assert '已扩大' in c._calibration_noise_hint()


def test_accepted_noisy_recalibration_uses_new_center_instead_of_old_one(tmp_path):
    first = _ready_controller(tmp_path)
    _calibrate_by_samples(first, 3., -2.)
    c = HeadController(tmp_path / 'head.json')
    assert c.calibration_from_last_session
    c.estimator = NumericEstimator()
    c.start_center(now=1.)
    for index in range(70):
        c.update({'yaw': 6. + (1.7 if index % 2 else -1.7),
                  'pitch': -2. + (1.25 if index % 3 else -1.25)},
                 640, 480, now=2.02 + index * .05)
        if not c.calibrating:
            break
    assert c.calibrated and not c.calibrating
    assert c.center_yaw > 4.
    assert not c.calibration_from_last_session
    assert c.status()['quality'].startswith('已校准')
