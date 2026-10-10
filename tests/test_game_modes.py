"""同一游戏的模式：持久化、控制边沿和语音后台切换。"""
import copy
import hashlib
import json
import threading
from pathlib import Path

import pytest

from motioncontrol.control_kernel import ControlKernel
from motioncontrol.game_profiles import GameProfileStore, ProfileSelectionChanged
from motioncontrol_shared.canonical import canonicalize
from motioncontrol_shared.profile_schema import normalize_action, normalize_modes
from motioncontrol_shared.profile_versions import SELECTION_SCHEMA
from test_alternate_body_action import Output, CYCLE, CONDITION, dispatch, squat_key
from test_configuration_coordination import application
from test_profile_api_v2 import request_for

ROOT = Path(__file__).resolve().parents[1]
MODE_ID = "mode-0123abcd"
W = {"type": "keyboard", "target": "W", "behavior": "hold"}
E = {"type": "keyboard", "target": "E", "behavior": "hold"}
F = {"type": "keyboard", "target": "F", "behavior": "hold"}
NEXT = {"type": "mode", "target": "NEXT", "behavior": "tap"}


@pytest.mark.parametrize("raw,expected", [(" next ", "NEXT"), ("default", "DEFAULT"),
                                        (" MODE-0123ABCD ", MODE_ID)])
def test_mode_action_normalizes_target_and_is_always_tap(raw, expected):
    assert normalize_action({"type": "mode", "target": raw, "behavior": "hold"}) == {
        "type": "mode", "target": expected, "behavior": "tap"}


@pytest.mark.parametrize("target", ["", "foo", "mode-123", "mode-xxxxxxxx", None, ["NEXT"]])
def test_mode_action_rejects_invalid_target(target):
    with pytest.raises(ValueError):
        normalize_action({"type": "mode", "target": target})


@pytest.mark.parametrize("items", [
    [{"id": MODE_ID, "name": "默认"}],
    [{"id": MODE_ID, "name": "开车"}, {"id": "mode-11111111", "name": " 开车 "}],
    [{"id": MODE_ID, "name": "步行"}, {"id": MODE_ID, "name": "开车"}],
    [{"id": "bad", "name": "开车"}],
    [{"id": MODE_ID, "name": ""}],
    [{"id": MODE_ID, "name": "字" * 13}],
    [{"id": f"mode-{i:08x}", "name": str(i)} for i in range(9)],
])
def test_modes_reject_bad_names_ids_duplicates_and_limit(items):
    with pytest.raises(ValueError):
        normalize_modes(items)


