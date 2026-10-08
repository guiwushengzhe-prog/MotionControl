"""相机热切换与彻底停止，使用替代设备，不产生真实鼠标或游戏输出。"""
import io
import threading

import pytest

from motioncontrol.control_kernel import CameraUnavailable
from test_configuration_coordination import application
from test_profile_api_v2 import request_for


def mock_camera(app, monkeypatch):
    camera, events = app.RUNTIME.camera, []
    def stopped(*args, **kwargs):
        events.append("release")
        camera.running = False
        return camera.status()
    def started():
        events.append("start")
        camera.running = True
        return camera.status()
    monkeypatch.setattr(camera, "stop_and_wait", stopped)
    monkeypatch.setattr(camera, "start", started)
    monkeypatch.setattr(app, "_set_audio_source", lambda *args, **kwargs: {})
    return camera, events


def test_running_camera_selection_releases_then_starts_and_preserves_controls(application, monkeypatch):
    app = application
    camera, events = mock_camera(app, monkeypatch)
    monkeypatch.setattr(app, "_set_output_enabled", lambda enabled: events.append(("output", enabled)))
    controls = (dict(app.KERNEL.head_controller.config), dict(app.KERNEL.hand_mouse_controller.config))
    camera.running = True
    replies = []
    request = request_for(app, "/api/camera/config", replies)
    request._body = lambda: {"device": "kinect2:test-camera"}
    request.do_POST()
    assert replies[-1][0] == 200
    assert replies[-1][1]["camera"]["running"]
    assert replies[-1][1]["body_mode"] == "computer"
    assert events[:3] == [("output", False), "release", "start"]
    assert app.INPUT_BRIDGE.status()["body_enabled"]
    request._body = lambda: {"device": "0"}
    request.do_POST()
    assert replies[-1][1]["camera"]["camera_device"] == "0"
    assert replies[-1][1]["camera"]["running"]
    assert app.KERNEL.general_setting("camera_device") == "0"
    assert controls == (app.KERNEL.head_controller.config, app.KERNEL.hand_mouse_controller.config)
    camera.running = False


def test_stop_wins_over_camera_start_already_in_progress(application, monkeypatch):
    app = application
    camera, events = mock_camera(app, monkeypatch)
    entered, finish, released = threading.Event(), threading.Event(), threading.Event()
    failures = []
    def slow_start():
        entered.set()
        assert finish.wait(3)
        camera.running = True
        return camera.status()
    monkeypatch.setattr(camera, "start", slow_start)
    monkeypatch.setattr(app, "emergency_stop_all", lambda: released.set() or {"enabled": False})
    def select():
        try:
            app._select_camera_input("0")
        except Exception as error:
            failures.append(error)
    selection = threading.Thread(target=select)
    selection.start()
    assert entered.wait(1)
    stopped = []
    stop = threading.Thread(target=lambda: stopped.append(app.stop_recognition_all()))
    stop.start()
    assert released.wait(1)  # 相机还没启动完，输出已经先松开。
    finish.set()
    selection.join(3)
    stop.join(3)
    assert not selection.is_alive() and not stop.is_alive()
    assert len(failures) == 1 and isinstance(failures[0], CameraUnavailable)
    assert not camera.running
    assert not app.INPUT_BRIDGE.status()["body_enabled"]
    assert stopped[0]["output"]["enabled"] is False
    assert not stopped[0]["voice"]["connected"]


@pytest.mark.parametrize("stage", ["headers", "body"])
@pytest.mark.parametrize("error", [BrokenPipeError, ConnectionResetError, ConnectionAbortedError])
def test_disconnected_response_does_not_attempt_a_second_response(application, stage, error):
    request = object.__new__(application.AdminHandler)
    request.send_response = lambda status: None
    request.send_header = lambda *args: None
    request.close_connection = False
    def disconnected(*args):
        raise error("client disconnected")
    request.end_headers = disconnected if stage == "headers" else lambda: None
    request.wfile = type("Writer", (), {"write": disconnected})() if stage == "body" else io.BytesIO()
    request._send_json({"ok": True})
    assert request.close_connection

def test_switch_waits_for_capture_release_before_allowing_restart(monkeypatch):
    from test_control_coordination import FakeCapture, FakeDetector, _camera, _shorten_worker_joins
    capture, detector = FakeCapture(), FakeDetector()
    camera = _camera(monkeypatch, capture, detector)
    try:
        camera.start()
        assert capture.entered.wait(1)
        _shorten_worker_joins(camera, monkeypatch)
        timer = threading.Timer(.15, capture.finish.set)
        timer.start()
        camera.stop_and_wait(timeout_s=1)
        timer.join(1)
        assert camera._session is None
        assert capture.release_count == 1
        assert detector.close_count == 1
    finally:
        capture.finish.set()
        camera.stop()

def test_kinect_accepts_first_frame_after_thirty_seconds(monkeypatch):
    from types import SimpleNamespace
    from motioncontrol import kinect_camera
    clock = [0.0]
    camera = object.__new__(kinect_camera.KinectCapture)
    camera._closed = False
    camera.last_error = None
    camera._sequence = camera._read_sequence = 0
    camera._reader = SimpleNamespace(is_alive=lambda: True)
    frame = object()
    class Waiting:
        calls = 0
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
        def wait(self, _seconds):
            self.calls += 1
            clock[0] = 30.0 if self.calls == 1 else 35.0
            if self.calls == 2:
                camera._sequence = 1
                camera._latest = (frame, {"device_id": "test"})
    camera._condition = Waiting()
    monkeypatch.setattr(kinect_camera.time, "monotonic", lambda: clock[0])
    ok, received = camera.read()
    assert ok and received is frame
    assert camera.last_error is None

@pytest.mark.parametrize("reported_ids,accepted,changed", [
    (["kinect2:", "kinect2:chosen", "kinect2:", "kinect2:chosen"], 2, False),
    (["kinect2:chosen", "kinect2:another"], 1, True),
])
def test_kinect_waits_for_serial_but_refuses_a_different_device(reported_ids, accepted, changed):
    import json
    import struct
    import time
    from types import SimpleNamespace
    from motioncontrol.kinect_camera import KinectCapture
    camera = object.__new__(KinectCapture)
    camera.device_id = "kinect2:chosen"
    camera._closed = False
    camera.last_error = ""
    camera._sequence = camera._read_sequence = 0
    camera._condition = threading.Condition()
    camera._started = time.monotonic()
    pixels = bytes(640 * 360 * 3)
    packets = []
    for device_id in reported_ids:
        metadata = json.dumps({"device_id": device_id, "qpc": time.perf_counter()}).encode()
        packets.extend([struct.pack("<II", len(metadata), len(pixels)), metadata, pixels])
    class Pipe(io.BytesIO):
        def read(self, size):
            result = super().read(size)
            if not result:
                camera._closed = True
            return result
    camera._process = SimpleNamespace(stdout=Pipe(b"".join(packets)), stderr=io.BytesIO(), poll=lambda: 0)
    camera._read_loop()
    assert camera._sequence == accepted
    assert ("设备已改变" in camera.last_error) is changed
