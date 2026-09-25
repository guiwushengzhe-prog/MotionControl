from __future__ import annotations

from pathlib import Path

import pytest

from motioncontrol_shared.motion_conflicts import (
    find_motion_conflicts,
    selected_motion_ids_from_bindings,
    selected_motion_ids_from_config,
    validate_motion_bindings,
    validate_motion_config,
)


def test_jumping_jack_and_hands_up_are_mutually_exclusive():
    conflicts = find_motion_conflicts(("jumping_jack", "hands_up"))
    assert conflicts == (("jumping_jack", "hands_up"),)
    with pytest.raises(ValueError, match="开合跳.*双手举过头"):
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


def test_other_motion_combinations_remain_configurable():
    bindings = {
        "motion.march": {"action": {"type": "gamepad_axis", "target": "LS_UP"}},
        "motion.calf_back": {"action": {"type": "gamepad", "target": "B"}},
        "motion.squat": {"action": {"type": "gamepad", "target": "X"}},
        "motion.side_step_jack": {"action": {"type": "gamepad", "target": "A"}},
    }
    assert selected_motion_ids_from_bindings(bindings) == {"march", "calf_back", "squat", "side_step_jack"}
    validate_motion_bindings(bindings)


def test_jumping_jack_and_side_step_jack_are_configurable_together():
    validate_motion_bindings(
        {
            "motions": {
                "jumping_jack": {"action": {"type": "gamepad", "target": "A"}},
                "side_step_jack": {"action": {"type": "gamepad", "target": "B"}},
            }
        }
    )


def test_the_mapping_table_greys_out_downloaded_motions_too():
    """开合跳、双手举过头是从官方动作库下载的，不在程序自带的那份触发器里。

    界面上的互斥原来只遍历自带的原地踏步、小腿向后抬起，于是选了开合跳，双手举过头
    照样能选，要到保存时才被服务端退回来。tools/check_ui_v2.cjs 在浏览器里走了一遍。
    """
    app = (Path(__file__).resolve().parent.parent / "web" / "app.js").read_text(encoding="utf-8")
    body = app[app.index("function syncMotionConflictChoices()"):]
    body = body[:body.index("\n}\n")]
    assert "profileTriggers().filter(item=>item.group==='motions')" in body
    assert "BASE_PROFILE_TRIGGERS" not in body
