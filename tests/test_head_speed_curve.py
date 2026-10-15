"""左右速度、偏转曲线与保存配置走实际控制链；输出使用内存替身。"""

import json

import pytest

from motioncontrol.control_kernel import ControlKernel
from motioncontrol.head_control import HeadController
from motioncontrol.intent_library import _NullOutput
from motioncontrol.output_backend import OutputManager
from motioncontrol.responsive_head_control import ResponsiveHeadControl
from motioncontrol.roll_tilt_control import RollTiltControl, gentle_turn_rate
from test_output_actions_v097 import Keyboard, Mouse, Pad
from test_roll_tilt_control import controller, eyes


@pytest.mark.parametrize('gamma', [1., 2., 3.])
def test_curve_is_the_configured_power_with_fixed_endpoints(gamma):
    assert gentle_turn_rate(0., gamma) == 0.
    assert gentle_turn_rate(1., gamma) == 1.
    assert gentle_turn_rate(.5, gamma) == pytest.approx(.5 ** gamma)
    assert gentle_turn_rate(.9, gamma) - gentle_turn_rate(.8, gamma) + 1e-12 >= (
        gentle_turn_rate(.2, gamma) - gentle_turn_rate(.1, gamma)
    )


@pytest.mark.parametrize('route', ['roll_tilt', 'head_responsive'])
def test_curve_keeps_symmetric_directions_and_start_stop_rules(route):
    trajectories = []
    for gamma in (1., 2., 3.):
        c = RollTiltControl() if route == 'roll_tilt' else ResponsiveHeadControl()
        trajectory = []
        angles = [0., 3., 4., 4., 4., 8., 12., 18., 0., -4., -4., -4., -8., -18., 0.]
        for frame, angle in enumerate(angles):
            if route == 'roll_tilt':
                value = c.update(angle, 1. + frame * .04, center=0., noise=.1,
                                 deadzone=.1, curve_gamma=gamma)
            else:
                value = c.update(angle, 0., 1. + frame * .04, center_tilt=0.,
                                 noise_tilt=.1, noise_yaw=.1, yaw_span=18.,
                                 deadzone=.1, curve_gamma=gamma)
            trajectory.append((c.state, value > 0, value < 0))
        trajectories.append(trajectory)
    assert trajectories[0] == trajectories[1] == trajectories[2]
    for gamma in (1., 2., 3.):
        values = []
        for sign in (-1., 1.):
            c = RollTiltControl() if route == 'roll_tilt' else ResponsiveHeadControl()
            for frame in range(15):
                if route == 'roll_tilt':
                    value = c.update(sign * 10., 1. + frame / 30, center=0., noise=.1,
                                     deadzone=.1, curve_gamma=gamma)
                else:
                    value = c.update(sign * 10., 0., 1. + frame / 30, center_tilt=0.,
                                     noise_tilt=.1, noise_yaw=.1, yaw_span=18.,
                                     deadzone=.1, curve_gamma=gamma)
            values.append(value)
        assert values[0] == pytest.approx(-values[1])


def test_roll_center_status_keeps_the_actual_noise_and_stability_threshold():
    c = RollTiltControl()
    assert c.update(0., 1., center=0., noise=1.5, deadzone=.15) == 0.
    assert c.threshold == pytest.approx(4.5)
    assert c.update(4., 1.04, center=0., noise=1.5, deadzone=.15) == 0.
    assert c.state == 'CENTER'
    responsive = ResponsiveHeadControl()
    assert responsive.update(0., 0., 1., center_tilt=0., noise_tilt=1.5,
                             noise_yaw=.1, yaw_span=18., deadzone=.15) == 0.
    assert responsive.tilt_stop_threshold == pytest.approx(4.5)


