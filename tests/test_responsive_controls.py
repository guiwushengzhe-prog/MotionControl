"""新旧控制共存、起停延迟和同时操作的回归检查。"""

import math

import pytest

from motioncontrol.control_kernel import ControlKernel
from motioncontrol.head_control import HeadController, HeadEstimate
from motioncontrol.intent_library import make_replay_kernel
from motioncontrol.responsive_head_control import ResponsiveHeadControl
from motioncontrol.responsive_march import ResponsiveMarch
from test_march_foot_separation import lifted, with_elbows
from test_minimal_controls import KernelOutput, _standing_pose, _zone_feeder
from test_roll_tilt_control import eyes


def head_value(c, tilt, yaw, now):
    return c.update(tilt, yaw, now, center_tilt=0., noise_tilt=.1,
                    noise_yaw=.1, yaw_span=18., deadzone=.1)


@pytest.mark.parametrize('fps', [16, 30, 60])
@pytest.mark.parametrize('source', ['tilt', 'yaw'])
def test_head_start_reverse_and_stop_use_the_current_frame(fps, source):
    c = ResponsiveHeadControl()
    tilt, yaw = (8., 0.) if source == 'tilt' else (0., .5)
    assert head_value(c, tilt, yaw, 1.) > .15
    assert head_value(c, -tilt, -yaw, 1. + 1 / fps) < -.15
    assert head_value(c, 0., 0., 1. + 2 / fps) == 0.


def test_head_deadzone_noise_missing_signals_and_no_double_speed():
    c = ResponsiveHeadControl()
    for i in range(100):
        assert head_value(c, .8 * math.sin(i), .1 * math.cos(i), 1 + i / 30) == 0.
    tilt_only = head_value(c, 9., 0., 5.)
    assert head_value(c, 9., .5, 5.04) == pytest.approx(tilt_only)
    assert head_value(c, math.nan, .5, 5.08) > 0
    assert head_value(c, math.nan, math.nan, 5.12) == 0
    # 转脸门槛比侧倾门槛高；小幅侧倾可以响应，同幅度转脸仍处于死区。
    assert head_value(c, 4., 0., 5.16) > 0
    assert head_value(c, 0., 4 / 18, 5.20) == 0


@pytest.mark.parametrize('source', ['tilt', 'yaw'])
def test_head_stops_with_residual_deflection_and_stays_stopped(source):
    c = ResponsiveHeadControl()
    def at(value, now):
        return head_value(c, value if source == 'tilt' else 0.,
                          value / 18 if source == 'yaw' else 0., now)
    assert at(10., 1.) > 0
    # 无需精确回到校准中心，回正范围内的残余偏移当帧停止。
    assert at(2.9 if source == 'tilt' else 3.9, 1.04) == 0
    # 停止线和起动线之间的小抖动不重新开始转向。
    residual = 3.3 if source == 'tilt' else 4.2
    assert at(residual, 1.08) == 0
    assert at(-residual, 1.12) == 0
    assert at(-6., 1.16) < 0
    assert at(0., 1.20) == 0


@pytest.mark.parametrize('source', ['tilt', 'yaw'])
def test_head_curve_is_fine_near_center_monotone_and_bounded(source):
    c = ResponsiveHeadControl()
    values = []
    for index, degrees in enumerate((5., 6., 8., 10., 12., 15., 18., 25.)):
        values.append(head_value(c, degrees if source == 'tilt' else 0.,
                                 degrees / 18 if source == 'yaw' else 0., 1 + index / 30))
    assert 0 < values[0] < .10  # 中心附近可细调。
    assert all(a < b for a, b in zip(values[:6], values[1:7]))
    assert all(0 <= value <= 1 for value in values)
    assert values[-2] > .85  # 大幅动作仍能快速转向。
    assert values[-1] > .97
    # 保持偏移仍持续转向；速度并非取决于头正在移动。
    assert head_value(c, 25. if source == 'tilt' else 0.,
                      25 / 18 if source == 'yaw' else 0., 1.27) > .99


def test_head_moderate_deflection_is_more_responsive_without_shrinking_the_stop_zone():
    c = ResponsiveHeadControl()
    assert .20 < head_value(c, 7.5, 0., 1.) < .30
    assert head_value(c, 2.9, 0., 1.04) == 0
    assert head_value(c, 3.3, 0., 1.08) == 0
    assert head_value(c, 3.5, 0., 1.12) > 0


