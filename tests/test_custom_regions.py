"""每个游戏新增独立区域：保存、模式共享和真实内核的假输出回放。"""
import copy
import json
import re
from pathlib import Path

import pytest

from motioncontrol.control_kernel import ControlKernel, RUNTIME_BODY_ZONES
from motioncontrol.game_profiles import GameProfileStore, ProfileSelectionChanged
from motioncontrol_shared.canonical import canonicalize
from motioncontrol_shared.describe import describe
from test_alternate_body_action import Output
from test_configuration_coordination import application
from test_game_modes import get, post
from test_minimal_controls import _standing_pose, _zone_feeder


ROOT = Path(__file__).resolve().parents[1]
RECT = {"x1": .10, "x2": .28, "y1": .12, "y2": .32}
OTHER_RECT = {"x1": .72, "x2": .90, "y1": .12, "y2": .32}
W = {"type": "keyboard", "target": "W", "behavior": "hold"}
E = {"type": "keyboard", "target": "E", "behavior": "hold"}
REGION_ID = "user_0123abcd"


def region(ident=REGION_ID, name="独立区域", rect=None):
    return {"id": ident, "name": name, "rect": dict(rect or RECT)}


def pose_in(*, left=None, right=None, **points):
    pose = _standing_pose()
    for name, rect in {"left_wrist": left, "right_wrist": right, **points}.items():
        if rect is not None:
            pose[name] = {"x": (rect["x1"] + rect["x2"]) / 2,
                          "y": (rect["y1"] + rect["y2"]) / 2, "score": .95}
    return pose


def held(output):
    return {item["id"]: item["action"]["target"] for item in output.holds
            if item["id"].startswith("zone.user_")}


@pytest.fixture
def region_kernel(monkeypatch):
    output = Output()
    kernel = ControlKernel(output)
    kernel.configure_hand_mouse({"enabled": False})
    feed = _zone_feeder(kernel, monkeypatch)
    yield kernel, output, feed
    kernel.close()


def test_store_regions_are_per_game_shared_across_modes_and_survive_restart():
    store = GameProfileStore(ROOT)
    game = store.selected_id()
    added = store.add_region(game, name="交互", rect=RECT)
    assert re.fullmatch(r"user_[0-9a-f]{8}", added["id"])
    assert added["rect"] == RECT
    mode = store.add_mode("开车")
    store.set_overrides({"zone." + added["id"]: {"action": W}})
    store.set_overrides({"zone." + added["id"]: {"action": E}}, mode_id=mode["id"])
    moved = {**RECT, "x1": .14, "x2": .34}
    updated = store.update_region(game, added["id"], name="互动", rect=moved)
    assert updated == {**added, "name": "互动", "rect": moved}
    for mode_id, target in ((None, "W"), (mode["id"], "E")):
        effective = store.effective_profile(mode_id)
        assert effective["regions"] == [updated]
        assert effective["bindings"]["zones"][added["id"]]["action"]["target"] == target
    other = store.add_custom_game("区域隔离游戏")
    store.select(other["id"])
    assert store.effective_profile()["regions"] == []
    store.select(game)
    restored = GameProfileStore(ROOT)
    assert restored.effective_profile()["regions"] == [updated]
    assert restored.effective_profile(mode["id"])["regions"] == [updated]
    saved = json.loads(restored.selection_path.read_text(encoding="utf-8"))
    assert saved["regions_by_profile"][game] == [updated]


def test_store_deletion_cleans_default_and_every_mode_and_body_conditions():
    store = GameProfileStore(ROOT)
    game = store.selected_id()
    added = store.add_region(game)
    trigger = "zone." + added["id"]
    condition = {"action": W, "alternate_mode": "with_trigger", "alternate_action": E,
                 "alternate_when": trigger}
    store.set_overrides({trigger: {"action": W}, "motion.squat": condition})
    drive = store.add_mode("开车")
    store.set_overrides({trigger: {"action": E}, "motion.squat": condition}, mode_id=drive["id"])
    store.remove_region(game, added["id"])
    for mode_id in (None, drive["id"]):
        effective = store.effective_profile(mode_id)
        assert effective["regions"] == []
        assert added["id"] not in effective["bindings"]["zones"]
        assert trigger not in json.dumps(effective["overrides"], ensure_ascii=False)
    assert added["id"] not in GameProfileStore(ROOT).selection_path.read_text(encoding="utf-8")


