"""口令按模型词表拆词：能按单字就按单字，单字查不到再拼整词，拼不上的字照实报。

小模型的词表里单字不全（「堡」只在「城堡」里），写了查不到的字，Vosk 只是悄悄丢掉，
那句口令说多少遍都听不到。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from motioncontrol.voice_backend import VoskCommandRecognizer, find_vosk_model, grammar_tokens

ROOT = Path(__file__).resolve().parent.parent

# 假词表：单字「堡」「啡」「笛」不在，整词「城堡」「咖啡」「体感」在。
VOCAB = set("体感爬绳城咖地图向左走吹") | {"城堡", "咖啡", "体感", "ok"}


def known(word: str) -> bool:
    return word in VOCAB


def test_phrases_whose_characters_are_all_known_split_into_single_characters_as_before():
    # 「体感」本身也是整词，但单字都在就按单字拆：和 2026-08-28 验过的拆法一样。
    assert grammar_tokens("体感爬绳", known) == (["体", "感", "爬", "绳"], [])


def test_a_character_only_known_inside_a_word_is_kept_as_that_word():
    assert grammar_tokens("体感城堡", known) == (["体", "感", "城堡"], [])
    assert grammar_tokens("体感咖啡", known) == (["体", "感", "咖啡"], [])


def test_a_character_no_word_can_cover_is_reported():
    _tokens, unheard = grammar_tokens("体感吹笛", known)
    assert unheard == ["笛"]


def test_each_unheard_character_is_reported_once():
    _tokens, unheard = grammar_tokens("笛笛吹笛", known)
    assert unheard == ["笛"]


def test_punctuation_and_spaces_are_folded_away_like_the_parser_does():
    assert grammar_tokens("向左，走", known) == (["向", "左", "走"], [])
    assert grammar_tokens(" 体感 地图 ", known) == (["体", "感", "地", "图"], [])


def test_letters_and_digits_the_model_does_not_know_are_reported():
    assert grammar_tokens("OK", known) == (["ok"], [])
    assert grammar_tokens("体感F9", known)[1] == ["f", "9"]
    assert grammar_tokens("jump", known) == ([], ["jump"])


def test_empty_phrase_has_no_tokens():
    assert grammar_tokens(" ，", known) == ([], [])


MODEL = find_vosk_model(ROOT)


@pytest.mark.skipif(MODEL is None, reason="没有 Vosk 中文模型")
def test_the_real_model_hears_climbing_rope_and_castle_but_not_flute():
    pytest.importorskip("vosk")
    recognizer = VoskCommandRecognizer(MODEL, ["体感爬绳", "体感城堡", "体感吹笛"])
    try:
        assert "体 感 爬 绳" in recognizer.supported
        assert "体 感 城堡" in recognizer.supported
        # 认不出的口令不进 grammar，免得 Vosk 丢掉一个字后拿剩下的半句去匹配。
        assert not any("吹" in entry for entry in recognizer.supported)
        assert recognizer.unheard == {"体感吹笛": ["笛"]}
        assert recognizer.unsupported == ["体感吹笛"]
    finally:
        recognizer.close()


@pytest.mark.skipif(MODEL is None, reason="没有 Vosk 中文模型")
def test_the_voice_service_says_which_phrases_it_can_never_hear(tmp_path, monkeypatch):
    pytest.importorskip("vosk")
    import shutil

    from motioncontrol.voice_backend import VoiceService

    generated = tmp_path / "config" / "generated_voice"
    generated.mkdir(parents=True)
    shutil.copy(ROOT / "config" / "generated_voice" / "voice_action_map.json", generated)
    monkeypatch.setenv("VOSK_MODEL_PATH", str(MODEL))
    service = VoiceService(tmp_path, lambda action: {"executed": True})
    service.configure([
        {"phrase": "吹笛", "type": "keyboard", "target": "M", "behavior": "tap"},
        {"phrase": "城堡", "type": "keyboard", "target": "N", "behavior": "tap"},
    ])
    # 设置页那一行汇总读的就是这个。
    assert {"phrase": "体感吹笛", "chars": ["笛"]} in service.status()["unheard"]
    # 发给手机的 grammar 和电脑自己用的是同一份，已经拆好词。
    assert "体 感 城堡" in service.grammar_entries()
    assert not any("吹" in entry for entry in service.grammar_entries())
    # 输入框底下的提示问的是这个。
    assert service.check_phrases(["体感爬绳", "体感吹笛"]) == {"available": True, "results": [
        {"phrase": "体感爬绳", "unheard": []},
        {"phrase": "体感吹笛", "unheard": ["笛"]},
    ]}


def test_without_a_model_the_check_says_it_cannot_tell(tmp_path, monkeypatch):
    from motioncontrol.voice_backend import VoiceService

    monkeypatch.delenv("VOSK_MODEL_PATH", raising=False)
    service = VoiceService(tmp_path, lambda action: {"executed": True})
    assert service.check_phrases(["体感吹笛"]) == {"available": False, "results": []}
    assert service.grammar_entries() == []
