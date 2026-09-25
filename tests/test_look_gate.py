"""上下视角那道绿色闸（lookGate）：跟着人走的时候从第一帧就在，定住以后照样能用。

原来放在参考场景的测试里。参考场景删了，这两条测的是内核自己的闸，搬到这里。
"""

from motioncontrol.control_kernel import ControlKernel
from conftest import apply_layout


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


def _prepare_head(kernel):
    """Make the delegated v0.9.3 HeadController ready without touching kernel.head snapshots.

    These zone tests are not head-estimator tests.  v0.9.3 owns calibration
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



def test_a_frozen_gate_enables_right_wrist_vertical_but_head_only_drives_x(monkeypatch):
    output = FakeOutput()
    kernel = ControlKernel(output)
    try:
        clock = [0.0]
        monkeypatch.setattr("motioncontrol.control_kernel.time.monotonic", lambda: clock[0])
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
        apply_layout(kernel, layout)
        _prepare_head(kernel)
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


def test_the_following_gate_is_there_from_the_first_frame_and_arms(monkeypatch):
    output = FakeOutput()
    kernel = ControlKernel(output)
    try:
        clock = [0.0]
        monkeypatch.setattr("motioncontrol.control_kernel.time.monotonic", lambda: clock[0])
        def feed(current_pose, count):
            state = None
            for _ in range(count):
                clock[0] += 0.10
                state = kernel.handle_pose_map("first-run", current_pose, width=640, height=480)
            return state

        _prepare_head(kernel)
        # 上下视角默认是关的（它和手控鼠标抢同一只右手）。这条钉的是"开着的时候
        # 首次启动也有一个能用的临时闸"，所以先把它打开。
        kernel.configure_head(vertical_look_source="hand")
        base = pose()
        state = feed(base, 1)
        assert state["zones_frozen"] is False
        assert {"leftHand", "rightHand", "leftFoot", "rightFoot", "headJump", "lookGate"}.issubset(state["zones"])
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
