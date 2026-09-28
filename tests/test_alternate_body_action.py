"""Body-action outputs selected by a held gesture or a resettable sequence."""

from pathlib import Path

import pytest

from motioncontrol.control_kernel import ControlKernel
from motioncontrol.game_profiles import GameProfileStore
from motioncontrol_shared.profile_schema import normalize_overrides


ROOT = Path(__file__).resolve().parents[1]
C = {"type": "keyboard", "target": "C", "behavior": "hold"}
Z = {"type": "keyboard", "target": "Z", "behavior": "hold"}
X = {"type": "keyboard", "target": "X", "behavior": "hold"}
CYCLE = {"action": C, "alternate_mode": "cycle", "alternate_action": Z,
         "reset_trigger": "zone.headJump"}
CONDITION = {"action": C, "alternate_mode": "with_trigger", "alternate_action": Z,
             "alternate_when": "pose.hands_cross"}


class Output:
    def __init__(self):
        self.holds = []
        self.pulses = []

    def set_action_holds(self, holds, source_group="controls"):
        self.holds = list(holds)

    def execute_action(self, action):
        self.pulses.append(dict(action))

    def set_buttons(self, *args, **kwargs):
        pass

    def set_holds(self, *args, **kwargs):
        pass

    def apply(self, *args, **kwargs):
        pass

    def status(self):
        return {}


def dispatch(kernel, *, squat=False, modifier=False, reset=False, now=1.):
    with kernel._lock:
        kernel.motion_active = {"squat"} if squat else set()
        kernel.pose_active = {"hands_cross"} if modifier else set()
        kernel.zone_state["headJump"]["pressed"] = reset
        kernel._dispatch_controls_locked(now)


def squat_key(output):
    return [item["action"]["target"] for item in output.holds
            if item["id"] == "motion.squat"]


def test_two_step_cycle_and_unmapped_head_zone_reset():
    output = Output()
    kernel = ControlKernel(output)
    try:
        kernel.configure_bindings({"motions": {"squat": CYCLE},
                                   "zones": {"headJump": {"disabled": True}}})
        phone = []
        kernel.configure_trigger_listener(phone.append)
        for index, expected in enumerate(("C", "Z", "C")):
            dispatch(kernel, squat=True, now=1.+index)
            assert squat_key(output) == [expected]
            for frame in range(5):
                dispatch(kernel, squat=True, now=1.+index+(frame+1)/60)
                assert squat_key(output) == [expected]
            assert phone[-1]["fired"][0]["action"]["target"] == expected
            assert kernel.status()["recent_triggers"][-1]["action"]["target"] == expected
            dispatch(kernel, now=1.5+index)
            assert squat_key(output) == []
        dispatch(kernel, reset=True, now=4.5)
        dispatch(kernel, reset=True, now=4.6)
        dispatch(kernel, now=4.7)
        dispatch(kernel, squat=True, now=5.)
        assert squat_key(output) == ["C"]
    finally:
        kernel.close()


def test_optional_third_step_and_reset_while_primary_held():
    output = Output()
    kernel = ControlKernel(output)
    try:
        kernel.configure_bindings({"motions": {"squat": {**CYCLE, "extra_actions": [X]}},
                                   "zones": {"headJump": {"disabled": True}}})
        for index, expected in enumerate(("C", "Z", "X", "C")):
            dispatch(kernel, squat=True, now=1.+index)
            assert squat_key(output) == [expected]
            dispatch(kernel, now=1.4+index)
        dispatch(kernel, squat=True, now=5.)
        assert squat_key(output) == ["Z"]
        dispatch(kernel, squat=True, reset=True, now=5.1)
        assert squat_key(output) == ["Z"]
        dispatch(kernel, now=5.2)
        dispatch(kernel, squat=True, now=5.3)
        assert squat_key(output) == ["C"]
    finally:
        kernel.close()


def test_reset_zone_works_even_when_a_chain_manages_its_output():
    output = Output()
    kernel = ControlKernel(output)
    try:
        kernel.configure_bindings({"motions": {"squat": CYCLE}})
        dispatch(kernel, squat=True, now=1.)
        dispatch(kernel, now=1.1)
        dispatch(kernel, squat=True, now=1.2)
        assert squat_key(output) == ["Z"]
        dispatch(kernel, now=1.3)
        kernel.configure_action_chain({"enabled": True, "chains": [{
            "id": "jump_squat", "start": "zone.headJump", "continue_condition": "motion.squat",
            "sustain_condition": "motion.squat", "end_condition": "motion.stand",
            "hold_action": "zone.headJump"}]})
        dispatch(kernel, reset=True, now=2.)
        dispatch(kernel, now=2.1)
        dispatch(kernel, squat=True, now=2.2)
        assert squat_key(output) == ["C"]
    finally:
        kernel.close()


def test_companion_action_selects_only_at_primary_onset():
    output = Output()
    kernel = ControlKernel(output)
    try:
        kernel.configure_bindings({"motions": {"squat": CONDITION}})
        for index in range(3):
            dispatch(kernel, squat=True, now=1.+index)
            assert squat_key(output) == ["C"]
            dispatch(kernel, now=1.4+index)
        dispatch(kernel, modifier=True, now=4.)
        dispatch(kernel, squat=True, modifier=True, now=4.1)
        assert squat_key(output) == ["Z"]
        dispatch(kernel, squat=True, now=4.2)
        assert squat_key(output) == ["Z"]
        dispatch(kernel, now=4.3)
        dispatch(kernel, squat=True, now=4.4)
        assert squat_key(output) == ["C"]
    finally:
        kernel.close()


def test_conditional_second_output_taps_once():
    output = Output()
    kernel = ControlKernel(output)
    try:
        kernel.configure_bindings({"motions": {"squat": {**CONDITION,
                                   "alternate_action": {**Z, "behavior": "tap"}}}})
        for frame in range(20):
            dispatch(kernel, squat=True, modifier=True, now=1.+frame/30)
        assert squat_key(output) == []
        assert [action["target"] for action in output.pulses] == ["Z"]
    finally:
        kernel.close()


@pytest.mark.parametrize("binding", [CYCLE, CONDITION, {**CYCLE, "extra_actions": [X]}])
def test_rules_round_trip_with_game_profile(isolated_user_data, binding):
    first = GameProfileStore(ROOT)
    first.set_overrides({"motion.squat": binding})
    second = GameProfileStore(ROOT)
    expected = normalize_overrides({"motion.squat": binding})["motion.squat"]
    assert second.effective_profile()["bindings"]["motions"]["squat"] == expected


@pytest.mark.parametrize("trigger,binding", [
    ("motion.squat", {**CYCLE, "alternate_action": {"type": "keyboard", "target": "UNKNOWN"}}),
    ("motion.squat", {**CYCLE, "alternate_action": {**Z, "behavior": "release"}}),
    ("motion.squat", {**CYCLE, "reset_trigger": "motion.squat"}),
    ("motion.squat", {**CONDITION, "alternate_when": "motion.squat"}),
    ("motion.squat", {**CONDITION, "alternate_when": "voice.foo"}),
    ("motion.squat", {**CYCLE, "reset_trigger": ""}),
    ("motion.squat", {**CYCLE, "extra_actions": "X"}),
    ("motion.squat", {"action": C, "alternate_action": Z}),
    ("zone.leftHand", CYCLE),
    ("voice.game.profile_slot_01", CONDITION),
])
def test_invalid_or_non_body_rule_is_rejected(trigger, binding):
    with pytest.raises(ValueError):
        normalize_overrides({trigger: binding})
