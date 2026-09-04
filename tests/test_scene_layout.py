import math
from pathlib import Path

import cv2
import numpy as np

from control_kernel import ControlKernel
from scene_layout import SceneLayoutManager
from voice_backend import VoiceService


class FakeOutput:
    def __init__(self):
        self.enabled = True
        self.axes = (0.0, 0.0)
        self.buttons = []
        self.holds = []

    def set_buttons(self, buttons, **kwargs):
        self.buttons = list(buttons)

    def set_holds(self, holds, **kwargs):
        self.holds = list(holds)

    def apply(self, x, y):
        self.axes = (float(x), float(y))

    def clear_source(self, source):
        pass


def pose():
    p = {"x": 0.5, "y": 0.5, "z": 0.0, "score": 0.95}
    out = {
        "nose": {**p, "x": 0.50, "y": 0.28},
        "left_eye": {**p, "x": 0.47, "y": 0.27},
        "right_eye": {**p, "x": 0.53, "y": 0.27},
        "left_ear": {**p, "x": 0.45, "y": 0.28},
        "right_ear": {**p, "x": 0.55, "y": 0.28},
        "left_shoulder": {**p, "x": 0.40, "y": 0.42},
        "right_shoulder": {**p, "x": 0.60, "y": 0.42},
        "left_wrist": {**p, "x": 0.30, "y": 0.50},
        "right_wrist": {**p, "x": 0.70, "y": 0.50},
        "left_hip": {**p, "x": 0.44, "y": 0.68},
        "right_hip": {**p, "x": 0.56, "y": 0.68},
        "left_ankle": {**p, "x": 0.44, "y": 0.92},
        "right_ankle": {**p, "x": 0.56, "y": 0.92},
    }
    return out


def _prepare_v093_head_for_scene_test(kernel):
    """Make the delegated v0.9.3 HeadController ready without touching kernel.head snapshots.

    These scene/Zone tests are not head-estimator tests.  v0.9.3 owns calibration
    state inside ``kernel.head_controller``; ``kernel.head`` is only a status snapshot.
    Mirror the existing v0.9.x head-control test setup and leave the synthetic
    scene pose free to produce zero head output if it lacks full PnP landmarks.
    """
    controller = kernel.head_controller
    controller.center_pending = False
    controller.calibrating = False
    controller.calibrated = True
    controller.center_yaw = 0.0
    controller.center_pitch = 0.0
    controller.noise_yaw = 0.0
    controller.noise_pitch = 0.0
    controller._recompute_deadzone()
    controller._reset_filters()
    kernel.head = controller.status()


def transform_pose(src, matrix, width=640, height=480):
    result = {}
    for name, point in src.items():
        vec = np.array([point["x"] * width, point["y"] * height, 1.0], dtype=np.float64)
        dst = matrix @ vec
        dst /= dst[2]
        result[name] = {**point, "x": float(dst[0] / width), "y": float(dst[1] / height)}
    return result


def textured_frame():
    rng = np.random.default_rng(1234)
    img = np.full((480, 640, 3), 30, dtype=np.uint8)
    for _ in range(180):
        x = int(rng.integers(8, 632)); y = int(rng.integers(8, 472))
        radius = int(rng.integers(2, 7)); value = int(rng.integers(80, 245))
        cv2.circle(img, (x, y), radius, (value, value, value), -1)
    for x in range(40, 640, 80):
        cv2.line(img, (x, 0), (x, 480), (90, 90, 90), 1)
    return img


def test_reference_capture_creates_fixed_gate_and_wrist_center(tmp_path):
    manager = SceneLayoutManager(tmp_path)
    state = manager.capture_reference(textured_frame(), pose(), {})
    assert state["configured"] is True
    assert "lookGate" in state["zones"]
    assert state["vertical_look"]["point"] == "right_wrist"
    assert (tmp_path / "config" / "scene_reference.jpg").is_file()
    assert (tmp_path / "config" / "scene_layout.json").is_file()


def test_manual_rematch_transforms_session_without_overwriting_reference(tmp_path):
    manager = SceneLayoutManager(tmp_path)
    frame = textured_frame()
    p0 = pose()
    manager.capture_reference(frame, p0, {})
    original = manager.reference["zones"]["lookGate"].copy()

    angle = math.radians(3.0)
    scale = 1.03
    tx, ty = 18.0, -10.0
    matrix = np.array([
        [scale * math.cos(angle), -scale * math.sin(angle), tx],
        [scale * math.sin(angle),  scale * math.cos(angle), ty],
        [0.0, 0.0, 1.0],
    ], dtype=np.float64)
    warped = cv2.warpPerspective(frame, matrix, (640, 480))
    p1 = transform_pose(p0, matrix)
    state = manager.rematch(warped, p1)
    assert state["last_result"]["ok"] is True
    assert state["adapted"] is True
    assert manager.reference["zones"]["lookGate"] == original
    assert manager.session["zones"]["lookGate"] != original


