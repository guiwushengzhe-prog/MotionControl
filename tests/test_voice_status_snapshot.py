"""Configuration-derived diagnostics stay current without rebuilding on polls."""

import json
import shutil
from pathlib import Path

import pytest

from motioncontrol.user_paths import user_path
from motioncontrol.voice_backend import VoiceService

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def voice(tmp_path):
    folder = tmp_path / "config/generated_voice"
    folder.mkdir(parents=True)
    shutil.copy(ROOT / "config/generated_voice/voice_action_map.json", folder)
    service = VoiceService(tmp_path, lambda action: {"executed": True})
    yield service
    service.close()


def mapping(phrase):
    return {"phrase": phrase, "type": "keyboard", "target": "SPACE", "behavior": "tap"}


def profile(phrase):
    return {"voice": {"game.profile_slot_01": {"phrase": phrase}}}


def test_conflicts_follow_profile_and_shared_configuration_changes(voice):
    voice.configure([mapping("跳跃")])
    voice.configure_profile_bindings(profile("跳跃"))
    assert any("跳跃" in item for item in voice.status()["phrase_conflicts"])
    voice.configure_profile_bindings(profile("闪避"))
    assert not voice.status()["phrase_conflicts"]
    voice.configure([], wake_word="动作", emergency_stop_phrases=["动作停下"])
    voice.configure([mapping("跳跃")])
    voice.configure_profile_bindings(profile("跳跃"))
    assert any("动作跳跃" in item for item in voice.status()["phrase_conflicts"])
    voice.configure_profile_bindings({})
    assert not voice.status()["phrase_conflicts"]


def test_rejected_save_keeps_published_diagnostics(voice, monkeypatch):
    voice.configure([mapping("跳跃")])
    voice.configure_profile_bindings(profile("跳跃"))
    before = voice.status()

    def fail_save(self):
        raise OSError("disk full")

    monkeypatch.setattr(VoiceService, "_save_configuration", fail_save)
    with pytest.raises(OSError, match="disk full"):
        voice.configure([])
    after = voice.status()
    assert after["mappings"] == before["mappings"]
    assert after["phrase_conflicts"] == before["phrase_conflicts"]


def test_repeated_polls_preserve_dynamic_values_and_own_diagnostics(voice, monkeypatch):
    voice.configure([mapping("跳跃")])
    voice.configure_profile_bindings(profile("跳跃"))
    commands_for = voice._commands_for
    rebuilds = []

    def count_build(bindings):
        rebuilds.append(bindings)
        return commands_for(bindings)

    monkeypatch.setattr(voice, "_commands_for", count_build)
    voice.status()["phrase_conflicts"].clear()
    for i in range(100):
        voice.last_partial = str(i)
        voice.commands_heard = i
        result = voice.status()
        assert result["partial"] == str(i)
        assert result["commands_heard"] == i
        assert result["phrase_conflicts"]
    assert not rebuilds
    voice.configure_profile_bindings({})
    assert rebuilds
    assert not voice.status()["phrase_conflicts"]


def test_startup_removes_old_shadowed_mappings_from_diagnostics(tmp_path):
    path = user_path("voice_mappings")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mappings": [mapping("体感挪动区域")]}), encoding="utf-8")
    folder = tmp_path / "config/generated_voice"
    folder.mkdir(parents=True)
    shutil.copy(ROOT / "config/generated_voice/voice_action_map.json", folder)
    service = VoiceService(tmp_path, lambda action: {"executed": True})
    try:
        assert not service.status()["mappings"]
        assert not service.status()["phrase_conflicts"]
    finally:
        service.close()
