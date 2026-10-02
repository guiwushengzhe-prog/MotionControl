"""Source ownership, camera lifecycle and sampling-time regression coverage.

Camera tests use real worker threads with deterministic fake devices, so they
exercise shutdown races without requiring OpenCV, MediaPipe or a camera.
"""

from __future__ import annotations

import sys
import threading
import time
from types import SimpleNamespace

import pytest

from motioncontrol.control_kernel import CameraUnavailable, ControlKernel, MP_NAMES, NativeCameraService
from motioncontrol.input_bridge import InputBridge
from test_control_kernel import FakeOutput, FakePeer, pose_map
from test_mobile_input_bridge import pose_frame


class BridgeKernel:
    def __init__(self):
        self.frames = []
        self.cleared = []
        self.active_body_source = None
        self.body_last_at = 0.0

    def handle_pose_message(self, source_id, message, *, return_status=True):
        self.frames.append((source_id, message["sequence"]))
        self.active_body_source = source_id
        self.body_last_at = time.monotonic()
        return {} if return_status else None

    def clear_source(self, source_id):
        self.cleared.append(source_id)
        if self.active_body_source == source_id:
            self.active_body_source = None


@pytest.fixture()
def bridge(monkeypatch):
    made = InputBridge(FakeOutput(), BridgeKernel())
    monkeypatch.setattr(made, "usb_tether_status", lambda: {})
    yield made
    made.close()


def test_explicit_body_stop_rejects_later_phone_frames_until_reenabled(bridge):
    phone = FakePeer()
    bridge._handle_pose(phone, pose_frame("phone", 1))
    bridge.set_body_enabled(False)
    bridge._handle_pose(phone, pose_frame("phone", 2))
    assert bridge.kernel.frames == [("mobile_pose:phone", 1)]
    assert bridge.kernel.active_body_source is None
    assert bridge.status()["mobile_pose_source_id"] is None

    bridge.set_body_enabled(True)
    bridge._handle_pose(phone, pose_frame("phone", 3))
    assert bridge.kernel.frames[-1] == ("mobile_pose:phone", 3)


def test_first_phone_keeps_ownership_until_disconnect(bridge):
    first, second = FakePeer(), FakePeer()
    bridge._handle_pose(first, pose_frame("first", 0))
    bridge._handle_pose(second, pose_frame("second", 0))
    bridge._handle_pose(second, pose_frame("second", 1))
    assert bridge.kernel.frames == [("mobile_pose:first", 0)]
    assert bridge.status()["mobile_pose_source_id"] == "mobile_pose:first"

    bridge.disconnect(first)
    bridge._handle_pose(second, pose_frame("second", 2))
    assert bridge.kernel.frames[-1] == ("mobile_pose:second", 2)
    assert bridge.status()["mobile_pose_source_id"] == "mobile_pose:second"


def test_duplicate_and_backwards_sequences_do_not_reenter_the_kernel(bridge):
    phone = FakePeer()
    for sequence in (5, 5, 4, 6):
        bridge._handle_pose(phone, pose_frame("phone", sequence))
    assert bridge.kernel.frames == [("mobile_pose:phone", 5), ("mobile_pose:phone", 6)]


def test_clearing_a_stale_source_does_not_reset_a_live_peer_sequence(bridge):
    phone = FakePeer()
    bridge._handle_pose(phone, pose_frame("phone", 5))
    # The watchdog releases a stale source but leaves the socket connected.
    with bridge._lock:
        bridge._clear_source_locked("mobile_pose:phone")
    bridge._handle_pose(phone, pose_frame("phone", 0))
    assert bridge.kernel.frames == [("mobile_pose:phone", 5)]
    bridge._handle_pose(phone, pose_frame("phone", 6))
    assert bridge.kernel.frames[-1] == ("mobile_pose:phone", 6)


def test_same_device_reconnect_starts_at_zero_and_survives_old_peer_disconnect(bridge):
    old, new = FakePeer(), FakePeer()
    bridge._handle_pose(old, pose_frame("phone", 9))
    bridge._handle_pose(new, pose_frame("phone", 0))
    bridge.disconnect(old)
    assert bridge.status()["mobile_pose_source_id"] == "mobile_pose:phone"
    bridge._handle_pose(new, pose_frame("phone", 1))
    assert bridge.kernel.frames == [
        ("mobile_pose:phone", 9), ("mobile_pose:phone", 0), ("mobile_pose:phone", 1),
    ]