def test_store_layout_updates_multiple_regions_atomically():
    store = GameProfileStore(ROOT)
    game = store.selected_id()
    first = store.add_region(game, rect=RECT)
    second = store.add_region(game, rect=OTHER_RECT)
    moved = {**RECT, "y1": .16, "y2": .36}
    before = store.selection_path.read_bytes()
    for changes in ({first["id"]: moved, second["id"]: {**OTHER_RECT, "x2": 2}},
                    {first["id"]: moved, "leftHand": RECT},
                    {"user_ffffffff": RECT}):
        with pytest.raises((ValueError, KeyError)):
            store.update_region_rects(game, changes)
        assert store.selection_path.read_bytes() == before
    store.update_region_rects(game, {first["id"]: moved, second["id"]: RECT})
    saved = GameProfileStore(ROOT).effective_profile()["regions"]
    assert {item["id"]: item["rect"] for item in saved} == {first["id"]: moved, second["id"]: RECT}


def test_deleting_a_custom_game_removes_its_saved_region_definitions():
    store = GameProfileStore(ROOT)
    custom = store.add_custom_game("临时游戏")
    store.select(custom["id"])
    added = store.add_region(custom["id"])
    store.remove_custom_game(custom["id"])
    saved = store.selection_path.read_text(encoding="utf-8")
    assert added["id"] not in saved
    assert GameProfileStore(ROOT).effective_profile()["regions"] == []


@pytest.mark.parametrize("rect", [
    {**RECT, "x1": -.01}, {**RECT, "x2": 1.01},
    {**RECT, "x1": .5, "x2": .4}, {**RECT, "x2": .11},
    {**RECT, "y1": float("nan")}, {**RECT, "y2": float("inf")},
    {"x1": .1, "x2": .3, "y1": .1},
])
def test_invalid_rectangle_does_not_change_saved_region(rect):
    store = GameProfileStore(ROOT)
    game = store.selected_id()
    added = store.add_region(game, rect=RECT)
    before = store.selection_path.read_bytes()
    with pytest.raises(ValueError):
        store.update_region(game, added["id"], rect=rect)
    assert store.selection_path.read_bytes() == before
    assert store.effective_profile()["regions"] == [added]


def test_names_limit_builtin_ids_and_stale_game_are_refused():
    store = GameProfileStore(ROOT)
    game = store.selected_id()
    for name in (" ", "字" * 25):
        with pytest.raises(ValueError):
            store.add_region(game, name=name)
    for _ in range(12):
        store.add_region(game)
    with pytest.raises(ValueError):
        store.add_region(game)
    for ident in ("leftHand", "headJump", "lookGate"):
        with pytest.raises(ValueError):
            store.remove_region(game, ident)
    with pytest.raises(ProfileSelectionChanged):
        store.add_region("old-game")
    other = store.add_custom_game("另一个游戏")
    store.select(other["id"])
    with pytest.raises(ProfileSelectionChanged):
        store.add_region(game)
    assert store.effective_profile()["regions"] == []


@pytest.mark.parametrize("trigger_mode", ["simple", "smart"])
def test_new_region_uses_either_wrist_and_holds_only_while_inside(region_kernel, trigger_mode):
    kernel, output, feed = region_kernel
    kernel.configure_zone_trigger_mode(trigger_mode)
    kernel.configure_regions([region()])
    # 创建后还没有绑键也能定位这个框；不会借用内置区域的默认输出。
    state = kernel.status()["zones"][REGION_ID]
    assert state["custom"] and state["name"] == "独立区域" and state["rect"] == RECT
    feed(pose_in(left=RECT), 6)
    assert "zone." + REGION_ID not in held(output)
    kernel.configure_bindings({"zones": {REGION_ID: {"action": W}}})
    for side in ("left", "right"):
        feed(pose_in(**{side: RECT}), 6)
        assert held(output)["zone." + REGION_ID] == "W"
        feed(_standing_pose(), 6)
        assert "zone." + REGION_ID not in held(output)


