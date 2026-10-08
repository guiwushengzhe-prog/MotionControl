from __future__ import annotations

from pathlib import Path
from web_source import read_web_js

import pytest

from motioncontrol_shared.motion_conflicts import (
    find_motion_conflicts,
    selected_motion_ids_from_bindings,
    selected_motion_ids_from_config,
    validate_motion_bindings,
    validate_motion_config,
)


# 开合跳下架后现在没有互斥的动作了（见 motion_conflicts）。规则本身还在，下面用一组
# 假的互斥来测它，免得以后再加一组时发现早就坏了。
@pytest.fixture
def squat_excludes_hands_up(monkeypatch):
    from motioncontrol_shared import motion_conflicts
    monkeypatch.setattr(motion_conflicts, "MOTION_CONFLICT_GROUPS", (("squat", "hands_up"),))


def test_there_are_no_conflicting_motions_now():
    validate_motion_bindings(
        {
            "motions": {
                "jump": {"action": {"type": "gamepad", "target": "A"}},
                "hands_up": {"action": {"type": "gamepad", "target": "Y"}},
                "side_step_jack": {"action": {"type": "gamepad", "target": "B"}},
            }
        }
    )


def test_a_conflict_group_is_refused_on_save(squat_excludes_hands_up):
    conflicts = find_motion_conflicts(("squat", "hands_up"))
    assert conflicts == (("squat", "hands_up"),)
    with pytest.raises(ValueError, match="下蹲.*双手举过头"):
        validate_motion_bindings(
            {
                "motions": {
                    "squat": {"action": {"type": "gamepad", "target": "A"}},
                    "hands_up": {"action": {"type": "gamepad", "target": "Y"}},
                }
            }
        )


def test_disabled_binding_does_not_count_as_selected(squat_excludes_hands_up):
    bindings = {
        "motions": {
            "squat": {"disabled": True},
            "hands_up": {"action": {"type": "gamepad", "target": "Y"}},
        }
    }
    assert selected_motion_ids_from_bindings(bindings) == {"hands_up"}
    validate_motion_bindings(bindings)


def test_legacy_config_conflicts_are_rejected_only_when_enabled(squat_excludes_hands_up):
    items = [
        {"id": "squat", "enabled": True, "target": "A"},
        {"id": "hands_up", "enabled": False, "target": "Y"},
    ]
    assert selected_motion_ids_from_config(items) == {"squat"}
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


def test_the_mapping_table_greys_out_downloaded_motions_too():
    """下蹲、双手举过头是从官方动作库下载的，不在程序自带的那份触发器里。

    界面上的互斥原来只遍历自带的原地踏步、小腿向后抬起，于是选了开合跳，双手举过头
    照样能选，要到保存时才被服务端退回来。tools/check_ui_v2.cjs 在浏览器里走了一遍。
    """
    app = read_web_js()
    body = app[app.index("function syncMotionConflictChoices()"):]
    body = body[:body.index("\n}\n")]
    assert "profileTriggers().filter(item=>item.group==='motions')" in body
    assert "BASE_PROFILE_TRIGGERS" not in body
