"""每个游戏默认带着走路、后退两个动作，其余动作默认不绑。

用手柄的游戏：原地踏步→左摇杆上、小腿向后抬起→左摇杆下。
一个手柄输出都没有的键鼠游戏：原地踏步→W、小腿向后抬起→S。
"""

from __future__ import annotations

import pathlib

from motioncontrol_shared.catalog import load_catalog, load_profile
from motioncontrol_shared.profile_schema import _merge_bindings, with_default_bindings

REPO = pathlib.Path(__file__).resolve().parent.parent
LIBRARY = REPO / "game_profiles"

STICK = {
    "march": {"action": {"type": "gamepad_axis", "target": "LS_UP", "behavior": "hold"}},
    "calf_back": {"action": {"type": "gamepad_axis", "target": "LS_DOWN", "behavior": "hold"}},
}
KEYS = {
    "march": {"action": {"type": "keyboard", "target": "W", "behavior": "hold"}},
    "calf_back": {"action": {"type": "keyboard", "target": "S", "behavior": "hold"}},
}
PAD_ZONE = {"zones": {"leftFoot": {"action": {"type": "gamepad", "target": "LB", "behavior": "hold"}}}}
KEY_ZONE = {"zones": {"leftFoot": {"action": {"type": "keyboard", "target": "Q", "behavior": "hold"}}}}


def test_every_shipped_profile_walks_and_backs_up():
    catalog = load_catalog(LIBRARY)
    seen = {"stick": 0, "keys": 0}
    for entry in catalog["games"]:
        motions = load_profile(LIBRARY, entry["id"], catalog)["bindings"]["motions"]
        # 其余动作不预先绑：要用的到动作库里自己挑。
        assert set(motions) == {"march", "calf_back"}, entry["id"]
        assert motions in (STICK, KEYS), entry["id"]
        seen["stick" if motions == STICK else "keys"] += 1
    # 两种都真的出现了，不是规则悄悄失效、全落到同一边。
    assert seen["stick"] and seen["keys"], seen


def test_the_generic_profile_uses_the_stick():
    assert load_profile(LIBRARY, "generic-xbox")["bindings"]["motions"] == STICK


def test_a_keyboard_only_profile_uses_w_and_s():
    assert with_default_bindings(KEY_ZONE)["motions"] == KEYS


def test_any_gamepad_output_means_the_stick():
    mixed = {**KEY_ZONE, "poses": {"hands_cross": {"action": {"type": "gamepad", "target": "START", "behavior": "tap"}}}}
    assert with_default_bindings(mixed)["motions"] == STICK


def test_a_profile_that_names_the_motion_keeps_its_own_choice():
    bindings = {**PAD_ZONE, "motions": {
        "march": {"action": {"type": "keyboard", "target": "UP", "behavior": "hold"}},
        "calf_back": {"disabled": True},
    }}
    motions = with_default_bindings(bindings)["motions"]
    assert motions["march"]["action"]["target"] == "UP"
    assert motions["calf_back"] == {"disabled": True}


def test_the_player_can_still_unbind_a_default():
    merged = _merge_bindings(with_default_bindings(PAD_ZONE), {"motion.march": None})
    assert merged["motions"]["march"] == {"disabled": True}
    assert merged["motions"]["calf_back"] == STICK["calf_back"]


def test_defaults_do_not_leak_between_profiles():
    first = with_default_bindings(PAD_ZONE)
    first["motions"]["march"]["action"]["target"] = "LS_LEFT"
    assert with_default_bindings(PAD_ZONE)["motions"]["march"] == STICK["march"]
    # 传进来的那份也不能被改。
    assert "motions" not in PAD_ZONE
