"""首步响应实验的起停行为；仅使用合成抬脚高度。"""

import pytest

from motioncontrol.responsive_march import ResponsiveMarch


@pytest.mark.parametrize("side", ["left", "right"])
def test_first_step_confirms_one_foot_then_stops_on_hold_or_ground(side):
    c = ResponsiveMarch()
    other = "right" if side == "left" else "left"
    assert not c.update({side: .10, other: 0.}, 1., first_step=True)
    assert c.update({side: .12, other: 0.}, 1.04, first_step=True)
    assert c.update({side: .14, other: 0.}, 1.08, first_step=True)
    for i in range(1, 10):
        active = c.update({side: .14, other: 0.}, 1.08 + i / 30, first_step=True)
    assert not active
    for i in range(10, 35):
        assert not c.update({side: .14, other: 0.}, 1.08 + i / 30, first_step=True)
    c.reset()
    c.update({side: .10, other: 0.}, 2., first_step=True)
    assert c.update({side: .12, other: 0.}, 2.04, first_step=True)
    assert c.update({side: 0., other: 0.}, 2.08, first_step=True)
    for i in range(1, 6):
        active = c.update({side: 0., other: 0.}, 2.08 + i / 30, first_step=True)
    assert not active


def test_first_step_reuses_alternation_and_default_still_waits_for_second_foot():
    fast, original = ResponsiveMarch(), ResponsiveMarch()
    for c, enabled in ((fast, True), (original, False)):
        assert not c.update({"left": .10, "right": 0.}, 1., first_step=enabled)
        assert c.update({"left": .12, "right": 0.}, 1.04, first_step=enabled) == enabled
        c.update({"left": 0., "right": 0.}, 1.08, first_step=enabled)
        assert c.update({"left": 0., "right": .12}, 1.16, first_step=enabled)
        assert c.update({"left": .03, "right": 0.}, 1.3, first_step=enabled)


@pytest.mark.parametrize("case", ["jumping", "two_feet", "excluded", "blocked", "small_noise"])
def test_first_step_does_not_start_without_single_valid_foot(case):
    c = ResponsiveMarch()
    for i in range(15):
        lifts = {"left": .12, "right": 0.}
        kwargs = {"first_step": True}
        if case == "jumping":
            kwargs["jumping"] = True
        elif case == "two_feet":
            lifts["right"] = .12
        elif case == "excluded":
            kwargs["excluded"] = ("left",)
        elif case == "blocked":
            kwargs["blocked"] = True
        else:
            lifts["left"] = .007 * (i % 3)
        assert not c.update(lifts, 1. + i / 30, **kwargs)


def test_jump_without_simultaneous_foot_evidence_cannot_start_alternation():
    c = ResponsiveMarch()
    for i in range(20):
        lifts = {"left": .12 if i < 8 else 0., "right": .12 if i >= 8 else 0.}
        assert not c.update(lifts, 1. + i / 30, first_step=True, jumping=True)
