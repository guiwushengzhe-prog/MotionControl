"""「停住语音按住」：任何触发器都能停住某一条语音持续按住。

以前语音只有一种「松开同一语音按键」：说松开的那句口令得自己再选一遍同样的键，
界面上看不出它松的是哪一条。现在它是一种动作，目标就是那条口令；区域、动作、
姿势、另一句口令都能绑。
"""

from __future__ import annotations

import pytest

from motioncontrol.control_kernel import ControlKernel
from motioncontrol.output_backend import OutputManager
from motioncontrol_shared.profile_schema import normalize_action, normalize_bindings


class Pad:
    def __init__(self) -> None:
        self.buttons: tuple = ()

    def set_buttons(self, names) -> None:
        self.buttons = tuple(sorted(names))

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
    made._combo_stick_lead = 0.0
    made.set_config(enabled=True)
    yield made
    made.close()


def test_schema_takes_a_command_id_and_is_always_a_tap():
    action = normalize_action({"type": "voice_release", "target": "voice.game.profile_slot_01",
                               "behavior": "hold"})
    assert action == {"type": "voice_release", "target": "game.profile_slot_01", "behavior": "tap"}
    with pytest.raises(ValueError):
        normalize_action({"type": "voice_release", "target": ""})


def test_body_triggers_may_bind_it():
    grouped = normalize_bindings({
        "zones": {"headJump": {"action": {"type": "voice_release", "target": "game.profile_slot_01"}}},
    })
    assert grouped["zones"]["headJump"]["action"]["type"] == "voice_release"


def test_releases_a_combo_whoever_said_it(output):
    output.execute_voice_action({"type": "gamepad", "target": ["LB", "LS_UP"], "behavior": "hold",
                                 "source": "phone-a"})
    output.execute_voice_action({"type": "gamepad", "target": "X", "behavior": "hold",
                                 "source": "phone-b"})
    assert output._pad.buttons == ("LB", "X")
    output.release_voice_hold({"type": "gamepad", "target": "LS_UP+LB"})
    assert output._pad.buttons == ("X",)
    assert [item["target"] for item in output.status()["voice_latches"]] == ["X"]


def test_leaves_body_holds_alone(output):
    output.set_action_holds([{"id": "zone.leftHand", "action": {"type": "gamepad", "target": "LB"}}])
    output.execute_voice_action({"type": "gamepad", "target": "LB", "behavior": "hold"})
    output.release_voice_hold({"type": "gamepad", "target": "LB"})
    assert output._pad.buttons == ("LB",)
    assert output.status()["voice_latches"] == []


class RecordingOutput:
    def __init__(self):
        self.released = []
        self.executed = []

    def release_voice_hold(self, action):
        self.released.append(action)

    def execute_action(self, action):
        self.executed.append(action)

    def set_action_holds(self, *args, **kwargs):
        pass

    def set_buttons(self, *args, **kwargs):
        pass

    def set_holds(self, *args, **kwargs):
        pass

    def apply(self, *args, **kwargs):
        pass


def test_a_zone_edge_releases_the_voice_commands_current_keys(isolated_user_data):
    output = RecordingOutput()
    kernel = ControlKernel(output)
    kernel.configure_bindings({
        "zones": {"headJump": {"action": {"type": "voice_release", "target": "game.profile_slot_01"}}},
        "voice": {"game.profile_slot_01": {"phrase": "体感爬绳", "action": {
            "type": "gamepad", "target": ["LB", "LS_UP"], "behavior": "hold"}}},
    })
    kernel.zone_state["headJump"]["pressed"] = True
    kernel._dispatch_controls_locked(1.0)
    kernel._dispatch_controls_locked(1.1)  # 还在区域里，不再松第二次
    assert [a["target"] for a in output.released] == [["LB", "LS_UP"]]
    assert output.executed == []


def test_a_missing_command_does_nothing(isolated_user_data):
    output = RecordingOutput()
    kernel = ControlKernel(output)
    assert kernel.release_voice_hold("game.profile_slot_09")["executed"] is False
    assert output.released == []
