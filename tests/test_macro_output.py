"""键盘宏跑起来之后：按对顺序、停得下来、不会自己重启。

这一路和别的输出都不一样，因为它**自己有一条线程**。相机和控制在同一个线程上，
让它睡着等宏跑完等于丢掉三十帧，人会看到画面卡住。代价就是这个文件要验的那几件事：

* 停得下来。急停、说"松开"、看门狗，三种停法都要真的停住，而不是等宏跑完。
* 不会自己重启。``set_action_holds`` 每帧都调，看见"该跑但没在跑"就启一遍的话，
  一秒会从头启动三十次，游戏里听到的是一串乱按。
* 按完一定松开。线程里任何一步出岔子，都不能留下一个按住不放的键。
"""

from __future__ import annotations

import time

import pytest

from motioncontrol.output_backend import OutputManager


class Mouse:
    available = True
    last_error = None

    def __init__(self):
        self.pressed = set()
        self.wheels = []

    def move(self, dx, dy=0):
        return True

    def set_button(self, button, pressed):
        self.pressed.add(button) if pressed else self.pressed.discard(button)

    def wheel(self, direction, notches=1):
        self.wheels.append((direction, notches))

    def release_all(self):
        self.pressed.clear()


class Keyboard:
    available = True
    last_error = None

    def __init__(self):
        self.pressed = set()
        # 按下的先后顺序。宏的全部意义就在顺序上，只看"最后按了什么"验不出来。
        self.log: list[tuple[str, bool]] = []

    def set_key(self, key, pressed):
        self.pressed.add(key) if pressed else self.pressed.discard(key)
        self.log.append((key, pressed))

    def release_all(self):
        self.pressed.clear()


class Pad:
    def __init__(self):
        self.buttons = ()
        self.left_stick = (0, 0)
        self.triggers = (0, 0)

    def set_buttons(self, names):
        self.buttons = tuple(names)

    def set_left_stick(self, x, y=0):
        self.left_stick = (x, y)

    def set_right_stick(self, x, y=0):
        pass

    def set_triggers(self, left, right):
        self.triggers = (left, right)

    def reset(self):
        self.buttons = ()
        self.left_stick = (0, 0)
        self.triggers = (0, 0)

    def close(self):
        pass


class Library:
    """一个只有两个方法的假宏库。输出后端是鸭子类型接它的，所以不需要真文件。"""

    def __init__(self, macros: dict[str, tuple[list[dict], bool]]):
        self.macros = macros

    def expanded(self, macro_id):
        return list(self.macros.get(str(macro_id), ([], False))[0])

    def repeats(self, macro_id):
        return bool(self.macros.get(str(macro_id), ([], False))[1])


def step(target, hold_ms=10, gap_ms=0, step_type="keyboard"):
    return {"type": step_type, "target": target, "hold_ms": hold_ms, "gap_ms": gap_ms}


@pytest.fixture()
def rig(tmp_path):
    keyboard = Keyboard()
    mouse = Mouse()
    out = OutputManager(tmp_path, mouse=mouse, keyboard=keyboard)
    out._pad = Pad()
    out.set_config(enabled=True)
    yield out, keyboard, mouse
    out.close()


def presses(keyboard) -> list[str]:
    return [key for key, pressed in keyboard.log if pressed]


