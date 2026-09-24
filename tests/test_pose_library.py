"""动作库：每个内置动作都有名字、怎么做、火柴人示范，以及做的时候会扫过哪些圈。"""

from __future__ import annotations

from motioncontrol_shared import describe, motion_conflicts, pose_library
from motioncontrol_shared.pose_library import library_payload, sweeps_first, zone_crossers


def test_every_built_in_action_has_a_demo_and_a_how_to():
    payload = library_payload()
    ids = {item["id"] for item in payload}
    assert ids == set(describe.MOTION_NAMES) | set(describe.POSE_NAMES)
    for item in payload:
        assert item["how"], item["id"]
        frames = item["demo"]["frames"]
        assert len(frames) >= 2, f"{item['id']} 的示范要能动"
        for frame in frames:
            for x, y in frame["points"].values():
                assert 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0


def test_names_are_written_once():
    """以前 motion_conflicts 叫「双手过头」、界面叫「双手举过头」，各说各的。"""
    assert motion_conflicts.MOTION_DISPLAY_NAMES["hands_up"] == describe.MOTION_NAMES["hands_up"]
    assert describe.MOTION_NAMES["calf_back"] == "小腿向后抬起"


def test_which_actions_sweep_which_zones():
    """2026-09-25 的真人录像里量出来的：举双手扫过两边手区，开合跳还会跳进头顶区。"""
    assert set(zone_crossers("leftHand")) >= {"motion.hands_up", "motion.jumping_jack"}
    assert "motion.jumping_jack" in zone_crossers("headJump")
    assert "motion.march" not in zone_crossers("leftFoot"), "踏步是往上抬，不往外伸"
    assert sweeps_first("motion.hands_up"), "手还没举到头顶，就先扫过了手区"
    assert not sweeps_first("motion.calf_back"), "脚是动作认出来之后才碰到脚区的"
    known = {zone for entry in pose_library.LIBRARY for zone in entry["passes_zones"]}
    assert known <= {"leftHand", "rightHand", "leftFoot", "rightFoot", "headJump"}
