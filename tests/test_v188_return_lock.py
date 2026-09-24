"""Focused return-clutch checks for the gesture_v188 horizontal ratchet."""

import pytest

from motioncontrol.head_control import _RelativeYawAxisV153


_UPDATE = dict(
    start_angle=0.055,
    start_velocity=0.12,
    keep_velocity=0.045,
    return_velocity=0.060,
    stop_grace_s=0.075,
    acceleration_stop=1.15,
)


def _step(axis: _RelativeYawAxisV153, norm: float, index: int) -> float:
    now = index / 30.0
    return axis.update(norm, now, raw_norm=norm, **_UPDATE)


def _committed_right_turn() -> _RelativeYawAxisV153:
    axis = _RelativeYawAxisV153()
    for index, norm in enumerate((0.0, 0.03, 0.06, 0.09, 0.12, 0.15, 0.18, 0.20)):
        _step(axis, norm, index)
    assert axis.committed
    assert axis.state == "TURN_RIGHT"
    return axis


def test_committed_turn_latches_on_clear_return_before_center():
    axis = _committed_right_turn()

    _step(axis, 0.20, 8)
    _step(axis, 0.20, 9)
    _step(axis, 0.17, 10)
    output = _step(axis, 0.13, 11)

    assert output == pytest.approx(0.0)
    assert axis.state == "RETURNING"
    assert axis.return_latched


def test_center_crossing_before_latch_blocks_old_low_reverse_gate():
    axis = _committed_right_turn()

    # This is the failure shape from the replay: the committed turn reaches
    # the centre, then the next sample lands around -0.14.
    _step(axis, 0.02, 8)
    output = _step(axis, -0.141, 9)

    assert output == pytest.approx(0.0)
    assert axis.state == "RETURNING"
    assert axis.return_latched
    assert axis._return_center_seen


def test_return_lock_does_not_resume_original_side_before_center_settles():
    axis = _RelativeYawAxisV153()
    axis._return_latched = True
    axis._return_from_direction = 1

    # A bounce on the original side remains muted; it is not a continuation
    # until the calibrated centre corridor has been reached and settled.
    outputs = [_step(axis, norm, index) for index, norm in enumerate((0.0, 0.03, 0.05, 0.07, 0.08, 0.09))]

    assert outputs == pytest.approx([0.0] * len(outputs))
    assert axis.return_latched
    assert axis.state == "RETURNING"


def test_pre_center_false_return_can_recover_sustained_original_turn():
    axis = _RelativeYawAxisV153()
    axis._return_latched = True
    axis._return_from_direction = 1

    # No centre sample has been seen: preserve the existing recovery path for
    # a filter reversal that settles back into the original turn direction.
    outputs = [_step(axis, norm, index + 1) for index, norm in enumerate((0.11, 0.12, 0.13, 0.14, 0.15, 0.16))]

    assert not axis._return_center_seen
    assert any(output > 0.0 for output in outputs)
    assert axis.state == "TURN_RIGHT"
    assert not axis.return_latched


def test_return_lock_stays_muted_through_a_low_angle_opposite_overshoot():
    axis = _RelativeYawAxisV153()
    axis._return_latched = True
    axis._return_from_direction = 1

    # Crossing the centre and reaching a small opposite deflection is still
    # part of the return; the latch must remain closed.
    outputs = [_step(axis, norm, index) for index, norm in enumerate((0.0, -0.03, -0.06, -0.09, -0.10, -0.11, -0.12))]

    assert outputs == pytest.approx([0.0] * len(outputs))
    assert axis.return_latched
    assert axis._return_center_seen
    assert axis.state == "RETURNING"


def test_return_lock_does_not_rearm_on_clear_but_continuous_opposite_turn():
    axis = _RelativeYawAxisV153()
    axis._return_latched = True
    axis._return_from_direction = 1

    assert _step(axis, 0.0, 0) == pytest.approx(0.0)
    outputs = [
        _step(axis, norm, index)
        for index, norm in enumerate((-0.66, -0.70, -0.74, -0.78, -0.82, -0.86, -0.90, -0.94), start=1)
    ]

    assert outputs == pytest.approx([0.0] * len(outputs))
    assert axis.return_latched
    assert not axis._return_opposite_settled


