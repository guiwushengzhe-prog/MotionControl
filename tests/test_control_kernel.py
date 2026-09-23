import time

from motioncontrol.control_kernel import ControlKernel, MP_NAMES, NativeCameraService, CameraUnavailable
from motioncontrol.input_bridge import InputBridge


class FakeOutput:
    def __init__(self):
        self.buttons = []
        self.holds = []
        self.axes = (0.0, 0.0)
        self.sensors = {}
        self.cleared = []

    def set_buttons(self, buttons, **kwargs):
        self.buttons = list(buttons)

    def set_holds(self, holds, **kwargs):
        self.holds = list(holds)

    def apply(self, x, y):
        self.axes = (x, y)

    def set_sensor_state(self, source, buttons, **kwargs):
        self.sensors[source] = {
            "buttons": set(buttons),
            **kwargs,
        }

    def clear_source(self, source):
        self.cleared.append(source)
        self.sensors.pop(source, None)


class FakePeer:
    def __init__(self):
        self.source_ids = set()
        self.accepted_inputs = 0
        self.messages = []
        self.desktop = False

    def send_json(self, message):
        self.messages.append(message)

    def close(self):
        pass


def pose_map(*, hands_up=True):
    point = {"x": 0.5, "y": 0.5, "z": 0.0, "score": 0.95}
    values = {name: dict(point) for name in (
        "nose", "left_ear", "right_ear", "left_eye", "right_eye",
        "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
        "left_wrist", "right_wrist", "left_hip", "right_hip",
        "left_knee", "right_knee", "left_ankle", "right_ankle",
        "left_heel", "right_heel", "left_foot_index", "right_foot_index",
    )}
    values.update({
        "nose": {**point, "x": 0.5, "y": 0.35},
        "left_ear": {**point, "x": 0.46, "y": 0.36},
        "right_ear": {**point, "x": 0.54, "y": 0.36},
        "left_eye": {**point, "x": 0.47, "y": 0.35},
        "right_eye": {**point, "x": 0.53, "y": 0.35},
        "left_shoulder": {**point, "x": 0.40, "y": 0.50},
        "right_shoulder": {**point, "x": 0.60, "y": 0.50},
        "left_elbow": {**point, "x": 0.33, "y": 0.34},
        "right_elbow": {**point, "x": 0.67, "y": 0.34},
        "left_wrist": {**point, "x": 0.275, "y": 0.18 if hands_up else 0.60},
        "right_wrist": {**point, "x": 0.725, "y": 0.18 if hands_up else 0.60},
        "left_hip": {**point, "x": 0.44, "y": 0.72},
        "right_hip": {**point, "x": 0.56, "y": 0.72},
        "left_knee": {**point, "x": 0.45, "y": 0.84},
        "right_knee": {**point, "x": 0.55, "y": 0.84},
        "left_ankle": {**point, "x": 0.45, "y": 0.96},
        "right_ankle": {**point, "x": 0.55, "y": 0.96},
        "left_heel": {**point, "x": 0.44, "y": 0.97},
        "right_heel": {**point, "x": 0.56, "y": 0.97},
        "left_foot_index": {**point, "x": 0.46, "y": 0.98},
        "right_foot_index": {**point, "x": 0.54, "y": 0.98},
    })
    return values


def pose_frame(device_id="offline-test"):
    values = pose_map()
    landmarks = [
        {"x": values.get(name, {"x": 0.5})["x"],
         "y": values.get(name, {"y": 0.5})["y"],
         "z": values.get(name, {"z": 0.0})["z"],
         "visibility": values.get(name, {"score": 0.95})["score"]}
        for name in MP_NAMES
    ]
    return {
        "type": "pose_frame_v2", "role": "camera", "device_id": device_id,
        "sequence": 0, "captured_at_ms": 1000, "width": 640, "height": 480,
        "camera_facing": "user", "orientation_degrees": 0,
        "preview_mirrored": True, "coordinates_mirrored": False,
        "poses": [{"pose": landmarks}], "hands": [], "inference_ms": 4.0,
    }


def test_phone_pose_reaches_kernel_without_browser_and_triggers_motion():
    output = FakeOutput()
    kernel = ControlKernel(output)
    try:
        kernel.configure_motions([{"id": "hands_up", "enabled": True, "target": "Y", "type": "gamepad"}])
        for _ in range(3):
            state = kernel.handle_pose_map("mobile_pose:offline-test", pose_map())
        assert state["active_body_source"] == "mobile_pose:offline-test"
        assert "hands_up" in state["motions"]
        assert any(item.get("id") == "hands_up" for item in output.holds)
        assert state["pose"] is not None
    finally:
        kernel.close()



