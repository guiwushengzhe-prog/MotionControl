"""区域位置和触发点草稿一次保存：模式隔离、实际触发及失败恢复。"""
import copy

import pytest

from motioncontrol.body_reference import make_reference
from motioncontrol_shared.pose_points import POSE_POINT_LABELS
from test_alternate_body_action import Output
from test_configuration_coordination import application  # noqa: F401
from test_game_modes import get, post
from test_minimal_controls import _zone_feeder
from test_zone_layout import full_standing
from test_zone_trigger_points import pose_with


RECT = {"x1": .8, "x2": .95, "y1": .1, "y2": .25}
ACTION = {"type": "keyboard", "target": "Q", "behavior": "hold"}


@pytest.fixture
def editor(application):
    app = application
    app.KERNEL.body_reference = make_reference([full_standing()], 640, 480, {"kind": "test"})
    app.KERNEL.configure_hand_mouse({"enabled": False})
    app.KERNEL.output = Output()
    app.KERNEL.set_frozen_zones({name: dict(RECT) for name in app.RUNTIME_BODY_ZONES})
    return app


def install(app, overrides, mode_id=None):
    code, data = post(app, "/api/game-profiles/overrides", {
        "profile_id": app.PROFILES.selected_id(), "mode_id": mode_id, "overrides": overrides})
    assert code == 200, data


def save(app, snapshot, choices, **extra):
    return post(app, "/api/zones/layout", {
        "profile_id": snapshot["profile_id"], "mode_id": snapshot["mode_id"],
        "rects": snapshot["rects"], "modes": snapshot["modes"],
        "reference": snapshot["reference"], "trigger_choices": choices, **extra})


def files(app):
    return {path: path.read_bytes() if path.exists() else None
            for path in (app.PROFILES.selection_path, app.KERNEL._general_settings_path())}


def test_editor_reports_effective_choices_defaults_and_chinese_labels(editor):
    app = editor
    install(app, {"zone.leftHand": {"action": ACTION, "trigger_points": [],
                                  "trigger_segments": [["left_wrist", "left_index"]]}})
    code, data = get(app, "/api/zones/editor")
    assert code == 200 and data["mode_id"] is None
    assert data["point_labels"] == POSE_POINT_LABELS and len(data["point_labels"]) == 33
    assert data["trigger_choices"]["leftHand"] == {
        "trigger_points": [], "trigger_segments": [["left_wrist", "left_index"]]}
    assert data["trigger_choices"]["rightFoot"]["trigger_points"] == list(app.RUNTIME_BODY_ZONES["rightFoot"]["points"])


def test_saved_new_point_triggers_and_previous_point_no_longer_triggers(editor, monkeypatch):
    app = editor
    install(app, {"zone.leftHand": {"action": ACTION, "trigger_points": ["left_wrist"]}})
    snapshot = get(app, "/api/zones/editor")[1]
    code, data = save(app, snapshot, {"leftHand": {"trigger_points": ["left_elbow"]}})
    assert code == 200, data
    assert app.KERNEL.control_bindings["zone.leftHand"]["action"] == ACTION
    app.KERNEL.configure_zone_trigger_mode("simple")
    feed = _zone_feeder(app.KERNEL, monkeypatch)
    feed(pose_with(left_wrist=True, left_elbow=False), 3)
    assert not app.KERNEL.zone_state["leftHand"]["pressed"]
    feed(pose_with(left_wrist=False, left_elbow=True), 3)
    assert app.KERNEL.zone_state["leftHand"]["pressed"]
    assert any(item["id"] == "zone.leftHand" and item["action"] == ACTION for item in app.KERNEL.output.holds)


def test_partial_choice_save_preserves_binding_rules_disabled_and_other_segment_group(editor):
    app = editor
    binding = {"action": ACTION, "with_motion": True,
               "trigger_points": ["left_elbow"],
               "trigger_segments": [["left_wrist", "left_index"], ["right_wrist", "right_index"]]}
    alternate = {"action": ACTION, "alternate_mode": "with_trigger", "alternate_when": "motion.march",
                 "alternate_action": {**ACTION, "target": "E"}}
    install(app, {"zone.leftHand": binding, "zone.rightFoot": {"disabled": True}, "motion.squat": alternate})
    snapshot = get(app, "/api/zones/editor")[1]
    moved = {**snapshot["rects"], "leftHand": {**RECT, "x1": .77}}
    code, data = save(app, snapshot, {
        "leftHand": {"trigger_points": [], "trigger_segments": [["right_wrist", "right_index"]]},
        "rightFoot": {"trigger_points": ["nose"], "trigger_segments": []}}, rects=moved)
    assert code == 200, data
    saved = app.PROFILES.effective_profile()["bindings"]["zones"]
    assert saved["leftHand"] == {**binding, "trigger_points": [], "trigger_segments": [["right_wrist", "right_index"]]}
    assert saved["rightFoot"] == {"disabled": True, "trigger_points": ["nose"], "trigger_segments": []}
    assert app.PROFILES.effective_profile()["bindings"]["motions"]["squat"] == alternate
    assert app.KERNEL._zone_point_groups_locked("leftHand") == (("right_wrist", "right_index"),)
    assert app.KERNEL.frozen_rects["leftHand"] == moved["leftHand"]


