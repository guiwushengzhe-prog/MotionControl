from control_kernel import ControlKernel


class Output:
    enabled = True

    def __init__(self):
        self.axes = []

    def apply(self, x, y=0.0):
        self.axes.append((float(x), float(y)))

    def set_buttons(self, *args, **kwargs):
        pass

    def set_holds(self, *args, **kwargs):
        pass

    def set_action_holds(self, *args, **kwargs):
        pass

    def clear_source(self, *args, **kwargs):
        pass


class Head:
    calibrating = False
    center_deadline = 0.0
    config = {"sensitivity_y": 46.0, "invert_y": False}

    def __init__(self, x=0.7):
        self.x = x

    def update(self, *args, **kwargs):
        return self.x, 0.0

    def configure(self, **kwargs):
        pass

    def status(self, now=None):
        return {
            "algorithm": "pnp", "calibrated": True,
            "horizontal_calibrated": True, "output_x": self.x,
        }

    def reset_tracking(self):
        pass


def pose(offset=0.0):
    result = {
        "left_shoulder": {"x": 0.40, "y": 0.30, "score": 1.0},
        "right_shoulder": {"x": 0.60, "y": 0.30, "score": 1.0},
        "left_hip": {"x": 0.44, "y": 0.55, "score": 1.0},
        "right_hip": {"x": 0.56, "y": 0.55, "score": 1.0},
    }
    for name, x, y in (
        ("left_elbow", .34, .42), ("right_elbow", .66, .42),
        ("left_wrist", .29, .53), ("right_wrist", .71, .53),
        ("left_knee", .45, .73), ("right_knee", .55, .73),
        ("left_ankle", .44, .92), ("right_ankle", .56, .92),
    ):
        result[name] = {"x": x + offset, "y": y - offset, "score": 1.0}
    return result


def test_strong_body_motion_pauses_horizontal_until_body_and_head_settle(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    output = Output()
    kernel = ControlKernel(output)
    kernel.head_controller = Head(.7)
    try:
        now = 10.0
        kernel._update_body_motion_guard_locked(pose(), now)
        for offset in (.28, -.28, .28):
            now += 1 / 30
            kernel._update_body_motion_guard_locked(pose(offset), now)
        assert kernel.body_motion_guard_active is True
        kernel._update_head_locked(pose(.28), now)
        assert output.axes[-1] == (0.0, 0.0)

        # Merely stopping the limbs is not enough if the selected head
        # algorithm still believes the view is off-center.
        for _ in range(12):
            now += 1 / 30
            kernel._update_body_motion_guard_locked(pose(.28), now)
            kernel._update_head_locked(pose(.28), now)
        assert kernel.body_motion_guard_active is True
        assert output.axes[-1] == (0.0, 0.0)

        kernel.head_controller.x = 0.0
        for _ in range(4):
            now += 1 / 30
            kernel._update_body_motion_guard_locked(pose(.28), now)
            kernel._update_head_locked(pose(.28), now)
        assert kernel.body_motion_guard_active is False
    finally:
        kernel.close()


def test_body_motion_guard_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    output = Output()
    kernel = ControlKernel(output)
    kernel.head_controller = Head(.7)
    try:
        kernel.configure_head(body_motion_guard=False)
        kernel._update_head_locked(pose(), 10.0)
        assert output.axes[-1] == (.7, 0.0)
    finally:
        kernel.close()
