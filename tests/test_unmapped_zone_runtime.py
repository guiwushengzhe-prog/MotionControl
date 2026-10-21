"""未映射区域不显示、不认作触发；编辑器仍保留完整几何。"""

from types import SimpleNamespace

import pytest

from motioncontrol.control_kernel import ControlKernel
from test_minimal_controls import KernelOutput, _standing_pose, _zone_feeder


REGION = "user_a1234567"
RECT = {"x1": .32, "x2": .47, "y1": .57, "y2": .75}
ACTION = {"type": "keyboard", "target": "Q", "behavior": "hold"}


@pytest.fixture
def zone(monkeypatch):
    kernel = ControlKernel(KernelOutput())
    feed = _zone_feeder(kernel, monkeypatch)
    kernel.configure_hand_mouse({"enabled": False})
    kernel.configure_zone_trigger_mode("simple")
    kernel.configure_regions([{"id": REGION, "name": "自定义区域", "rect": RECT}])
    feed(_standing_pose(), 10)
    yield kernel, feed
    kernel.close()


@pytest.mark.parametrize("binding", [None, {"action": ACTION, "disabled": True},
                                     {"action": {"type": "macro", "target": "missing"}}])
def test_unmapped_custom_region_never_counts_as_hit_and_remains_editable(zone, binding):
    kernel, feed = zone
    kernel.configure_bindings({"zones": {REGION: binding}} if binding is not None else {})
    feed(_standing_pose(), 3)
    status = kernel.runtime_zones_locked()[REGION]
    assert status["rect"] is None
    assert not status["mapped"] and not status["recognized"] and not status["pressed"]
    assert not kernel.zone_state[REGION].get("raw_inside", False)
    assert f"zone.{REGION}" not in kernel.trigger_previous
    assert not any(item["trigger"] == f"zone.{REGION}" for item in kernel.recent_triggers)
    assert kernel.zone_editor()["rects"][REGION] == RECT


def test_mapping_and_mode_changes_immediately_control_visibility_and_hit(zone):
    kernel, feed = zone
    kernel.configure_bindings({"zones": {REGION: {"action": ACTION}}}, mode={"id": "one", "name": "模式一"})
    feed(_standing_pose(), 2)
    assert kernel.runtime_zones_locked()[REGION]["mapped"]
    assert kernel.runtime_zones_locked()[REGION]["pressed"]
    kernel.configure_bindings({"zones": {REGION: {"disabled": True}}}, mode={"id": "two", "name": "模式二"})
    status = kernel.runtime_zones_locked()[REGION]
    assert status["rect"] is None and not status["recognized"] and not status["pressed"]
    feed(_standing_pose(), 1)
    assert not kernel.zone_state[REGION].get("raw_inside", False)


def test_existing_macro_is_a_valid_mapping_but_removed_macro_is_not(zone):
    kernel, feed = zone
    kernel.macro_store = SimpleNamespace(get=lambda ident: {"id": ident}, repeats=lambda ident: False)
    kernel.configure_bindings({"zones": {REGION: {"action": {"type": "macro", "target": "macro_one"}}}})
    feed(_standing_pose(), 1)
    assert kernel.runtime_zones_locked()[REGION]["pressed"]
    kernel.macro_store = SimpleNamespace(get=lambda ident: None)
    feed(_standing_pose(), 1)
    assert not kernel.runtime_zones_locked()[REGION]["recognized"]
    assert kernel.runtime_zones_locked()[REGION]["rect"] is None