def test_superseded_peer_cannot_reclaim_the_reconnected_device(bridge):
    old, new = FakePeer(), FakePeer()
    bridge._handle_pose(old, pose_frame("phone", 9))
    bridge._handle_pose(new, pose_frame("phone", 0))
    bridge._handle_pose(old, pose_frame("phone", 10))
    bridge._handle_pose(new, pose_frame("phone", 1))
    assert bridge.kernel.frames == [
        ("mobile_pose:phone", 9), ("mobile_pose:phone", 0), ("mobile_pose:phone", 1),
    ]
    assert bridge._source_peers["mobile_pose:phone"] is new


def test_legacy_phone_clock_is_not_compared_to_the_server_wall_clock(bridge, monkeypatch):
    monkeypatch.setattr("motioncontrol.input_bridge.time.time", lambda: 1000.0)
    bridge._handle_pose(FakePeer(), pose_frame("phone", 0))
    assert bridge.kernel.frames == [("mobile_pose:phone", 0)]
    assert bridge.status()["pose_rejected"]["stale"] == 0


def test_stale_phone_frame_is_rejected_without_clock_sync(bridge):
    message = pose_frame("phone", 0)
    message["sent_at_ms"] = message["captured_at_ms"] + 501
    bridge._handle_pose(FakePeer(), message)
    assert bridge.kernel.frames == []
    assert bridge.status()["pose_rejected"]["stale"] == 1


def test_clock_sync_rejects_network_delayed_frames_after_a_fresh_sample(bridge, monkeypatch):
    wall_clock = [1000.0]
    monkeypatch.setattr("motioncontrol.input_bridge.time.time", lambda: wall_clock[0])
    phone = FakePeer()
    bridge.handle_message(phone, {"type": "clock_sync", "client_sent_ms": 1000000})
    fresh = pose_frame("phone", 0)
    fresh.update(captured_at_ms=999990, sent_at_ms=999995)
    bridge._handle_pose(phone, fresh)
    assert bridge.kernel.frames == [("mobile_pose:phone", 0)]
    assert bridge._pose_sessions[phone]["clock_trusted"]

    wall_clock[0] += .6
    delayed = pose_frame("phone", 1)
    delayed.update(captured_at_ms=1000010, sent_at_ms=1000015)
    bridge._handle_pose(phone, delayed)
    assert bridge.kernel.frames == [("mobile_pose:phone", 0)]
    assert bridge.status()["pose_rejected"]["stale"] == 1


def test_clock_resync_allows_clock_changes_and_reestablishes_freshness(bridge, monkeypatch):
    monkeypatch.setattr("motioncontrol.input_bridge.time.time", lambda: 1000.0)
    phone = FakePeer()
    bridge.handle_message(phone, {"type": "clock_sync", "client_sent_ms": 1000000})
    fresh = pose_frame("phone", 0)
    fresh.update(captured_at_ms=1000000, sent_at_ms=1000000)
    bridge._handle_pose(phone, fresh)
    assert bridge._pose_sessions[phone]["clock_trusted"]

    bridge.handle_message(phone, {"type": "clock_sync", "client_sent_ms": 1000})
    changed = pose_frame("phone", 1)
    changed.update(captured_at_ms=1000, sent_at_ms=1001)
    bridge._handle_pose(phone, changed)
    assert bridge.kernel.frames[-1] == ("mobile_pose:phone", 1)
    assert not bridge._pose_sessions[phone]["clock_trusted"]

    aligned = pose_frame("phone", 2)
    aligned.update(captured_at_ms=1000000, sent_at_ms=1000000)
    bridge._handle_pose(phone, aligned)
    assert bridge.kernel.frames[-1] == ("mobile_pose:phone", 2)
    assert bridge._pose_sessions[phone]["clock_trusted"]
    assert bridge.status()["pose_rejected"]["stale"] == 0