def test_legacy_canonical_bytes_and_digest_are_unchanged():
    legacy = {"schema": SELECTION_SCHEMA, "selected_id": "generic-xbox", "overrides_by_profile": {}}
    expected = (json.dumps(legacy, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    result = canonicalize("profile_selection", legacy)
    assert result.payload == expected
    assert result.sha256 == hashlib.sha256(expected).hexdigest()
    assert canonicalize("profile_selection", {**legacy, "modes_by_profile": {"generic-xbox": []}}).payload == expected


def test_canonical_selection_and_game_bundle_preserve_normalized_modes():
    modes = [{"id": "MODE-0123ABCD", "name": " 开车 ", "overrides": {
        "zone.rightHand": {"action": F}, "voice.game.profile_slot_01": {"action": NEXT}}}]
    selection = canonicalize("profile_selection", {"schema": SELECTION_SCHEMA,
        "selected_id": "generic-xbox", "overrides_by_profile": {},
        "modes_by_profile": {"generic-xbox": modes}})
    bundle = canonicalize("game_bundle", {"game_id": "generic-xbox", "overrides": {}, "motions": [], "modes": modes})
    assert selection.data["modes_by_profile"]["generic-xbox"] == normalize_modes(modes)
    assert bundle.data["modes"] == normalize_modes(modes)


def test_store_modes_copy_edit_reload_and_remove():
    store = GameProfileStore(ROOT)
    store.set_overrides({"zone.rightHand": {"action": E}})
    drive = store.add_mode("开车")
    assert store.effective_profile(drive["id"])["bindings"]["zones"]["rightHand"]["action"] == E
    store.set_overrides({"zone.rightHand": {"action": F}}, mode_id=drive["id"])
    copied = store.add_mode("骑车", copy_from=drive["id"])
    store.rename_mode(drive["id"], "驾驶")
    restored = GameProfileStore(ROOT)
    assert restored.modes() == [{"id": None, "name": "默认"}, {**drive, "name": "驾驶"}, copied]
    assert restored.effective_profile()["bindings"]["zones"]["rightHand"]["action"] == E
    assert restored.effective_profile(copied["id"])["bindings"]["zones"]["rightHand"]["action"] == F
    restored.remove_mode(drive["id"])
    with pytest.raises(ProfileSelectionChanged):
        restored.set_overrides({}, mode_id=drive["id"])
    restored.remove_mode(copied["id"])
    assert "modes_by_profile" not in restored._selection
    assert GameProfileStore(ROOT).effective_profile()["mode"] == {"id": None, "name": "默认"}


def test_store_deleting_custom_game_removes_its_modes():
    store = GameProfileStore(ROOT)
    custom = store.add_custom_game("模式测试游戏")
    store.select(custom["id"])
    store.add_mode("开车")
    store.remove_custom_game(custom["id"])
    assert custom["id"] not in store._selection.get("modes_by_profile", {})
    assert store.effective_profile()["mode"]["id"] is None


class ModeOutput(Output):
    def __init__(self):
        super().__init__()
        self.history = []

    def active_voice_triggers(self):
        return set()

    def set_action_holds(self, holds, source_group="controls"):
        super().set_action_holds(holds, source_group)
        self.history.append(copy.deepcopy(self.holds))


@pytest.fixture
def mode_kernel():
    output = ModeOutput()
    kernel = ControlKernel(output)
    yield kernel, output
    kernel.close()


def test_switch_keeps_unchanged_walk_and_tap_releases_changed_zone_until_new_edge(mode_kernel):
    kernel, output = mode_kernel
    bindings = {"motions": {"march": {"action": W}}, "poses": {
        "hands_cross": {"action": {**E, "behavior": "tap"}}}, "zones": {"rightHand": {"action": E}}}
    kernel.configure_bindings(bindings)
    with kernel._lock:
        kernel.motion_active = {"march"}
        kernel.pose_active = {"hands_cross"}
        kernel.zone_state["rightHand"]["pressed"] = True
        kernel._dispatch_controls_locked(1.)
    before = len(output.history)
    changed = copy.deepcopy(bindings)
    changed["zones"]["rightHand"]["action"] = F
    kernel.switch_bindings(changed, mode={"id": MODE_ID, "name": "开车"})
    assert all(any(item["action"] == W for item in holds) for holds in output.history[before:])
    assert [a["target"] for a in output.pulses] == ["E"]
    assert not any(h["id"] == "zone.rightHand" for h in output.holds)
    with kernel._lock:
        kernel.zone_state["rightHand"]["pressed"] = False
        kernel._dispatch_controls_locked(2.)
        kernel.zone_state["rightHand"]["pressed"] = True
        kernel._dispatch_controls_locked(3.)
    assert next(h["action"] for h in output.holds if h["id"] == "zone.rightHand") == F
    assert kernel.status()["game_mode"] == {"id": MODE_ID, "name": "开车"}


def test_mode_trigger_fires_once_until_released(mode_kernel):
    kernel, _ = mode_kernel
    calls = []
    bindings = {"poses": {"hands_cross": {"action": NEXT}}}
    kernel.configure_bindings(bindings)
    kernel.configure_mode_action_handler(lambda target, trigger: calls.append((target, trigger)))
    dispatch(kernel, modifier=True)
    kernel.switch_bindings(bindings, mode={"id": MODE_ID, "name": "开车"})
    dispatch(kernel, modifier=True, now=2.)
    assert calls == [("NEXT", "pose.hands_cross")]
    dispatch(kernel, now=3.)
    dispatch(kernel, modifier=True, now=4.)
    assert len(calls) == 2


@pytest.mark.parametrize("rule", [CONDITION, CYCLE])
def test_switch_preserves_rule_state_if_same_and_resets_if_changed(mode_kernel, rule):
    kernel, output = mode_kernel
    bindings = {"motions": {"squat": rule}}
    kernel.configure_bindings(bindings)
    dispatch(kernel, squat=True, modifier=True)
    if rule == CYCLE:
        dispatch(kernel, now=1.1)
        dispatch(kernel, squat=True, now=1.2)
    assert squat_key(output) == ["Z"]
    kernel.switch_bindings(bindings, mode={"id": MODE_ID, "name": "开车"})
    dispatch(kernel, squat=True, now=2.)
    assert squat_key(output) == ["Z"]
    changed = copy.deepcopy(bindings)
    changed["motions"]["squat"]["alternate_action"] = F
    kernel.switch_bindings(changed)
    assert squat_key(output) == []
    dispatch(kernel, now=3.)
    dispatch(kernel, squat=True, now=4.)
    assert squat_key(output) == ["C"]


def post(app, route, body):
    replies = []
    request = request_for(app, route, replies)
    request._body = lambda: body
    request.do_POST()
    return replies[-1]


def get(app, route):
    replies = []
    request_for(app, route, replies).do_GET()
    return replies[-1]


def make_api_mode(app):
    code, data = post(app, "/api/game-profiles/modes", {"op": "add", "name": "开车"})
    assert code == 200, data
    return data["profile"]["mode"]["id"]


def test_api_modes_edit_independently_and_switch_voice_without_rebuild(application, monkeypatch):
    app = application
    drive = make_api_mode(app)
    overrides = {"zone.rightHand": {"action": F}, "voice.game.profile_slot_01": {"phrase": "驾驶口令", "action": F}}
    assert post(app, "/api/game-profiles/overrides", {"mode_id": drive, "overrides": overrides})[0] == 200
    assert app.PROFILES.effective_profile()["overrides"] == {}
    grammar = app.VOICE.grammar_phrases()
    rebuilds = []
    monkeypatch.setattr(app.VOICE, "_rebuild_recognizer", lambda: rebuilds.append(True))
    before_disk = app.PROFILES.selection_path.read_bytes()
    assert post(app, "/api/game-profiles/mode", {"target": drive})[0] == 200
    assert app.KERNEL.control_bindings["zone.rightHand"]["action"] == F
    assert app.KERNEL.game_mode["id"] == drive
    assert app.VOICE._profile_bindings["voice"]["game.profile_slot_01"]["phrase"] == "驾驶口令"
    assert app.VOICE.grammar_phrases() == grammar
    assert rebuilds == []
    assert app.PROFILES.selection_path.read_bytes() == before_disk
    assert get(app, "/api/game-profiles/selected?mode=DEFAULT")[1]["profile"]["mode"]["id"] is None
    assert get(app, "/api/game-profiles/selected")[1]["profile"]["mode"]["id"] == drive
    post(app, "/api/game-profiles/mode", {"target": "NEXT"})
    assert app.KERNEL.game_mode["id"] is None
    assert not app.VOICE._match_and_execute("驾驶口令", enforce_wake=False)["matched"]
    assert rebuilds == []


@pytest.mark.parametrize("trigger", ["pose", "game_voice", "shared_voice"])
def test_trigger_switch_uses_background_queue_not_voice_thread(application, monkeypatch, trigger):
    app = application
    drive = make_api_mode(app)
    app.PROFILES.set_overrides({"pose.hands_cross": {"action": NEXT},
                              "voice.game.profile_slot_01": {"phrase": "换模式", "action": NEXT}})
    app._apply_effective_profile()
    app.VOICE.configure([{ "phrase": "通用切换", **NEXT}], wake_word="")
    completed = threading.Event()
    calls = []
    original = app.switch_game_mode

    def watched(target):
        result = original(target)
        calls.append(threading.get_ident())
        completed.set()
        return result

    monkeypatch.setattr(app, "switch_game_mode", watched)
    # 语音持锁期间必须返回，队列随后按配置锁→语音锁顺序切换。
    with app.VOICE._lock:
        if trigger == "pose":
            with app.KERNEL._lock:
                app.KERNEL.pose_active = {"hands_cross"}
                app.KERNEL._dispatch_controls_locked(1.)
        else:
            phrase = "换模式" if trigger == "game_voice" else "通用切换"
            assert app.VOICE._match_and_execute(phrase, enforce_wake=True)["matched"]
        assert not completed.is_set()
    assert completed.wait(3.), app.SYSTEM_COMMANDS.last_error
    assert calls == [app.SYSTEM_COMMANDS._thread.ident]
    assert calls[0] != threading.get_ident()
    assert app.KERNEL.game_mode["id"] == drive


def test_switch_game_delete_active_and_restart_return_to_default(application):
    app = application
    drive = make_api_mode(app)
    app.switch_game_mode(drive)
    custom = app.PROFILES.add_custom_game("另一个游戏")
    assert post(app, "/api/game-profiles/select", {"id": custom["id"]})[0] == 200
    assert app.KERNEL.game_mode["id"] is None
    post(app, "/api/game-profiles/select", {"id": "generic-xbox"})
    app.switch_game_mode(drive)
    app.GAME_MODE.update(game=None, id=None)  # 新进程的初始运行状态。
    app._apply_effective_profile()
    assert app.KERNEL.game_mode["id"] is None
    app.switch_game_mode(drive)
    assert post(app, "/api/game-profiles/modes", {"op": "remove", "id": drive})[0] == 200
    assert app.KERNEL.game_mode["id"] is None
    assert post(app, "/api/game-profiles/overrides", {"mode_id": drive, "overrides": {}})[0] == 409


def test_failed_install_restores_current_mode_bindings_voice_and_disk(application, monkeypatch):
    app = application
    drive = make_api_mode(app)
    app.PROFILES.set_overrides({"zone.rightHand": {"action": F}}, mode_id=drive)
    app._apply_effective_profile()
    app.switch_game_mode(drive)
    before = (copy.deepcopy(app.GAME_MODE), copy.deepcopy(app.KERNEL.control_bindings),
              copy.deepcopy(app.VOICE._profile_bindings), copy.deepcopy(app.VOICE._mode_bindings),
              app.PROFILES.selection_path.read_bytes())
    monkeypatch.setattr(app, "save_motion_config", lambda items: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        app._install_game_bundle({"game_id": "generic-xbox", "overrides": {}, "motions": [],
                                 "modes": [{"id": MODE_ID, "name": "替换", "overrides": {}}]})
    assert (app.GAME_MODE, app.KERNEL.control_bindings, app.VOICE._profile_bindings,
            app.VOICE._mode_bindings, app.PROFILES.selection_path.read_bytes()) == before


def test_deleted_mode_read_and_stale_game_switch_are_refused(application):
    app = application
    assert get(app, "/api/game-profiles/selected?mode=" )[1]["profile"]["mode"]["id"] is None
    assert get(app, "/api/game-profiles/selected?mode=" + MODE_ID)[0] == 409
    assert post(app, "/api/game-profiles/mode", {"profile_id": "old-game", "target": "NEXT"})[0] == 409


def test_voice_mode_switch_reports_a_full_queue(application, monkeypatch):
    app = application
    monkeypatch.setattr(app.SYSTEM_COMMANDS, "submit", lambda action: {"queued": False, "executed": False, "reason": "队列已满"})
    assert app._queue_mode_switch("NEXT") == {"queued": False, "executed": False, "reason": "队列已满"}


def test_mode_descriptions_use_names_and_include_other_mode_mappings():
    from motioncontrol_shared.describe import describe, describe_action

    modes = [{"id": MODE_ID, "name": "开车", "overrides": {"zone.rightHand": {"action": F}}}]
    assert describe_action(NEXT) == "切到下一个模式"
    assert describe_action({"type": "mode", "target": "DEFAULT"}) == "回到默认模式"
    assert describe_action({"type": "mode", "target": MODE_ID}, modes) == "切到「开车」模式"
    result = describe("game_bundle", {"game_id": "generic-xbox", "overrides": {
        "pose.hands_cross": {"action": {"type": "mode", "target": MODE_ID}}}, "modes": modes})
    assert result["groups"][0]["items"][0]["action"] == "切到「开车」模式"
    assert result["modes"][0]["groups"][0]["items"][0]["action"] == "键盘 F · 持续按住"
    selection = describe("profile_selection", {"modes_by_profile": {"generic-xbox": modes}})
    assert selection["games"][0]["modes"][0]["name"] == "开车"


@pytest.mark.parametrize("game_id", [None, "generic-xbox"])
def test_install_modes_without_default_overrides_keeps_local_default(application, game_id):
    app = application
    app.PROFILES.set_overrides({"zone.rightHand": {"action": E}})
    modes = [{"id": MODE_ID, "name": "开车", "overrides": {"zone.rightHand": {"action": F}}}]
    app._install_profile_selection({"selected_id": "generic-xbox", "overrides_by_profile": {},
                                    "modes_by_profile": {"generic-xbox": modes}}, game_id)
    assert app.PROFILES.effective_profile(MODE_ID)["bindings"]["zones"]["rightHand"]["action"] == F
    assert app.PROFILES.effective_profile()["bindings"]["zones"]["rightHand"]["action"] == E
    # 旧格式分享不会删除本机已有模式。
    app._install_profile_selection({"selected_id": "generic-xbox", "overrides_by_profile": {"generic-xbox": {}}}, game_id)
    assert app.PROFILES.modes()[1]["id"] == MODE_ID


def test_editing_inactive_mode_does_not_release_live_keys_or_repeat_tap(application, monkeypatch):
    app = application
    drive = make_api_mode(app)
    output = ModeOutput()
    app.KERNEL.output = output
    app.PROFILES.set_overrides({"motion.march": {"action": W},
        "pose.hands_cross": {"action": {**E, "behavior": "tap"}}})
    app._apply_effective_profile()
    with app.KERNEL._lock:
        app.KERNEL.motion_active = {"march"}
        app.KERNEL.pose_active = {"hands_cross"}
        app.KERNEL._dispatch_controls_locked(1.)
    before = len(output.history)
    rebuilds = []
    monkeypatch.setattr(app.VOICE, "_rebuild_recognizer", lambda: rebuilds.append(True))
    code, data = post(app, "/api/game-profiles/overrides", {"mode_id": drive, "overrides": {"zone.rightHand": {"action": F}}})
    assert code == 200, data
    assert app.KERNEL.game_mode["id"] is None
    assert all(any(h["action"] == W for h in holds) for holds in output.history[before:])
    with app.KERNEL._lock:
        app.KERNEL._dispatch_controls_locked(2.)
    assert [a["target"] for a in output.pulses] == ["E"]
    assert rebuilds == []