def test_multiple_regions_have_independent_keys_and_deleting_one_releases_it(region_kernel):
    kernel, output, feed = region_kernel
    second = "user_11111111"
    kernel.configure_regions([region(), region(second, "另一个框", OTHER_RECT)])
    kernel.configure_bindings({"zones": {REGION_ID: {"action": W}, second: {"action": E}}})
    feed(pose_in(left=RECT), 6)
    assert held(output) == {"zone." + REGION_ID: "W"}
    feed(pose_in(left=RECT, right=OTHER_RECT), 6)
    assert held(output) == {"zone." + REGION_ID: "W", "zone." + second: "E"}
    kernel.configure_regions([region(second, "另一个框", OTHER_RECT)])
    assert held(output) == {"zone." + second: "E"}
    assert REGION_ID not in kernel.status()["zones"]
    feed(_standing_pose(), 6)
    assert held(output) == {}


def test_region_stays_on_screen_and_editing_its_position_releases_the_old_key(region_kernel):
    kernel, output, feed = region_kernel
    kernel.configure_regions([region()])
    kernel.configure_bindings({"zones": {REGION_ID: {"action": W}}})
    feed(pose_in(left=RECT), 6)
    assert held(output)["zone." + REGION_ID] == "W"
    moved_body = _standing_pose(dx=.12)
    moved_body["left_wrist"] = pose_in(left=RECT)["left_wrist"]
    feed(moved_body, 12)
    assert kernel.status()["zones"][REGION_ID]["rect"] == RECT
    assert held(output)["zone." + REGION_ID] == "W"
    kernel.configure_regions([region(rect=OTHER_RECT)])
    assert "zone." + REGION_ID not in held(output)
    feed(pose_in(right=OTHER_RECT), 6)
    assert held(output)["zone." + REGION_ID] == "W"


def test_mode_change_keeps_region_geometry_and_waits_for_new_edge_when_key_changes(region_kernel):
    kernel, output, feed = region_kernel
    definition = region()
    bindings = {"zones": {REGION_ID: {"action": W}}}
    kernel.configure_regions([definition])
    kernel.configure_bindings(bindings)
    feed(pose_in(left=RECT), 6)
    kernel.configure_regions([definition])
    kernel.switch_bindings(bindings, mode={"id": "mode-0123abcd", "name": "同键模式"})
    assert held(output)["zone." + REGION_ID] == "W"
    kernel.switch_bindings({"zones": {REGION_ID: {"action": E}}})
    assert "zone." + REGION_ID not in held(output)
    feed(pose_in(left=RECT), 6)
    assert "zone." + REGION_ID not in held(output)
    assert kernel.status()["zones"][REGION_ID]["rect"] == RECT
    feed(_standing_pose(), 6)
    feed(pose_in(left=RECT), 6)
    assert held(output)["zone." + REGION_ID] == "E"


def test_custom_ankle_point_does_not_require_builtin_foot_side_kick(region_kernel):
    kernel, output, feed = region_kernel
    ankle_rect = {"x1": .43, "x2": .49, "y1": .92, "y2": .99}
    kernel.configure_regions([region(rect=ankle_rect)])
    kernel.configure_bindings({"zones": {REGION_ID: {"action": W, "trigger_points": ["left_ankle"]}}})
    feed(_standing_pose(), 15)
    assert held(output)["zone." + REGION_ID] == "W"
    assert kernel.feet["lift"]["left"] == pytest.approx(0)


def test_custom_points_and_connected_segments_keep_existing_selection_rules(region_kernel):
    kernel, output, feed = region_kernel
    kernel.configure_regions([region()])
    kernel.configure_bindings({"zones": {REGION_ID: {
        "action": W, "trigger_points": [], "trigger_segments": [["left_wrist", "left_index"]]}}})
    feed(pose_in(left=RECT), 6)
    assert "zone." + REGION_ID not in held(output)
    feed(pose_in(left=RECT, left_index=RECT), 6)
    assert held(output)["zone." + REGION_ID] == "W"
    kernel.configure_bindings({"zones": {REGION_ID: {"action": E, "trigger_points": ["right_index"]}}})
    feed(pose_in(left=RECT, left_index=RECT), 6)
    assert "zone." + REGION_ID not in held(output)
    feed(pose_in(right_index=RECT), 6)
    assert held(output)["zone." + REGION_ID] == "E"


