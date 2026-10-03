import threading
import time

import pytest

from motioncontrol.input_bridge import InputBridge
from motioncontrol.output_backend import OutputManager


class Peer:
    desktop = False

    def __init__(self):
        self.source_ids = set()
        self.accepted_inputs = 0
        self.messages = []
        self._closed = False

    def send_json(self, message):
        self.messages.append(message)

    def close(self):
        self._closed = True


class Mouse:
    available = True
    last_error = None

    def __init__(self):
        self.pressed = set()
        self.moves = []
        self.edges = []
        self.released = threading.Event()

    def move(self, dx, dy=0):
        self.moves.append((dx, dy))
        return True

    def set_button(self, button, pressed):
        self.edges.append((button, pressed))
        if pressed:
            self.pressed.add(button)
        else:
            self.pressed.discard(button)
            self.released.set()

    def release_all(self):
        for button in tuple(self.pressed):
            self.set_button(button, False)


class Keyboard:
    available = True
    last_error = None

    def __init__(self):
        self.pressed = set()

    def release_all(self):
        self.pressed.clear()


@pytest.fixture
def setup(tmp_path):
    mouse = Mouse()
    output = OutputManager(tmp_path, mouse=mouse, keyboard=Keyboard())
    output.set_config(enabled=True)
    bridge = InputBridge(output)
    try:
        yield bridge, mouse, output
    finally:
        bridge.close()
        output.close()


def frame(sequence=0, *, buttons=0, dx=0, dy=0, device_id="phone"):
    return {
        "type": "mouse_frame", "role": "mouse", "device_id": device_id,
        "sequence": sequence, "captured_at_ms": 1000 + sequence,
        "dx": dx, "dy": dy, "buttons": buttons,
    }


def test_relative_mouse_and_button_edges_use_existing_output(setup):
    bridge, mouse, _ = setup
    peer = Peer()
    bridge.handle_message(peer, frame(buttons=1, dx=12, dy=-8))
    bridge.handle_message(peer, frame(1, buttons=3))
    bridge.handle_message(peer, frame(2, buttons=2))
    bridge.handle_message(peer, frame(3))
    assert mouse.moves == [(12, -8)]
    assert mouse.edges == [("LEFT", True), ("RIGHT", True), ("LEFT", False), ("RIGHT", False)]
    assert not mouse.pressed
    assert peer.messages == []
    assert bridge.status()["handheld_connected"]
    assert bridge.status()["handheld_sources"][0]["source_kind"] == "mobile_mouse"


def test_disconnect_releases_only_this_phone_buttons(setup):
    bridge, mouse, _ = setup
    first, second = Peer(), Peer()
    bridge.handle_message(first, frame(buttons=1))
    bridge.handle_message(second, frame(buttons=2, device_id="other"))
    bridge.disconnect(first)
    assert mouse.pressed == {"RIGHT"}
    bridge.disconnect(second)
    assert not mouse.pressed
    assert not bridge.status()["handheld_connected"]


def test_new_connection_can_restart_sequence_and_old_connection_cannot_reclaim(setup):
    bridge, mouse, _ = setup
    old, new = Peer(), Peer()
    bridge.handle_message(old, frame(12, buttons=1, dx=3))
    bridge.handle_message(new, frame(0, buttons=2, dx=5))
    bridge.handle_message(old, frame(13, buttons=1, dx=40))
    bridge.disconnect(old)
    assert mouse.moves == [(3, 0), (5, 0)]
    assert mouse.pressed == {"RIGHT"}
    assert bridge.status()["mouse_rejected"]["owner"] == 1


def test_repeated_or_stale_movement_is_not_applied_twice(setup):
    bridge, mouse, _ = setup
    peer = Peer()
    bridge.handle_message(peer, frame(dx=2))
    bridge.handle_message(peer, frame(dx=99))
    bridge.handle_message(peer, dict(frame(1, buttons=1, dx=99), sent_at_ms=2000))
    assert mouse.moves == [(2, 0)]
    assert not mouse.pressed
    assert bridge.status()["mouse_rejected"] == {"owner": 0, "sequence": 1, "stale": 1}


def test_timed_out_old_connection_cannot_reclaim_after_reconnect(setup):
    bridge, mouse, _ = setup
    old, new = Peer(), Peer()
    bridge.handle_message(old, frame(10, buttons=1))
    with bridge._lock:
        bridge._mouse_sources["mobile_mouse:phone"]["received_at"] = time.monotonic() - 1
    assert mouse.released.wait(1)
    bridge.handle_message(new, frame(buttons=2))
    bridge.handle_message(old, frame(11, buttons=1, dx=100))
    assert mouse.pressed == {"RIGHT"} and not mouse.moves


@pytest.mark.parametrize("field,value", [
    ("role", "sensor"), ("device_id", " "), ("sequence", True),
    ("sequence", -1), ("dx", True), ("dx", 1.5), ("dx", 32768),
    ("dy", float("inf")), ("captured_at_ms", float("nan")),
    ("captured_at_ms", 10 ** 400),
    ("sent_at_ms", float("inf")), ("buttons", True), ("buttons", 4),
])
def test_invalid_input_does_not_move_or_latch_buttons(setup, field, value):
    bridge, mouse, _ = setup
    peer = Peer()
    bridge.handle_message(peer, dict(frame(buttons=1, dx=4), **{field: value}))
    assert not mouse.moves and not mouse.pressed and not peer.source_ids
    assert peer.messages[-1]["type"] == "error"


def test_stopped_input_releases_buttons_without_closing_connection(setup):
    bridge, mouse, _ = setup
    peer = Peer()
    bridge.handle_message(peer, frame(buttons=3))
    with bridge._lock:
        bridge._mouse_sources["mobile_mouse:phone"]["received_at"] = time.monotonic() - 1
    assert mouse.released.wait(1)
    with bridge._lock:
        assert not mouse.pressed
    assert not peer._closed
    bridge.handle_message(peer, frame(1, buttons=2, dx=-2))
    assert mouse.pressed == {"RIGHT"}
    assert mouse.moves == [(-2, 0)]


def test_global_pause_is_respected_and_phone_can_request_resume(setup):
    bridge, mouse, output = setup
    peer = Peer()
    output.set_config(enabled=False)
    bridge.handle_message(peer, frame(buttons=3, dx=8))
    assert not mouse.pressed and not mouse.moves
    bridge.handle_message(peer, {
        "type": "game_output_control", "role": "mouse", "device_id": "phone", "enabled": True,
    })
    assert peer.messages[-1] == {"type": "game_output_state_v1", "enabled": True, "ok": True}
    bridge.handle_message(peer, frame(1, buttons=1, dx=2))
    assert mouse.pressed == {"LEFT"} and mouse.moves == [(2, 0)]
    output.set_config(enabled=False)
    assert not mouse.pressed


def test_mouse_ack_does_not_require_body_pose_or_emit_camera_zone_messages(setup):
    bridge, _, _ = setup
    peer = Peer()
    for sequence in range(15):
        bridge.handle_message(peer, frame(sequence))
    assert [message["type"] for message in peer.messages] == ["ack"]
    assert peer.messages[0]["accepted"] and not peer.messages[0]["pose_visible"]
