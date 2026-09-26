"""区域「进去就按」、定住跟随框、映射表里的「系统功能」。

进去就按：进框那一帧就按、出框那一帧就松，防误触那几条一条都不走，只留握拳控制鼠标
的那只手不按。定住：框停在定住那一刻的位置，不再跟着人走，可以拖着改；存盘，重启
还在。区域挪到我这里：定住的整组框按人现在的位置搬过来，拖过的大小不变——这是删掉
参考场景之后，"摄像头碰歪了、人换了站位"的办法。系统功能：区域、动作、姿势都能绑，
触发那一下执行一次。
"""

from __future__ import annotations

import json
import threading

import pytest

from motioncontrol.control_kernel import (
    FROZEN_ZONE_MIN_SIZE, ControlKernel, legacy_scene_to_frozen,
)
from motioncontrol_shared.mapping_schema import normalize_voice_mappings
from motioncontrol_shared.profile_schema import (
    BINDING_SYSTEM_TARGETS, action_catalog, normalize_action, normalize_bindings,
)
from test_zone_smart import into, with_elbows
from test_minimal_controls import KernelOutput, _standing_pose, _zone_feeder


def standing():
    return with_elbows(_standing_pose())


@pytest.fixture()
def kernel():
    made = ControlKernel(KernelOutput())
    yield made
    made.close()


# ---------- 进去就按 ----------

def test_simple_mode_presses_on_the_first_frame_inside_and_releases_on_the_first_outside(kernel, monkeypatch):
    feed = _zone_feeder(kernel, monkeypatch)
    feed(standing(), 10)
    kernel.configure_zone_trigger_mode("simple")
    # 手先停在框下沿外面一点（真人的手是一路抬上来的，不会一帧从腰间跳进框里）。
    feed(into(kernel, standing(), "leftHand", -.01), 5)
    assert not kernel.zone_state["leftHand"]["pressed"]
    # 擦着边进去：智能判定下还没进够深，进去就按下第一帧就按。
    feed(into(kernel, standing(), "leftHand", .005), 1)
    assert kernel.zone_state["leftHand"]["pressed"]
    feed(into(kernel, standing(), "leftHand", -.03), 1)
    assert not kernel.zone_state["leftHand"]["pressed"]


def test_simple_mode_does_not_need_both_hands_to_wait(kernel, monkeypatch):
    feed = _zone_feeder(kernel, monkeypatch)
    feed(standing(), 10)
    kernel.configure_zone_trigger_mode("simple")
    pose = into(kernel, into(kernel, standing(), "leftHand", .12), "rightHand", .12)
    feed(pose, 1)
    assert kernel.zone_state["leftHand"]["pressed"] and kernel.zone_state["rightHand"]["pressed"]


def test_simple_mode_zones_do_not_yield_to_motions(kernel):
    kernel.configure_motions([{"id": "hands_up", "enabled": True, "type": "gamepad", "target": "Y"}])
    assert kernel.status()["zone_overlaps"]["leftHand"]["yields"] is True
    kernel.configure_zone_trigger_mode("simple")
    overlaps = kernel.status()["zone_overlaps"]
    assert overlaps["leftHand"] == {"triggers": ["motion.hands_up"], "yields": False, "with_motion": False,
                                    "rates": {"motion.hands_up": {"source": "declared"}}}


def test_simple_mode_still_skips_the_hand_steering_the_mouse(kernel, monkeypatch):
    feed = _zone_feeder(kernel, monkeypatch)
    feed(standing(), 10)
    kernel.configure_zone_trigger_mode("simple")
    monkeypatch.setattr(kernel.hand_mouse_controller, "owns_hand", lambda hand: hand == "left")
    feed(into(kernel, standing(), "leftHand", .12), 3)
    assert not kernel.zone_state["leftHand"]["pressed"]


def test_the_mode_is_saved_and_a_bad_one_is_refused(kernel, tmp_path):
    with pytest.raises(ValueError):
        kernel.configure_zone_trigger_mode("fastest")
    kernel.configure_zone_trigger_mode("simple")
    again = ControlKernel(KernelOutput())
    try:
        assert again.zone_trigger_mode == "simple"
    finally:
        again.close()


# ---------- 定住跟随框 ----------

def test_frozen_zones_stay_where_they_were_when_the_body_moves(kernel, monkeypatch):
    feed = _zone_feeder(kernel, monkeypatch)
    feed(standing(), 10)
    before = dict(kernel.zone_rects["leftHand"])
    result = kernel.freeze_zones(True)
    assert result["executed"] and result["status"]["zones_frozen"]
    feed(with_elbows(_standing_pose(dx=.15)), 20)
    assert kernel.zone_rects["leftHand"] == pytest.approx(before, abs=1e-3)
    kernel.freeze_zones(False)
    feed(with_elbows(_standing_pose(dx=.15)), 20)
    assert kernel.zone_rects["leftHand"]["x2"] > before["x2"] + .05, "恢复跟随后又跟着人走"


