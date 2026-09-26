"""动作库：每个动作都有名字、怎么做、火柴人示范、星级，以及做的时候会扫过哪些圈。

程序只自带原地踏步和小腿向后抬起，别的动作从云端官方动作库下载（conftest 默认
当作全都下载了）。"""

from __future__ import annotations

import copy
import json

import pytest

from motioncontrol_shared import describe, motion_conflicts, pose_library
from motioncontrol_shared.pose_library import (
    BODY_PART_NAMES, RATING_NAMES, library_payload, normalize_action, zone_crossers,
)
from conftest import official_pose_docs


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
    known = {zone for entry in pose_library.entries() for zone in entry["passes_zones"]}
    assert known <= {"leftHand", "rightHand", "leftFoot", "rightFoot", "headJump"}


# --- 内置和下载 ---------------------------------------------------------------

def test_only_march_and_calf_back_come_with_the_program(no_downloaded_actions):
    payload = library_payload()
    assert [item["id"] for item in payload] == ["march", "calf_back"]
    assert all(item["source"] == "builtin" for item in payload)
    assert set(describe.MOTION_NAMES) == {"march", "calf_back"}
    assert not describe.POSE_NAMES
    assert zone_crossers("leftHand") == ()


def test_downloaded_actions_join_the_library_and_the_names():
    payload = {item["id"]: item for item in library_payload()}
    assert payload["jumping_jack"]["source"] == "cloud"
    assert payload["jumping_jack"]["revision"] >= 1
    assert describe.MOTION_NAMES["jumping_jack"] == "开合跳"
    assert describe.POSE_NAMES["hands_cross"] == "双手交叉"


# --- 星级 -------------------------------------------------------------------

def test_every_action_has_all_three_ratings_and_at_least_one_body_part():
    for item in library_payload():
        assert set(item["ratings"]) == set(RATING_NAMES), item["id"]
        assert all(1 <= stars <= 5 for stars in item["ratings"].values()), item["id"]
        assert item["body_parts"], item["id"]
        assert set(item["body_parts"]) <= set(BODY_PART_NAMES), item["id"]
        assert all(1 <= stars <= 5 for stars in item["body_parts"].values()), item["id"]


def test_the_ratings_order_the_actions_the_way_a_player_would():
    """几条一眼就该对的：开合跳比举手累，下蹲练腿最多，提膝碰肘练核心最多。"""
    items = {item["id"]: item for item in library_payload()}
    assert items["jumping_jack"]["ratings"]["intensity"] > items["hands_up"]["ratings"]["intensity"]
    assert max(items, key=lambda ident: items[ident]["body_parts"].get("legs", 0)) == "squat"
    assert max(items, key=lambda ident: items[ident]["body_parts"].get("core", 0)) == "cross_knee_elbow"


# --- 官方动作文件 ------------------------------------------------------------

def _doc(ident="jumping_jack"):
    return copy.deepcopy(next(doc for doc in official_pose_docs() if doc["id"] == ident))


@pytest.mark.parametrize("change, message", [
    (lambda d: d.update(id="march"), "程序自带"),
    (lambda d: d.update(id="custom1"), "编号不对"),
    (lambda d: d["ratings"].update(intensity=6), "1~5"),
    (lambda d: d["ratings"].pop("recognition"), "星级要有且只有"),
    (lambda d: d.update(body_parts={}), "至少要写一个"),
    (lambda d: d["body_parts"].update(neck=2), "不认识的锻炼部位"),
    (lambda d: d["demo"]["frames"].pop(), "2~8 帧"),
    (lambda d: d["demo"]["frames"][0].pop("nose"), "13 个点"),
    (lambda d: d["rule"].update(when=["exec", "x"]), "识别规则不对"),
    (lambda d: d.update(passes_zones=["mouth"]), "不认识的区域"),
    (lambda d: d.update(schema="v0"), "格式版本"),
])
def test_a_broken_action_file_is_refused(change, message):
    doc = _doc()
    change(doc)
    with pytest.raises(ValueError, match=message):
        normalize_action(doc)


def test_signing_bytes_do_not_depend_on_key_order():
    doc = _doc()
    shuffled = json.loads(json.dumps(doc, sort_keys=False))
    reordered = dict(reversed(list(shuffled.items())))
    assert pose_library.canonical_bytes(doc) == pose_library.canonical_bytes(reordered)
