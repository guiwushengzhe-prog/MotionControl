"""回正跨帧与跟踪突跳不能造成持续锁死或鼠标猛冲。"""

import pytest

from motioncontrol.head_control import _RelativeYawAxisV153


PARAMS = dict(start_angle=.055, start_velocity=.12, keep_velocity=.045,
              return_velocity=.060, stop_grace_s=.075, acceleration_stop=1.15)


def advance(axis, value, now):
    return axis.update(value, now, raw_norm=value, **PARAMS)


def turning(direction=1, fps=30):
    axis = _RelativeYawAxisV153()
    for index in range(fps // 2 + 1):
        advance(axis, direction * .6 * index / fps, 1 + index / fps)
    assert axis.committed
    assert direction * axis.output > 0
    return axis, 1.5


@pytest.mark.parametrize('direction', [-1, 1])
@pytest.mark.parametrize('fps', [15, 25, 30, 60])
def test_continuous_crossing_without_a_center_sample_can_rearm(direction, fps):
    axis, now = turning(direction, fps)
    for value in (.24, .18, .12, .06, -.06):
        now += 1 / fps
        advance(axis, direction * value, now)
    assert axis.return_latched
    assert axis.output == 0
    # 过中心后的少量过冲仍然静默，不能直接变成反方向输入。
    for value in (-.08, -.10, -.12):
        now += 1 / fps
        assert advance(axis, direction * value, now) == 0
    outputs = []
    # 连续、明显的新反向动作才允许重新输出，不要求恰好采到零点。
    for index in range(fps):
        now += 1 / fps
        outputs.append(advance(axis, direction * (-.12 - .5 * (index + 1) / fps), now))
    assert any(direction * output < 0 for output in outputs)
    assert not axis.return_latched


@pytest.mark.parametrize('direction', [-1, 1])
def test_active_turn_rejects_a_tracking_spike_and_stationary_tail(direction):
    axis, now = turning(direction)
    # 已在输出时突然跳到远处；不能把估计器跳变当作转头加速。
    now += 1 / 30
    assert advance(axis, direction * 1.3, now) == 0
    for _ in range(30):
        now += 1 / 30
        assert advance(axis, direction * 1.3, now) == 0
    outputs = []
    for index in range(15):
        now += 1 / 30
        outputs.append(advance(axis, direction * (1.3 + .02 * (index + 1)), now))
    assert any(direction * output > 0 for output in outputs)


@pytest.mark.parametrize('direction', [-1, 1])
def test_single_tracking_jump_across_center_does_not_count_as_return(direction):
    axis, now = turning(direction)
    for value in (.24, .18, .12):
        now += 1 / 30
        advance(axis, direction * value, now)
    assert axis.return_latched
    for value in [-1.0] * 25:
        now += 1 / 30
        assert advance(axis, direction * value, now) == 0
    assert axis.return_latched