def test_head_real_calibration_noise_preserves_a_usable_stop_range():
    c = ResponsiveHeadControl()
    def at(tilt, yaw, now):
        return c.update(tilt, yaw / 18, now, center_tilt=-.3, noise_tilt=2.242,
                        noise_yaw=1.544, yaw_span=18., deadzone=.1)
    assert at(12., 0., 1.) > 0
    assert at(5., 5., 1.04) == 0
    assert at(6.7, 5.7, 1.08) == 0
    assert at(-12., 0., 1.12) < 0
    assert at(-.3, 0., 1.16) == 0


def full_head(tmp_path, monkeypatch):
    c = HeadController(tmp_path / 'head.json')
    c.configure(horizontal_algorithm='head_responsive')
    c.calibrated = True
    c.center_yaw = c.center_pitch = c.center_tilt = 0.
    c.noise_yaw = c.noise_pitch = c.noise_tilt = .1
    c._generic_center = (0., .1, 0., .1)
    c._calibration_algorithm = 'pnp'
    monkeypatch.setattr(c.estimator, 'estimate', lambda *args: HeadEstimate(True, yaw=0., pitch=0., roll=0., confidence=1.))
    return c


def test_full_head_does_not_reintroduce_slew_or_return_delay(tmp_path, monkeypatch):
    c = full_head(tmp_path, monkeypatch)
    c._x_intent_v153._return_latched = True
    assert c.update(eyes(8.), 640, 480, now=1.)[0] > 0
    assert c.update(eyes(-8.), 640, 480, now=1.04)[0] < 0
    assert c.update(eyes(0.), 640, 480, now=1.08)[0] == 0
    assert not c.status()['yaw_return_latched']
    c.configure(invert_x=True)
    assert c.update(eyes(8.), 640, 480, now=1.12)[0] < 0


def test_full_head_yaw_and_tilt_can_work_independently(tmp_path, monkeypatch):
    c = full_head(tmp_path, monkeypatch)
    monkeypatch.setattr(c.estimator, 'estimate', lambda *args: HeadEstimate(True, yaw=9., confidence=.9))
    assert c.update({}, 640, 480, now=1.)[0] > 0
    assert c.status()['responsive_head_source'] == 'yaw'
    assert c.status()['normalized_x'] == pytest.approx(.5)
    monkeypatch.setattr(c.estimator, 'estimate', lambda *args: HeadEstimate(True, yaw=20., confidence=.3))
    assert c.update(eyes(0.), 640, 480, now=1.04)[0] == 0
    monkeypatch.setattr(c.estimator, 'estimate', lambda *args: HeadEstimate(False, error='missing yaw'))
    assert c.update(eyes(-8.), 640, 480, now=1.08)[0] < 0
    assert c.update({}, 640, 480, now=1.12)[0] == 0


def test_new_head_calibration_survives_restart_and_switch_to_old(tmp_path, monkeypatch):
    c = full_head(tmp_path, monkeypatch)
    c.start_center(1.)
    for i in range(100):
        c.update(eyes(3.), 640, 480, now=1 + i * .04)
    assert c.calibrated
    reloaded = HeadController(tmp_path / 'head.json')
    assert reloaded.config['horizontal_algorithm'] == 'head_responsive'
    assert reloaded.status()['horizontal_calibrated']
    reloaded.configure(horizontal_algorithm='roll_tilt')
    assert reloaded.status()['horizontal_calibrated']
    assert reloaded.center_tilt == pytest.approx(3.)
    assert reloaded._tilt_control.update(11., 6., center=3., noise=.1, deadzone=.1) == 0.


def start_march(c):
    for t, left, right in [(1., 0., 0.), (1.1, .1, 0.), (1.14, .14, 0.),
                            (1.2, 0., 0.)]:
        assert not c.update({'left': left, 'right': right}, t)
    assert c.update({'left': 0., 'right': .1}, 1.25)
    assert c.update({'left': 0., 'right': .14}, 1.29)


