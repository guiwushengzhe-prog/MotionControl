"""区域编辑的跟随比例、稳定参照与跨文件原子保存。"""
import copy
import json

import pytest

from motioncontrol.control_kernel import ControlKernel
from motioncontrol.game_profiles import GameProfileStore
from motioncontrol.zone_layout import body_anchor, relative_rect
from motioncontrol_shared.regions import normalize_regions
from motioncontrol_shared.canonical import canonicalize
from test_alternate_body_action import Output
from test_configuration_coordination import application
from test_custom_regions import RECT, REGION_ID, ROOT, region, region_kernel
from test_game_modes import get, post
from test_minimal_controls import _standing_pose
from motioncontrol.body_reference import make_reference


def full_standing():
    pose = _standing_pose()
    for side in ("left", "right"):
        shoulder, wrist = pose[f"{side}_shoulder"], pose[f"{side}_wrist"]
        pose[f"{side}_elbow"] = {"x": (shoulder["x"]+wrist["x"])/2,
                                  "y": (shoulder["y"]+wrist["y"])/2, "score": .95}
    return pose


@pytest.fixture(autouse=True)
def full_body_editor_reference(request):
    if "region_kernel" in request.fixturenames:
        request.getfixturevalue("region_kernel")[0].body_reference = make_reference(
            [full_standing()], 640, 480, {"kind": "test"})
    if "application" in request.fixturenames:
        request.getfixturevalue("application").KERNEL.body_reference = make_reference(
            [full_standing()], 640, 480, {"kind": "test"})


def shifted(pose, dx=0, dy=0, scale=1):
    return {name: {**point, "x": .5 + (point["x"] - .5) * scale + dx,
                   "y": .5 + (point["y"] - .5) * scale + dy}
            for name, point in pose.items()}


def test_follow_fields_survive_normalization_and_legacy_stays_fixed():
    legacy = region()
    following = {**legacy, "mode": "following", "follow": {"x1": -1, "x2": 0, "y1": -1, "y2": 0}}
    assert normalize_regions([legacy]) == [legacy]
    assert normalize_regions([following]) == [following]
    bundle = {"game_id": "generic-xbox", "overrides": {}, "motions": [], "regions": [following]}
    document = canonicalize("game_bundle", bundle)
    assert canonicalize("game_bundle", json.loads(document.payload)).data["regions"] == [following]
    for broken in ({**following, "mode": "bad"}, {**following, "follow": {**following["follow"], "x2": float("nan")}}):
        with pytest.raises(ValueError):
            normalize_regions([broken])


def test_custom_follow_moves_and_scales_but_fixed_stays_still(region_kernel):
    kernel, _, feed = region_kernel
    pose = _standing_pose()
    anchor = body_anchor(pose, kernel.width, kernel.height)
    custom = {**region(), "mode": "following", "follow": relative_rect(RECT, anchor)}
    fixed = region("user_89abcdef")
    kernel.configure_regions([custom, fixed])
    feed(pose, 3)
    assert kernel.zone_rects[REGION_ID] == pytest.approx(RECT)
    moved = shifted(pose, dx=.08, dy=.04, scale=.8)
    feed(moved, 3)
    result = kernel.zone_rects[REGION_ID]
    assert result["x2"] - result["x1"] == pytest.approx((RECT["x2"] - RECT["x1"]) * .8)
    assert result["x1"] == pytest.approx(.5 + (RECT["x1"] - .5) * .8 + .08)
    assert result["y1"] == pytest.approx(.5 + (RECT["y1"] - .5) * .8 + .04)
    assert kernel.zone_rects[fixed["id"]] == RECT


def test_editor_uses_saved_full_body_without_adapting_to_current_half_body(region_kernel):
    kernel, _, feed = region_kernel
    pose = {name: point for name, point in _standing_pose().items()
            if not any(part in name for part in ("hip", "knee", "ankle", "heel", "foot"))}
    feed(pose, 3)
    before = copy.deepcopy(kernel.latest_pose)
    editor = kernel.zone_editor()
    assert editor["reference"]["source"] == "measured"
    assert editor["reference"]["pose"] == kernel.body_reference["pose"]
    assert editor["reference"]["ranges"] == []
    assert kernel.latest_pose == before
    assert not kernel.zones_frozen
    prepared, _ = kernel.prepare_zone_layout(editor["rects"], editor["modes"], editor["reference"])
    assert not prepared["zones_frozen"]


def test_editor_without_measured_full_body_requests_fit_and_does_not_invent_legs(region_kernel):
    kernel, _, feed = region_kernel
    kernel.body_reference = None
    feed(_standing_pose(), 3)
    editor = kernel.zone_editor()
    assert not editor["reference"]["ready"]
    assert editor["reference"]["pose"] == {}
    assert editor["reference"]["ranges"] == []


