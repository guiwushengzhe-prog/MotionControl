"""可选前缀、两类配置的相同输出方式，以及口令清单的真实内容。"""
import copy
import json
import shutil
from pathlib import Path

import pytest

from motioncontrol.user_paths import user_path
from motioncontrol.voice_backend import VoiceService
from motioncontrol_shared.mapping_schema import shared_voice_command_id
from test_configuration_coordination import application
from test_profile_api_v2 import request_for

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def voice(tmp_path, monkeypatch):
    folder = tmp_path / "config/generated_voice"
    folder.mkdir(parents=True)
    shutil.copy(ROOT / "config/generated_voice/voice_action_map.json", folder)
    monkeypatch.setattr(VoiceService, "_rebuild_recognizer", lambda self: None)
    calls = []
    service = VoiceService(tmp_path, lambda action: calls.append(action) or {"executed": True})
    yield service, calls
    service.close()


def mapping(phrase="跳跃", behavior="tap"):
    return {"phrase": phrase, "type": "keyboard", "target": "SPACE", "behavior": behavior}


@pytest.mark.parametrize("prefix", ["", "体感", "小助手请听我的游戏指令"])
@pytest.mark.parametrize("system", [False, True])
def test_all_system_commands_use_the_same_optional_prefix_even_with_old_scope_flag(voice, prefix, system):
    service, calls = voice
    service.configure([mapping()], wake_word=prefix, wake_system_commands=system)
    service.configure_profile_bindings({"voice": {"game.profile_slot_01": {
        "phrase": "爬绳", "action": {"type": "keyboard", "target": "C", "behavior": "hold"}}}})
    system_prefix = prefix
    assert prefix + "跳跃" in service.grammar_phrases()
    assert system_prefix + "开始输出" in service.grammar_phrases()
    assert "爬绳" in service.grammar_phrases()
    assert service._match_and_execute(prefix + "跳跃", enforce_wake=True)["matched"]
    assert calls[-1]["phrase"] == prefix + "跳跃"
    assert service._match_and_execute("爬绳", enforce_wake=True)["matched"]
    assert calls[-1]["command_id"] == "game.profile_slot_01"
    assert service._match_and_execute(system_prefix + "开始输出", enforce_wake=True)["matched"]
    if system_prefix:
        assert not service._match_and_execute("开始输出", enforce_wake=True)["matched"]
    if prefix:
        assert not service._match_and_execute("跳跃", enforce_wake=True)["matched"]


def test_empty_default_applies_to_built_in_stop_too(voice):
    service, _ = voice
    assert service.wake_word == ""
    assert service.wake_system_commands
    assert "紧急停止" in service.grammar_phrases()
    assert "体感紧急停止" not in service.grammar_phrases()
    assert service._validate_wake_word("   ") == ""


@pytest.mark.parametrize("prefix", ["", "小助手"])
def test_old_false_system_flag_migrates_without_changing_the_user_prefix(voice, prefix):
    service, _ = voice
    path = user_path("personal_voice")
    path.write_text(json.dumps({"wake_word": prefix, "wake_system_commands": False,
                              "voice_rules_version": 2}), encoding="utf-8")
    restored = VoiceService(service.root, lambda _: {"executed": True})
    try:
        assert restored.wake_word == prefix
        assert restored.system_wake_word == prefix
        assert restored.spoken_emergency_phrases() == [prefix+"紧急停止"]
        assert json.loads(path.read_text(encoding="utf-8"))["wake_system_commands"]
    finally:
        restored.close()


def test_phone_text_without_prefix_uses_the_shared_mapping(voice):
    service, calls = voice
    service.configure([mapping(behavior="hold")], wake_word="")
    _, result = service.accept_phone_text("phone", "device", "跳跃")
    assert result["matched"]
    assert calls[-1]["behavior"] == "hold"
    assert calls[-1]["phrase"] == "跳跃"


@pytest.mark.parametrize("behavior", ["tap", "hold", "release"])
def test_shared_and_game_phrases_dispatch_the_same_behavior(application, monkeypatch, behavior):
    service, calls = application.VOICE, []
    monkeypatch.setattr(application.OUTPUT, "execute_voice_action", lambda action: calls.append(action) or {"executed": True})
    service.configure([mapping(behavior=behavior)], wake_word="")
    application.PROFILES.set_overrides({"voice.game.profile_slot_01": {
        "phrase": "游戏跳跃", "action": mapping(behavior=behavior)}})
    application._apply_effective_profile()
    for phrase in ["跳跃", "游戏跳跃"]:
        assert service._match_and_execute(phrase, enforce_wake=True)["matched"]
        assert calls[-1]["behavior"] == behavior


