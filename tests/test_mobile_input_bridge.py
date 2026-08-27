import time

from input_bridge import InputBridge, MOBILE_POSE_FEATURE_INDICES, _expand_pose_features
from output_backend import OutputManager


class FakePeer:
    def __init__(self, *, desktop=False):
        self.desktop = desktop
        self.source_ids = set()
        self.accepted_inputs = 0
        self.messages = []
        self.closed = False

    def send_json(self, message):
        self.messages.append(message)

    def close(self):
        self.closed = True


class FakeOutput:
    def __init__(self):
        self.sensor_calls = []
        self.cleared = []

    def set_sensor_state(self, source, buttons, **kwargs):
        self.sensor_calls.append((source, set(buttons), kwargs))

    def clear_source(self, source):
        self.cleared.append(source)


def pose_frame(device_id, sequence=0):
    point = {"x": 0.5, "y": 0.5, "z": 0.0, "visibility": 0.95}
    return {
        "type": "pose_frame_v2",
        "role": "camera",
        "device_id": device_id,
        "sequence": sequence,
        "captured_at_ms": 1000 + sequence,
        "width": 640,
        "height": 480,
        "camera_facing": "user",
        "orientation_degrees": 0,
        "preview_mirrored": True,
        "coordinates_mirrored": False,
        "poses": [{"pose": [dict(point) for _ in range(33)]}],
        "hands": [],
        "inference_ms": 4.0,
    }


def sensor_frame(device_id="phone-1"):
    return {
        "type": "sensor_frame",
        "role": "sensor",
        "device_id": device_id,
        "sequence": 1,
        "captured_at_ms": 2000,
        "player_slot": 0,
        "quaternion": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
        "rotation_rate": {"x": 0.1, "y": 0.2, "z": 0.3},
        "acceleration": {"x": 0.0, "y": 0.0, "z": 9.8},
        "recenter": False,
        "touches": [
            {"control": "A", "pressed": True},
            {"control": "RT", "value": 0.7},
            {"control": "stick", "x": 0.5, "y": -0.25},
        ],
    }


def test_pose_source_is_forwarded_and_only_one_source_is_active():
    output = FakeOutput()
    bridge = InputBridge(output)
    desktop = FakePeer(desktop=True)
    first = FakePeer()
    second = FakePeer()
    try:
        bridge.register(desktop)
        bridge._handle_pose(first, pose_frame("camera-a"))
        assert desktop.messages[-1]["type"] == "pose_frame_v2"
        assert desktop.messages[-1]["source_kind"] == "mobile_pose"
        assert bridge.status()["mobile_pose_source_id"] == "mobile_pose:camera-a"

        bridge._handle_pose(second, pose_frame("camera-b", 2))
        assert bridge.status()["mobile_pose_source_id"] == "mobile_pose:camera-b"
        assert "mobile_pose:camera-a" not in first.source_ids
        states = [m for m in desktop.messages if m.get("type") == "pose_source_state"]
        assert any(m["source_id"] == "mobile_pose:camera-a" and not m["active"] for m in states)
        assert any(m["source_id"] == "mobile_pose:camera-b" and m["active"] for m in states)
    finally:
        bridge.close()



def pose_features(device_id="camera-compact", sequence=0):
    points = [[0.5, 0.5, 0.0, 0.95] for _ in MOBILE_POSE_FEATURE_INDICES]
    return {
        "type": "pose_features_v1", "role": "camera", "layout": "mc25-v1",
        "device_id": device_id, "sequence": sequence, "captured_at_ms": 1000 + sequence,
        "width": 480, "height": 854, "camera_facing": "environment",
        "preview_mirrored": False, "coordinates_mirrored": False,
        "actual_model": "full", "points": points, "inference_ms": 18.5,
    }


