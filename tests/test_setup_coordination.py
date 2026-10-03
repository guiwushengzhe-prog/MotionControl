"""量身、固定圈、自选触发点和个人头控的衔接；全部隔离用户数据与真实输出。"""
import copy
import time

import pytest

from motioncontrol.control_kernel import ControlKernel, RUNTIME_BODY_ZONES
from motioncontrol.intent_library import learning_signature
from motioncontrol.zone_fit import DEFAULT_ZONE_FIT, ZoneFitSession, normalize_zone_fit
from test_responsive_controls import full_head
from test_roll_tilt_control import eyes
from test_zone_fit import Output, W, H, _drive_kernel, run


@pytest.mark.parametrize('fixed', [False, True])
def test_fit_keeps_custom_groups_and_only_updates_measured_zones(fixed):
    kernel = ControlKernel(Output())
    kernel.width, kernel.height = W, H
    initial = {name: {'x1': .01, 'x2': .12, 'y1': .01, 'y2': .12}
               for name in RUNTIME_BODY_ZONES}
    initial['lookGate'] = {'x1': .2, 'x2': .3, 'y1': .2, 'y2': .3}
    try:
        kernel.configure_hand_mouse({'enabled': False})
        kernel.configure_bindings({'zones': {'leftHand': {
            'action': {'type': 'keyboard', 'target': 'Q'}, 'trigger_points': [],
            'trigger_segments': [['left_wrist', 'left_elbow']],
        }}})
        if fixed:
            kernel.set_frozen_zones(initial)
        kernel.start_zone_fit()
        _drive_kernel(kernel)
        fit = kernel.status()['zone_fit']
        assert fit['state'] == 'done'
        assert fit['preserved_zones'] == ['leftHand']
        assert kernel.zone_fit['zones']['leftHand'] == DEFAULT_ZONE_FIT['leftHand']
        assert 'leftHand' not in fit['applied_regions']
        assert set(fit['applied_regions']) == set(RUNTIME_BODY_ZONES) - {'leftHand'}
        assert fit['regions_mode'] == ('fixed' if fixed else 'following')
        assert kernel.zones_frozen is fixed
        groups = kernel.status()['zones']['leftHand']['trigger_groups']
        assert tuple(map(tuple, groups)) == (('left_elbow', 'left_wrist'),)
        if fixed:
            assert kernel.frozen_rects['leftHand'] == initial['leftHand']
            assert kernel.frozen_rects['lookGate'] == initial['lookGate']
            reference = kernel.zone_fit_session.reference_pose
            expected = kernel._compute_body_zones(reference, time.monotonic(), reference=True)
            for name in fit['applied_regions']:
                assert kernel.frozen_rects[name] == pytest.approx(expected[name], abs=.00005)
            saved = copy.deepcopy(kernel.frozen_rects)
    finally:
        kernel.close()
    if fixed:
        again = ControlKernel(Output())
        try:
            assert again.zones_frozen
            assert again.frozen_rects == saved
            # 正式服务启动还会从当前游戏载入映射，内核本身不存游戏配置。
            again.configure_bindings({'zones': {'leftHand': {
                'action': {'type': 'keyboard', 'target': 'Q'}, 'trigger_points': [],
                'trigger_segments': [['left_wrist', 'left_elbow']],
            }}})
            again.latest_pose = reference
            again.reset_zone_fit()
            assert again.zones_frozen
            assert again.frozen_rects['leftHand'] == saved['leftHand']
            assert again.frozen_rects['lookGate'] == saved['lookGate']
            assert again.frozen_rects['rightHand'] != saved['rightHand']
            expected = again._compute_body_zones(reference, time.monotonic(), reference=True)
            assert again.frozen_rects['rightHand'] == pytest.approx(expected['rightHand'], abs=.00005)
        finally:
            again.close()


def test_grip_only_remeasurement_does_not_move_fixed_regions():
    kernel = ControlKernel(Output())
    initial = {'leftFoot': {'x1': .2, 'x2': .35, 'y1': .7, 'y2': .9}}
    try:
        kernel.set_frozen_zones(initial)
        kernel.configure_hand_mouse({'enabled': True, 'horizontal_hand': 'left', 'vertical_hand': 'off'})
        kernel.start_zone_fit(body=False)
        session = kernel.zone_fit_session
        run(session, start=session.prepare_until)
        assert session.state == 'done'
        kernel._apply_zone_fit_locked()
        assert session.applied_grip
        assert session.applied_regions == []
        assert kernel.frozen_rects == initial
        assert kernel.zones_frozen
    finally:
        kernel.close()


def test_custom_points_skip_unrelated_default_exercises():
    original = normalize_zone_fit(None)
    original['zones']['leftHand']['bottom'] = .72
    session = ZoneFitSession(original, 100., excluded_zones=tuple(RUNTIME_BODY_ZONES))
    assert session.phases == ('stand',)
    run(session)
    assert session.state == 'done'
    assert session.result() == original
    assert session.values == {}
    assert set(session.status()['preserved_zones']) == set(RUNTIME_BODY_ZONES)


@pytest.mark.parametrize('noise', [.1, 2.4])
@pytest.mark.parametrize('algorithm', ['roll_tilt', 'head_responsive', 'gesture_v188'])
def test_motion_factor_applies_after_personal_noise_and_algorithm_floors(tmp_path, monkeypatch, noise, algorithm):
    controller = full_head(tmp_path, monkeypatch)
    controller.configure(horizontal_algorithm=algorithm)
    controller.noise_tilt = controller.noise_yaw = noise
    starts = []
    if algorithm == 'gesture_v188':
        def capture(*args, **kwargs):
            starts.append(kwargs['start_angle'])
            return 0.
        monkeypatch.setattr(controller._x_intent_v153, 'update', capture)
    controller.update(eyes(12.), W, H, now=1.)
    if algorithm == 'gesture_v188':
        baseline = starts[-1]
    else:
        baseline = controller.status()['tilt_deadzone_deg']
    controller.update(eyes(12.), W, H, now=1.04, motion_deadzone_scale=1.2)
    if algorithm != 'gesture_v188':
        assert controller.status()['tilt_deadzone_deg'] == pytest.approx(baseline * 1.2)
    else:
        assert starts[-1] == pytest.approx(baseline * 1.2)
    assert controller.config['deadzone'] == .1
    assert controller.status()['motion_deadzone_scale'] == 1.2
    controller.update(eyes(12.), W, H, now=1.08)
    if algorithm != 'gesture_v188':
        assert controller.status()['tilt_deadzone_deg'] == pytest.approx(baseline)
    assert controller.status()['motion_deadzone_scale'] == 1.


def test_switching_march_algorithm_invalidates_recorded_analysis():
    snapshot = {'march_algorithm': 'legacy', 'custom_poses': [], 'pose_actions': []}
    before = learning_signature(snapshot, {})
    snapshot['march_algorithm'] = 'responsive'
    assert learning_signature(snapshot, {}) != before