@pytest.mark.parametrize("via_message", [False, True])
def test_pose_hot_path_skips_status_copy_and_still_updates_control(monkeypatch, via_message):
    output = FakeOutput()
    kernel = ControlKernel(output)
    try:
        kernel.configure_motions([{"id": "hands_up", "enabled": True, "target": "Y", "type": "gamepad"}])

        def unexpected_status(_now):
            pytest.fail("pose ingestion requested the full UI status")

        monkeypatch.setattr(kernel, "status_locked", unexpected_status)
        for index in range(3):
            if via_message:
                # No capture clock on this compatibility-path fixture: retain
                # its three-frame debounce while checking message ingestion.
                message = pose_frame("phone", index)
                message.pop("captured_at_ms")
                message["poses"][0]["pose"] = [
                    {"x": p["x"], "y": p["y"], "z": p.get("z", 0.0), "visibility": p["score"]}
                    for p in (
                        pose_map().get(name, {"x": .5, "y": .5, "score": .95})
                        for name in MP_NAMES
                    )
                ]
                result = kernel.handle_pose_message("mobile_pose:phone", message, return_status=False)
            else:
                result = kernel.handle_pose_map("mobile_pose:phone", pose_map(), return_status=False)
            assert result is None
        assert kernel.latest_pose is not None
        assert "hands_up" in kernel.motion_active
        assert any(item.get("id") == "hands_up" for item in output.holds)
    finally:
        kernel.close()


def test_pose_ingestion_keeps_default_status_return():
    kernel = ControlKernel(FakeOutput())
    try:
        state = kernel.handle_pose_map("camera", pose_map())
        assert state["active_body_source"] == "camera"
        assert state["pose"] is not None
    finally:
        kernel.close()


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "camera workers did not reach the expected state"
        threading.Event().wait(.005)


class CameraKernel:
    def __init__(self):
        self.frames = []
        self.cleared = []
        self.stereo = SimpleNamespace(observe_pc=lambda *args: self.frames.append(args))

    def handle_pose_map(self, source_id, pose, **kwargs):
        self.frames.append((source_id, pose, kwargs))

    def clear_source(self, source_id):
        self.cleared.append(source_id)


class FakeCapture:
    def __init__(self, *, one_frame=False):
        self.one_frame = one_frame
        self.entered = threading.Event()
        self.finish = threading.Event()
        self.release_count = 0

    def read(self):
        self.entered.set()
        if self.one_frame:
            self.one_frame = False
            return True, SimpleNamespace(shape=(480, 640, 3))
        assert self.finish.wait(3), "test did not unblock capture"
        return False, None

    def release(self):
        self.release_count += 1


class FakeDetector:
    def __init__(self, *, blocked=False):
        self.entered = threading.Event()
        self.finish = threading.Event()
        if not blocked:
            self.finish.set()
        self.close_count = 0

    def detect_for_video(self, *_args):
        self.entered.set()
        assert self.finish.wait(3), "test did not unblock inference"
        return SimpleNamespace(pose_landmarks=[], pose_world_landmarks=[])

    def close(self):
        self.close_count += 1


def _camera(monkeypatch, capture, detector):
    cv2 = SimpleNamespace(
        ROTATE_90_CLOCKWISE=0, ROTATE_90_COUNTERCLOCKWISE=1, ROTATE_180=2,
        COLOR_BGR2RGB=3, cvtColor=lambda frame, _code: frame,
    )
    mp = SimpleNamespace(Image=lambda **kwargs: kwargs, ImageFormat=SimpleNamespace(SRGB=1))
    monkeypatch.setitem(sys.modules, "cv2", cv2)
    camera = NativeCameraService(CameraKernel())
    monkeypatch.setattr(camera, "_create_detector", lambda: (mp, detector))
    monkeypatch.setattr(camera, "_select_capture", lambda _cv2: (capture, None))
    monkeypatch.setattr(camera, "_save_backend_cache", lambda *_args: None)
    return camera


def _shorten_worker_joins(camera, monkeypatch):
    for thread in (camera._capture_thread, camera._inference_thread, camera._preview_thread):
        original_join = thread.join
        monkeypatch.setattr(thread, "join", lambda timeout=None, join=original_join: join(.02))