def test_freezing_with_nobody_in_view_says_why(kernel):
    result = kernel.freeze_zones(True)
    assert not result["executed"] and "头和双肩" in result["reason"]
    assert not kernel.zones_frozen


def test_freezing_twice_keeps_what_was_dragged(kernel, monkeypatch):
    feed = _zone_feeder(kernel, monkeypatch)
    feed(standing(), 10)
    kernel.freeze_zones(True)
    kernel.update_frozen_zones({"headJump": {"x1": .40, "y1": .05, "x2": .60, "y2": .15}})
    kernel.freeze_zones(True)
    assert kernel.frozen_rects["headJump"] == {"x1": .40, "y1": .05, "x2": .60, "y2": .15}


def test_a_dragged_frozen_zone_is_what_triggers(kernel, monkeypatch):
    feed = _zone_feeder(kernel, monkeypatch)
    feed(standing(), 10)
    kernel.configure_zone_trigger_mode("simple")
    kernel.freeze_zones(True)
    # 左手区挪到画面正中间偏下：垂着的手腕 (.40, .66) 正好在里面。
    kernel.update_frozen_zones({"leftHand": {"x1": .35, "y1": .60, "x2": .45, "y2": .70}})
    feed(standing(), 1)
    assert kernel.zone_state["leftHand"]["pressed"]


def test_editing_clamps_and_keeps_a_minimum_size(kernel, monkeypatch):
    feed = _zone_feeder(kernel, monkeypatch)
    feed(standing(), 10)
    kernel.freeze_zones(True)
    kernel.update_frozen_zones({"leftFoot": {"x1": -1, "y1": .5, "x2": -.5, "y2": .5}})
    rect = kernel.frozen_rects["leftFoot"]
    assert rect["x1"] >= 0 and rect["x2"] - rect["x1"] >= FROZEN_ZONE_MIN_SIZE - 1e-9
    assert rect["y2"] - rect["y1"] >= FROZEN_ZONE_MIN_SIZE - 1e-9


def test_editing_needs_the_zones_to_be_frozen(kernel):
    with pytest.raises(ValueError, match="没有定住"):
        kernel.update_frozen_zones({"leftHand": {"x1": 0, "y1": 0, "x2": .2, "y2": .2}})


def test_frozen_zones_survive_a_restart_and_losing_the_body(kernel, monkeypatch):
    feed = _zone_feeder(kernel, monkeypatch)
    feed(standing(), 10)
    kernel.freeze_zones(True)
    kernel.update_frozen_zones({"rightHand": {"x1": .70, "y1": .10, "x2": .90, "y2": .30}})
    kernel._clear_body_outputs_locked()
    assert kernel.zone_rects["rightHand"] == {"x1": .70, "y1": .10, "x2": .90, "y2": .30}, "人走开了框还在"
    again = ControlKernel(KernelOutput())
    try:
        assert again.status()["zones_frozen"]
        assert again.status()["zones"]["rightHand"]["rect"] == {"x1": .70, "y1": .10, "x2": .90, "y2": .30}
    finally:
        again.close()


def test_a_frozen_flag_without_any_box_is_ignored(tmp_path, monkeypatch):
    probe = ControlKernel(KernelOutput())
    path = probe._general_settings_path()
    probe.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"zone_freeze": {"frozen": True, "rects": {"leftHand": "bad"}}}), encoding="utf-8")
    again = ControlKernel(KernelOutput())
    try:
        assert not again.zones_frozen
    finally:
        again.close()


# ---------- 区域挪到我这里 ----------

def test_moving_here_carries_the_dragged_boxes_along_with_the_body(kernel, monkeypatch):
    feed = _zone_feeder(kernel, monkeypatch)
    feed(standing(), 10)
    kernel.freeze_zones(True)
    kernel.update_frozen_zones({"headJump": {"x1": .45, "y1": .10, "x2": .55, "y2": .20}})
    # 人往右挪了 0.1，又退后了一点（躯干变短到 0.8 倍，以胯为中心）。
    moved = with_elbows(_standing_pose(dx=.10))
    for name in ("left_shoulder", "right_shoulder"):
        moved[name]["y"] = .64 - (.64 - .40) * .8
    feed(moved, 3)
    assert kernel.frozen_rects["headJump"] == {"x1": .45, "y1": .10, "x2": .55, "y2": .20}, "定住了就不跟着走"
    result = kernel.move_zones_here()
    assert result["executed"] and result["moved"]
    jump = kernel.frozen_rects["headJump"]
    # 胯中点从 (.50, .64) 到 (.60, .64)，比例 0.8：x 相对胯的 -.05 变成 -.04。
    assert jump == pytest.approx({"x1": .56, "y1": .208, "x2": .64, "y2": .288}, abs=2e-3)


