"""本游戏口令可增删，沿用映射保存和模式切换，不打开麦克风。"""
import copy

import pytest

from motioncontrol.game_profiles import GameProfileStore
from motioncontrol_shared.profile_schema import _merge_bindings, normalize_bindings, normalize_overrides
from test_configuration_coordination import application
from test_profile_api_v2 import request_for
from test_voice_optional_wake import voice


def binding(phrase):
    return {"phrase": phrase, "action": {"type": "keyboard", "target": "V", "behavior": "tap"}}


@pytest.mark.parametrize("number", [13, 64])
def test_new_slot_registers_phrase_and_dispatches_stable_id(voice, number):
    service, calls = voice
    ident = f"game.profile_slot_{number:02d}"
    service.configure_profile_bindings({"voice": {ident: binding("新加口令")}})
    assert "新加口令" in service.grammar_phrases()
    assert service._match_and_execute("新加口令", enforce_wake=True)["matched"]
    assert calls[-1]["command_id"] == ident
    _, result = service.accept_phone_command("phone", "device", ident)
    assert result["matched"]
    assert calls[-1]["command_id"] == ident


def test_deleted_phrase_leaves_index_can_be_reused_and_stale_id_cannot_dispatch(voice):
    service, calls = voice
    old = {"voice": {"game.profile_slot_01": binding("游戏口令")}}
    service.configure_profile_bindings(old)
    stale = copy.deepcopy(service._phrase_index["游戏口令"])
    service.configure_profile_bindings({"voice": {"game.profile_slot_01": {"disabled": True}}})
    assert "游戏口令" not in service.grammar_phrases()
    assert not service._match_and_execute("游戏口令", enforce_wake=True)["matched"]
    assert not service._execute_command_action(stale)["matched"]
    assert not calls
    new = {"voice": {"game.profile_slot_01": {"disabled": True},
                     "game.profile_slot_13": binding("游戏口令")}}
    service.check_profile_phrases(new)
    service.configure_profile_bindings(new)
    assert service._match_and_execute("游戏口令", enforce_wake=True)["matched"]
    assert calls[-1]["command_id"] == "game.profile_slot_13"


def test_dynamic_slots_use_all_modes_grammar_without_rebuilding_on_switch(voice, monkeypatch):
    service, calls = voice
    walking = {"voice": {"game.profile_slot_13": binding("步行口令")}}
    driving = {"voice": {"game.profile_slot_14": binding("驾驶口令")}}
    service.configure_profile_bindings(walking, [walking, driving])
    grammar = service.grammar_phrases()
    assert {"步行口令", "驾驶口令"} <= set(grammar)
    rebuilds = []
    monkeypatch.setattr(service, "_rebuild_recognizer", lambda: rebuilds.append(True))
    service.switch_profile_bindings(driving, mode_bindings=[walking, driving])
    assert service.grammar_phrases() == grammar
    assert not rebuilds
    assert not service._match_and_execute("步行口令", enforce_wake=True)["matched"]
    assert service._match_and_execute("驾驶口令", enforce_wake=True)["matched"]
    assert calls[-1]["command_id"] == "game.profile_slot_14"


@pytest.mark.parametrize("ident", ["game.profile_slot_00", "game.profile_slot_65", "game.profile_slot_013"])
def test_slot_limits_apply_to_bindings_overrides_and_disabled_entries(ident):
    with pytest.raises(ValueError, match="口令编号"):
        normalize_bindings({"voice": {ident: binding("越界口令")}})
    for value in [None, binding("越界口令")]:
        with pytest.raises(ValueError, match="口令编号"):
            normalize_overrides({f"voice.{ident}": value})
        with pytest.raises(ValueError, match="口令编号"):
            _merge_bindings({}, {f"voice.{ident}": value})


@pytest.mark.parametrize("number", [1, 9])
def test_historical_unpadded_slot_keeps_id_and_replaces_default_catalog_entry(voice, number):
    service, calls = voice
    ident = f"game.profile_slot_{number}"
    canonical_id = f"game.profile_slot_{number:02d}"
    current = {"voice": {ident: binding("历史口令")}}
    assert normalize_bindings(current)["voice"][ident] == binding("历史口令")
    assert normalize_overrides({f"voice.{ident}": binding("历史口令")})[f"voice.{ident}"] == binding("历史口令")
    assert _merge_bindings({}, {f"voice.{ident}": None})["voice"][ident]["disabled"]
    service.check_profile_phrases(current)
    service.configure_profile_bindings(current)
    ids = {item["id"] for item in service.command_registry.values()}
    assert ident in ids
    assert canonical_id not in ids
    assert service._match_and_execute("历史口令", enforce_wake=True)["matched"]
    assert calls[-1]["command_id"] == ident
    service.configure_profile_bindings({"voice": {ident: {"disabled": True}}})
    assert not ({ident, canonical_id} & {item["id"] for item in service.command_registry.values()})


def test_explicit_canonical_slot_wins_over_historical_alias_without_phrase_conflict(voice):
    service, calls = voice
    current = {"voice": {ident: binding("同一句口令")
                         for ident in ["game.profile_slot_1", "game.profile_slot_01"]}}
    service.check_profile_phrases(current)
    service.configure_profile_bindings(current)
    assert service._match_and_execute("同一句口令", enforce_wake=True)["matched"]
    assert calls[-1]["command_id"] == "game.profile_slot_01"
    assert "game.profile_slot_1" not in {item["id"] for item in service.command_registry.values()}


def test_dynamic_slot_persists_through_existing_api_and_disabled_slot_is_restorable_in_catalog(application, monkeypatch):
    app = application
    monkeypatch.setattr(app.VOICE, "_rebuild_recognizer", lambda: None)
    overrides = {"voice.game.profile_slot_01": None, "voice.game.profile_slot_13": binding("新增游戏口令")}
    replies = []
    request = request_for(app, "/api/game-profiles/overrides", replies)
    request._body = lambda: {"profile_id": app.PROFILES.selected_id(), "overrides": overrides}
    request.do_POST()
    assert replies[-1][0] == 200
    restored = GameProfileStore(app.ROOT)
    assert restored.effective_profile()["bindings"]["voice"]["game.profile_slot_13"] == binding("新增游戏口令")
    catalog = {item["id"]: item for item in app.voice_command_catalog()["commands"]}
    assert catalog["game.profile_slot_13"]["phrase"] == "新增游戏口令"
    assert catalog["game.profile_slot_13"]["effective_action"]["target"] == "V"
    assert catalog["game.profile_slot_01"]["effective_action"] is None
    assert "game.profile_slot_01" not in {item["id"] for item in app.VOICE.command_registry.values()}
    noted = []
    monkeypatch.setattr(app.KERNEL, "note_trigger", lambda *args, **kwargs: noted.append(args))
    monkeypatch.setattr(app.OUTPUT, "execute_voice_action", lambda *args: pytest.fail("已删除口令不应输出"))
    assert not app.execute_voice_action({"command_id": "game.profile_slot_01", "type": "keyboard", "target": "F1"})["executed"]
    assert not noted