def test_stable_pose_snapshot_rejects_single_frame_zone_placement_outlier():
    output = FakeOutput()
    kernel = ControlKernel(output)
    try:
        for index in range(9):
            pose = pose_map(hands_up=False)
            pose["left_ear"] = dict(pose["left_ear"])
            pose["left_ear"]["x"] = 0.92 if index == 8 else 0.46
            kernel.handle_pose_map("mobile_pose:stable", pose)
        stable = kernel.stable_pose_snapshot(window_s=0.90, min_samples=6)
        assert stable is not None
        assert abs(stable["left_ear"]["x"] - 0.46) < 0.01
    finally:
        kernel.close()

def test_sensor_and_disconnect_are_handled_by_kernel_directly():
    output = FakeOutput()
    kernel = ControlKernel(output)
    try:
        source = "mobile_sensor:offline-test:0"
        kernel.handle_sensor(source, {"A"}, right_trigger=0.7, stick_x=0.4, stick_y=-0.2)
        assert output.sensors[source]["buttons"] == {"A"}
        assert output.sensors[source]["right_trigger"] == 0.7
        kernel.clear_source(source)
        assert source in output.cleared
        assert source not in kernel.status()["handheld_sources"]
    finally:
        kernel.close()


def test_runtime_zone_snapshot_preserves_dynamic_rect_geometry_and_pressed_state():
    kernel = ControlKernel(FakeOutput())
    try:
        rect = {"x1": 0.1, "x2": 0.4, "y1": 0.0, "y2": 0.3}
        with kernel._lock:
            kernel.zone_rects["leftHand"] = rect
            kernel.zone_state["leftHand"]["pressed"] = True
        zones = kernel.runtime_zones()
        assert zones["leftHand"] == {"rect": rect, "pressed": True}
        assert zones["leftHandUpper"] == zones["leftHand"]
    finally:
        kernel.close()


def test_runtime_zone_snapshot_preserves_fixed_circle_geometry():
    kernel = ControlKernel(FakeOutput())
    try:
        circle = {"shape": "circle", "cx": 0.7, "cy": 0.4, "r": 0.1}
        with kernel._lock:
            kernel.fixed_zones_enabled = True
            kernel.fixed_zones["rightHand"] = circle
            kernel.zone_state["rightHand"]["pressed"] = True
        assert kernel.runtime_zones()["rightHand"] == {"circle": circle, "pressed": True}
    finally:
        kernel.close()


def test_websocket_input_bridge_forwards_phone_pose_to_kernel_without_ui():
    output = FakeOutput()
    kernel = ControlKernel(output)
    bridge = InputBridge(output, kernel)
    peer = FakePeer()
    try:
        bridge.set_body_mode("phone")
        bridge._handle_pose(peer, pose_frame())
        state = kernel.status()
        assert state["active_body_source"] == "mobile_pose:offline-test"
        assert state["pose"] is not None
        bridge.disconnect(peer)
        assert kernel.status()["active_body_source"] is None
    finally:
        bridge.close()
        kernel.close()


def test_monotonic_watchdog_releases_pose_and_sensor():
    output = FakeOutput()
    kernel = ControlKernel(output, watchdog_timeout=0.30)
    try:
        kernel.handle_pose_map("mobile_pose:watchdog", pose_map())
        kernel.handle_sensor("mobile_sensor:watchdog:0", {"B"})
        time.sleep(0.38)
        state = kernel.status()
        assert state["active_body_source"] is None
        assert state["handheld_sources"] == {}
        assert "mobile_sensor:watchdog:0" in output.cleared
    finally:
        kernel.close()


def test_native_camera_has_explicit_dependency_or_device_smoke():
    output = FakeOutput()
    kernel = ControlKernel(output)
    camera = NativeCameraService(kernel)
    try:
        try:
            camera.start()
        except CameraUnavailable as exc:
            assert "MediaPipe" in str(exc) or "摄像头" in str(exc)
        else:
            camera.stop()
            assert camera.status()["running"] is False
    finally:
        camera.stop()
        kernel.close()