@pytest.mark.parametrize('fps', [16, 30, 60])
def test_march_opposite_leg_starts_in_the_threshold_frame(fps):
    c = ResponsiveMarch()
    dt = 1 / fps
    assert not c.update({'left': .10, 'right': 0.}, 1.)
    assert not c.update({'left': .12, 'right': 0.}, 1. + 2 * dt)
    assert not c.update({'left': 0., 'right': 0.}, 1. + 3 * dt)
    for index in range(4, 8):
        assert not c.update({'left': 0., 'right': 0.}, 1. + index * dt)
    assert not c.update({'left': 0., 'right': .06}, 1. + 8 * dt)
    assert c.update({'left': 0., 'right': .08}, 1. + 9 * dt)


def test_march_expired_alternation_and_simultaneous_lifts_do_not_start():
    c = ResponsiveMarch()
    for index in range(80):
        t = 1 + index / 30
        left = .12 if index < 3 else 0.
        right = .12 if index > 60 else 0.
        assert not c.update({'left': left, 'right': right}, t)
    c.reset()
    for index in range(80):
        height = .12 if index % 20 < 6 else 0.
        assert not c.update({'left': height, 'right': height}, 1 + index / 30)


def test_new_march_stops_after_grounding_or_holding_a_leg():
    c = ResponsiveMarch()
    start_march(c)
    assert c.update({'left': 0., 'right': 0.}, 1.33)
    assert c.update({'left': 0., 'right': 0.}, 1.40)
    assert not c.update({'left': 0., 'right': 0.}, 1.46)
    c.reset()
    start_march(c)
    assert not c.update({'left': 0., 'right': .14}, 1.49)
    assert not c.update(None, 1.53)


@pytest.mark.parametrize('fps', [16, 30, 60])
@pytest.mark.parametrize('cadence', [.45, .65])
def test_regular_alternation_stays_continuous_and_stops(fps, cadence):
    c = ResponsiveMarch()
    active = []
    for i in range(int(4 * fps)):
        t = i / fps
        phase = (t % cadence) / cadence
        left = max(0., .16 * math.sin(phase * math.tau))
        right = max(0., -.16 * math.sin(phase * math.tau))
        active.append(c.update({'left': left, 'right': right}, 10 + t))
    assert all(active[int(2 * fps):]), '正常连续交替踏步不应反复断开'
    for i in range(1, int(.3 * fps) + 1):
        value = c.update({'left': 0., 'right': 0.}, 14 + i / fps)
    assert not value


def test_single_leg_and_excluded_movements_never_start():
    c = ResponsiveMarch()
    for i in range(100):
        assert not c.update({'left': .12 if i % 12 < 6 else 0., 'right': 0.}, 1 + i / 30)
    c.reset()
    for i in range(100):
        assert not c.update({'left': .12 if i % 12 < 6 else 0.,
                             'right': .12 if i % 12 >= 6 else 0.}, 1 + i / 30, excluded=('left', 'right'))


@pytest.mark.parametrize('algorithm', ['legacy', 'responsive'])
def test_kernel_options_round_trip_and_replay(algorithm):
    kernel = ControlKernel(KernelOutput())
    try:
        assert kernel.march_algorithm == 'legacy'
        kernel.configure_march_algorithm(algorithm)
        snapshot = kernel.replay_snapshot()
    finally:
        kernel.close()
    reloaded = ControlKernel(KernelOutput())
    replay = make_replay_kernel(snapshot)
    try:
        assert reloaded.march_algorithm == algorithm
        assert replay.march_algorithm == algorithm
        with pytest.raises(ValueError):
            reloaded.configure_march_algorithm('unknown')
    finally:
        reloaded.close()
        replay.close()


@pytest.mark.parametrize('algorithm', ['legacy', 'responsive'])
def test_kernel_march_start_and_measured_stop_tail(monkeypatch, algorithm):
    kernel = ControlKernel(KernelOutput())
    try:
        kernel.configure_march_algorithm(algorithm)
        feed = _zone_feeder(kernel, monkeypatch)
        feed(_standing_pose(), 20)
        feed(lifted('left'), 4)
        assert 'march' not in kernel.motion_active
        feed(_standing_pose(), 2)
        feed(lifted('right'), 3)
        assert 'march' in kernel.motion_active
        feed(_standing_pose(), 5)
        assert ('march' in kernel.motion_active) == (algorithm == 'legacy')
    finally:
        kernel.close()


