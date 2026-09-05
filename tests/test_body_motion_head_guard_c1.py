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


def test_raw_fast_path_starts_before_ema_reaches_threshold(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    kernel = ControlKernel(Output())
    try:
        now = 10.0
        kernel._update_body_motion_guard_locked(pose(), now)
        now += 1 / 30
        kernel._update_body_motion_guard_locked(pose(.03), now)
        assert kernel.body_motion_guard_raw >= 2.50
        assert kernel.body_motion_guard_score < 2.50
        assert kernel.body_motion_guard_active is True
    finally:
        kernel.close()


def test_raw_fast_path_ignores_low_raw_motion(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    kernel = ControlKernel(Output())
    try:
        now = 10.0
        kernel._update_body_motion_guard_locked(pose(), now)
        now += 1 / 30
        kernel._update_body_motion_guard_locked(pose(.005), now)
        assert kernel.body_motion_guard_raw < 2.50
        assert kernel.body_motion_guard_active is False
    finally:
        kernel.close()


def test_raw_fast_path_requires_eight_common_velocity_points(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    kernel = ControlKernel(Output())
    try:
        now = 10.0
        kernel._update_body_motion_guard_locked(pose(), now)
        sparse = pose(.03)
        keep = {
            "left_shoulder", "right_shoulder", "left_hip", "right_hip",
            "left_elbow", "right_elbow", "left_wrist",
        }
        sparse = {name: point for name, point in sparse.items() if name in keep}
        now += 1 / 30
        kernel._update_body_motion_guard_locked(sparse, now)
        assert kernel.body_motion_guard_raw >= 2.50
        assert kernel.body_motion_guard_score < 2.50
        assert kernel.body_motion_guard_active is False
    finally:
        kernel.close()


def test_motion_active_still_starts_guard_without_raw_fast_path(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    kernel = ControlKernel(Output())
    try:
        now = 10.0
        kernel._update_body_motion_guard_locked(pose(), now)
        kernel.motion_active.add("squat")
        now += 1 / 30
        kernel._update_body_motion_guard_locked(pose(), now)
        assert kernel.body_motion_guard_raw < 2.50
        assert kernel.body_motion_guard_active is True
    finally:
        kernel.close()


def test_raw_fast_path_does_not_keep_renewing_hold_while_already_active(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    kernel = ControlKernel(Output())
    try:
        now = 10.0
        kernel._update_body_motion_guard_locked(pose(), now)
        now += 1 / 30
        kernel._update_body_motion_guard_locked(pose(.03), now)
        assert kernel.body_motion_guard_active is True
        first_hold = kernel.body_motion_guard_hold_until

        # Another raw spike while already active must not turn the new fast path
        # into a persistence path. Legacy EMA/motion_active own persistence.
        kernel.body_motion_guard_score = 0.0
        kernel.motion_active.clear()
        now += 1 / 30
        kernel._update_body_motion_guard_locked(pose(.045), now)
        assert kernel.body_motion_guard_raw >= 2.50
        assert kernel.body_motion_guard_hold_until == first_hold
    finally:
        kernel.close()