def test_empty_prefix_and_scope_survive_restart_and_failed_save(voice, monkeypatch):
    service, _ = voice
    service.configure([mapping()], wake_word="小助手", wake_system_commands=True)
    service.configure(service.mappings, wake_word="", wake_system_commands=True)
    restored = VoiceService(service.root, lambda _: {"executed": True})
    try:
        assert restored.wake_word == ""
        assert restored.wake_system_commands
        before = copy.deepcopy(service.status())
        monkeypatch.setattr(VoiceService, "_save_configuration", lambda _: (_ for _ in ()).throw(OSError("保存失败")))
        with pytest.raises(OSError):
            service.configure(service.mappings, wake_word="变化", wake_system_commands=False)
        assert service.status()["wake_word"] == before["wake_word"]
        assert service.status()["wake_system_commands"] == before["wake_system_commands"]
    finally:
        restored.close()


def test_custom_emergency_uses_shared_prefix_and_built_in_uses_system_scope(voice):
    service, _ = voice
    stops = []
    service.emergency_stop = lambda: stops.append(True) or {"executed": True}
    service.configure([], wake_word="小助手", emergency_stop_phrases=["快停下"])
    assert service.spoken_emergency_phrases() == ["小助手紧急停止", "小助手快停下"]
    assert service._match_and_execute("小助手快停下", enforce_wake=True)["emergency"]
    service.configure([], wake_word="", wake_system_commands=True)
    assert service.spoken_emergency_phrases() == ["紧急停止", "快停下"]
    assert service._match_and_execute("紧急停止", enforce_wake=True)["emergency"]
    assert len(stops) == 2


def test_old_personal_prefix_and_custom_stop_migrate_once(voice):
    service, _ = voice
    path = user_path("personal_voice")
    path.write_text(json.dumps({"wake_word": "小助手", "emergency_stop_phrases": ["体感停下"]}), encoding="utf-8")
    restored = VoiceService(service.root, lambda _: {"executed": True})
    again = VoiceService(service.root, lambda _: {"executed": True})
    try:
        assert restored.wake_word == "小助手"
        assert restored.wake_system_commands
        assert restored.spoken_emergency_phrases() == ["小助手紧急停止", "小助手停下"]
        assert again.spoken_emergency_phrases() == restored.spoken_emergency_phrases()
    finally:
        restored.close()
        again.close()


def test_saved_shared_and_game_actions_are_present_in_real_catalog(application):
    app = application
    app.VOICE.configure([mapping(behavior="hold")], wake_word="", wake_system_commands=True)
    app.PROFILES.set_overrides({"voice.game.profile_slot_01": {
        "phrase": "爬绳", "action": {"type": "keyboard", "target": "C", "behavior": "hold"}}})
    app._apply_effective_profile()
    catalog = app.voice_command_catalog()
    by_id = {item["id"]: item for item in catalog["commands"]}
    shared_id = shared_voice_command_id("跳跃")
    assert by_id[shared_id]["phrase"] == "跳跃"
    assert by_id[shared_id]["effective_action"]["behavior"] == "hold"
    assert by_id["game.profile_slot_01"]["phrase"] == "爬绳"
    assert by_id["game.profile_slot_01"]["effective_action"]["target"] == "C"
    assert by_id["game.profile_slot_02"]["effective_action"] is None
    assert by_id["system.emergency_stop"]["phrase"] == "紧急停止"


def test_api_accepts_empty_prefix_and_system_scope(application):
    assert application.VOICE.wake_word == "", "全新服务不应从随附配置重新填回体感"
    replies = []
    request = request_for(application, "/api/voice/config", replies)
    request._body = lambda: {"mappings": [mapping()], "wake_word": "", "wake_system_commands": True}
    request.do_POST()
    assert replies[-1][0] == 200
    assert replies[-1][1]["wake_word"] == ""
    assert replies[-1][1]["wake_system_commands"]