def test_camera_read_failure_closes_resources_and_allows_restart(monkeypatch):
    capture, detector = FakeCapture(), FakeDetector()
    camera = _camera(monkeypatch, capture, detector)
    mp, _ = camera._create_detector()
    replacement = None
    try:
        camera.start()
        assert capture.entered.wait(1)
        capture.finish.set()
        _wait_until(lambda: camera._session is None)
        state = camera.status()
        assert not state["running"]
        assert state["lifecycle"] == "failed"
        assert state["last_error"]
        assert capture.release_count == detector.close_count == 1

        replacement, new_detector = FakeCapture(), FakeDetector()
        monkeypatch.setattr(camera, "_select_capture", lambda _cv2: (replacement, None))
        monkeypatch.setattr(camera, "_create_detector", lambda: (mp, new_detector))
        camera.start()
        assert replacement.entered.wait(1)
        assert camera.status()["running"]
        replacement.finish.set()
        _wait_until(lambda: camera._session is None)
        assert replacement.release_count == new_detector.close_count == 1
    finally:
        capture.finish.set()
        if replacement is not None:
            replacement.finish.set()
        camera.stop()


def test_camera_submits_capture_time_without_building_full_status(monkeypatch):
    capture, detector = FakeCapture(one_frame=True), FakeDetector()
    camera = _camera(monkeypatch, capture, detector)
    try:
        camera.start()
        _wait_until(lambda: bool(camera.kernel.frames))
        source_id, _pose, kwargs = camera.kernel.frames[0]
        assert source_id == "computer_camera"
        assert kwargs["return_status"] is False
        assert kwargs["sample_at"] == camera._latest_capture_at
        assert kwargs["sample_at"] <= time.monotonic()
    finally:
        capture.finish.set()
        camera.stop()


def test_camera_inference_failure_automatically_releases_resources(monkeypatch):
    capture, detector = FakeCapture(one_frame=True), FakeDetector()

    def failed_inference(*_args):
        detector.entered.set()
        raise RuntimeError("inference test failure")

    monkeypatch.setattr(detector, "detect_for_video", failed_inference)
    camera = _camera(monkeypatch, capture, detector)
    try:
        camera.start()
        assert detector.entered.wait(1)
        capture.finish.set()
        _wait_until(lambda: camera._session is None)
        assert camera.status()["lifecycle"] == "failed"
        assert camera.status()["last_error"] == "inference test failure"
        assert not camera.status()["running"]
        assert capture.release_count == detector.close_count == 1
        assert camera.kernel.frames == []
    finally:
        capture.finish.set()
        camera.stop()


@pytest.mark.parametrize("blocked_stage", ["capture", "inference"])
def test_camera_stop_blocks_restart_and_discards_old_session_results(monkeypatch, blocked_stage):
    capture = FakeCapture(one_frame=blocked_stage == "inference")
    detector = FakeDetector(blocked=blocked_stage == "inference")
    camera = _camera(monkeypatch, capture, detector)
    try:
        camera.start()
        assert capture.entered.wait(1)
        if blocked_stage == "inference":
            assert detector.entered.wait(1)
        session = camera._session
        _shorten_worker_joins(camera, monkeypatch)
        state = camera.stop()
        assert state["lifecycle"] == "stopping"
        assert not state["running"]
        assert camera._session is session
        assert capture.release_count == detector.close_count == 0
        with pytest.raises(CameraUnavailable):
            camera.start()
        assert camera._session is session

        capture.finish.set()
        detector.finish.set()
        _wait_until(lambda: camera._session is None)
        assert camera.kernel.frames == [], "a stopped session committed an in-flight result"
        assert capture.release_count == detector.close_count == 1
        assert camera.status()["lifecycle"] == "stopped"
    finally:
        capture.finish.set()
        detector.finish.set()
        camera.stop()