@pytest.mark.parametrize("direction", [-1, 1])
def test_opposite_side_stop_then_new_outward_motion_rearms_symmetrically(direction):
    axis = _RelativeYawAxisV153()
    axis._return_latched = True
    axis._return_from_direction = -direction

    values = [0.0, direction * 0.08, direction * 0.14, direction * 0.20]
    for index, value in enumerate(values):
        assert _step(axis, value, index) == pytest.approx(0.0)
    # This is the opposite-side peak of the original return, not a new turn.
    for index in range(4, 10):
        assert _step(axis, direction * 0.20, index) == pytest.approx(0.0)
    assert axis.return_latched
    assert axis._return_opposite_settled

    outputs = []
    for index, value in enumerate((0.22, 0.24, 0.26, 0.28), start=10):
        outputs.append(_step(axis, direction * value, index))
    assert any(direction * output > 0.0 for output in outputs)
    assert not axis.return_latched
    assert axis.state == ("TURN_RIGHT" if direction > 0 else "TURN_LEFT")


@pytest.mark.parametrize("direction", [-1, 1])
def test_committed_hold_enters_stable_offset_within_three_frames(direction):
    axis = _RelativeYawAxisV153()
    for index in range(11):
        _step(axis, direction * 0.03 * index, index)
    assert axis.committed

    outputs = [_step(axis, direction * 0.30, index) for index in range(11, 14)]

    assert outputs[0] * direction > 0.0
    assert outputs[1] * direction > 0.0
    assert outputs[2] == pytest.approx(0.0)
    assert axis.state == "STABLE_OFFSET"
    assert not axis.committed


def test_center_settle_unlocks_normal_new_turn_detection():
    axis = _RelativeYawAxisV153()
    axis._return_latched = True
    axis._return_from_direction = 1

    for index in range(8):
        assert _step(axis, 0.0, index) == pytest.approx(0.0)
    assert not axis.return_latched
    assert axis.state == "CENTER"

    outputs = [_step(axis, norm, index + 8) for index, norm in enumerate((0.03, 0.06, 0.09, 0.12))]
    assert any(output > 0.0 for output in outputs)
    assert axis.state == "TURN_RIGHT"


@pytest.mark.parametrize("direction", [-1, 1])
def test_return_lock_settles_at_quiet_off_center_pose(direction):
    axis = _RelativeYawAxisV153()
    axis._return_latched = True
    axis._return_from_direction = direction

    outputs = [_step(axis, direction * 0.14, index) for index in range(12)]

    assert outputs == pytest.approx([0.0] * len(outputs))
    assert not axis.return_latched
    assert axis.state == "STABLE_OFFSET"
    assert axis._hold_anchor == pytest.approx(direction * 0.14)
    assert not axis._held_from_turn

    fresh_outward = [
        _step(axis, direction * value, index)
        for index, value in enumerate((0.16, 0.18, 0.20, 0.22, 0.24), start=12)
    ]
    assert any(direction * output > 0.0 for output in fresh_outward)


@pytest.mark.parametrize("direction", [-1, 1])
def test_quiet_return_offset_does_not_mute_the_next_cross_center_turn(direction):
    axis = _RelativeYawAxisV153()
    axis._return_latched = True
    axis._return_from_direction = direction
    for index in range(12):
        assert _step(axis, direction * 0.14, index) == pytest.approx(0.0)
    assert not axis.return_latched
    assert not axis._held_from_turn

    values = (0.10, 0.06, 0.02, -0.03, -0.08, -0.13, -0.18, -0.23)
    outputs = [
        _step(axis, direction * value, index)
        for index, value in enumerate(values, start=12)
    ]
    assert any(direction * output < 0.0 for output in outputs)


@pytest.mark.parametrize("direction", [-1, 1])
def test_return_lock_does_not_settle_during_slow_continuous_motion(direction):
    axis = _RelativeYawAxisV153()
    axis._return_latched = True
    axis._return_from_direction = -direction

    outputs = [
        _step(axis, direction * 0.012 * index, index)
        for index in range(1, 31)
    ]

    assert outputs == pytest.approx([0.0] * len(outputs))
    assert axis.return_latched
    assert axis.state == "RETURNING"
