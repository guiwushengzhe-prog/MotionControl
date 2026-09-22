"""The two mapping documents, and the vocabularies the cloud shares with the desktop.

The failure these guard against is specific and quiet: the cloud accepts an
upload the desktop will later refuse, so a user downloads a config that installs
cleanly and then does not work. That can only happen if the two sides disagree
about what is valid, so the tests below assert they cannot.
"""

from __future__ import annotations

import json
import sys

import pytest

from motioncontrol_shared.canonical import _NORMALIZERS, canonicalize
from motioncontrol_shared.mapping_schema import (
    DEFAULT_EMERGENCY_STOP,
    normalize_emergency_phrases,
    normalize_key_combo,
    normalize_motion_item,
    normalize_voice_mappings,
)
from motioncontrol_shared.profile_versions import (
    MOTION_MAPPINGS_SCHEMA,
    VOICE_MAPPINGS_SCHEMA,
)
from motioncontrol_shared.sync_allowlist import CLOUD_SYNC_ALLOWLIST

windows_only = pytest.mark.skipif(
    sys.platform != "win32",
    reason="output_backend imports ctypes.wintypes",
)


def test_every_syncable_file_has_a_canonical_form():
    """A new file on the whitelist with no normaliser would reach the hash raw.

    只要求包含，不要求相等。两者曾经是一一对应的，直到 game_bundle 出现：它是
    一种**文档**，不是一个文件——一个游戏的按键绑定加身体动作，由两个文件拼出来。
    危险的只有一个方向：白名单上有文件、而没有对应的规范化函数。
    """
    assert CLOUD_SYNC_ALLOWLIST <= set(_NORMALIZERS)


def test_every_document_type_the_cloud_accepts_has_a_normaliser():
    """云端收下一种它不会规范化的文档，等于把未校验的内容直接拿去算哈希。"""
    import typing

    from cloud.app.schemas import DocType

    assert set(typing.get_args(DocType)) == set(_NORMALIZERS)


@windows_only
def test_shared_vocabulary_matches_the_desktop():
    """Adding a key to the desktop without mirroring it here must fail loudly.

    The cloud validates keyboard and gamepad targets from the shared tables; if
    they drift from the ones output_backend actually drives, the cloud starts
    either rejecting valid configs or accepting unplayable ones.
    """
    from motioncontrol.output_backend import KEY_CODES, XUSB_GAMEPAD_BUTTONS, KeyboardOutput

    from motioncontrol_shared.profile_schema import GAMEPAD_BUTTONS, KEYBOARD_KEYS

    assert set(KEY_CODES) == KEYBOARD_KEYS
    assert set(XUSB_GAMEPAD_BUTTONS) == GAMEPAD_BUTTONS
    # normalize_key_combo has to fold names the same way, alias for alias.
    for key in sorted(KEYBOARD_KEYS):
        assert normalize_key_combo(key) == KeyboardOutput.normalize(key)
    for spelling in ("control", "windows", "return", "del"):
        assert normalize_key_combo(spelling) == KeyboardOutput.normalize(spelling)


@windows_only
def test_voice_rules_are_the_desktop_rules():
    """voice_backend must delegate rather than keep its own copy."""
    from motioncontrol_shared import mapping_schema
    from motioncontrol.voice_backend import VoiceService

    assert VoiceService._validate_mappings is mapping_schema.normalize_voice_mappings
    assert VoiceService._validate_wake_word is mapping_schema.normalize_wake_word
    assert (VoiceService._validate_emergency_phrases
            is mapping_schema.normalize_emergency_phrases)


# --- voice -------------------------------------------------------------------

def _voice(**overrides):
    doc = {"wake_word": "体感", "emergency_stop_phrases": [], "mappings": []}
    doc.update(overrides)
    return doc


def test_voice_mapping_order_is_preserved():
    """First match wins at recognition time, so order is meaning, not noise."""
    phrases = ["开始", "停止", "跳跃"]
    doc = _voice(mappings=[{"phrase": p, "type": "keyboard", "target": "A"} for p in phrases])
    result = canonicalize("voice_mappings", doc).data
    assert [item["phrase"] for item in result["mappings"]] == phrases


def test_voice_reinstates_the_emergency_stop_phrase():
    """内置的那句急停不是可选项：输出卡住时它是唯一的出口。"""
    assert DEFAULT_EMERGENCY_STOP in normalize_emergency_phrases(["随便"])


def test_a_shared_voice_config_carries_neither_the_wake_word_nor_the_stop_phrases():
    """分享语音配置分的是"说什么话按什么键"，不是把自己的唤醒词装到别人机器上。

    以前它们跟着一起走，于是装一份别人的配置会把自己的唤醒词换掉——装的人
    只会发现"我的唤醒词自己变了"，想不到是装配置装的。
    """
    result = canonicalize("voice_mappings", _voice(
        wake_word="别人的唤醒词", emergency_stop_phrases=["随便"])).data
    assert "wake_word" not in result
    assert "emergency_stop_phrases" not in result


def test_an_old_document_that_still_carries_them_is_accepted():
    """已经传上去的配置里还带着这两项。报错会把它们变成无法重传的废文件，
    所以是收下但丢掉，不是拒绝。"""
    result = canonicalize("voice_mappings", _voice(
        wake_word="体感", emergency_stop_phrases=["紧急停止"],
        mappings=[{"phrase": "开始", "type": "keyboard", "target": "A"}])).data
    assert [item["phrase"] for item in result["mappings"]] == ["开始"]


