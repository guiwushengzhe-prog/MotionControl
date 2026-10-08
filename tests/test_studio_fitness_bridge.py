"""新增手机协议与已有配对/相机所有权兼容，不使用实际设备。"""
import base64

import cv2
import numpy as np
import pytest

from motioncontrol.fitness import FitnessStore
from motioncontrol.input_bridge import InputBridge
from test_mobile_input_bridge import FakeOutput, FakePeer


@pytest.fixture
def bridge(tmp_path):
    instance = InputBridge(FakeOutput())
    store = FitnessStore(tmp_path / "fitness.json", background=False)
    instance.configure_fitness(store)
    yield instance, store
    instance.close()
    store.close()


def video(sequence=0, width=32, height=24):
    ok, jpeg = cv2.imencode(".jpg", np.full((height, width, 3), 127, np.uint8))
    assert ok
    return {"type": "studio_video_frame_v1", "sequence": sequence,
            "device_id": "camera", "jpeg": base64.b64encode(jpeg).decode(), "captured_at_ms": 1000}


def test_fitness_control_uses_absolute_history_and_does_not_steal_camera_role(bridge):
    instance, store = bridge
    peer = FakePeer()
    instance.handle_message(peer, {"type": "hello_v1", "role": "camera"})
    instance.handle_message(peer, {"type": "fitness_control_v1", "action": "start", "role": "fitness"})
    assert peer._mobile_role == "camera"
    assert store.state()["status"] == "active"
    instance.handle_message(peer, {"type": "fitness_control_v1", "action": "history"})
    assert peer.messages[-1]["type"] == "fitness_history_v1"
    assert len(peer.messages[-1]["sessions"]) == 1
    denied = FakePeer()
    denied.paired = False
    instance.handle_message(denied, {"type": "fitness_control_v1", "action": "finish"})
    assert store.state()["status"] == "active"


def test_video_demand_role_order_owner_and_disconnect_are_enforced(bridge):
    instance, _ = bridge
    a, b = FakePeer(), FakePeer()
    instance.handle_message(a, {"type": "hello_v1", "role": "camera"})
    instance.handle_message(b, {"type": "hello_v1", "role": "camera"})
    instance.handle_message(a, video())
    assert instance.latest_studio_frame() is None
    instance.set_studio_video(True)
    instance.handle_message(a, video(1))
    assert instance.latest_studio_frame()["sequence"] == 1
    instance.handle_message(a, video(0))
    instance.handle_message(b, video(2))
    assert instance.latest_studio_frame()["sequence"] == 1
    instance.disconnect(a)
    assert instance.latest_studio_frame() is None
    instance.handle_message(b, video(3))
    assert instance.latest_studio_frame()["sequence"] == 3
    instance.set_studio_video(False)
    assert instance.latest_studio_frame() is None


def test_video_rejects_actual_oversized_image_and_non_camera(bridge):
    instance, _ = bridge
    peer = FakePeer()
    instance.handle_message(peer, {"type": "hello_v1", "role": "fitness"})
    instance.set_studio_video(True)
    instance.handle_message(peer, video())
    assert instance.latest_studio_frame() is None
    instance.handle_message(peer, {"type": "hello_v1", "role": "camera"})
    instance.handle_message(peer, video(1, 32, 641))
    assert instance.latest_studio_frame() is None
    assert peer.messages[-1]["type"] == "error"


def test_request_change_during_decode_cannot_restore_an_old_video_frame(bridge, monkeypatch):
    instance, _ = bridge
    peer = FakePeer()
    instance.handle_message(peer, {"type": "hello_v1", "role": "camera"})
    instance.set_studio_video(True)
    decode = cv2.imdecode

    def switch_request(*args):
        instance.set_studio_video(False)
        instance.set_studio_video(True)
        return decode(*args)

    monkeypatch.setattr(cv2, "imdecode", switch_request)
    instance.handle_message(peer, video())
    assert instance.latest_studio_frame() is None