def test_curve_and_200_percent_save_reload_through_kernel_configuration():
    kernel = ControlKernel(_NullOutput())
    try:
        status = kernel.configure_head(sensitivity_x=200., sensitivity_y=200.,
                                       horizontal_curve_gamma=2.6)
        assert status['head']['sensitivity_x'] == 200.
        assert status['head']['sensitivity_y'] == 100.
        assert status['head']['horizontal_curve_gamma'] == 2.6
    finally:
        kernel.close()
    reloaded = ControlKernel(_NullOutput())
    try:
        assert reloaded.head_controller.config['sensitivity_x'] == 200.
        assert reloaded.head_controller.config['horizontal_curve_gamma'] == 2.6
        reloaded.configure_head(horizontal_curve_gamma=4., sensitivity_x=250.)
        assert reloaded.head_controller.config['horizontal_curve_gamma'] == 3.
        assert reloaded.head_controller.config['sensitivity_x'] == 200.
        reloaded.configure_head(horizontal_curve_gamma=.5)
        assert reloaded.head_controller.config['horizontal_curve_gamma'] == 1.
    finally:
        reloaded.close()


def test_old_profile_without_curve_uses_default(tmp_path):
    path = tmp_path / 'head.json'
    c = HeadController(path)
    c.configure(sensitivity_x=200., horizontal_curve_gamma=2.8)
    payload = json.loads(path.read_text(encoding='utf-8'))
    del payload['params']['horizontal_curve_gamma']
    path.write_text(json.dumps(payload), encoding='utf-8')
    reloaded = HeadController(path)
    assert reloaded.config['horizontal_curve_gamma'] == 2.
    assert reloaded.config['sensitivity_x'] == 200.


@pytest.mark.parametrize('direction', [-1., 1.])
def test_head_200_percent_reaches_double_horizontal_output(tmp_path, monkeypatch, direction):
    values = []
    for sensitivity in (100., 200.):
        c = controller(tmp_path, monkeypatch)
        c.configure(sensitivity_x=sensitivity, horizontal_curve_gamma=2.)
        for frame in range(30):
            value = c.update(eyes(direction * 25.), 640, 480, now=1. + frame / 30)[0]
        values.append(value)
    assert values[0] == pytest.approx(direction)
    assert values[1] == pytest.approx(2. * values[0])


def test_output_mouse_200_percent_doubles_x_without_expanding_y_or_gamepad(tmp_path, monkeypatch):
    clock = [1.]
    monkeypatch.setattr('motioncontrol.output_backend.time.monotonic', lambda: clock[0])

    class RecordingMouse(Mouse):
        def __init__(self):
            super().__init__()
            self.moves = []

        def move(self, dx, dy=0):
            self.moves.append((dx, dy))
            return True

    class RecordingPad(Pad):
        def set_right_stick(self, x, y=0):
            self.right_stick = (x, y)

    moves = []
    for x in (1., 2.):
        mouse = RecordingMouse()
        out = OutputManager(tmp_path, mouse=mouse, keyboard=Keyboard())
        try:
            out.set_config(enabled=True)
            out.last_update = .95
            out.apply(x, 2.)
            moves.append(mouse.moves[-1])
            assert out.last_y == 1.
            pad = RecordingPad()
            out._pad = pad
            out.set_config(mode='gamepad', gamepad_gain=3.)
            out.apply(2., -2.)
            assert pad.right_stick == (1., -1.)
        finally:
            out.close()
    assert moves[1][0] == 2 * moves[0][0]
    assert moves[1][1] == moves[0][1]


def test_kernel_keeps_200_percent_head_x_until_output_boundary(monkeypatch):
    class RecordingOutput(_NullOutput):
        def apply(self, x, y):
            self.last = (x, y)

    output = RecordingOutput()
    kernel = ControlKernel(output, persist=False)
    try:
        monkeypatch.setattr(kernel.head_controller, 'update', lambda *args, **kwargs: (2., 0.))
        kernel.hand_mouse_controller.configure({'enabled': False})
        kernel._update_head_locked(eyes(18.), 1.)
        assert output.last == (2., 0.)
    finally:
        kernel.close()