def test_voice_rejects_a_duplicate_phrase_under_punctuation_folding():
    doc = _voice(mappings=[
        {"phrase": "开始", "type": "keyboard", "target": "A"},
        {"phrase": "开 始。", "type": "keyboard", "target": "B"},
    ])
    with pytest.raises(ValueError, match="命令词重复"):
        canonicalize("voice_mappings", doc)


@pytest.mark.parametrize("mapping, message", [
    ({"phrase": "x", "type": "gamepad", "target": "Z"}, "暂不支持的 Xbox 键"),
    ({"phrase": "x", "type": "system", "target": "OUTPUT.NUKE"}, "暂不支持的系统命令"),
    ({"phrase": "x", "type": "wat", "target": "A"}, "未知输出类型"),
    ({"phrase": "x", "type": "keyboard", "target": "ENTER+NOPE"}, "不支持的键盘键"),
    ({"phrase": "x", "type": "system", "target": "OUTPUT.START", "behavior": "hold"},
     "系统命令只能点按"),
])
def test_voice_rejections_carry_the_desktop_wording(mapping, message):
    """The user reads these, so they are part of the contract, not debug text."""
    with pytest.raises(ValueError, match=message):
        normalize_voice_mappings([mapping])


# --- motions -----------------------------------------------------------------

def test_motions_are_sorted_so_reordering_does_not_create_a_version():
    items = [{"id": ident, "name": ident, "enabled": False,
              "type": "gamepad", "target": "A"} for ident in ("squat", "march", "hands_up")]
    forward = canonicalize("motion_mappings", {"motions": items})
    backward = canonicalize("motion_mappings", {"motions": list(reversed(items))})
    assert forward.sha256 == backward.sha256
    assert [m["id"] for m in forward.data["motions"]] == ["hands_up", "march", "squat"]


def test_motion_conflict_rule_applies_on_upload():
    """A jumping jack contains the hands-overhead pose; mapping both double-fires."""
    doc = {"motions": [
        {"id": "jumping_jack", "name": "开合跳", "enabled": True, "type": "gamepad", "target": "A"},
        {"id": "hands_up", "name": "双手举过头顶", "enabled": True, "type": "gamepad", "target": "Y"},
    ]}
    with pytest.raises(ValueError, match="不能同时映射"):
        canonicalize("motion_mappings", doc)


def test_motion_enabled_without_an_output_is_rejected():
    with pytest.raises(ValueError, match="已启用但没有设置输出"):
        normalize_motion_item({"id": "squat", "name": "下蹲", "enabled": True,
                               "type": "gamepad", "target": ""})


def test_disabled_motion_may_have_no_output():
    item = normalize_motion_item({"id": "squat", "name": "下蹲", "enabled": False,
                                  "type": "gamepad", "target": ""})
    assert item == {"id": "squat", "name": "下蹲", "enabled": False,
                    "type": "gamepad", "target": ""}


def test_duplicate_motion_ids_are_rejected():
    doc = {"motions": [
        {"id": "squat", "name": "a", "enabled": False, "type": "gamepad", "target": "A"},
        {"id": "squat", "name": "b", "enabled": False, "type": "gamepad", "target": "B"},
    ]}
    with pytest.raises(ValueError, match="动作 id 重复"):
        canonicalize("motion_mappings", doc)


# --- schema handling ---------------------------------------------------------

@pytest.mark.parametrize("doc_type, doc, schema", [
    ("motion_mappings", {"motions": []}, MOTION_MAPPINGS_SCHEMA),
    ("voice_mappings", {"mappings": []}, VOICE_MAPPINGS_SCHEMA),
])
def test_a_legacy_file_without_a_schema_is_accepted_and_stamped(doc_type, doc, schema):
    """Every installed copy predates the schema field; refusing them is not an option."""
    assert canonicalize(doc_type, doc).data["schema"] == schema


@pytest.mark.parametrize("doc_type, doc", [
    ("motion_mappings", {"schema": "motioncontrol.motion_mappings.v9", "motions": []}),
    ("voice_mappings", {"schema": "motioncontrol.voice_mappings.v9", "mappings": []}),
])
def test_an_unknown_schema_is_refused_rather_than_guessed_at(doc_type, doc):
    with pytest.raises(ValueError, match="unsupported"):
        canonicalize(doc_type, doc)


@pytest.mark.parametrize("doc_type, doc", [
    ("motion_mappings", {"motions": [{"id": "squat", "name": "下蹲", "enabled": True,
                                      "type": "gamepad", "target": "X"}]}),
    ("voice_mappings", {"wake_word": "体感", "mappings": [
        {"phrase": "开始", "type": "keyboard", "target": "ENTER", "synonyms": ["启动"]}]}),
])
def test_canonical_bytes_are_stable_and_platform_neutral(doc_type, doc):
    once = canonicalize(doc_type, doc)
    twice = canonicalize(doc_type, json.loads(once.payload.decode("utf-8")))
    assert once.sha256 == twice.sha256
    assert once.payload == twice.payload
    assert b"\r" not in once.payload
    assert once.payload.endswith(b"\n")