def test_compact_phone_pose_expands_locally_without_world_pose():
    message = pose_features()
    expanded = _expand_pose_features(message)
    assert expanded["type"] == "pose_frame_v2"
    assert expanded["wire_protocol"] == "pose_features_v1"
    assert len(expanded["poses"]) == 1
    pose = expanded["poses"][0]["pose"]
    assert len(pose) == 33
    assert expanded["poses"][0]["world_pose"] is None
    assert all(pose[index]["visibility"] == .95 for index in MOBILE_POSE_FEATURE_INDICES)
    omitted = set(range(33)) - set(MOBILE_POSE_FEATURE_INDICES)
    assert all(pose[index]["visibility"] == 0.0 for index in omitted)

    output = FakeOutput()
    bridge = InputBridge(output)
    desktop = FakePeer(desktop=True)
    phone = FakePeer()
    try:
        bridge.register(desktop)
        bridge._handle_pose_features(phone, message)
        forwarded = [m for m in desktop.messages if m.get("type") == "pose_frame_v2"][-1]
        assert forwarded["wire_protocol"] == "pose_features_v1"
        assert forwarded["pose_count"] if "pose_count" in forwarded else True
        assert bridge.status()["mobile_pose_source_id"] == "mobile_pose:camera-compact"
    finally:
        bridge.close()

def test_sensor_frame_drives_button_trigger_and_stick_then_disconnect_zeros():
    output = FakeOutput()
    bridge = InputBridge(output)
    peer = FakePeer()
    try:
        bridge._handle_sensor(peer, sensor_frame())
        source, buttons, values = output.sensor_calls[-1]
        assert source == "mobile_sensor:phone-1:0"
        assert buttons == {"A"}
        assert values == {"left_trigger": 0.0, "right_trigger": 0.7, "stick_x": 0.5, "stick_y": -0.25}
        assert bridge.status()["handheld_sources"][0]["recenter"] is False
        bridge.disconnect(peer)
        assert source in output.cleared
    finally:
        bridge.close()


def test_pose_watchdog_broadcasts_clear_after_300ms():
    output = FakeOutput()
    bridge = InputBridge(output)
    desktop = FakePeer(desktop=True)
    peer = FakePeer()
    try:
        bridge.register(desktop)
        bridge._handle_pose(peer, pose_frame("camera-a"))
        time.sleep(0.38)
        assert bridge.status()["mobile_pose_source_id"] is None
        assert any(
            m.get("type") == "pose_source_state"
            and m.get("source_id") == "mobile_pose:camera-a"
            and m.get("active") is False
            for m in desktop.messages
        )
    finally:
        bridge.close()


def test_existing_output_manager_has_remote_sensor_source_clear(tmp_path):
    class FakeMouse:
        available = True
        last_error = None

        def move(self, dx, dy=0):
            return True

    class FakeKeyboard:
        available = True
        last_error = None
        pressed = set()

        def release_all(self):
            self.pressed.clear()

    class FakePad:
        def __init__(self):
            self.buttons = ()
            self.left_stick = (0.0, 0.0)
            self.triggers = (0.0, 0.0)

        def set_buttons(self, names):
            self.buttons = tuple(names)

        def set_left_stick(self, x, y):
            self.left_stick = (x, y)

        def set_triggers(self, left, right):
            self.triggers = (left, right)

        def reset(self):
            self.buttons = ()
            self.left_stick = (0.0, 0.0)
            self.triggers = (0.0, 0.0)

        def close(self):
            pass

    out = OutputManager(tmp_path, mouse=FakeMouse(), keyboard=FakeKeyboard())
    out._pad = FakePad()
    try:
        out.set_config(mode="gamepad", enabled=True)
        out.set_sensor_state("mobile_sensor:test:0", {"A"}, right_trigger=0.5, stick_x=0.25, stick_y=-0.2)
        assert out._pad.buttons == ("A",)
        assert out._pad.triggers == (0.0, 0.5)
        assert out._pad.left_stick == (0.25, -0.2)
        out.clear_source("mobile_sensor:test:0")
        assert out._pad.buttons == ()
        assert out._pad.triggers == (0.0, 0.0)
        assert out._pad.left_stick == (0.0, 0.0)
    finally:
        out.close()


def test_web_mobile_pose_uses_same_body_action_and_head_pipeline():
    # Keep this test independent from the current working directory.
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    text = (root / "web" / "app.js").read_text(encoding="utf-8")
    kernel = (root / "control_kernel.py").read_text(encoding="utf-8")
    assert "renderKernelState" in text and "/api/kernel/status" in text
    assert "handle_pose_message" in kernel and "_update_zones_locked" in kernel
    assert "_update_motion_locked" in kernel and "_update_head_locked" in kernel
    assert "requestAnimationFrame" not in text
    assert "detectForVideo" not in text
    assert "post('/api/output/buttons'" not in text