def test_rejected_rematch_keeps_existing_session(tmp_path):
    manager = SceneLayoutManager(tmp_path)
    frame = textured_frame()
    manager.capture_reference(frame, pose(), {})
    before = manager.session["zones"].copy()
    bad_pose = pose()
    bad_pose["nose"]["x"] = 0.9
    bad_pose["left_shoulder"]["x"] = 0.75
    bad_pose["right_shoulder"]["x"] = 0.95
    bad_pose["left_hip"]["x"] = 0.78
    bad_pose["right_hip"]["x"] = 0.92
    state = manager.rematch(frame, bad_pose)
    assert state["last_result"]["ok"] is False
    assert manager.session["zones"] == before


def test_fixed_gate_enables_right_wrist_vertical_but_head_only_drives_x(monkeypatch):
    output = FakeOutput()
    kernel = ControlKernel(output)
    try:
        clock = [0.0]
        monkeypatch.setattr("control_kernel.time.monotonic", lambda: clock[0])
        def feed(source, current_pose, count):
            state = None
            for _ in range(count):
                clock[0] += 0.10
                state = kernel.handle_pose_map(source, current_pose, width=640, height=480)
            return state

        layout = {
            "zones": {
                "lookGate": {"shape": "circle", "cx": 0.30, "cy": 0.50, "r": 0.08},
                "leftHandUpper": {"shape": "circle", "cx": 0.1, "cy": 0.1, "r": 0.03},
                "leftHandLower": {"shape": "circle", "cx": 0.1, "cy": 0.2, "r": 0.03},
                "rightHandUpper": {"shape": "circle", "cx": 0.9, "cy": 0.1, "r": 0.03},
                "rightHandLower": {"shape": "circle", "cx": 0.9, "cy": 0.2, "r": 0.03},
                "leftFoot": {"shape": "circle", "cx": 0.1, "cy": 0.9, "r": 0.03},
                "rightFoot": {"shape": "circle", "cx": 0.9, "cy": 0.9, "r": 0.03},
            },
            "vertical_look": {"enabled": True, "gate_zone_id": "lookGate", "point": "right_wrist", "center_x": 0.70, "center_y": 0.50, "range_y": 0.20, "deadzone": 0.05},
        }
        kernel.configure_scene_layout(layout)
        _prepare_v093_head_for_scene_test(kernel)
        neutral = pose()
        neutral["right_wrist"]["y"] = 0.62
        state = feed("test", neutral, 8)
        assert state["head"]["vertical_gate_active"] is True
        assert state["head"]["vertical_wrist_anchor_rel_y"] is not None
        assert state["head"]["output_x"] == 0.0
        neutral["right_wrist"]["y"] = 0.76
        state = feed("test", neutral, 3)
        assert state["head"]["output_y"] > 0
        # Move the left wrist out of the gate; vertical output decays toward zero.
        neutral["left_wrist"]["x"] = 0.05
        state = feed("test", neutral, 10)
        assert state["head"]["vertical_gate_active"] is False
        assert abs(state["head"]["output_y"]) < 1.0
    finally:
        kernel.close()


def test_system_voice_actions_are_validated_without_needing_model(tmp_path):
    items = VoiceService._validate_mappings([
        {"phrase": "截图", "type": "system", "target": "scene.capture_reference"},
        {"phrase": "开始输出", "type": "system", "target": "OUTPUT.START"},
    ])
    assert items[0]["target"] == "SCENE.CAPTURE_REFERENCE"
    assert items[1]["target"] == "OUTPUT.START"


def test_reference_capture_auto_places_exactly_seven_recommended_circles(tmp_path):
    manager = SceneLayoutManager(tmp_path)
    p = pose()
    state = manager.capture_reference(textured_frame(), p, {
        # Deliberately absurd legacy rectangles: v0.8.3 must ignore them.
        "leftHandUpper": {"x1": 0.90, "x2": 0.99, "y1": 0.90, "y2": 0.99},
    })
    zones = state["zones"]
    assert set(zones) == {
        "leftHandUpper", "leftHandLower", "rightHandUpper", "rightHandLower",
        "leftFoot", "rightFoot", "lookGate",
    }
    assert len(zones) == 7
    assert manager.reference["layout_profile"] == "seven-zone-body-recommended-v1"

    # Two hand zones are above the head, two extend outward from the ears.
    assert zones["leftHandUpper"]["cy"] < p["left_ear"]["y"]
    assert zones["rightHandUpper"]["cy"] < p["right_ear"]["y"]
    assert zones["leftHandLower"]["cx"] < p["left_ear"]["x"]
    assert zones["rightHandLower"]["cx"] > p["right_ear"]["x"]

    # Kick targets are outward and lifted from the resting ankles so standing
    # still cannot immediately press LB/RB.
    assert zones["leftFoot"]["cx"] < p["left_ankle"]["x"]
    assert zones["rightFoot"]["cx"] > p["right_ankle"]["x"]
    assert zones["leftFoot"]["cy"] < p["left_ankle"]["y"]
    assert zones["rightFoot"]["cy"] < p["right_ankle"]["y"]

    # lookGate is near the estimated chin on the left side and remains a
    # left-wrist gate for right-wrist vertical view control.
    assert zones["lookGate"]["cx"] < 0.50
    assert p["left_ear"]["y"] < zones["lookGate"]["cy"] < p["left_shoulder"]["y"]
    assert state["vertical_look"]["gate_zone_id"] == "lookGate"
    assert state["vertical_look"]["point"] == "right_wrist"


