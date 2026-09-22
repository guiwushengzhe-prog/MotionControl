"""小回摆不掐断外转，真正回正仍停止；两侧都必须成立。"""

import pytest

from motioncontrol.head_control import _RelativeYawAxisV153


def feed(axis, value, index):
    return axis.update(
        value, index / 30.0, raw_norm=value,
        start_angle=0.055, start_velocity=0.12, keep_velocity=0.045,
        return_velocity=0.060, stop_grace_s=0.075, acceleration_stop=1.15,
    )


def outward_turn(direction):
    axis = _RelativeYawAxisV153()
    for index in range(11):
        output = feed(axis, direction * index * 0.03, index)
    assert direction * output > 0
    assert axis.committed
    return axis


@pytest.mark.parametrize("direction", [-1, 1])
def test_small_correction_keeps_outward_turn_active(direction):
    axis = outward_turn(direction)
    # 四帧回摆合计 0.03，再继续外转；旧版在回摆末帧误锁回正。
    correction = [0.30 - 0.03 * (index + 1) / 4 for index in range(4)]
    continuation = [0.30 + index * 0.03 for index in range(6)]
    for index, value in enumerate(correction + continuation, start=11):
        assert direction * feed(axis, direction * value, index) > 0
        assert not axis.return_latched


@pytest.mark.parametrize("direction", [-1, 1])
def test_real_return_stops_and_stays_silent_to_center(direction):
    axis = outward_turn(direction)
    for index, value in enumerate([0.26, 0.22, 0.18, 0.12, 0.06, 0.0], start=11):
        output = feed(axis, direction * value, index)
        # 与压缩包基线一致：连续回正三帧（约 0.1 秒）后停止，且到中心都不反向。
        if index >= 13:
            assert output == 0.0
            assert axis.return_latched
    for index in range(17, 28):
        assert feed(axis, 0.0, index) == 0.0
    assert not axis.return_latched
