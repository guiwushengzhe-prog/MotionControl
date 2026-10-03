import math

import pytest

from test_responsive_controls import full_head
from test_roll_tilt_control import eyes
from motioncontrol.control_kernel import BODY_MOTION_GUARD_VERSION

from motioncontrol.control_kernel import ControlKernel


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


def _enabled_guard_kernel(output):
    """显式打开保护，避免算法测试依赖新的默认关闭值。"""
    kernel = ControlKernel(output)
    kernel.configure_head(body_motion_guard=True)
    return kernel


# 旧的整段封锁、动作后否决和回正解锁规则已取消；改验实际输出与温和恢复。
def frame(kernel, offset, angle, now):
    value = {**pose(offset), **eyes(angle)}
    kernel._update_body_motion_guard_locked(value, now)
    kernel._update_head_locked(value, now)


@pytest.mark.parametrize("fps", [16, 30, 60, 120])
def test_moving_body_keeps_clear_head_turns_and_immediate_reversal(tmp_path, monkeypatch, fps):
    output = Output()
    kernel = _enabled_guard_kernel(output)
    kernel.head_controller = full_head(tmp_path, monkeypatch)
    try:
        frame(kernel, 0., 12., 10.)
        for i in range(1, fps // 2):
            angle = 12. if i % 2 else -12.
            frame(kernel, .08 * math.sin(i), angle, 10. + i / fps)
            assert output.axes[-1][0] * angle > 0
            assert 1 <= kernel.body_motion_guard_scale <= 1.2
            assert kernel.status()["head"]["horizontal_paused_by_body_motion"] is False
        frame(kernel, 0., 0., 10.6)
        assert output.axes[-1][0] == 0
    finally:
        kernel.close()


@pytest.mark.parametrize("fps", [16, 30, 60])
def test_small_accidental_tilt_is_muted_but_recovers_without_recentering(tmp_path, monkeypatch, fps):
    output = Output()
    kernel = _enabled_guard_kernel(output)
    kernel.head_controller = full_head(tmp_path, monkeypatch)
    try:
        frame(kernel, 0., 0., 10.)
        for i in range(1, fps // 2 + 1):
            frame(kernel, .08 if i % 2 else -.08, 3.6, 10. + i / fps)
        assert output.axes[-1][0] == 0
        assert kernel.body_motion_guard_scale > 1.15
        # 手脚停住，仍保持同样的小幅偏头，不需要回正或等动作标签消失。
        kernel.motion_active.add("squat")
        kernel.pose_active.add("custom1")
        kernel.body_motion_action_risk.add("squat")
        offset = .08 if (fps // 2) % 2 else -.08
        for i in range(1, round(.45 * fps) + 1):
            frame(kernel, offset, 3.6, 10.5 + i / fps)
        assert output.axes[-1][0] > 0
        assert kernel.body_motion_guard_scale < 1.01
    finally:
        kernel.close()


def test_jitter_single_joint_and_head_only_motion_do_not_expand_deadzone(tmp_path, monkeypatch):
    kernel = _enabled_guard_kernel(Output())
    try:
        kernel._update_body_motion_guard_locked(pose(), 10.)
        for i in range(1, 30):
            value = pose(.0005 * math.sin(i))
            value["left_wrist"]["x"] += .3 * math.sin(i)
            value.update(eyes(12. * math.sin(i)))
            kernel._update_body_motion_guard_locked(value, 10. + i / 30)
            assert kernel.body_motion_guard_scale == 1.
    finally:
        kernel.close()


def test_crouch_translation_and_tracking_reset(tmp_path, monkeypatch):
    kernel = _enabled_guard_kernel(Output())
    try:
        kernel._update_body_motion_guard_locked(pose(), 10.)
        moving = pose()
        for point in moving.values():
            point["y"] += .03
        kernel._update_body_motion_guard_locked(moving, 10.04)
        assert kernel.body_motion_guard_scale > 1.
        for point in moving.values():
            point["score"] = .1
        kernel._update_body_motion_guard_locked(moving, 10.08)
        assert kernel.body_motion_guard_scale == 1.
        kernel._update_body_motion_guard_locked(pose(), 10.12)
        assert kernel.body_motion_guard_scale == 1.
        kernel._update_body_motion_guard_locked(pose(.1), 11.)
        assert kernel.body_motion_guard_scale == 1., "中断后不把坐标跳变算作身体运动"
    finally:
        kernel.close()


def test_motion_boost_does_not_write_personal_settings_and_off_restores_baseline(tmp_path, monkeypatch):
    output = Output()
    kernel = _enabled_guard_kernel(output)
    controller = full_head(tmp_path, monkeypatch)
    kernel.head_controller = controller
    try:
        before = controller.profile_path.read_bytes()
        frame(kernel, 0., 12., 10.)
        baseline = output.axes[-1][0]
        frame(kernel, .08, 12., 10.04)
        assert .85 * baseline < output.axes[-1][0] < baseline
        assert controller.profile_path.read_bytes() == before
        assert controller.config["deadzone"] == .1
        kernel.configure_head(body_motion_guard=False)
        frame(kernel, -.08, 12., 10.08)
        assert kernel.body_motion_guard_scale == 1.
        assert output.axes[-1][0] == pytest.approx(baseline, abs=.01)
    finally:
        kernel.close()


def _sample_action_risk_transition(kernel, ident, fps, on_s, off_s, activating=True):
    state = kernel.body_motion_action_risk_debounce[ident]
    if not activating:
        state.update({"active": True, "on_since": 0.0, "off_since": 0.0})
    start = 20.0
    first = None
    for frame in range(1, 20):
        now = start + frame / float(fps)
        if first is None:
            first = now
        active = kernel._set_body_motion_action_risk_timed(
            ident, activating, now, on_s, off_s
        )
        if activating and active:
            return now - first
        if not activating and not active:
            return now - first
    raise AssertionError(f"action risk did not transition: {ident} {fps} FPS")


def test_action_risk_preserves_30fps_motion_debounce_semantics(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    cases = (
        ("march", 1, 2, 0.000, 0.030),
        ("squat", 3, 4, 0.060, 0.095),
        ("cross_knee_elbow", 2, 3, 0.030, 0.060),
    )
    for ident, on_frames, off_frames, on_s, off_s in cases:
        old = _enabled_guard_kernel(Output())
        timed = _enabled_guard_kernel(Output())
        try:
            start = 30.0
            old_on = timed_on = None
            for frame in range(1, 10):
                now = start + frame / 30.0
                if old._set_motion_debounced(ident, True, on_frames, off_frames) and old_on is None:
                    old_on = frame
                if timed._set_body_motion_action_risk_timed(ident, True, now, on_s, off_s) and timed_on is None:
                    timed_on = frame
            assert timed_on == old_on

            old.motion_debounce[ident].update({"active": True, "on": 0, "off": 0})
            timed.body_motion_action_risk_debounce[ident].update(
                {"active": True, "on_since": 0.0, "off_since": 0.0}
            )
            old_off = timed_off = None
            for frame in range(1, 10):
                now = start + 1.0 + frame / 30.0
                if not old._set_motion_debounced(ident, False, on_frames, off_frames) and old_off is None:
                    old_off = frame
                if not timed._set_body_motion_action_risk_timed(ident, False, now, on_s, off_s) and timed_off is None:
                    timed_off = frame
            assert timed_off == old_off
        finally:
            old.close()
            timed.close()


def test_action_risk_time_semantics_do_not_shrink_at_high_fps(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    for fps in (20, 30, 45, 60):
        kernel = _enabled_guard_kernel(Output())
        try:
            on_delay = _sample_action_risk_transition(
                kernel, "squat", fps, 0.060, 0.095, activating=True
            )
            assert 0.060 - 1e-6 <= on_delay <= 0.101
        finally:
            kernel.close()

        kernel = _enabled_guard_kernel(Output())
        try:
            off_delay = _sample_action_risk_transition(
                kernel, "squat", fps, 0.060, 0.095, activating=False
            )
            assert 0.095 - 1e-6 <= off_delay <= 0.151
        finally:
            kernel.close()


def test_clear_body_resets_action_risk_state(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    kernel = _enabled_guard_kernel(Output())
    try:
        kernel.body_motion_action_risk.add("squat")
        kernel.body_motion_action_risk_debounce["squat"].update(
            {"active": True, "on_since": 10.0, "off_since": 11.0}
        )
        kernel._clear_body_outputs_locked()
        assert kernel.body_motion_action_risk == set()
        assert kernel.body_motion_action_risk_debounce["squat"] == {
            "active": False, "on_since": 0.0, "off_since": 0.0
        }
    finally:
        kernel.close()


def test_status_exposes_body_motion_guard_version(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    kernel = _enabled_guard_kernel(Output())
    try:
        state = kernel.status()
        assert state["body_motion_guard_version"] == BODY_MOTION_GUARD_VERSION
        assert state["head"]["body_motion_guard_version"] == BODY_MOTION_GUARD_VERSION
    finally:
        kernel.close()
