"""语音按住的键要能被看见，姿势按住的不用。

这两种按住对使用者不是一回事。姿势按住是看得见的——手还交叉着、腿还抬着，人自己
知道自己在按。语音按住是隐形的：三十秒前说了一句「保持左肩键」，之后忘了，游戏
开始不对劲，人只会以为是误触或者软件坏了，根本想不到去说一句「松开」。

所以状态里单报语音这一类。多报了（把区域、姿势也塞进去）反而把这条信息稀释成
噪音——那些本来就不需要提醒。
"""

from __future__ import annotations

import pytest

from motioncontrol.output_backend import OutputManager


class Pad:
    def __init__(self) -> None:
        self.buttons: tuple = ()

    def set_buttons(self, names) -> None:
        self.buttons = tuple(names)

    def set_left_stick(self, *args, **kwargs) -> None:
        pass

    def set_trigger(self, *args, **kwargs) -> None:
        pass

    def reset(self) -> None:
        self.buttons = ()

    def close(self) -> None:
        pass


@pytest.fixture()
def output(tmp_path):
    made = OutputManager(tmp_path)
    made._pad = Pad()
    made.set_config(enabled=True)
    yield made
    made.close()


def hold(output, target="LB"):
    output.execute_voice_action({"type": "gamepad", "target": target, "behavior": "hold"})


def release(output, target="LB"):
    output.execute_voice_action({"type": "gamepad", "target": target, "behavior": "release"})


def test_nothing_held_reports_nothing(output):
    assert output.status()["voice_latches"] == []


def test_a_voice_hold_shows_up(output):
    hold(output)
    latches = output.status()["voice_latches"]
    assert len(latches) == 1
    assert latches[0]["type"] == "gamepad"
    assert latches[0]["target"] == "LB"


def test_saying_release_clears_it(output):
    hold(output)
    release(output)
    assert output.status()["voice_latches"] == []


def test_a_pose_hold_is_not_reported(output):
    """姿势按住不进这个列表——人看着自己的手就知道，不需要被提醒。"""
    output.set_buttons(["LB"], source="zones")
    assert output.status()["voice_latches"] == []
    assert "LB" in output.status()["buttons"]


def test_a_pose_letting_go_does_not_hide_the_voice_latch(output):
    """两个来源按同一个键时，姿势松手不该让提示跟着消失——键还按着呢。"""
    output.set_buttons(["LB"], source="zones")
    hold(output)
    output.set_buttons([], source="zones")
    assert "LB" in output.status()["buttons"], "语音还按着，键不该松"
    assert len(output.status()["voice_latches"]) == 1, "提示不该跟着姿势一起消失"


def test_several_latches_are_all_listed(output):
    hold(output, "LB")
    hold(output, "RB")
    targets = {item["target"] for item in output.status()["voice_latches"]}
    assert targets == {"LB", "RB"}


def test_a_tap_never_latches(output):
    """点一下就放开的不该留下提示，否则满屏都是提示，真正要紧的那条就被埋了。"""
    output.execute_voice_action({"type": "gamepad", "target": "A", "behavior": "tap"})
    assert output.status()["voice_latches"] == []


def test_a_keyboard_hold_is_reported_too(output):
    """语音按住的不只有手柄键。键盘那份同样隐形，同样要报。"""
    output.execute_voice_action({"type": "keyboard", "target": "W", "behavior": "hold"})
    latches = output.status()["voice_latches"]
    assert len(latches) == 1 and latches[0]["type"] == "keyboard"


def test_emergency_stop_clears_the_latches(output):
    """急停之后键已经放了，提示还留着就是在说谎。"""
    hold(output)
    output.emergency_stop()
    assert output.status()["voice_latches"] == []
