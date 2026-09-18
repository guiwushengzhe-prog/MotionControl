"""关掉上下视角之后，那道闸必须整个消失。

起因是一个真实的误导：玩家用手控鼠标转视角时，右手已经在管视角了，而上下视角
那道闸抢的是同一只右手——绿框摆在那里，还写着「左手放这里」，指挥人去做一个
不起作用的动作。

输出早就被 vertical_look["enabled"] 挡住了，但区域本身照样上报，界面照样画。
一个不起作用却还在指挥人的提示，比没有更糟，所以这里钉住两件事：关掉之后区域
不再上报，以及这个开关不依赖场景布局。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from motioncontrol.control_kernel import ControlKernel  # noqa: E402


class Output:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def sink(*args, **kwargs):
            self.calls.append(name)
        return sink


@pytest.fixture()
def kernel(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    return ControlKernel(Output())


def test_off_is_accepted_without_a_saved_scene_layout(kernel):
    """没定位过区域的玩家不会保存布局，开关不能只存在那里面。"""
    kernel.configure_head(vertical_look_source="off")
    assert kernel.vertical_look["enabled"] is False
    assert kernel.vertical_gate_active is False


def test_turning_it_off_hides_the_gate_zone(kernel):
    kernel.zone_rects["lookGate"] = (0.1, 0.1, 0.2, 0.2)
    kernel.fixed_zones_enabled = False

    kernel.configure_head(vertical_look_source="hand")
    assert kernel._gate_available() is True, "开着的时候闸应该在"

    kernel.configure_head(vertical_look_source="off")
    assert kernel._gate_available() is False, "关掉之后界面不该再画绿框"


def test_off_is_also_honoured_for_a_fixed_scene(kernel):
    kernel.fixed_zones_enabled = True
    kernel.fixed_zones = {"lookGate": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}}

    kernel.configure_head(vertical_look_source="hand")
    assert kernel._gate_available() is True

    kernel.configure_head(vertical_look_source="off")
    assert kernel._gate_available() is False


def test_turning_it_back_on_remembers_which_side_was_chosen(kernel):
    kernel.configure_head(vertical_look_source="head")
    kernel.configure_head(vertical_look_source="off")
    assert kernel.vertical_look["source"] == "head", "关掉不该顺手把选择也抹了"

    kernel.configure_head(vertical_look_source="head")
    assert kernel.vertical_look["enabled"] is True
    assert kernel.vertical_look["source"] == "head"


def test_a_real_source_turns_it_back_on(kernel):
    kernel.configure_head(vertical_look_source="off")
    kernel.configure_head(vertical_look_source="hand")
    assert kernel.vertical_look["enabled"] is True
    assert kernel.vertical_look["source"] == "hand"


def test_nonsense_still_raises(kernel):
    with pytest.raises(ValueError):
        kernel.configure_head(vertical_look_source="左脚")
