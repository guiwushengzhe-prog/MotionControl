"""双手独立起点、方向归属、释放、迁移与内核输出的实验回归。"""
import pytest

from motioncontrol.axis_hand_mouse import AxisHandMouseController, normalize_config
from motioncontrol.control_kernel import ControlKernel
from test_hand_mouse_control import pose, hand_points
from test_hand_mouse_kernel import _Output, body, feed, fingers


def both(right=(0.65, 0.5), left=(0.3, 0.5), right_spread=0.2, left_spread=0.2):
    return {**pose('right', wrist=right, spread=right_spread),
            **pose('left', wrist=left, spread=left_spread)}


@pytest.mark.parametrize('horizontal,vertical', [('right','left'), ('left','right'), ('right','right'), ('left','left')])
def test_all_assignments_ignore_the_other_coordinate(horizontal, vertical):
    ctl = AxisHandMouseController()
    ctl.configure({'horizontal_hand': horizontal, 'vertical_hand': vertical, 'deadzone': 0})
    ctl.update(both(), 1)
    # 右手向右上、左手向左下：交换分配会改变两个方向的符号。
    state = ctl.update(both(right=(0.7, 0.45), left=(0.25, 0.55)), 2)
    assert state['output_x'] * (-1 if horizontal == 'right' else 1) > 0
    assert state['output_y'] * (-1 if vertical == 'right' else 1) > 0


def test_default_axes_ignore_cross_axis_motion():
    ctl = AxisHandMouseController()
    ctl.configure({'deadzone': 0})
    ctl.update(both(), 1)
    state = ctl.update(both(right=(0.65, 0.6), left=(0.4, 0.5)), 2)
    assert (state['output_x'], state['output_y']) == (0, 0)


@pytest.mark.parametrize('released', ['left', 'right'])
@pytest.mark.parametrize('loss', [False, True])
def test_release_or_loss_only_stops_that_hands_axis(released, loss):
    ctl = AxisHandMouseController()
    ctl.configure({'deadzone': 0})
    ctl.update(both(), 1)
    moving = both(right=(0.7, 0.55), left=(0.35, 0.55))
    ctl.update(moving, 2)
    if loss:
        moving = {k:v for k,v in moving.items() if not k.startswith(released)}
    else:
        moving.update(pose(released, spread=0.7, wrist=(0.7,0.55) if released=='right' else (0.35,0.55)))
    state = ctl.update(moving, 3)
    stopped, active = ('output_y','output_x') if released=='left' else ('output_x','output_y')
    assert state[stopped] == 0
    assert state[active] != 0


def test_each_hand_uses_its_own_finger_joints():
    ctl = AxisHandMouseController()
    state = ctl.update(both(), 1, {'right': hand_points(curl=2, wrist=(0.65,0.5), reach=0.05),
                                  'left': hand_points(curl=1, wrist=(0.3,0.5), reach=0.05)})
    assert state['hands']['right']['engaged'] is False
    assert state['hands']['left']['engaged'] is True
    assert all(s['grip_source']=='hand' for s in state['hands'].values())


def test_config_change_reanchors_and_invalid_change_is_atomic():
    ctl = AxisHandMouseController()
    ctl.update(both(), 1)
    with pytest.raises(ValueError):
        ctl.configure({'vertical_hand': 'foot'})
    assert ctl.engaged
    ctl.configure({'horizontal_hand':'left', 'vertical_hand':'right'})
    assert not ctl.engaged
    state = ctl.update(both(right=(0.7,0.55),left=(0.35,0.55)), 2)
    assert (state['output_x'],state['output_y']) == (0,0)


def test_old_config_migrates_without_losing_tuning():
    config = normalize_config(None, {'hand':'left','sensitivity':55,'deadzone':0.2})
    assert config['horizontal_hand']=='left' and config['vertical_hand']=='right'
    assert config['sensitivity']==55 and config['deadzone']==0.2
    assert 'hand' not in config


def test_kernel_split_output_zones_loss_and_reconnect():
    output = _Output()
    kernel = ControlKernel(output)
    kernel.configure_hand_mouse({'horizontal_hand':'right', 'vertical_hand':'left', 'deadzone':0})
    initial = {**body(), **both()}
    feed(kernel, initial)
    feed(kernel, {**body(), **both(right=(0.7,0.5),left=(0.3,0.6))})
    assert output.applied[-1][0]<0 and output.applied[-1][1]>0
    assert kernel._hand_mouse_owns_zone('leftHand')
    assert kernel._hand_mouse_owns_zone('rightHand')
    assert kernel._hand_mouse_owns_zone('lookGate')
    assert not kernel._hand_mouse_owns_zone('leftFoot')
    with kernel._lock:
        kernel._clear_body_locked()
    assert not kernel.hand_mouse_controller.engaged
    assert output.applied[-1] == (0,0)
    feed(kernel, initial)
    assert output.applied[-1] == (0,0)


def test_kernel_delivers_both_hands_joints_and_saves_choices():
    kernel = ControlKernel(_Output())
    kernel.configure_hand_mouse({'horizontal_hand':'left','vertical_hand':'right'})
    kernel.handle_pose_map('test', {**body(), **both()}, width=640, height=480,
                          hands={'left':fingers(curl=1,wrist=(0.3,0.5)),
                                 'right':fingers(curl=2,wrist=(0.65,0.5))})
    state = kernel.hand_mouse_controller.status()
    assert state['hands']['left']['engaged'] and not state['hands']['right']['engaged']
    restored = ControlKernel(_Output()).hand_mouse_controller.config
    assert restored['horizontal_hand']=='left' and restored['vertical_hand']=='right'