def test_tap_fires_once_per_entry_and_existing_regions_still_work(region_kernel):
    kernel, output, feed = region_kernel
    kernel.configure_zone_trigger_mode("simple")
    kernel.configure_regions([region()])
    bindings = {REGION_ID: {"action": {**W, "behavior": "tap"}},
                **{ident: {"action": E} for ident in RUNTIME_BODY_ZONES}}
    kernel.configure_bindings({"zones": bindings})
    builtin_rects = {ident: OTHER_RECT for ident in RUNTIME_BODY_ZONES}
    kernel.set_frozen_zones(builtin_rects)
    feed(pose_in(left=RECT), 6)
    assert [action["target"] for action in output.pulses] == ["W"]
    feed(pose_in(left=RECT), 6)
    assert len(output.pulses) == 1
    feed(_standing_pose(), 6)
    feed(pose_in(left=RECT), 6)
    assert [action["target"] for action in output.pulses] == ["W", "W"]
    for ident, point in (("leftHand", "left_wrist"), ("rightHand", "right_wrist"), ("headJump", "nose")):
        feed(pose_in(**{point: OTHER_RECT}), 6)
        assert kernel.zone_state[ident]["pressed"]
    assert set(RUNTIME_BODY_ZONES) <= set(kernel.status()["zones"])


def api_add(app, **fields):
    code, data = post(app, "/api/game-profiles/regions", {
        "op": "add", "profile_id": app.PROFILES.selected_id(), "rect": RECT, "mode": "fixed", **fields})
    assert code == 200, data
    return data["profile"]["regions"][-1]


def test_api_add_update_switch_game_mode_and_remove_release_current_output(application, monkeypatch):
    app = application
    output = Output()
    monkeypatch.setattr(app.KERNEL, "output", output)
    app.KERNEL.configure_hand_mouse({"enabled": False})
    added = api_add(app, name="互动")
    ident, game = added["id"], app.PROFILES.selected_id()
    trigger = "zone." + ident
    assert post(app, "/api/game-profiles/overrides", {
        "profile_id": game, "overrides": {trigger: {"action": W}}})[0] == 200
    code, mode_data = post(app, "/api/game-profiles/modes", {"op": "add", "name": "开车"})
    assert code == 200
    mode = mode_data["profile"]["mode"]["id"]
    assert post(app, "/api/game-profiles/overrides", {
        "profile_id": game, "mode_id": mode, "overrides": {trigger: {"action": E}}})[0] == 200
    app.switch_game_mode(mode)
    moved = {**RECT, "x1": .12}
    code, changed = post(app, "/api/game-profiles/regions", {
        "op": "update", "profile_id": game, "id": ident, "name": "交互", "rect": moved})
    assert code == 200, changed
    assert changed["profile"]["mode"]["id"] == mode
    assert app.KERNEL.status()["zones"][ident]["rect"] == moved
    assert get(app, "/api/game-profiles/selected?mode=DEFAULT")[1]["profile"]["regions"] == changed["profile"]["regions"]
    other = app.PROFILES.add_custom_game("不共用区域的游戏")
    assert post(app, "/api/game-profiles/select", {"id": other["id"]})[0] == 200
    assert ident not in app.KERNEL.status()["zones"]
    assert post(app, "/api/game-profiles/select", {"id": game})[0] == 200
    assert ident in app.KERNEL.status()["zones"]
    app.switch_game_mode(mode)
    feed = _zone_feeder(app.KERNEL, monkeypatch)
    feed(pose_in(left=moved), 6)
    assert held(output)[trigger] == "E"
    code, removed = post(app, "/api/game-profiles/regions", {
        "op": "remove", "profile_id": game, "id": ident})
    assert code == 200, removed
    assert trigger not in held(output)
    assert ident not in app.KERNEL.status()["zones"]
    assert app.PROFILES.effective_profile()["regions"] == []
    assert app.PROFILES.effective_profile(mode)["regions"] == []