def test_moving_here_keeps_edges_that_sit_on_the_image_border(kernel, monkeypatch):
    feed = _zone_feeder(kernel, monkeypatch)
    feed(standing(), 10)
    kernel.freeze_zones(True)
    before = dict(kernel.frozen_rects["leftHand"])
    assert before["x1"] == 0.0 and before["y1"] == 0.0, "跟随的手区贴着画面左边和上边"
    feed(with_elbows(_standing_pose(dx=.10)), 3)
    kernel.move_zones_here()
    after = kernel.frozen_rects["leftHand"]
    assert after["x1"] == 0.0 and after["y1"] == 0.0
    assert after["x2"] == pytest.approx(before["x2"] + .10, abs=2e-3)


def test_moving_here_while_following_just_freezes_in_place(kernel, monkeypatch):
    feed = _zone_feeder(kernel, monkeypatch)
    feed(standing(), 10)
    result = kernel.move_zones_here()
    assert result["executed"] and kernel.zones_frozen and kernel.frozen_anchor is not None


def test_moving_here_needs_to_see_the_body(kernel, monkeypatch):
    feed = _zone_feeder(kernel, monkeypatch)
    feed(standing(), 10)
    kernel.freeze_zones(True)
    kernel.handle_pose_map("camera", None, width=640, height=480)
    result = kernel.move_zones_here()
    assert not result["executed"] and "胯" in result["reason"]
    assert kernel.zones_frozen, "没看清人就什么都不动"


def test_the_body_anchor_survives_a_restart(kernel, monkeypatch):
    feed = _zone_feeder(kernel, monkeypatch)
    feed(standing(), 10)
    kernel.freeze_zones(True)
    anchor = dict(kernel.frozen_anchor)
    again = ControlKernel(KernelOutput())
    try:
        assert again.frozen_anchor == anchor
        assert again.status()["zones_anchor_known"] is True
    finally:
        again.close()


# ---------- 旧版参考场景 ----------

OLD_SCENE = {
    "version": 2,
    "zones": {
        "leftHand": {"shape": "circle", "cx": .20, "cy": .20, "r": .08},
        "rightHandUpper": {"shape": "circle", "cx": .80, "cy": .18, "r": .05},
        "rightHandLower": {"shape": "circle", "cx": .82, "cy": .30, "r": .05},
        "leftFoot": {"shape": "circle", "cx": .30, "cy": .85, "r": .06},
    },
    "vertical_look": {"enabled": True, "source": "hand", "range_y": .25, "deadzone": .05,
                      "exclusive_axes": True, "body_motion_guard": True},
    "pose": {name: {**point, "score": .95} for name, point in {
        "left_shoulder": {"x": .42, "y": .40}, "right_shoulder": {"x": .58, "y": .40},
        "left_hip": {"x": .45, "y": .64}, "right_hip": {"x": .55, "y": .64}}.items()},
}


def test_old_scene_circles_become_frozen_boxes():
    rects, vertical, anchor = legacy_scene_to_frozen(OLD_SCENE)
    assert rects["leftHand"] == {"x1": .12, "y1": .12, "x2": .28, "y2": .28}
    assert rects["rightHand"]["x1"] < .77 and rects["rightHand"]["y2"] > .34, "旧的上下两个圈合成一个"
    assert "headJump" in rects, "旧布局没有头顶区，按两手上圈的位置补一个"
    assert vertical["range_y"] == .25
    assert anchor == {"x": .50, "y": .64, "scale": .24}


def _write_old_scene(photo=True):
    from motioncontrol.user_paths import user_path

    layout = user_path("scene_layout")
    layout.parent.mkdir(parents=True, exist_ok=True)
    layout.write_text(json.dumps(OLD_SCENE), encoding="utf-8")
    if photo:
        user_path("scene_reference").write_bytes(b"jpeg")
    return layout


def test_a_recorded_scene_is_migrated_once_on_upgrade():
    layout = _write_old_scene()
    first = ControlKernel(KernelOutput())
    try:
        status = first.status()
        assert status["zones_frozen"] and status["zones_anchor_known"]
        assert status["zones"]["leftFoot"]["rect"] == {"x1": .24, "y1": .79, "x2": .36, "y2": .91}
        assert first.vertical_look["range_y"] == .25 and first.body_motion_guard_enabled is True
        first.freeze_zones(False)
    finally:
        first.close()
    assert layout.is_file(), "旧文件不删，留着用户自己处理"
    again = ControlKernel(KernelOutput())
    try:
        assert not again.zones_frozen, "只迁移一次：恢复跟随以后不能又被旧文件定回去"
    finally:
        again.close()


