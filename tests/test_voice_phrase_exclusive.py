"""一句口令只能有一个主人：内置口令、本游戏口令、通用口令不能同名。

以前可以同名，识别时内置的先匹配，于是通用口令里和它同名的那几条从来没生效过，
界面上却看着能改——表上写闪避是空格，实际按的是 Shift。
"""

import json
import shutil
from pathlib import Path

import pytest

from motioncontrol.user_paths import user_path
from motioncontrol.voice_backend import VoiceService

ROOT = Path(__file__).resolve().parents[1]

JUMP_SLOT = {"voice": {"game.profile_slot_01": {
    "phrase": "跳跃", "action": {"type": "keyboard", "target": "SPACE", "behavior": "tap"}}}}


def make_service(tmp_path, calls=None):
    generated = tmp_path / "config" / "generated_voice"
    generated.mkdir(parents=True)
    shutil.copy(ROOT / "config" / "generated_voice" / "voice_action_map.json", generated)
    sink = calls if calls is not None else []
    return VoiceService(tmp_path, lambda action: sink.append(action) or {"executed": True})


def shared(phrase, target="M"):
    return {"phrase": phrase, "type": "keyboard", "target": target, "behavior": "tap"}


def test_built_in_commands_are_only_system_functions_and_the_game_slots():
    catalog = json.loads((ROOT / "config" / "voice_commands_v094.json").read_text(encoding="utf-8"))
    for command in catalog["commands"]:
        assert command["kind"] == "system" or command["id"].startswith("game.profile_slot_"), command["id"]


def test_a_shared_phrase_cannot_take_a_built_in_one(tmp_path):
    voice = make_service(tmp_path)
    voice.configure([shared("地图")])
    with pytest.raises(ValueError, match="内置口令"):
        voice.configure([shared("地图"), shared("挪动区域", "F12")])
    assert [item["phrase"] for item in voice.mappings] == ["地图"], "被拒的那次不该留下任何改动"


def test_a_shared_phrase_cannot_take_an_emergency_one(tmp_path):
    voice = make_service(tmp_path)
    with pytest.raises(ValueError, match="急停口令"):
        voice.configure([shared("紧急停止")])


def test_a_game_phrase_cannot_take_a_shared_one(tmp_path):
    voice = make_service(tmp_path)
    voice.configure([shared("跳跃", "SPACE")])
    with pytest.raises(ValueError, match="通用口令"):
        voice.check_profile_phrases(JUMP_SLOT)


def test_two_game_phrases_cannot_be_the_same(tmp_path):
    voice = make_service(tmp_path)
    both = {"voice": {**JUMP_SLOT["voice"], "game.profile_slot_02": {
        "phrase": "跳跃", "action": {"type": "keyboard", "target": "J", "behavior": "tap"}}}}
    with pytest.raises(ValueError, match="两条本游戏口令"):
        voice.check_profile_phrases(both)


def test_a_clash_that_arrives_by_switching_games_is_reported(tmp_path):
    """存的时候拦得住，换游戏拦不住——那就照实告诉界面。"""
    voice = make_service(tmp_path)
    voice.configure([shared("跳跃", "SPACE")])
    voice.configure_profile_bindings(JUMP_SLOT)
    assert any("跳跃" in text for text in voice.status()["phrase_conflicts"])


def test_saving_shared_phrases_keeps_the_game_phrases(tmp_path):
    """存一次通用口令，本游戏口令仍保持自己的免唤醒短语。"""
    voice = make_service(tmp_path)
    voice.configure_profile_bindings(JUMP_SLOT)
    voice.configure([shared("地图")])
    assert "跳跃" in voice.grammar_phrases()


def test_switching_games_does_not_carry_a_phrase_over(tmp_path):
    voice = make_service(tmp_path)
    voice.configure_profile_bindings(JUMP_SLOT)
    voice.configure_profile_bindings({})
    assert "跳跃" not in voice.grammar_phrases()
    assert "功能一" in voice.grammar_phrases()
    assert "体感功能一" in voice.grammar_phrases()  # 旧版本说法继续兼容


def test_a_shared_phrase_that_used_to_be_shadowed_now_works(tmp_path):
    """「体感地图」以前被内置口令截走，通用口令里改成什么键都没用。"""
    calls = []
    voice = make_service(tmp_path, calls)
    voice.configure([shared("地图", "TAB")])
    result = voice._match_and_execute("体感地图", enforce_wake=True)
    assert result["matched"] is True
    assert calls[-1]["target"] == "TAB"


def test_old_shared_phrases_that_never_worked_are_dropped(tmp_path):
    path = user_path("voice_mappings")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mappings": [
        {"phrase": "挪动区域", "type": "system", "target": "ZONES.MOVE_HERE", "behavior": "tap"},
        shared("地图"),
    ]}, ensure_ascii=False), encoding="utf-8")
    voice = make_service(tmp_path)
    assert [item["phrase"] for item in voice.mappings] == ["地图"]
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert [item["phrase"] for item in saved["mappings"]] == ["地图"], "去掉之后要落盘，否则每次启动都在丢"


def test_an_extra_stop_phrase_stops_and_cannot_double_as_a_shared_one(tmp_path):
    """界面上多加的急停就是通用口令表里的一行，存的时候还是存进急停口令那一份。"""
    stops = []
    voice = make_service(tmp_path)
    voice.emergency_stop = lambda: stops.append(True) or {"executed": True}
    voice.configure([], emergency_stop_phrases=["体感停下"])
    assert voice._match_and_execute("体感停下", enforce_wake=True)["emergency"] is True
    assert stops, "多加的急停没走急停那条路"
    with pytest.raises(ValueError, match="急停口令"):
        voice.configure([shared("停下")], emergency_stop_phrases=["体感停下"])


def test_a_shared_phrase_still_pointing_at_the_removed_reference_scene_moves_the_zones(tmp_path):
    """参考场景删了。原来绑着「记录参考场景」的通用口令改做「区域挪到我这里」，不整份退回。"""
    path = user_path("voice_mappings")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mappings": [
        {"phrase": "对位", "type": "system", "target": "SCENE.REMATCH", "behavior": "tap"},
        shared("地图"),
    ]}, ensure_ascii=False), encoding="utf-8")
    voice = make_service(tmp_path)
    assert [(item["phrase"], item["target"]) for item in voice.mappings] == [
        ("对位", "ZONES.MOVE_HERE"), ("地图", "M")]
