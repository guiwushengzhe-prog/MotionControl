"""完整口令链路的配合、重置和结束状态，不打开相机或真实游戏输出。"""
import pytest

from motioncontrol_shared.mapping_schema import shared_voice_command_id
from test_alternate_body_action import C, CONDITION, CYCLE, dispatch
from test_configuration_coordination import application
from test_output_actions_v097 import manager


def squat_key(output):
    return sorted(output.keyboard.pressed & {"C", "Z", "X"})


@pytest.mark.parametrize("scope", ["game", "shared"])
def test_spoken_hold_is_a_companion_until_released_or_disconnected(application, monkeypatch, tmp_path, scope):
    app = application
    output, _, _, _ = manager(tmp_path)
    monkeypatch.setattr(app.KERNEL, "output", output)
    monkeypatch.setattr(app.OUTPUT, "execute_voice_action", output.execute_voice_action)
    monkeypatch.setattr(app.OUTPUT, "clear_source", output.clear_source)
    monkeypatch.setattr(app.VOICE, "_rebuild_recognizer", lambda: None)
    phrase = "配合口令"
    ident = "game.profile_slot_01" if scope == "game" else shared_voice_command_id(phrase)
    action = {"type": "keyboard", "target": "V", "behavior": "hold"}
    bindings = {"motions": {"squat": {**CONDITION, "alternate_when": f"voice.{ident}"}},
                "voice": {ident: {"phrase": phrase, "action": action}} if scope == "game" else {}}
    app.KERNEL.configure_bindings(bindings)
    app.VOICE.configure_profile_bindings(bindings)
    app.VOICE.configure([] if scope == "game" else [{"phrase": phrase, **action}], wake_word="")
    try:
        assert app.VOICE._match_and_execute(phrase, source_id=None, enforce_wake=True)["matched"]
        assert output.active_voice_triggers() == {f"voice.{ident}"}
        dispatch(app.KERNEL, squat=True)
        assert squat_key(output) == ["Z"]
        output.release_voice_hold(action)
        assert output.active_voice_triggers() == set()
        dispatch(app.KERNEL, squat=True, now=1.1)
        assert squat_key(output) == ["Z"]  # 已选键保持到这次主动作结束。
        dispatch(app.KERNEL, now=1.2)
        dispatch(app.KERNEL, squat=True, now=1.3)
        assert squat_key(output) == ["C"]
        dispatch(app.KERNEL, now=1.4)
        app.VOICE._match_and_execute(phrase, source_id=None, enforce_wake=True)
        output.clear_source("voice")
        dispatch(app.KERNEL, squat=True, now=1.5)
        assert squat_key(output) == ["C"]
    finally:
        output.close()


def test_instant_voice_output_can_pair_once_and_reset_even_between_frames(application, monkeypatch, tmp_path):
    app = application
    output, mouse, _, _ = manager(tmp_path)
    monkeypatch.setattr(app.KERNEL, "output", output)
    monkeypatch.setattr(app.OUTPUT, "execute_voice_action", output.execute_voice_action)
    ident = "game.profile_slot_01"
    action = {"type": "mouse_wheel", "target": "SCROLL_UP", "behavior": "tap"}
    app.KERNEL.configure_bindings({"motions": {"squat": {**CONDITION, "alternate_when": f"voice.{ident}"}},
                                  "voice": {ident: {"action": action}}})
    command = {"command_id": ident, "type": "keyboard", "target": "F1"}
    try:
        assert app.execute_voice_action(command)["executed"]
        assert mouse.wheels == [("SCROLL_UP", 1)]
        dispatch(app.KERNEL, squat=True)
        assert squat_key(output) == ["Z"]
        dispatch(app.KERNEL, now=1.1)
        dispatch(app.KERNEL, squat=True, now=1.2)
        assert squat_key(output) == ["C"]

        app.KERNEL.configure_bindings({"motions": {"squat": {**CYCLE, "reset_trigger": f"voice.{ident}"}},
                                      "voice": {ident: {"action": action}}})
        dispatch(app.KERNEL, squat=True, now=2.)
        dispatch(app.KERNEL, now=2.1)
        assert app.KERNEL._sequence_next["motion.squat"] == 1
        assert app.execute_voice_action(command)["executed"]
        assert app.KERNEL._sequence_next["motion.squat"] == 0
        dispatch(app.KERNEL, squat=True, now=2.2)
        assert squat_key(output) == ["C"]
        dispatch(app.KERNEL, now=2.3)
        dispatch(app.KERNEL, squat=True, now=2.4)
        assert squat_key(output) == ["Z"]
    finally:
        output.close()


def test_disabled_voice_and_paused_output_cannot_arm_a_condition(application, monkeypatch, tmp_path):
    app = application
    output, _, _, _ = manager(tmp_path)
    monkeypatch.setattr(app.KERNEL, "output", output)
    monkeypatch.setattr(app.OUTPUT, "execute_voice_action", output.execute_voice_action)
    ident = "game.profile_slot_01"
    app.KERNEL.configure_bindings({"voice": {ident: {"disabled": True}}})
    command = {"command_id": ident, **C}
    try:
        assert not app.execute_voice_action(command)["executed"]
        assert not app.KERNEL._voice_rule_pulses
        assert not output.active_voice_triggers()
        app.KERNEL.configure_bindings({"voice": {ident: {"action": C}}})
        assert app.execute_voice_action(command)["executed"]
        assert output.active_voice_triggers() == {f"voice.{ident}"}
        output.set_config(enabled=False)
        assert not output.active_voice_triggers()
        assert not app.execute_voice_action(command)["executed"]
        assert not app.KERNEL._voice_rule_pulses
        output.set_config(enabled=True)
        assert not output.active_voice_triggers()
        output.execute_voice_action({"command_id": "game.profile_slot_02", **C})
        assert output.active_voice_triggers() == {"voice.game.profile_slot_02"}
    finally:
        output.close()