def test_api_refuses_missing_stale_profile_builtin_and_bad_rect_without_mutation(application):
    app = application
    added = api_add(app)
    game = app.PROFILES.selected_id()
    before = copy.deepcopy(app.PROFILES.effective_profile())
    payloads = [
        ({"op": "add"}, 409),
        ({"op": "add", "profile_id": "old-game"}, 409),
        ({"op": "remove", "profile_id": game, "id": "leftHand"}, 400),
        ({"op": "update", "profile_id": game, "id": added["id"], "rect": {**RECT, "x1": -1}}, 400),
    ]
    for payload, expected in payloads:
        code, data = post(app, "/api/game-profiles/regions", payload)
        assert code == expected, data
        assert app.PROFILES.effective_profile() == before


def test_api_layout_validates_all_rectangles_before_saving(application):
    app = application
    first = api_add(app)
    second = api_add(app, rect=OTHER_RECT)
    game = app.PROFILES.selected_id()
    before = copy.deepcopy(app.PROFILES.effective_profile()["regions"])
    moved = {**RECT, "y1": .15}
    code, data = post(app, "/api/game-profiles/regions", {
        "op": "layout", "profile_id": game,
        "rects": {first["id"]: moved, second["id"]: {**OTHER_RECT, "y2": float("nan")}}})
    assert code == 400, data
    assert app.PROFILES.effective_profile()["regions"] == before
    code, data = post(app, "/api/game-profiles/regions", {
        "op": "layout", "profile_id": game, "rects": {first["id"]: moved}})
    assert code == 200, data
    assert app.KERNEL.status()["zones"][first["id"]]["rect"] == moved
    assert app.KERNEL.status()["zones"][second["id"]]["rect"] == OTHER_RECT


def test_api_region_edit_and_delete_never_release_another_held_region(application, monkeypatch):
    app = application
    output = Output()
    monkeypatch.setattr(app.KERNEL, "output", output)
    app.KERNEL.configure_hand_mouse({"enabled": False})
    first, second = api_add(app), api_add(app, rect=OTHER_RECT)
    game = app.PROFILES.selected_id()
    first_key, second_key = "zone." + first["id"], "zone." + second["id"]
    assert post(app, "/api/game-profiles/overrides", {
        "profile_id": game, "overrides": {first_key: {"action": W}, second_key: {"action": E}}})[0] == 200
    feed = _zone_feeder(app.KERNEL, monkeypatch)
    feed(pose_in(left=RECT, right=OTHER_RECT), 6)
    assert held(output) == {first_key: "W", second_key: "E"}
    changes = []

    def record(holds, source_group="controls"):
        output.holds = list(holds)
        changes.append(held(output))

    monkeypatch.setattr(output, "set_action_holds", record)
    assert post(app, "/api/game-profiles/regions", {
        "op": "update", "profile_id": game, "id": first["id"], "rect": {**RECT, "x1": -1}})[0] == 400
    assert held(output) == {first_key: "W", second_key: "E"}
    assert post(app, "/api/game-profiles/regions", {
        "op": "remove", "profile_id": game, "id": first["id"]})[0] == 200
    assert held(output) == {second_key: "E"}
    assert changes and all(state.get(second_key) == "E" for state in changes)


@pytest.mark.parametrize("document_type", ["profile_selection", "game_bundle"])
def test_shared_document_preserves_region_geometry_and_readable_name(document_type):
    trigger = "zone." + REGION_ID
    if document_type == "profile_selection":
        document = {"schema": "motioncontrol.profile_selection.v2", "selected_id": "generic-xbox", "overrides_by_profile": {
            "generic-xbox": {trigger: {"action": W}}},
            "regions_by_profile": {"generic-xbox": [region()]}}
    else:
        document = {"game_id": "generic-xbox", "overrides": {trigger: {"action": W}},
                    "motions": [], "regions": [region()]}
    canonical = canonicalize(document_type, document)
    restored = canonicalize(document_type, json.loads(canonical.payload)).data
    regions = restored["regions_by_profile"]["generic-xbox"] if document_type == "profile_selection" else restored["regions"]
    assert regions == [region()]
    described = describe(document_type, restored)
    game = described["games"][0] if document_type == "profile_selection" else described
    item = next(item for group in game["groups"] for item in group["items"] if item["trigger"] == trigger)
    assert item["name"] == "独立区域" and item["action"] == "键盘 W · 持续按住"
