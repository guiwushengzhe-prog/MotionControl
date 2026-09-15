import time

import pytest

from game_profiles import flatten_bindings
from voice_backend import VoiceService
from test_output_actions_v097 import manager


def test_voice_latches_are_isolated_from_pulses_other_targets_and_sources(tmp_path):
    out, _, _, pad = manager(tmp_path)
    def voice(target, behavior, source="voice:phone"):
        return out.execute_voice_action(dict(type="gamepad", target=target, behavior=behavior, source=source, duration=0.02))
    try:
        out.set_buttons(["RB"], source="physical:test")
        voice("RB", "hold")
        voice("A", "hold")
        voice("RB", "tap")
        voice("A", "tap")
        time.sleep(0.12)
        assert set(pad.buttons) == {"RB", "A"}
        voice("RB", "release")
        assert set(pad.buttons) == {"RB", "A"}
        out.clear_source("physical:test")
        assert set(pad.buttons) == {"A"}
        voice("RB", "hold", "voice:computer")
        out.clear_source("voice:phone")
        assert set(pad.buttons) == {"RB"}
        voice(["LB", "RB"], "hold")
        voice(["RB", "LB"], "release")
        assert set(pad.buttons) == {"RB"}
        out.set_config(enabled=False)
        assert not pad.buttons
    finally:
        out.close()


def test_phone_mapping_hold_release_reconfigure_and_disconnect(tmp_path):
    out, _, _, pad = manager(tmp_path)
    service = VoiceService(tmp_path, out.execute_voice_action, clear_source=out.clear_source)
    mappings = [dict(phrase="保持右肩键", type="gamepad", target="RB", behavior="hold"),
                dict(phrase="松开右肩键", type="gamepad", target="RB", behavior="release")]
    try:
        service.configure(mappings)
        def say(phrase):
            return service.accept_phone_text("phone", "device", "体感" + phrase)
        say("保持右肩键")
        assert set(pad.buttons) == {"RB"}
        say("松开右肩键")
        assert not pad.buttons
        say("保持右肩键")
        service.configure(mappings)
        assert not pad.buttons
        say("保持右肩键")
        service.disconnect("phone")
        assert not pad.buttons
        service._match_and_execute("保持右肩键", source_id="phone")
        assert not pad.buttons
        assert service.mappings[0]["behavior"] == "hold"
    finally:
        out.close()


def test_voice_profile_preserves_hold_release_but_defaults_to_tap():
    result = flatten_bindings({"voice": {
        "hold": {"type": "gamepad", "target": "RB", "behavior": "hold"},
        "release": {"type": "gamepad", "target": "RB", "behavior": "release"},
        "old": {"type": "keyboard", "target": "W"}},
        "poses": {"hands_cross": {"type": "gamepad", "target": "A", "behavior": "hold"}}})
    assert result["voice.hold"]["action"]["behavior"] == "hold"
    assert result["voice.release"]["action"]["behavior"] == "release"
    assert result["voice.old"]["action"]["behavior"] == "tap"
    assert result["pose.hands_cross"]["action"]["behavior"] == "tap"
    with pytest.raises(ValueError):
        VoiceService._validate_mappings([dict(phrase="停止", type="system", target="OUTPUT.STOP", behavior="hold")])