def test_unmapped_custom_region_can_save_points_without_enabling_an_action(editor):
    app = editor
    code, data = post(app, "/api/game-profiles/regions", {
        "op": "add", "profile_id": app.PROFILES.selected_id(), "name": "未映射区域"})
    assert code == 200, data
    ident = data["region"]["id"]
    snapshot = get(app, "/api/zones/editor")[1]
    assert snapshot["trigger_choices"][ident] == {"trigger_points": ["left_wrist", "right_wrist"], "trigger_segments": []}
    code, data = save(app, snapshot, {ident: {"trigger_points": ["nose"], "trigger_segments": []}})
    assert code == 200, data
    binding = app.PROFILES.effective_profile()["bindings"]["zones"][ident]
    assert binding == {"disabled": True, "trigger_points": ["nose"], "trigger_segments": []}
    assert app.KERNEL.control_bindings[f"zone.{ident}"] == binding
    assert not any(item["id"] == f"zone.{ident}" for item in app.KERNEL.output.holds)


def test_choices_belong_to_active_mode_and_stale_mode_is_refused(editor):
    app = editor
    install(app, {"zone.leftHand": {"action": ACTION, "trigger_points": ["left_wrist"]}})
    mode = app.PROFILES.add_mode("开车")["id"]
    assert app.switch_game_mode(mode)["executed"]
    snapshot = get(app, "/api/zones/editor")[1]
    assert snapshot["mode_id"] == mode
    code, data = save(app, snapshot, {"leftHand": {"trigger_points": ["nose"]}})
    assert code == 200, data
    assert app.PROFILES.effective_profile()["bindings"]["zones"]["leftHand"]["trigger_points"] == ["left_wrist"]
    assert app.PROFILES.effective_profile(mode)["bindings"]["zones"]["leftHand"]["trigger_points"] == ["nose"]
    for old_mode, new_mode in ((mode, "DEFAULT"), (None, mode)):
        snapshot = get(app, "/api/zones/editor")[1]
        assert snapshot["mode_id"] == old_mode
        app.switch_game_mode(new_mode)
        before, bindings = files(app), copy.deepcopy(app.KERNEL.control_bindings)
        code, data = save(app, snapshot, {"leftHand": {"trigger_points": ["left_elbow"]}})
        assert code == 409, data
        assert files(app) == before and app.KERNEL.control_bindings == bindings


@pytest.mark.parametrize("choices", [
    {"leftHand": {"trigger_points": ["unknown"]}},
    {"leftHand": {"trigger_segments": [["nose"]]}},
    {"leftHand": {"action": ACTION}},
    {"user_ffffffff": {"trigger_points": ["nose"]}},
])
def test_invalid_choices_change_neither_files_nor_held_state(editor, choices):
    app = editor
    snapshot = get(app, "/api/zones/editor")[1]
    app.KERNEL.zone_state["leftHand"]["pressed"] = True
    app.KERNEL.trigger_previous = {"zone.leftHand"}
    app.KERNEL._sequence_next = {"zone.leftHand": 1}
    states = copy.deepcopy((app.KERNEL.zone_state, app.KERNEL.trigger_previous, app.KERNEL._sequence_next))
    before = files(app)
    code, data = save(app, snapshot, choices)
    assert code in (400, 404), data
    assert files(app) == before
    assert (app.KERNEL.zone_state, app.KERNEL.trigger_previous, app.KERNEL._sequence_next) == states


def test_write_failure_restores_points_geometry_files_and_held_cycle(editor, monkeypatch):
    app = editor
    cycle = {"action": ACTION, "alternate_mode": "cycle", "alternate_action": {**ACTION, "target": "E"},
             "reset_trigger": "zone.headJump"}
    install(app, {"zone.leftHand": {"action": ACTION, "trigger_points": ["left_wrist"]}, "motion.squat": cycle})
    app.KERNEL.zone_state["leftHand"]["pressed"] = True
    app.KERNEL.motion_active = {"squat"}
    app.KERNEL._dispatch_controls_locked(1.)
    snapshot = get(app, "/api/zones/editor")[1]
    before = files(app)
    controls = copy.deepcopy((app.KERNEL.zone_state, app.KERNEL.trigger_previous, app.KERNEL._sequence_next,
                              app.KERNEL._selected_active, app.KERNEL.output.holds))
    bindings, geometry = copy.deepcopy(app.KERNEL.control_bindings), copy.deepcopy(app.KERNEL.frozen_rects)
    from motioncontrol import config_transaction
    original = config_transaction.atomic_bytes

    def fail_general(path, content):
        if path == app.KERNEL._general_settings_path():
            raise OSError("测试写盘失败")
        original(path, content)

    monkeypatch.setattr(config_transaction, "atomic_bytes", fail_general)
    changed = {**snapshot["rects"], "leftHand": {**RECT, "x1": .77}}
    code, data = save(app, snapshot, {"leftHand": {"trigger_points": ["nose"]}}, rects=changed)
    assert code == 400 and "测试写盘失败" in data["error"]
    assert files(app) == before and app.KERNEL.control_bindings == bindings
    assert app.KERNEL.frozen_rects == geometry
    assert (app.KERNEL.zone_state, app.KERNEL.trigger_previous, app.KERNEL._sequence_next,
            app.KERNEL._selected_active, app.KERNEL.output.holds) == controls
