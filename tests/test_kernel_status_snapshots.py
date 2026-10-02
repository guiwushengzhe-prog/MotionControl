"""Snapshot optimization must preserve detached data and live metadata."""
from __future__ import annotations

from collections import defaultdict
import copy

import pytest

from motioncontrol.control_kernel import ControlKernel, _snapshot_copy
from motioncontrol_shared import pose_library
from test_control_kernel import FakeOutput, pose_map


@pytest.fixture()
def kernel():
    made = ControlKernel(FakeOutput(), persist=False)
    made._stop.set()
    made._thread.join(timeout=1.0)
    yield made
    made.close()


def test_snapshot_copy_preserves_shared_references_and_cycles():
    shared = {"values": [1, {"nested": "value"}]}
    source = {"left": shared, "right": shared}
    source["cycle"] = source
    shared["values"].append(source)
    result = _snapshot_copy(source)
    assert result is not source
    assert result["left"] is result["right"]
    assert result["cycle"] is result
    assert result["left"]["values"][-1] is result
    result["left"]["values"][1]["nested"] = "modified"
    assert source["left"]["values"][1]["nested"] == "value"


def test_snapshot_copy_honors_memo_and_non_json_deepcopy_extensions():
    class Extension:
        def __init__(self, payload):
            self.payload = payload

        def __deepcopy__(self, memo):
            result = type(self)(None)
            memo[id(self)] = result
            result.payload = copy.deepcopy(self.payload, memo)
            return result

    child = defaultdict(list, {"items": [{"name": "original"}]})
    extension = Extension(child)
    source = {"extension": extension, "again": extension, "child": child,
              "tuple": (child,), "set": {1, 2}, "bytes": b"value"}
    result = _snapshot_copy(source)
    assert result["extension"] is result["again"]
    assert result["extension"].payload is result["child"] is result["tuple"][0]
    assert isinstance(result["child"], defaultdict)
    result["child"]["items"][0]["name"] = "modified"
    result["set"].add(3)
    assert child["items"][0]["name"] == "original"
    assert source["set"] == {1, 2}
    assert result["bytes"] is source["bytes"]
    replacement = {"memo": "preexisting"}
    assert _snapshot_copy(child, {id(child): replacement}) is replacement
    label = "atomic-memo-key"
    assert _snapshot_copy(label, {id(label): replacement}) is replacement


def test_status_and_phone_snapshots_cannot_change_live_controls(kernel):
    kernel.configure_bindings({"zones": {"leftHand": {
        "action": {"type": "gamepad", "target": "Y", "behavior": "hold"}}}})
    kernel.handle_pose_map("mobile_pose:test", pose_map(), return_status=False)
    kernel.handle_sensor("sensor", buttons={"A"}, quaternion={"x": .1}, return_status=False)
    kernel.set_zone_learning_state("ready", report={"items": [{"score": .5}]})
    state = kernel.status()
    state["pose"]["nose"]["x"] = -99
    state["control_bindings"]["zone.leftHand"]["action"]["target"] = "A"
    state["effective_bindings"]["zone.leftHand"]["action"]["target"] = "B"
    state["zones"]["leftHand"]["rect"]["x1"] = -99
    state["zones"]["leftHandUpper"]["rect"]["x1"] = -98
    state["head"]["hand_mouse"]["client_edit"] = True
    state["zone_fit"]["zones"]["leftHand"]["client_edit"] = True
    state["zone_learning"]["report"]["items"][0]["score"] = -99
    state["handheld_sources"]["sensor"]["quaternion"]["x"] = -99
    state["vertical_look"]["enabled"] = "modified"
    phone = kernel.effective_bindings()
    phone["zone.leftHand"]["action"]["target"] = "X"
    zones = kernel.runtime_zones()
    zones["leftHand"]["rect"]["x1"] = -97
    current = kernel.status()
    assert current["pose"]["nose"]["x"] == .5
    assert current["control_bindings"]["zone.leftHand"]["action"]["target"] == "Y"
    assert current["effective_bindings"]["zone.leftHand"]["action"]["target"] == "Y"
    assert current["zones"]["leftHand"]["rect"]["x1"] >= 0
    assert current["zones"]["leftHandUpper"]["rect"] == current["zones"]["leftHand"]["rect"]
    assert "client_edit" not in current["head"]["hand_mouse"]
    assert "client_edit" not in current["zone_fit"]["zones"]["leftHand"]
    assert current["zone_learning"]["report"]["items"][0]["score"] == .5
    assert current["handheld_sources"]["sensor"]["quaternion"]["x"] == .1
    assert isinstance(current["vertical_look"]["enabled"], bool)


def test_general_payload_detaches_all_configuration_in_one_copy(kernel):
    shared = [{"value": "original"}]
    kernel.remember_general_setting("extension", {"first": shared, "second": shared})
    kernel.frozen_rects = {"leftHand": {"x1": .1, "x2": .3, "y1": .2, "y2": .4}}
    kernel.frozen_anchor = {"x": .5, "y": .7}
    payload = kernel.general_settings_payload()
    assert payload["extension"]["first"] is payload["extension"]["second"]
    payload["extension"]["first"][0]["value"] = "modified"
    payload["hand_mouse"]["enabled"] = "modified"
    payload["trigger_recording"]["triggers"].append("modified")
    payload["zone_freeze"]["rects"]["leftHand"]["x1"] = -1
    payload["zone_freeze"]["anchor"]["x"] = -1
    current = kernel.general_settings_payload()
    assert current["extension"]["first"][0]["value"] == "original"
    assert isinstance(current["hand_mouse"]["enabled"], bool)
    assert "modified" not in current["trigger_recording"]["triggers"]
    assert current["zone_freeze"]["rects"]["leftHand"]["x1"] == .1
    assert current["zone_freeze"]["anchor"]["x"] == .5


def test_live_action_names_and_zone_declarations_do_not_build_demo_catalog(kernel, monkeypatch):
    docs = copy.deepcopy(list(pose_library.registered().values()))
    squat = next(doc for doc in docs if doc["id"] == "squat")
    squat["name"] = "新名称"
    squat["passes_zones"] = ["leftHand"]
    pose_library.register(docs)
    kernel.configure_motions([{"id": "squat", "enabled": True, "target": "A", "type": "gamepad"}])

    def demo_catalog():
        pytest.fail("Control/status metadata must not build animation frames")

    monkeypatch.setattr(pose_library, "entries", demo_catalog)
    state = kernel.status()
    names = {item["key"]: item["name"] for item in state["intent_items"]["all"]}
    assert names["action:motion.squat"] == "新名称"
    assert "motion.squat" in state["zone_overlaps"]["leftHand"]["triggers"]
    squat["name"] = "再次改名"
    squat["passes_zones"] = []
    pose_library.register(docs)
    updated = kernel.status()
    names = {item["key"]: item["name"] for item in updated["intent_items"]["all"]}
    assert names["action:motion.squat"] == "再次改名"
    assert "motion.squat" not in updated["zone_overlaps"].get("leftHand", {}).get("triggers", [])


def test_state_snapshot_keeps_shared_binding_references_inside_each_response(kernel):
    shared_action = {"type": "gamepad", "target": "B", "behavior": "hold"}
    with kernel._lock:
        kernel.control_bindings["zone.leftHand"] = {"action": shared_action, "extra_actions": [shared_action]}
    state = kernel.status()
    for field in ("control_bindings", "effective_bindings"):
        binding = state[field]["zone.leftHand"]
        assert binding["action"] is binding["extra_actions"][0]
        assert binding["action"] is not shared_action
