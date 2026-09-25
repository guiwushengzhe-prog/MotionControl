import threading
import time

from motioncontrol_shared.profile_schema import normalize_action
from test_game_profiles_v097 import make_store
from test_output_actions_v097 import manager


def test_combo_lead_is_saved_per_game_and_old_action_defaults(tmp_path):
    store = make_store(tmp_path)
    combo = {
        "zone.rightHandLower": {
            "action": {
                "type": "gamepad",
                "target": "LB+LS_UP",
                "combo_stick_lead_ms": 135,
            }
        }
    }
    saved = store.set_overrides(combo, "generic-xbox")
    assert saved["bindings"]["zones"]["rightHandLower"]["action"]["combo_stick_lead_ms"] == 135

    restarted = type(store)(tmp_path)
    assert restarted.effective_profile()["bindings"]["zones"]["rightHandLower"]["action"]["combo_stick_lead_ms"] == 135
    # Profiles written before this field existed continue to use the output
    # backend's 80 ms default and do not gain a spurious serialized key.
    assert "combo_stick_lead_ms" not in restarted.select("demo")["bindings"]["zones"]["rightHandLower"]["action"]


def test_combo_lead_is_bounded_and_accepts_legacy_alias():
    assert normalize_action({"type": "gamepad", "target": "LB+LS_UP", "lead_ms": -5})["combo_stick_lead_ms"] == 0
    assert normalize_action({"type": "gamepad", "target": "LB+LS_UP", "combo_stick_lead_ms": 999})["combo_stick_lead_ms"] == 200


def test_continuous_combo_delays_stick_by_profile_value(tmp_path):
    out, _mouse, _keyboard, pad = manager(tmp_path)
    try:
        out.set_action_holds([{
            "id": "zone.climb",
            "action": {
                "type": "gamepad",
                "target": ["LB", "LS_UP"],
                "combo_stick_lead_ms": 120,
            },
        }])
        assert pad.buttons == ("LB",)
        assert pad.left_stick == (0.0, 0.0)
        time.sleep(0.05)
        assert pad.left_stick == (0.0, 0.0)
        deadline = time.monotonic() + 0.4
        while pad.left_stick == (0.0, 0.0) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pad.left_stick == (0.0, 1.0)
    finally:
        out.close()


def test_tap_combo_uses_the_same_lead_before_release(tmp_path):
    out, _mouse, _keyboard, pad = manager(tmp_path)
    worker = threading.Thread(target=out.execute_action, kwargs={
        "action": {
            "type": "gamepad",
            "target": ["RB", "LS_RIGHT"],
            "combo_stick_lead_ms": 110,
            "duration": 0.04,
        },
    })
    try:
        worker.start()
        deadline = time.monotonic() + 0.2
        while pad.buttons != ("RB",) and time.monotonic() < deadline:
            time.sleep(0.005)
        assert pad.buttons == ("RB",)
        assert pad.left_stick == (0.0, 0.0)
        deadline = time.monotonic() + 0.3
        while pad.left_stick == (0.0, 0.0) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pad.left_stick == (1.0, 0.0)
        worker.join(0.5)
        assert not worker.is_alive()
    finally:
        if worker.is_alive():
            worker.join(0.5)
        out.close()


def test_replacing_combo_during_lead_does_not_apply_stale_stick(tmp_path):
    out, _mouse, _keyboard, pad = manager(tmp_path)
    first = [{
        "id": "pose.climb",
        "action": {"type": "gamepad", "target": ["LB", "LS_UP"], "combo_stick_lead_ms": 120},
    }]
    second = [{
        "id": "pose.climb",
        "action": {"type": "gamepad", "target": ["LB", "LS_LEFT"], "combo_stick_lead_ms": 120},
    }]
    try:
        out.set_action_holds(first)
        assert pad.left_stick == (0.0, 0.0)
        time.sleep(0.04)
        out.set_action_holds(second)
        assert pad.left_stick == (0.0, 0.0)
        deadline = time.monotonic() + 0.35
        while pad.left_stick == (0.0, 0.0) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pad.left_stick == (-1.0, 0.0)
    finally:
        out.set_action_holds([])
        out.close()


def test_nonblocking_default_lead_releases_after_stick_arrives(tmp_path):
    out, _mouse, _keyboard, pad = manager(tmp_path)
    try:
        out.execute_action({
            "type": "gamepad",
            "target": ["LB", "LS_UP"],
            "duration": 0.01,
            "nonblocking": True,
        })
        assert pad.buttons == ("LB",)
        assert pad.left_stick == (0.0, 0.0)
        deadline = time.monotonic() + 0.18
        while pad.left_stick == (0.0, 0.0) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pad.left_stick == (0.0, 1.0)
        deadline = time.monotonic() + 0.25
        while pad.buttons and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pad.buttons == ()
        assert pad.left_stick == (0.0, 0.0)
    finally:
        out.close()
