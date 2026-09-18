import json
from pathlib import Path

import pytest

from motioncontrol.game_profiles import GameProfileStore
from motioncontrol_shared.profile_schema import normalize_action, flatten_bindings


def make_store(tmp_path: Path):
    profiles = tmp_path / "game_profiles" / "profiles"
    profiles.mkdir(parents=True)
    (tmp_path / "config").mkdir()
    catalog = {
        "schema": "motioncontrol.game_catalog.v1",
        "games": [
            {"id": "generic-xbox", "name": "通用 Xbox / 默认", "profile": "profiles/generic-xbox.json"},
            {"id": "demo", "name": "Demo Game", "appid": 123, "profile": "profiles/demo.json"},
        ],
    }
    base = {
        "schema": "motioncontrol.game_profile.v1",
        "name": "通用 Xbox / 默认",
        "bindings": {"zones": {"rightHandLower": {"action": {"type": "gamepad", "target": "A"}}}},
    }
    demo = {
        "schema": "motioncontrol.game_profile.v1",
        "name": "Demo Game",
        "appid": 123,
        "bindings": {
            "zones": {"rightHandLower": {"action": {"type": "keyboard", "target": "SPACE"}}},
            "poses": {"hands_cross": {"action": {"type": "keyboard", "target": "ESC", "behavior": "hold"}}},
        },
    }
    (tmp_path / "game_profiles" / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    (profiles / "generic-xbox.json").write_text(json.dumps(base), encoding="utf-8")
    (profiles / "demo.json").write_text(json.dumps(demo), encoding="utf-8")
    return GameProfileStore(tmp_path)


def test_catalog_search_and_lazy_selected_profile(tmp_path):
    store = make_store(tmp_path)
    result = store.list_games("123")
    assert result["count"] == 1 and result["games"][0]["id"] == "demo"
    profile = store.select("demo")
    assert profile["name"] == "Demo Game"
    assert profile["bindings"]["zones"]["rightHandLower"]["action"]["target"] == "SPACE"


def test_overrides_can_change_or_explicitly_disable_trigger(tmp_path):
    store = make_store(tmp_path)
    store.select("demo")
    profile = store.set_overrides({
        "zone.rightHandLower": {"action": {"type": "mouse_button", "target": "LEFT"}},
        "pose.hands_cross": None,
    })
    assert profile["bindings"]["zones"]["rightHandLower"]["action"]["type"] == "mouse_button"
    assert profile["bindings"]["poses"]["hands_cross"] == {"disabled": True}


def test_wheel_is_forced_to_tap_but_a_pose_only_defaults_to_it():
    """A wheel has no held state; a pose does.

    Holding a wheel would scroll forever, so that rewrite stays.  A pose was
    rewritten the same way, which made "hold this while the pose lasts"
    impossible to express even though the recognizer releases it like any
    motion.  Poses now default to a single edge trigger and can opt into hold.
    """
    assert normalize_action({"type": "mouse_wheel", "target": "scroll_up", "behavior": "hold"})["behavior"] == "tap"
    default = flatten_bindings({"poses": {"hands_cross": {"action": {"type": "keyboard", "target": "ESC"}}}})
    assert default["pose.hands_cross"]["action"]["behavior"] == "tap"
    held = flatten_bindings({"poses": {"hands_cross": {"action": {"type": "keyboard", "target": "ESC", "behavior": "hold"}}}})
    assert held["pose.hands_cross"]["action"]["behavior"] == "hold"


def test_invalid_profile_action_is_rejected(tmp_path):
    store = make_store(tmp_path)
    store.select("demo")
    with pytest.raises(ValueError):
        store.set_overrides({"zone.rightHandLower": {"action": {"type": "mouse_wheel", "target": "SIDEWAYS"}}})


def test_unmapped_pose_and_voice_survive_binding_normalization():
    from motioncontrol_shared.profile_schema import flatten_bindings
    result = flatten_bindings({'poses':{'hands_cross':{'disabled':True}}, 'voice':{'jump':{'disabled':True}}, 'motions':{'march':{'action':{'type':'gamepad','target':'B'}}}})
    assert result['pose.hands_cross'] == {'disabled':True}
    assert result['voice.jump'] == {'disabled':True}
    assert result['motion.march']['action']['target'] == 'B'
