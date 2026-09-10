from __future__ import annotations

import pytest

from motion_conflicts import (
    find_motion_conflicts,
    selected_motion_ids_from_bindings,
    selected_motion_ids_from_config,
    validate_motion_bindings,
    validate_motion_config,
)


def test_jumping_jack_and_hands_up_are_mutually_exclusive():
    conflicts = find_motion_conflicts(("jumping_jack", "hands_up"))
    assert conflicts == (("jumping_jack", "hands_up"),)
    with pytest.raises(ValueError, match="开合跳.*双手过头"):
        validate_motion_bindings(
            {
                "motions": {
                    "jumping_jack": {"action": {"type": "gamepad", "target": "A"}},
                    "hands_up": {"action": {"type": "gamepad", "target": "Y"}},
                }
            }
        )


def test_disabled_binding_does_not_count_as_selected():
    bindings = {
        "motions": {
            "jumping_jack": {"disabled": True},
            "hands_up": {"action": {"type": "gamepad", "target": "Y"}},
        }
    }
    assert selected_motion_ids_from_bindings(bindings) == {"hands_up"}
    validate_motion_bindings(bindings)


def test_legacy_config_conflicts_are_rejected_only_when_enabled():
    items = [
        {"id": "jumping_jack", "enabled": True, "target": "A"},
        {"id": "hands_up", "enabled": False, "target": "Y"},
    ]
    assert selected_motion_ids_from_config(items) == {"jumping_jack"}
    validate_motion_config(items)
    items[1]["enabled"] = True
    with pytest.raises(ValueError, match="动作不能同时映射"):
        validate_motion_config(items)


def test_leg_motion_conflict_group_and_flattened_bindings():
    bindings = {
        "motion.march": {"action": {"type": "gamepad_axis", "target": "LS_UP"}},
        "motion.squat": {"action": {"type": "gamepad", "target": "X"}},
    }
    assert selected_motion_ids_from_bindings(bindings) == {"march", "squat"}
    with pytest.raises(ValueError, match="原地踏步.*下蹲"):
        validate_motion_bindings(bindings)

