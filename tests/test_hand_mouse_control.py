"""Fist-gated hand steering.

The pose model has no finger joints -- only wrist, thumb, index and pinky -- so
a fist is inferred from how far the fingertips sit from the wrist, normalised
by the forearm. These cover the properties that make that usable rather than
the exact threshold values, which are defaults meant to be tuned against the
live `spread` reading.
"""

from __future__ import annotations

import pytest

from hand_mouse_control import DEFAULT_CONFIG, HandMouseController, merge_config


def pose(hand="right", *, spread=0.5, wrist=(0.5, 0.5), forearm=0.2, visibility=0.9):
    """A pose map with the fingertips placed at a chosen spread."""
    wx, wy = wrist
    tip_distance = spread * forearm
    points = {
        f"{hand}_elbow": {"x": wx, "y": wy + forearm, "score": visibility},
        f"{hand}_wrist": {"x": wx, "y": wy, "score": visibility},
        f"{hand}_thumb": {"x": wx - tip_distance, "y": wy, "score": visibility},
        f"{hand}_index": {"x": wx, "y": wy - tip_distance, "score": visibility},
        f"{hand}_pinky": {"x": wx + tip_distance, "y": wy, "score": visibility},
    }
    return points


def controller(**overrides):
    ctl = HandMouseController()
    ctl.configure({"enabled": True, **overrides})
    return ctl


# --- fist detection -------------------------------------------------------


def test_open_hand_does_not_engage():
    ctl = controller()
    state = ctl.update(pose(spread=0.6), now=1.0)
    assert state["engaged"] is False
    assert state["state"] == "open"


def test_closed_fist_engages():
    ctl = controller()
    state = ctl.update(pose(spread=0.2), now=1.0)
    assert state["engaged"] is True


def test_spread_is_normalised_by_forearm_not_pixels():
    """The same gesture at a different distance from the camera must read alike."""
    ctl = controller()
    near, _ = ctl.measure_spread(pose(spread=0.25, forearm=0.30), "right")
    far, _ = ctl.measure_spread(pose(spread=0.25, forearm=0.08), "right")
    assert near == pytest.approx(far, abs=1e-6)


def test_hysteresis_prevents_chatter():
    """A value between the two thresholds keeps whatever state we were in."""
    ctl = controller(fist_close=0.30, fist_open=0.40)
    ctl.update(pose(spread=0.20), now=1.0)
    assert ctl.engaged is True
    # In the band: still closed.
    ctl.update(pose(spread=0.35), now=1.1)
    assert ctl.engaged is True
    # Past the open threshold: released.
    ctl.update(pose(spread=0.45), now=1.2)
    assert ctl.engaged is False
    # Back in the band from open: stays open.
    ctl.update(pose(spread=0.35), now=1.3)
    assert ctl.engaged is False


def test_overlapping_thresholds_are_rejected():
    with pytest.raises(ValueError, match="抖动"):
        merge_config(DEFAULT_CONFIG, {"fist_close": 0.4, "fist_open": 0.3})


def test_too_few_visible_tips_reads_as_unknown_not_open():
    """Claiming "open" on missing data would silently drop an active fist."""
    ctl = controller(min_tips=3)
    points = pose(spread=0.2)
    points["right_thumb"]["score"] = 0.0
    state = ctl.update(points, now=1.0)
    assert state["spread"] is None
    assert state["state"] == "lost"
    assert state["engaged"] is False


# --- movement -------------------------------------------------------------


def test_closing_captures_an_anchor_and_emits_nothing_yet():
    ctl = controller()
    state = ctl.update(pose(spread=0.2, wrist=(0.5, 0.5)), now=1.0)
    assert state["output_x"] == 0.0 and state["output_y"] == 0.0
    assert ctl.anchor == (0.5, 0.5)


def test_moving_while_closed_drives_both_axes():
    ctl = controller(deadzone=0.0)
    ctl.update(pose(spread=0.2, wrist=(0.5, 0.5)), now=1.0)
    state = ctl.update(pose(spread=0.2, wrist=(0.58, 0.56)), now=1.1)
    assert state["output_x"] > 0
    assert state["output_y"] > 0


def test_image_down_moves_pointer_down():
    """Image y grows downward and so does mouse dy, so no flip belongs here."""
    ctl = controller(deadzone=0.0)
    ctl.update(pose(spread=0.2, wrist=(0.5, 0.5)), now=1.0)
    state = ctl.update(pose(spread=0.2, wrist=(0.5, 0.62)), now=1.1)
    assert state["output_y"] > 0
    assert state["output_x"] == 0.0


