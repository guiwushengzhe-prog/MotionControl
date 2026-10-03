"""通用口令和本游戏口令使用同一输出，保存、执行和松开均可用。"""
import copy

import pytest

from motioncontrol.voice_backend import VoiceService
from motioncontrol_shared.canonical import canonicalize
from motioncontrol_shared.mapping_schema import normalize_voice_mappings, shared_voice_command_id
from motioncontrol_shared.profile_schema import normalize_action
from test_configuration_coordination import application
from test_output_actions_v097 import manager
from motioncontrol.key_macros import MacroStore


ACTIONS = [
    {"type": "keyboard", "target": "CTRL+S", "behavior": "tap"},
    {"type": "mouse_button", "target": "LEFT", "behavior": "hold"},
    {"type": "mouse_wheel", "target": "SCROLL_DOWN", "behavior": "tap"},
    {"type": "gamepad", "target": ["LB", "LS_UP"], "behavior": "hold", "combo_stick_lead_ms": 35},
    {"type": "gamepad_trigger", "target": "LT", "behavior": "hold"},
    {"type": "gamepad_axis", "target": "LS_LEFT", "behavior": "release"},
    {"type": "macro", "target": "test_macro", "behavior": "hold"},
    {"type": "voice_release", "target": ["game.profile_slot_01", "game.profile_slot_02"], "behavior": "tap"},
    {"type": "system", "target": "POSE.ADD_FRAME", "behavior": "tap"},
]


@pytest.mark.parametrize("action", ACTIONS, ids=lambda action: action["type"])
def test_shared_and_game_validation_and_cloud_round_trip_preserve_the_same_action(action):
    shared = normalize_voice_mappings([{"phrase": "口令", **action}])[0]
    assert {k: v for k, v in shared.items() if k != "phrase"} == normalize_action(action)
    assert canonicalize("voice_mappings", {"mappings": [shared]}).data["mappings"] == [shared]


def test_saved_new_outputs_dispatch_complete_actions_and_survive_reload(application, monkeypatch):
    app, calls = application, []
    monkeypatch.setattr(VoiceService, "_rebuild_recognizer", lambda self: None)
    monkeypatch.setattr(app.OUTPUT, "execute_voice_action", lambda action: calls.append(copy.deepcopy(action)) or {"executed": True})
    actions = copy.deepcopy(ACTIONS[:7])
    macro = app.MACROS.create(name="循环测试", repeat=True)
    app.KERNEL.configure_macros(app.MACROS)
    actions[-1]["target"] = macro["id"]
    mappings = [{"phrase": f"通用口令{i}", **action} for i, action in enumerate(actions)]
    app.VOICE.configure(mappings, wake_word="")
    for i, action in enumerate(actions):
        app.PROFILES.set_overrides({"voice.game.profile_slot_01": {"phrase": "游戏口令", "action": action}})
        app._apply_effective_profile()
        for phrase in (f"通用口令{i}", "游戏口令"):
            assert app.VOICE._match_and_execute(phrase, enforce_wake=True)["matched"]
            assert {key: calls[-1][key] for key in action} == action
    restored = VoiceService(app.VOICE.root, lambda _: {"executed": True})
    try:
        assert restored.mappings == mappings
    finally:
        restored.close()


def test_release_can_mix_shared_and_game_commands_and_references_survive_reordering(application, monkeypatch):
    app, released = application, []
    monkeypatch.setattr(VoiceService, "_rebuild_recognizer", lambda self: None)
    monkeypatch.setattr(app.OUTPUT, "release_voice_hold", lambda action: released.append(copy.deepcopy(action)))
    shared = {"phrase": "保持鼠标", "type": "mouse_button", "target": "LEFT", "behavior": "hold"}
    shared_id = shared_voice_command_id(shared["phrase"])
    app.VOICE.configure([shared], wake_word="")
    app.PROFILES.set_overrides({"voice.game.profile_slot_01": {
        "phrase": "保持左移", "action": {"type": "gamepad_axis", "target": "LS_LEFT", "behavior": "hold"}}})
    app._apply_effective_profile()
    stop = {"type": "voice_release", "target": [shared_id, "game.profile_slot_01"], "behavior": "tap"}
    assert app.execute_voice_action(stop)["executed"]
    assert [a["target"] for a in released] == ["LEFT", "LS_LEFT"]
    app.VOICE.configure([{"phrase": "其他", "type": "keyboard", "target": "C"}, shared], wake_word="小助手")
    released.clear()
    assert app.execute_voice_action(stop)["executed"]
    assert [a["target"] for a in released] == ["LEFT", "LS_LEFT"]
    app.VOICE.configure([], wake_word="")
    assert not app.execute_voice_action({**stop, "target": shared_id})["executed"]


def test_looping_voice_macro_uses_a_hold_source_even_if_saved_as_tap(tmp_path):
    output, _, _, _ = manager(tmp_path)
    store = MacroStore(tmp_path / "macros.json")
    macro = store.create(name="循环", repeat=True)
    output.configure_macros(store)
    action = {"type": "macro", "target": macro["id"], "behavior": "tap"}
    try:
        assert output.execute_voice_action(action)["executed"]
        assert output.status()["voice_latches"][0]["type"] == "macro"
        output.release_voice_hold(action)
        assert output.status()["voice_latches"] == []
        assert not output._macro_runs
    finally:
        output.close()