def test_recommended_layout_uses_actual_image_side_when_pose_is_horizontally_mirrored():
    p = pose()
    for point in p.values():
        point["x"] = 1.0 - point["x"]
    zones, _ = SceneLayoutManager._initial_layout(p, None)
    # Anatomical left is now image-right. "Outward" must still mean away from
    # the head, not hard-coded toward decreasing x.
    assert p["left_ear"]["x"] > p["right_ear"]["x"]
    assert zones["leftHandLower"]["cx"] > p["left_ear"]["x"]
    assert zones["rightHandLower"]["cx"] < p["right_ear"]["x"]
    assert zones["lookGate"]["cx"] > 0.50


def test_recommended_seven_zones_do_not_trigger_at_rest_and_use_intended_limbs():
    output = FakeOutput()
    kernel = ControlKernel(output)
    try:
        base = pose()
        zones, vertical = SceneLayoutManager._initial_layout(base, None)
        kernel.configure_scene_layout({"zones": zones, "vertical_look": vertical})
        _prepare_v093_head_for_scene_test(kernel)
        for _ in range(3):
            state = kernel.handle_pose_map("test", base, width=640, height=480)
        assert state["buttons"] == []
        assert state["head"]["vertical_gate_active"] is False

        cases = [
            ("leftHandUpper", "left_wrist", "Y"),
            ("leftHandLower", "left_wrist", "X"),
            ("rightHandUpper", "right_wrist", "B"),
            ("rightHandLower", "right_wrist", "A"),
            ("leftFoot", "left_ankle", "LB"),
            ("rightFoot", "right_ankle", "RB"),
        ]
        for zone_id, point_name, button in cases:
            p = pose()
            z = zones[zone_id]
            p[point_name]["x"], p[point_name]["y"] = z["cx"], z["cy"]
            # Clear prior zone debounce/state between isolated checks.
            for zs in kernel.zone_state.values():
                zs.update({"inside": 0, "outside": 0, "pressed": False})
            for _ in range(2):
                state = kernel.handle_pose_map("test", p, width=640, height=480)
            assert button in state["buttons"], (zone_id, point_name, state["buttons"])
    finally:
        kernel.close()


def test_first_run_without_saved_scene_exposes_and_arms_provisional_seventh_gate(monkeypatch):
    output = FakeOutput()
    kernel = ControlKernel(output)
    try:
        clock = [0.0]
        monkeypatch.setattr("control_kernel.time.monotonic", lambda: clock[0])
        def feed(current_pose, count):
            state = None
            for _ in range(count):
                clock[0] += 0.10
                state = kernel.handle_pose_map("first-run", current_pose, width=640, height=480)
            return state

        _prepare_v093_head_for_scene_test(kernel)
        base = pose()
        state = feed(base, 1)
        assert state["scene_mode"] == "body_relative_provisional"
        assert set(state["zones"]) == {
            "leftHandUpper", "leftHandLower", "rightHandUpper", "rightHandLower",
            "leftFoot", "rightFoot", "lookGate",
        }
        gate = state["zones"]["lookGate"]["rect"]
        assert gate and gate["x1"] < gate["x2"] and gate["y1"] < gate["y2"]

        armed = pose()
        armed["left_wrist"]["x"] = (gate["x1"] + gate["x2"]) / 2.0
        armed["left_wrist"]["y"] = (gate["y1"] + gate["y2"]) / 2.0
        armed["right_wrist"]["y"] = 0.50
        state = feed(armed, 8)
        assert state["vertical_gate_active"] is True
        assert state["head"]["vertical_wrist_anchor_rel_y"] is not None

        armed["right_wrist"]["y"] = 0.70
        state = feed(armed, 3)
        assert state["head"]["output_y"] > 0.0
    finally:
        kernel.close()