def test_head_can_reverse_while_new_march_is_held(tmp_path, monkeypatch):
    output = KernelOutput()
    kernel = ControlKernel(output)
    try:
        kernel.configure_march_algorithm('responsive')
        kernel.head_controller = full_head(tmp_path, monkeypatch)
        feed = _zone_feeder(kernel, monkeypatch)
        feed(_standing_pose(), 20)
        feed(lifted('left'), 3)
        feed(_standing_pose(), 2)
        pose = lifted('right')
        pose.update(eyes(8.))
        feed(pose, 3)
        assert 'march' in kernel.motion_active
        assert output.applied[-1][0] > 0
        pose.update(eyes(-8.))
        feed(pose)
        assert 'march' in kernel.motion_active
        assert output.applied[-1][0] < 0
    finally:
        kernel.close()


def test_new_march_starts_before_the_peak_with_calf_mapping(monkeypatch):
    first_frames = {}
    for algorithm in ('legacy', 'responsive'):
        kernel = ControlKernel(KernelOutput())
        try:
            kernel.configure_march_algorithm(algorithm)
            kernel.configure_motions([{'id': 'calf_back', 'enabled': True, 'type': 'gamepad', 'target': 'B'}])
            feed = _zone_feeder(kernel, monkeypatch)
            feed(_standing_pose(), 20)
            for height in (.10, .12, .14, .16, .18, .20, .20, .20):
                feed(lifted('left', knee=.08, ankle=height))
            feed(_standing_pose(), 2)
            for index, height in enumerate((.10, .12, .14, .16, .18, .20, .20, .20)):
                feed(lifted('right', knee=.08, ankle=height))
                if 'march' in kernel.motion_active:
                    first_frames.setdefault(algorithm, index)
        finally:
            kernel.close()
    assert first_frames['responsive'] == 0
    assert first_frames['legacy'] - first_frames['responsive'] >= 4


def test_new_march_does_not_count_alternating_calf_lifts(monkeypatch):
    kernel = ControlKernel(KernelOutput())
    try:
        kernel.configure_march_algorithm('responsive')
        kernel.configure_motions([{'id': 'calf_back', 'enabled': True, 'type': 'gamepad', 'target': 'B'}])
        feed = _zone_feeder(kernel, monkeypatch)
        feed(_standing_pose(), 20)
        for side in ('left', 'right') * 2:
            for rise in (.1, .3, .6, .6, .3, .1):
                feed(lifted(side, knee=.02, ankle=rise), 2)
                assert 'march' not in kernel.motion_active
            feed(_standing_pose(), 4)
    finally:
        kernel.close()


def test_incomplete_calf_lift_cannot_seed_an_early_opposite_step(monkeypatch):
    kernel = ControlKernel(KernelOutput())
    try:
        kernel.configure_march_algorithm('responsive')
        kernel.configure_motions([{'id': 'calf_back', 'enabled': True, 'type': 'gamepad', 'target': 'B'}])
        feed = _zone_feeder(kernel, monkeypatch)
        feed(_standing_pose(), 20)
        for side in ('left', 'right'):
            for rise in (.1, .25, .46, .46, .20, .08):
                feed(lifted(side, knee=.02, ankle=rise), 2)
                assert 'march' not in kernel.motion_active
            feed(_standing_pose(), 8)
    finally:
        kernel.close()


def test_new_march_does_not_count_knee_to_elbow_or_side_kicks(monkeypatch):
    kernel = ControlKernel(KernelOutput())
    try:
        kernel.configure_march_algorithm('responsive')
        kernel.configure_motions([{'id': 'cross_knee_elbow', 'enabled': True, 'type': 'gamepad', 'target': 'X'}])
        feed = _zone_feeder(kernel, monkeypatch)
        feed(with_elbows(_standing_pose()), 20)
        for side in ('left', 'right') * 2:
            feed(with_elbows(lifted(side, knee=.3, ankle=.3)), 5)
            assert 'march' not in kernel.motion_active
            feed(_standing_pose(), 4)
            feed(lifted(side, outward=.10), 5)
            assert 'march' not in kernel.motion_active
            feed(_standing_pose(), 4)
    finally:
        kernel.close()