def wait_until(predicate, timeout=2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def hold_item(trigger, macro_id):
    return {"id": trigger, "action": {"type": "macro", "target": macro_id, "behavior": "hold"}}


# --- 跑 ---------------------------------------------------------------------

def test_the_steps_run_in_order(rig):
    out, keyboard, _ = rig
    out.configure_macros(Library({"m1": ([step("Q"), step("W"), step("E")], False)}))
    out.execute_action({"type": "macro", "target": "m1", "source": "test"})
    assert wait_until(lambda: presses(keyboard) == ["Q", "W", "E"]), presses(keyboard)


def test_every_key_is_released_again(rig):
    out, keyboard, _ = rig
    out.configure_macros(Library({"m1": ([step("Q"), step("W")], False)}))
    out.execute_action({"type": "macro", "target": "m1", "source": "test"})
    assert wait_until(lambda: presses(keyboard) == ["Q", "W"])
    assert wait_until(lambda: not keyboard.pressed), f"还按着 {keyboard.pressed}"


def test_a_macro_can_press_the_mouse_and_the_pad_too(rig):
    """用户要的是"什么都能放"。"""
    out, keyboard, mouse = rig
    out.configure_macros(Library({"m1": ([
        step("Q"),
        step("LEFT", step_type="mouse_button"),
        step("A", step_type="gamepad"),
        step("SCROLL_UP", hold_ms=0, step_type="mouse_wheel"),
    ], False)}))
    out.execute_action({"type": "macro", "target": "m1", "source": "test"})
    assert wait_until(lambda: mouse.wheels == [("SCROLL_UP", 1)])
    assert presses(keyboard) == ["Q"]
    assert wait_until(lambda: not mouse.pressed and not out._pad.buttons)


def test_a_missing_macro_does_nothing_and_says_so(rig):
    out, keyboard, _ = rig
    out.configure_macros(Library({}))
    result = out.execute_action({"type": "macro", "target": "m_nope", "source": "test"})
    assert result["executed"] is False
    time.sleep(0.05)
    assert keyboard.log == []


def test_with_no_library_at_all_nothing_happens(rig):
    """宏库还没装上就触发——启动那一瞬间真会发生。不能崩。"""
    out, keyboard, _ = rig
    assert out.execute_action({"type": "macro", "target": "m1", "source": "test"})["executed"] is False


# --- 循环 -------------------------------------------------------------------

def test_a_repeating_macro_keeps_going_until_it_is_stopped(rig):
    out, keyboard, _ = rig
    out.configure_macros(Library({"m1": ([step("Q")], True)}))
    out.set_action_holds([hold_item("zone.leftHand", "m1")], source_group="controls")
    assert wait_until(lambda: presses(keyboard).count("Q") >= 3), presses(keyboard)
    out.set_action_holds([], source_group="controls")
    assert wait_until(lambda: not keyboard.pressed)
    settled = len(keyboard.log)
    time.sleep(0.12)
    assert len(keyboard.log) == settled, "松开之后还在按"


def test_a_one_shot_macro_is_not_restarted_every_frame(rig):
    """set_action_holds 每帧都调。看见"该跑但没在跑"就重启的话，一秒三十遍。"""
    out, keyboard, _ = rig
    out.configure_macros(Library({"m1": ([step("Q")], False)}))
    for _ in range(20):
        out.set_action_holds([hold_item("zone.leftHand", "m1")], source_group="controls")
        time.sleep(0.01)
    assert presses(keyboard).count("Q") == 1, presses(keyboard)


def test_leaving_and_entering_again_runs_it_again(rig):
    out, keyboard, _ = rig
    out.configure_macros(Library({"m1": ([step("Q")], False)}))
    out.set_action_holds([hold_item("zone.leftHand", "m1")], source_group="controls")
    assert wait_until(lambda: presses(keyboard).count("Q") == 1)
    out.set_action_holds([], source_group="controls")
    out.set_action_holds([hold_item("zone.leftHand", "m1")], source_group="controls")
    assert wait_until(lambda: presses(keyboard).count("Q") == 2), presses(keyboard)


def test_the_held_key_survives_the_frames_in_between(rig):
    """每帧清空重建的话，宏刚按下的键几毫秒后就被抹掉，下一步又按回来——
    游戏里听到的是一串乱按。"""
    out, keyboard, _ = rig
    out.configure_macros(Library({"m1": ([step("Q", hold_ms=300)], False)}))
    out.set_action_holds([hold_item("zone.leftHand", "m1")], source_group="controls")
    assert wait_until(lambda: "Q" in keyboard.pressed)
    for _ in range(6):
        out.set_action_holds([hold_item("zone.leftHand", "m1")], source_group="controls")
        time.sleep(0.01)
    assert "Q" in keyboard.pressed, "被每帧的清空抹掉了"
    assert keyboard.log.count(("Q", False)) == 0, "中途松开过"


# --- 停 ---------------------------------------------------------------------

def test_emergency_stop_stops_it_at_once(rig):
    out, keyboard, _ = rig
    out.configure_macros(Library({"m1": ([step("Q", hold_ms=1000)], True)}))
    out.set_action_holds([hold_item("zone.leftHand", "m1")], source_group="controls")
    assert wait_until(lambda: "Q" in keyboard.pressed)
    out.emergency_stop()
    assert not keyboard.pressed, "急停之后还按着"
    settled = len(keyboard.log)
    time.sleep(0.15)
    assert len(keyboard.log) == settled, "急停之后还在按键"


def test_clearing_a_source_stops_that_macro(rig):
    """语音说"松开"走的就是这条路。不停它，键会被清掉又被宏按回来。"""
    out, keyboard, _ = rig
    out.configure_macros(Library({"m1": ([step("Q", hold_ms=50)], True)}))
    out.execute_action({"type": "macro", "target": "m1", "source": "phone|voice-hold:macro:m1"},
                       persistent=True)
    assert wait_until(lambda: "Q" in keyboard.pressed)
    out.clear_source("phone|voice-hold:macro:m1")
    assert wait_until(lambda: not keyboard.pressed)
    settled = len(keyboard.log)
    time.sleep(0.15)
    assert len(keyboard.log) == settled


def test_a_voice_held_macro_shows_up_in_the_latch_list(rig):
    """语音按住是隐形的：说过一句就忘了，人只会以为软件坏了。宏更是如此——
    键在步与步之间是松的，光看按下的键根本看不出还有东西在跑。"""
    out, _, _ = rig
    out.configure_macros(Library({"m1": ([step("Q", hold_ms=20, gap_ms=20)], True)}))
    out.execute_voice_action({"type": "macro", "target": "m1", "behavior": "hold", "source": "phone"})
    latches = out.status()["voice_latches"]
    assert len(latches) == 1 and latches[0]["type"] == "macro" and latches[0]["target"] == "m1"
    out.execute_voice_action({"type": "macro", "target": "m1", "behavior": "release", "source": "phone"})
    assert wait_until(lambda: out.status()["voice_latches"] == [])


def test_turning_output_off_stops_it(rig):
    out, keyboard, _ = rig
    out.configure_macros(Library({"m1": ([step("Q", hold_ms=30)], True)}))
    out.set_action_holds([hold_item("zone.leftHand", "m1")], source_group="controls")
    assert wait_until(lambda: "Q" in keyboard.pressed)
    out.set_config(enabled=False)
    assert wait_until(lambda: not keyboard.pressed)
    settled = len(keyboard.log)
    time.sleep(0.15)
    assert len(keyboard.log) == settled, "关掉输出之后还在按键"


def test_the_watchdog_stops_a_macro_whose_trigger_went_quiet(rig):
    """控制这一路不再刷新，说明触发它的东西已经没了。宏是自己在按键的线程，
    不停它就会一直按下去。"""
    out, keyboard, _ = rig
    out.configure_macros(Library({"m1": ([step("Q", hold_ms=40, gap_ms=10)], True)}))
    out.set_action_holds([hold_item("zone.leftHand", "m1")], source_group="controls")
    assert wait_until(lambda: "Q" in keyboard.pressed)
    assert wait_until(lambda: not out._macro_runs, timeout=2.0), "看门狗没有把它停住"
    assert wait_until(lambda: not keyboard.pressed)


def test_a_finished_one_shot_is_not_reported_as_running(rig):
    """跑完留下的那个记号是给"别重启"用的，报出去界面上会一直亮着。"""
    out, keyboard, _ = rig
    out.configure_macros(Library({"m1": ([step("Q")], False)}))
    out.set_action_holds([hold_item("zone.leftHand", "m1")], source_group="controls")
    assert wait_until(lambda: presses(keyboard) == ["Q"])
    assert wait_until(lambda: out.status()["macros_running"] == [])