def test_invert_x_flips_only_the_horizontal_axis():
    ctl = controller(deadzone=0.0, invert_x=True)
    ctl.update(pose(spread=0.2, wrist=(0.5, 0.5)), now=1.0)
    state = ctl.update(pose(spread=0.2, wrist=(0.60, 0.60)), now=1.1)
    assert state["output_x"] < 0
    assert state["output_y"] > 0


def test_deadzone_suppresses_small_drift():
    ctl = controller(deadzone=0.5, range=0.5)
    ctl.update(pose(spread=0.2, wrist=(0.5, 0.5)), now=1.0)
    state = ctl.update(pose(spread=0.2, wrist=(0.51, 0.5)), now=1.1)
    assert state["output_x"] == 0.0


def test_output_is_clamped():
    ctl = controller(deadzone=0.0, sensitivity=200.0, range=0.1)
    ctl.update(pose(spread=0.2, wrist=(0.5, 0.5)), now=1.0)
    state = ctl.update(pose(spread=0.2, wrist=(0.9, 0.9)), now=1.1)
    assert -1.0 <= state["output_x"] <= 1.0
    assert -1.0 <= state["output_y"] <= 1.0
    assert state["output_x"] == 1.0


def test_reopening_and_closing_recentres():
    """Like lifting a mouse off the desk: the anchor follows the new position."""
    ctl = controller(deadzone=0.0)
    ctl.update(pose(spread=0.2, wrist=(0.5, 0.5)), now=1.0)
    ctl.update(pose(spread=0.2, wrist=(0.7, 0.5)), now=1.1)
    ctl.update(pose(spread=0.6, wrist=(0.7, 0.5)), now=1.2)  # open
    state = ctl.update(pose(spread=0.2, wrist=(0.7, 0.5)), now=1.3)  # close again
    assert ctl.anchor == (0.7, 0.5)
    assert state["output_x"] == 0.0


def test_releasing_zeroes_the_output():
    ctl = controller(deadzone=0.0)
    ctl.update(pose(spread=0.2, wrist=(0.5, 0.5)), now=1.0)
    ctl.update(pose(spread=0.2, wrist=(0.8, 0.8)), now=1.1)
    state = ctl.update(pose(spread=0.7, wrist=(0.8, 0.8)), now=1.2)
    assert state["output_x"] == 0.0 and state["output_y"] == 0.0
    assert state["state"] == "opened"


def test_losing_the_hand_releases_rather_than_freezing():
    """A stuck engagement would keep steering from a stale anchor."""
    ctl = controller(deadzone=0.0)
    ctl.update(pose(spread=0.2, wrist=(0.5, 0.5)), now=1.0)
    ctl.update(pose(spread=0.2, wrist=(0.7, 0.7)), now=1.1)
    state = ctl.update({}, now=1.2)
    assert state["engaged"] is False
    assert state["output_x"] == 0.0 and state["output_y"] == 0.0
    assert state["state"] == "lost"


# --- hand selection and config -------------------------------------------


def test_the_other_hand_is_ignored():
    ctl = controller(hand="left")
    state = ctl.update(pose(hand="right", spread=0.2), now=1.0)
    assert state["engaged"] is False
    assert state["state"] == "lost"


def test_left_hand_works_the_same():
    ctl = controller(hand="left", deadzone=0.0)
    ctl.update(pose(hand="left", spread=0.2, wrist=(0.5, 0.5)), now=1.0)
    state = ctl.update(pose(hand="left", spread=0.2, wrist=(0.6, 0.5)), now=1.1)
    assert state["engaged"] is True
    assert state["output_x"] > 0


def test_disabled_controller_emits_nothing():
    ctl = HandMouseController()
    state = ctl.update(pose(spread=0.1), now=1.0)
    assert state["engaged"] is False
    assert state["state"] == "disabled"


def test_unknown_setting_is_rejected():
    with pytest.raises(ValueError, match="unknown hand mouse setting"):
        merge_config(DEFAULT_CONFIG, {"nonsense": 1})


def test_invalid_hand_is_rejected():
    with pytest.raises(ValueError, match="hand must be"):
        merge_config(DEFAULT_CONFIG, {"hand": "foot"})


def test_reconfiguring_drops_the_anchor():
    """Keeping it would make the pointer jump when the next fist closes."""
    ctl = controller(deadzone=0.0)
    ctl.update(pose(spread=0.2, wrist=(0.5, 0.5)), now=1.0)
    assert ctl.anchor is not None
    ctl.configure({"sensitivity": 80.0})
    assert ctl.anchor is None
    assert ctl.engaged is False


def test_status_reports_spread_for_calibration():
    ctl = controller()
    state = ctl.update(pose(spread=0.33), now=1.0)
    assert state["spread"] == pytest.approx(0.33, abs=1e-3)
    assert state["tips_seen"] == 3