def test_a_scene_without_its_photo_was_never_active_and_is_not_migrated():
    _write_old_scene(photo=False)
    kernel = ControlKernel(KernelOutput())
    try:
        assert not kernel.zones_frozen
    finally:
        kernel.close()


# ---------- 系统功能 ----------

def test_schema_accepts_only_listed_system_functions_and_always_taps():
    action = normalize_action({"type": "system", "target": "zones.freeze_toggle", "behavior": "hold"})
    assert action == {"type": "system", "target": "ZONES.FREEZE_TOGGLE", "behavior": "tap"}
    with pytest.raises(ValueError, match="不支持的系统功能"):
        normalize_action({"type": "system", "target": "POSE.RECORD"})
    assert action_catalog()["system"]["targets"] == sorted(BINDING_SYSTEM_TARGETS)
    grouped = normalize_bindings({"poses": {"hands_cross": {"action": {"type": "system", "target": "HEAD.CENTER"}}}})
    assert grouped["poses"]["hands_cross"]["action"]["type"] == "system"


def test_voice_commands_can_freeze_and_follow_too():
    rows = normalize_voice_mappings([{"phrase": "定住", "type": "system", "target": "ZONES.FREEZE"},
                                     {"phrase": "跟着我", "type": "system", "target": "ZONES.FOLLOW"}])
    assert [row["target"] for row in rows] == ["ZONES.FREEZE", "ZONES.FOLLOW"]


def test_a_zone_bound_to_freeze_toggle_flips_once_per_entry(kernel, monkeypatch):
    feed = _zone_feeder(kernel, monkeypatch)
    kernel.configure_zone_trigger_mode("simple")
    kernel.configure_bindings({"zones": {"headJump": {"action": {"type": "system", "target": "ZONES.FREEZE_TOGGLE"}}}})
    feed(standing(), 10)
    jump = kernel.zone_rects["headJump"]
    nose_in = standing()
    nose_in["nose"] = {"x": (jump["x1"] + jump["x2"]) / 2, "y": (jump["y1"] + jump["y2"]) / 2, "score": .95}
    feed(nose_in, 5)
    assert kernel.zones_frozen, "进去那一下定住"
    feed(nose_in, 5)
    assert kernel.zones_frozen, "待在里面不会来回切"
    feed(standing(), 2)
    feed(nose_in, 1)
    assert not kernel.zones_frozen, "出来再进去才切回跟随"


def test_other_system_functions_go_to_the_handler_once(kernel, monkeypatch):
    feed = _zone_feeder(kernel, monkeypatch)
    calls, done = [], threading.Event()

    def handler(target, trigger):
        calls.append((target, trigger))
        done.set()

    kernel.configure_system_action_handler(handler)
    kernel.configure_zone_trigger_mode("simple")
    kernel.configure_bindings({"zones": {"leftHand": {"action": {"type": "system", "target": "OUTPUT.TOGGLE"}}}})
    feed(standing(), 10)
    feed(into(kernel, standing(), "leftHand", .12), 5)
    assert done.wait(2)
    assert calls == [("OUTPUT.TOGGLE", "zone.leftHand")]


def test_move_here_is_a_system_function_for_bindings_and_voice():
    assert normalize_action({"type": "system", "target": "ZONES.MOVE_HERE"})["target"] == "ZONES.MOVE_HERE"
    rows = normalize_voice_mappings([{"phrase": "挪过来", "type": "system", "target": "ZONES.MOVE_HERE"}])
    assert rows[0]["target"] == "ZONES.MOVE_HERE"


def test_a_pose_bound_to_move_here_moves_the_frozen_boxes(kernel, monkeypatch):
    feed = _zone_feeder(kernel, monkeypatch)
    kernel.configure_zone_trigger_mode("simple")
    kernel.configure_bindings({"zones": {"rightHand": {"action": {"type": "system", "target": "ZONES.MOVE_HERE"}}}})
    feed(standing(), 10)
    kernel.freeze_zones(True)
    before = dict(kernel.frozen_rects["headJump"])
    shifted = with_elbows(_standing_pose(dx=.05))
    feed(shifted, 2)
    feed(into(kernel, shifted, "rightHand", .12), 2)
    assert kernel.frozen_rects["headJump"]["x1"] == pytest.approx(before["x1"] + .05, abs=2e-3)

