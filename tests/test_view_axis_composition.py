"""视角组合的真实内核输出、失联释放与旧设置兼容。"""
import json

import pytest

from motioncontrol.axis_hand_mouse import AxisHandMouseController
from motioncontrol.control_kernel import ControlKernel
from motioncontrol.head_control import HeadController, HEAD_SIGNAL_VERSION
from motioncontrol.user_paths import user_path
from test_body_motion_head_guard import Head
from test_hand_mouse_kernel import _Output, body, feed


@pytest.mark.parametrize('hand', ['left', 'right'])
def test_vertical_fist_does_not_mute_head_and_release_stops_only_vertical(hand):
    output = _Output()
    kernel = ControlKernel(output)
    kernel.head_controller = Head(.7)
    try:
        kernel.configure_hand_mouse({'enabled': True, 'horizontal_hand': 'off',
                                     'vertical_hand': hand, 'deadzone': 0})
        feed(kernel, body(hand=hand, wrist=(.5, .3), spread=.2))
        feed(kernel, body(hand=hand, wrist=(.5, .4), spread=.2))
        assert output.applied[-1][0] == .7
        assert output.applied[-1][1] > 0
        assert kernel.head['output_x'] == .7
        assert kernel.head['output_y'] == output.applied[-1][1]
        feed(kernel, body(hand=hand, wrist=(.5, .4), spread=.8))
        assert output.applied[-1] == (.7, 0)
        feed(kernel, body(hand=hand, wrist=(.5, .4), spread=.2))
        missing = {k: v for k, v in body().items() if not k.startswith(hand)}
        feed(kernel, missing)
        assert output.applied[-1] == (.7, 0)
    finally:
        kernel.close()


def test_unused_hand_is_not_requested_or_engaged():
    c = AxisHandMouseController()
    c.configure({'horizontal_hand': 'off', 'vertical_hand': 'left'})
    assert c.tracking_request() == {'enabled': True, 'hand': 'left', 'hands': ['left']}
    c.update(body(hand='right', spread=.2), 1)
    assert not c.owns_hand('right')
    c.configure({'vertical_hand': 'off'})
    assert not c.tracking_request()['enabled']
    assert c.tracking_request()['hands'] == []
    assert c.compose_output(.7, -.3) == (.7, -.3)


def test_horizontal_fist_preserves_other_vertical_source():
    c = AxisHandMouseController()
    c.configure({'horizontal_hand': 'right', 'vertical_hand': 'off', 'deadzone': 0})
    c.update(body(wrist=(.5, .3), spread=.2), 1)
    c.update(body(wrist=(.6, .4), spread=.2), 2)
    x, y = c.compose_output(.7, -.3)
    assert x < 0 and y == -.3


def test_fresh_user_gets_tilt_and_left_vertical():
    kernel = ControlKernel(_Output())
    try:
        assert kernel.head_controller.config['horizontal_algorithm'] == 'roll_tilt'
        assert kernel.hand_mouse_controller.config['enabled']
        assert kernel.hand_mouse_controller.config['horizontal_hand'] == 'off'
        assert kernel.hand_mouse_controller.config['vertical_hand'] == 'left'
        assert not kernel.vertical_look['enabled']
        assert not kernel.body_motion_guard_enabled
    finally:
        kernel.close()


def test_single_axis_choice_survives_restart():
    first = ControlKernel(_Output())
    try:
        first.configure_hand_mouse({'horizontal_hand': 'off', 'vertical_hand': 'right'})
    finally:
        first.close()
    second = ControlKernel(_Output())
    try:
        assert second.hand_mouse_controller.tracking_request()['hands'] == ['right']
        assert second.hand_mouse_controller.config['horizontal_hand'] == 'off'
    finally:
        second.close()


def test_first_head_calibration_save_does_not_restore_two_hand_defaults():
    first = ControlKernel(_Output())
    try:
        first.head_controller._save_profile()
    finally:
        first.close()
    second = ControlKernel(_Output())
    try:
        assert second.hand_mouse_controller.config['horizontal_hand'] == 'off'
        assert second.hand_mouse_controller.config['vertical_hand'] == 'left'
    finally:
        second.close()


@pytest.mark.parametrize('policy', ['gesture_v188', 'gesture_v153', 'frozen22', 'roll_tilt', None])
def test_existing_settings_are_preserved(policy):
    params = {'enabled': False}
    if policy is not None:
        params['horizontal_algorithm'] = policy
    user_path('head_profile').write_text(json.dumps({'signal_version': HEAD_SIGNAL_VERSION,
                                                   'params': params}), encoding='utf-8')
    user_path('general_settings').write_text(json.dumps({'hand_mouse': {
        'enabled': True, 'horizontal_hand': 'right', 'vertical_hand': 'right', 'sensitivity': 73,
    }}), encoding='utf-8')
    kernel = ControlKernel(_Output())
    try:
        assert kernel.head_controller.config['horizontal_algorithm'] == (policy or 'gesture_v153')
        assert not kernel.head_controller.config['enabled']
        assert kernel.hand_mouse_controller.config['horizontal_hand'] == 'right'
        assert kernel.hand_mouse_controller.config['vertical_hand'] == 'right'
        assert kernel.hand_mouse_controller.config['sensitivity'] == 73
    finally:
        kernel.close()
