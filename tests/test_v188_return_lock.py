"""Focused return-clutch checks for the gesture_v188 horizontal ratchet."""

import pytest

from head_control import _RelativeYawAxisV153


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


def test_return_lock_requires_center_settle_and_delays_opposite_rearm():
    axis = _RelativeYawAxisV153()
    axis._return_latched = True
    axis._return_from_direction = 1

    # Crossing the centre and reaching a small opposite deflection is still
    # part of the return; the latch must remain closed.
    outputs = [_step(axis, norm, index) for index, norm in enumerate((0.0, -0.03, -0.06, -0.09, -0.10, -0.11, -0.12))]
    assert outputs == pytest.approx([0.0] * len(outputs))
    assert axis.return_latched
    assert axis._return_center_seen

    # A clearly sustained opposite turn is the only path that can re-arm
    # before a neutral settle window completes.
    opposite_outputs = [_step(axis, norm, index + len(outputs)) for index, norm in enumerate((-0.13, -0.14, -0.15, -0.16, -0.17, -0.18, -0.19, -0.20, -0.21, -0.22))]
    assert opposite_outputs[:2] == pytest.approx([0.0, 0.0])
    assert any(output < 0.0 for output in opposite_outputs)
    assert axis.state == "TURN_LEFT"
    assert not axis.return_latched


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