def test_builtin_edit_keeps_following_and_does_not_accumulate_offset(region_kernel):
    kernel, _, feed = region_kernel
    feed(_standing_pose(), 4)
    editor = kernel.zone_editor()
    target = dict(editor["rects"]["headJump"])
    target["x1"] += .03
    target["x2"] += .03
    prepared, _ = kernel.prepare_zone_layout({"headJump": target}, {}, editor["reference"])
    kernel.apply_zone_layout(prepared)
    feed(_standing_pose(), 30)
    assert not kernel.zones_frozen
    assert kernel.zone_rects["headJump"]["x1"] == pytest.approx(target["x1"], abs=.002)
    feed(_standing_pose(), 30)
    assert kernel.zone_rects["headJump"]["x1"] == pytest.approx(target["x1"], abs=.002)
    editor = kernel.zone_editor()
    modes = {name: "fixed" for name in editor["rects"]}
    prepared, _ = kernel.prepare_zone_layout(editor["rects"], modes, editor["reference"])
    kernel.apply_zone_layout(prepared)
    before = copy.deepcopy(kernel.frozen_rects)
    feed(shifted(_standing_pose(), dx=.1), 10)
    assert kernel.zones_frozen
    assert kernel.frozen_rects == before


def test_out_of_view_follow_region_open_and_save_is_lossless(region_kernel):
    kernel, _, feed = region_kernel
    feed(_standing_pose(), 3)
    custom = {**region(), "mode": "following", "follow": {"x1": -5, "x2": -4, "y1": -5, "y2": -4}}
    kernel.configure_regions([custom])
    editor = kernel.zone_editor()
    assert min(editor["rects"][REGION_ID].values()) >= 0
    prepared, regions = kernel.prepare_zone_layout(editor["rects"], editor["modes"], editor["reference"])
    assert regions == [custom]
    assert prepared["following_layout"] == {}


def test_api_add_defaults_to_current_builtin_mode_and_layout_persists(application, monkeypatch):
    app = application
    app.KERNEL.latest_pose = _standing_pose()
    game = app.PROFILES.selected_id()
    code, added = post(app, "/api/game-profiles/regions", {"op": "add", "profile_id": game, "rect": RECT})
    assert code == 200, added
    ident = added["region"]["id"]
    assert added["region"]["mode"] == "following"
    editor = get(app, "/api/zones/editor")[1]
    moved = {**editor["rects"][ident], "x1": .15, "x2": .35}
    code, data = post(app, "/api/zones/layout", {"profile_id": game,
        "rects": {ident: moved}, "reference": editor["reference"]})
    assert code == 200, data
    saved = GameProfileStore(ROOT).effective_profile()["regions"][0]
    assert saved["mode"] == "following"
    assert saved["rect"] == moved
    restored = ControlKernel(Output(), persist=True)
    try:
        restored.configure_regions([saved])
        assert restored.custom_regions[ident] == saved
    finally:
        restored.close()
    editor = get(app, "/api/zones/editor")[1]
    modes = {name: "fixed" for name in editor["rects"]}
    code, data = post(app, "/api/zones/layout", {"profile_id": game, "rects": editor["rects"],
        "modes": modes, "reference": editor["reference"]})
    assert code == 200, data
    assert app.KERNEL.zones_frozen
    assert data["profile"]["regions"][0]["mode"] == "fixed"
    code, added = post(app, "/api/game-profiles/regions", {"op": "add", "profile_id": game})
    assert code == 200, added
    assert added["region"]["mode"] == "fixed"


def test_api_layout_rolls_back_both_files_and_runtime_when_general_save_fails(application, monkeypatch):
    app = application
    app.KERNEL.latest_pose = _standing_pose()
    game = app.PROFILES.selected_id()
    added = post(app, "/api/game-profiles/regions", {"op": "add", "profile_id": game, "rect": RECT})[1]["region"]
    app.KERNEL._save_general_settings()
    paths = (app.PROFILES.selection_path, app.KERNEL._general_settings_path())
    files = {path: path.read_bytes() for path in paths}
    regions = copy.deepcopy(app.KERNEL.custom_regions)
    frozen = app.KERNEL.zones_frozen
    following = copy.deepcopy(app.KERNEL.following_layout)
    editor = get(app, "/api/zones/editor")[1]
    from motioncontrol import config_transaction
    original = config_transaction.atomic_bytes
    def fail(path, content):
        if path == app.KERNEL._general_settings_path():
            raise OSError("测试写盘失败")
        return original(path, content)
    monkeypatch.setattr(config_transaction, "atomic_bytes", fail)
    rects = {**editor["rects"], added["id"]: {**RECT, "x1": .14}}
    code, data = post(app, "/api/zones/layout", {"profile_id": game,
        "rects": rects, "modes": {name: "fixed" for name in rects},
        "reference": editor["reference"]})
    assert code == 400, data
    assert app.KERNEL.custom_regions == regions
    assert app.KERNEL.zones_frozen == frozen
    assert app.KERNEL.following_layout == following
    assert {path: path.read_bytes() for path in paths} == files
    assert app.PROFILES.effective_profile()["regions"] == list(regions.values())


def test_api_layout_rejects_stale_game_without_changes(application):
    app = application
    editor = get(app, "/api/zones/editor")[1]
    before = copy.deepcopy(app.PROFILES._selection)
    code, _ = post(app, "/api/zones/layout", {"profile_id": "wrong-game", "rects": editor["rects"],
        "modes": editor["modes"], "reference": editor["reference"]})
    assert code == 409
    assert app.PROFILES._selection == before