def test_timestamped_motion_debounce_keeps_the_same_duration_at_different_fps(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr("motioncontrol.control_kernel.time.monotonic", lambda: clock[0])
    transitions = []
    for fps in (15, 30, 60):
        kernel = ControlKernel(FakeOutput())
        try:
            on_at = off_at = None
            for index in range(fps + 1):
                clock[0] = 10.0 + index / fps
                kernel.handle_pose_map("camera", pose_map(), sample_at=clock[0], return_status=False)
                if on_at is None and "hands_up" in kernel.motion_active:
                    on_at = index / fps
            for index in range(fps // 2 + 1):
                clock[0] = 11.0 + index / fps
                kernel.handle_pose_map("camera", pose_map(hands_up=False), sample_at=clock[0], return_status=False)
                if off_at is None and "hands_up" not in kernel.motion_active:
                    off_at = index / fps
            assert on_at is not None and off_at is not None
            assert 2 / 30 - 1e-6 <= on_at <= 2 / 30 + 1 / fps + 1e-6
            assert 3 / 30 - 1e-6 <= off_at <= 3 / 30 + 1 / fps + 1e-6
            transitions.append((on_at, off_at))
        finally:
            kernel.close()
    for position in (0, 1):
        values = [row[position] for row in transitions]
        assert max(values) - min(values) <= 1 / 15 + 1e-6


def test_legacy_frames_still_require_three_samples_for_motion():
    kernel = ControlKernel(FakeOutput())
    try:
        for _ in range(2):
            kernel.handle_pose_map("camera", pose_map(), return_status=False)
            assert "hands_up" not in kernel.motion_active
        kernel.handle_pose_map("camera", pose_map(), return_status=False)
        assert "hands_up" in kernel.motion_active
    finally:
        kernel.close()


@pytest.mark.parametrize("kind", ["motion", "pose"])
def test_duplicate_capture_times_do_not_accumulate_debounce(monkeypatch, kind):
    clock = [10.0]
    monkeypatch.setattr("motioncontrol.control_kernel.time.monotonic", lambda: clock[0])
    kernel = ControlKernel(FakeOutput())
    decisions = []
    try:
        debounce = kernel._set_motion_debounced if kind == "motion" else kernel._set_pose_debounced
        ident = "calf_back" if kind == "motion" else "synthetic_pose"
        # Isolate the shared debounce boundary from the raw geometric rule.
        monkeypatch.setattr(kernel, "_process_pose_locked", lambda pose, _now:
                            decisions.append(debounce(ident, bool(pose), 3, 4)))
        for _ in range(20):
            kernel.handle_pose_map("camera", pose_map(), sample_at=clock[0], return_status=False)
        assert not any(decisions), "repeated sampling time falsely confirmed an action"
        clock[0] += 2 / 30
        kernel.handle_pose_map("camera", pose_map(), sample_at=clock[0], return_status=False)
        assert decisions[-1]

        clock[0] = 11.0
        for _ in range(20):
            kernel.handle_pose_map("camera", None, sample_at=clock[0], return_status=False)
            assert decisions[-1], "repeated sampling time falsely released an action"
        clock[0] += 3 / 30
        kernel.handle_pose_map("camera", None, sample_at=clock[0], return_status=False)
        assert not decisions[-1]
    finally:
        kernel.close()


def test_timestamped_zone_following_has_equal_response_at_15_30_60_fps(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr("motioncontrol.control_kernel.time.monotonic", lambda: clock[0])
    responses = []
    for fps in (15, 30, 60):
        kernel = ControlKernel(FakeOutput())
        try:
            clock[0] = 10.0
            kernel.handle_pose_map("camera", pose_map(hands_up=False), sample_at=clock[0], return_status=False)
            before = dict(kernel.zone_rects["leftHand"])
            shifted = pose_map(hands_up=False)
            for point in shifted.values():
                point["x"] += .12
            # One fifteenth of a second is exactly 1 / 2 / 4 samples. Compare
            # response early enough that a fixed per-frame alpha cannot pass.
            for index in range(1, fps // 15 + 1):
                clock[0] = 10.0 + index / fps
                kernel.handle_pose_map("camera", shifted, sample_at=clock[0], return_status=False)
            after = dict(kernel.zone_rects["leftHand"])
            assert after["x2"] > before["x2"] + .03
            responses.append(after)
        finally:
            kernel.close()
    assert responses[0] == pytest.approx(responses[1], abs=1e-6)
    assert responses[0] == pytest.approx(responses[2], abs=1e-6)
