import json
import time

import pytest

from motioncontrol_shared.profile_schema import flatten_bindings
from motioncontrol.voice_backend import VoiceService
from test_output_actions_v097 import manager

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
    # A pose now only defaults to tap; see test_game_profiles_v097 for the rule.
    assert result["pose.hands_cross"]["action"]["behavior"] == "hold"
    with pytest.raises(ValueError):
        VoiceService._validate_mappings([dict(phrase="停止", type="system", target="OUTPUT.STOP", behavior="hold")])


def test_grammar_phrases_cover_every_recognisable_phrase(tmp_path):
    """The phone builds its recognizer from this list, so it must be complete.

    It used to hold a hard-coded copy of the grammar, which silently drifted
    from the desktop's: a phrase added here was heard by the computer
    microphone and by nothing else.
    """
    service = VoiceService(tmp_path, lambda action: {'executed': True})
    service.configure([
        {'phrase': '保持左肩键', 'type': 'gamepad', 'target': 'LB', 'behavior': 'hold'},
        {'phrase': '地图', 'type': 'keyboard', 'target': 'M', 'synonyms': ['打开地图']},
    ])
    phrases = service.grammar_phrases()
    assert service.wake_word in phrases
    assert service.wake_word + '保持左肩键' in phrases
    assert service.wake_word + '打开地图' in phrases, 'synonyms must be recognisable too'
    for command in service.command_registry.values():
        assert command['phrase'] in phrases
    assert len(phrases) == len(set(phrases)), 'a repeated phrase would bloat the grammar'
    assert all(item and item.strip() for item in phrases)


def test_phone_payload_carries_the_phrase_list():
    server = (ROOT / 'server.py').read_text(encoding='utf-8')
    assert '"voice_phrases": VOICE.grammar_phrases()' in server


def test_profile_voice_phrase_updates_one_registry_for_phone_and_desktop(tmp_path):
    generated = tmp_path / 'config' / 'generated_voice'
    generated.mkdir(parents=True)
    (generated / 'voice_action_map.json').write_text(json.dumps({
        '体感功能一': {
            'id': 'game.profile_slot_01', 'label': '当前游戏功能1',
            'kind': 'keyboard', 'default_target': 'F1',
        },
    }, ensure_ascii=False), encoding='utf-8')
    calls = []
    service = VoiceService(tmp_path, lambda action: calls.append(action) or {'executed': True})
    service.configure_profile_bindings({'voice': {
        'game.profile_slot_01': {
            'phrase': '跳跃',
            'action': {'type': 'keyboard', 'target': 'SPACE', 'behavior': 'tap'},
        },
    }})
    assert '跳跃' in service.grammar_phrases()
    assert '体感功能一' not in service.grammar_phrases()
    assert '体感跳跃' in service.grammar_phrases()  # 旧版本说法继续兼容
    result = service._match_and_execute('跳跃', enforce_wake=True)
    assert result['matched'] is True
    assert calls[0]['command_id'] == 'game.profile_slot_01'


def test_a_game_profile_system_action_also_does_not_require_wake_word(tmp_path):
    generated = tmp_path / 'config' / 'generated_voice'
    generated.mkdir(parents=True)
    (generated / 'voice_action_map.json').write_text(json.dumps({
        '体感功能三': {
            'id': 'game.profile_slot_03', 'label': '当前游戏功能3',
            'kind': 'keyboard', 'default_target': 'F3',
        },
        '体感开始输出': {
            'id': 'output.start', 'label': '开始输出',
            'kind': 'system', 'default_target': 'OUTPUT.START',
        },
    }, ensure_ascii=False), encoding='utf-8')
    calls = []
    service = VoiceService(tmp_path, lambda action: calls.append(action) or {'executed': True})
    service.configure_profile_bindings({'voice': {
        'game.profile_slot_03': {
            'phrase': '截图',
            'action': {'type': 'system', 'target': 'ZONES.FREEZE', 'behavior': 'tap'},
        },
    }})

    result = service._match_and_execute('截图', enforce_wake=True)
    assert result['matched'] is True
    assert calls[-1]['command_id'] == 'game.profile_slot_03'
    assert service._match_and_execute('开始输出', enforce_wake=True)['reason'] == 'wake_word_required'
