"""触发状态推给手机：集合变了才推，推的路上不能卡住识别。

打游戏时人看不到电脑屏幕（游戏全屏），只看得见手机——所以"刚才按了什么"要显示在
手机上。但这件事有两个容易做坏的地方：

* **每帧推一次。** 按住不放的那几秒就是每秒三十条一模一样的消息，手机上什么都不会
  变，网络和电池却一直在烧。用户明说过"不要一直传输占资源"。
* **在识别线程里直接写 socket。** 回调是在控制线程、而且在锁里被调用的。一个网络
  变慢的手机就能把识别卡住，表现出来是掉帧——没人会把掉帧和"手机上那个显示"联系
  起来。
"""

from __future__ import annotations

import time
from pathlib import Path

from motioncontrol.control_kernel import ControlKernel

ROOT = Path(__file__).resolve().parent.parent
SERVER = (ROOT / "server.py").read_text(encoding="utf-8")


class Output:
    def set_action_holds(self, *args, **kwargs):
        return {}

    def set_buttons(self, *args, **kwargs):
        return {}

    def set_holds(self, *args, **kwargs):
        return {}

    def apply(self, *args, **kwargs):
        return {}

    def status(self):
        return {}


def frames(kernel, count=1):
    with kernel._lock:
        for _ in range(count):
            kernel._dispatch_controls_locked(time.monotonic())


def test_holding_still_is_one_message_not_thirty():
    kernel = ControlKernel(Output())
    heard = []
    kernel.configure_trigger_listener(heard.append)
    with kernel._lock:
        kernel.pose_active = {"hands_cross"}
    frames(kernel, 30)
    assert len(heard) == 1, f"按住一秒推了 {len(heard)} 次"
    kernel.close()


def test_letting_go_is_a_change_too():
    """松手也要推——不推的话手机上那个键会一直亮着，像是卡住了。"""
    kernel = ControlKernel(Output())
    heard = []
    kernel.configure_trigger_listener(heard.append)
    with kernel._lock:
        kernel.pose_active = {"hands_cross"}
    frames(kernel)
    with kernel._lock:
        kernel.pose_active = set()
    frames(kernel)
    assert len(heard) == 2
    assert heard[1]["held"] == []
    kernel.close()


def test_the_message_says_what_fired_and_what_it_presses():
    kernel = ControlKernel(Output())
    kernel.configure_bindings({"poses": {"hands_cross": {
        "action": {"type": "keyboard", "target": "F", "behavior": "hold"}}}})
    heard = []
    kernel.configure_trigger_listener(heard.append)
    with kernel._lock:
        kernel.pose_active = {"hands_cross"}
    frames(kernel)
    fired = heard[0]["fired"]
    assert fired[0]["id"] == "pose.hands_cross"
    assert fired[0]["action"]["target"] == "F"
    kernel.close()


def test_the_key_shown_is_the_one_that_really_fires():
    """区域有一层内置兜底：配置里没有 zone.headJump 时它照样按 A。推给手机的必须是
    真正会按下去的那个，不然手机上会写「未映射」而游戏里明明有反应。"""
    kernel = ControlKernel(Output())
    kernel.configure_bindings({})
    brief = kernel._trigger_brief_locked("zone.headJump")
    assert brief["action"] and brief["action"]["target"] == "A"
    kernel.close()


def test_a_listener_that_throws_does_not_break_control():
    kernel = ControlKernel(Output())

    def broken(payload):
        raise RuntimeError("手机那边坏了")

    kernel.configure_trigger_listener(broken)
    with kernel._lock:
        kernel.pose_active = {"hands_cross"}
    frames(kernel)  # 不抛就是过了
    kernel.close()


def test_the_server_never_writes_to_a_socket_from_the_control_thread():
    """内核调的那个必须是放下就走：put_nowait，满了就丢。"""
    assert "KERNEL.configure_trigger_listener(_queue_trigger_state)" in SERVER
    start = SERVER.index("def _queue_trigger_state")
    body = SERVER[start:start + 300]
    assert "put_nowait" in body
    assert "broadcast" not in body, "在控制线程里直接推了"


def test_the_queue_is_bounded():
    """显示用的状态，不是指令。攒着不丢的话内存一直涨、显示越来越滞后。"""
    assert "queue.Queue(maxsize=" in SERVER


def test_only_phones_get_it():
    """电脑自己的网页有自己的轮询，给它也推一份就是重复。"""
    bridge = (ROOT / "motioncontrol" / "input_bridge.py").read_text(encoding="utf-8")
    start = bridge.index("def broadcast_trigger_state")
    assert "not peer.desktop" in bridge[start:start + 1200]


def test_a_voice_command_reaches_the_phone_too():
    """语音说完就完，不会出现在按住的集合里——漏掉的话，说了一句口令手机上什么
    都不亮，人会以为没识别出来。"""
    kernel = ControlKernel(Output())
    heard = []
    kernel.configure_trigger_listener(heard.append)
    kernel.note_trigger("voice.ui.menu", {"type": "keyboard", "target": "ESC", "behavior": "tap"})
    assert len(heard) == 1
    assert heard[0]["fired"][0]["id"] == "voice.ui.menu"
    assert heard[0]["fired"][0]["action"]["target"] == "ESC"
    kernel.close()


def test_the_phone_is_told_what_really_fires():
    """手机上那排框要写真正会按下去的键，不是配置里写的。"""
    assert '"effective_bindings": KERNEL.effective_bindings()' in SERVER
    kernel = ControlKernel(Output())
    kernel.configure_bindings({})
    effective = kernel.effective_bindings()
    assert effective["zone.headJump"]["action"]["target"] == "A"
    kernel.close()
