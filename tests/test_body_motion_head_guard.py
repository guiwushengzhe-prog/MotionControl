import pytest

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


def test_body_motion_action_risk_starts_guard_without_raw_fast_path(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    kernel = ControlKernel(Output())
    try:
        now = 10.0
        kernel._update_body_motion_guard_locked(pose(), now)
        kernel.body_motion_action_risk.add("squat")
        now += 1 / 30
        kernel._update_body_motion_guard_locked(pose(), now)
        assert kernel.body_motion_guard_raw < 2.50
        assert kernel.body_motion_guard_active is True
    finally:
        kernel.close()


def test_motion_active_alone_no_longer_starts_body_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    kernel = ControlKernel(Output())
    try:
        now = 10.0
        kernel._update_body_motion_guard_locked(pose(), now)
        kernel.motion_active.add("squat")
        now += 1 / 30
        kernel._update_body_motion_guard_locked(pose(), now)
        assert kernel.body_motion_guard_raw < 2.50
        assert kernel.body_motion_action_risk == set()
        assert kernel.body_motion_guard_active is False
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


def shifted_pose(dx=0.0, dy=0.0):
    result = pose()
    for point in result.values():
        point["x"] += dx
        point["y"] += dy
    return result


def test_early_limb_pair_suppresses_current_frame_without_latching_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    kernel = ControlKernel(Output())
    try:
        now = 10.0
        base = pose()
        kernel._update_body_motion_guard_locked(base, now)
        moved = pose()
        moved["left_wrist"]["x"] += 0.035
        moved["left_elbow"]["x"] += 0.020
        now += 1 / 30
        kernel._update_body_motion_guard_locked(moved, now)
        assert kernel.body_motion_guard_raw < 2.50
        assert kernel.body_motion_guard_active is False
        assert kernel.body_motion_guard_early_evidence is True
        assert kernel._guard_horizontal_output_locked(0.7, now) == 0.0
    finally:
        kernel.close()


def test_single_joint_spike_does_not_use_early_limb_pair_path(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    kernel = ControlKernel(Output())
    try:
        now = 10.0
        base = pose()
        kernel._update_body_motion_guard_locked(base, now)
        moved = pose()
        moved["left_wrist"]["x"] += 0.040
        now += 1 / 30
        kernel._update_body_motion_guard_locked(moved, now)
        assert kernel.body_motion_guard_raw < 2.50
        assert kernel.body_motion_guard_active is False
        assert kernel.body_motion_guard_early_evidence is False
        assert kernel._guard_horizontal_output_locked(0.7, now) == 0.7
    finally:
        kernel.close()


def test_coherent_vertical_body_translation_suppresses_current_frame_without_latching_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    kernel = ControlKernel(Output())
    try:
        now = 10.0
        kernel._update_body_motion_guard_locked(shifted_pose(), now)
        now += 1 / 30
        kernel._update_body_motion_guard_locked(shifted_pose(dy=.01), now)
        assert kernel.body_motion_guard_raw < 2.50
        assert kernel.body_motion_guard_active is False
        assert kernel.body_motion_guard_early_evidence is True
        assert kernel._guard_horizontal_output_locked(0.7, now) == 0.0
    finally:
        kernel.close()


def test_coherent_horizontal_body_translation_does_not_use_vertical_path(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    kernel = ControlKernel(Output())
    try:
        now = 10.0
        kernel._update_body_motion_guard_locked(shifted_pose(), now)
        now += 1 / 30
        kernel._update_body_motion_guard_locked(shifted_pose(dx=.01), now)
        assert kernel.body_motion_guard_raw < 2.50
        assert kernel.body_motion_guard_active is False
    finally:
        kernel.close()


def test_early_evidence_bridges_short_output_lag_without_persistent_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    kernel = ControlKernel(Output())
    try:
        now = 10.0
        kernel._update_body_motion_guard_locked(pose(), now)
        moved = pose()
        moved["left_wrist"]["x"] += 0.035
        moved["left_elbow"]["x"] += 0.020
        now += 1 / 30
        kernel._update_body_motion_guard_locked(moved, now)
        assert kernel.body_motion_guard_active is False
        assert kernel._guard_horizontal_output_locked(0.7, now) == 0.0

        # Evidence can disappear on the next frame while the short output-only
        # bridge still suppresses delayed head jitter.
        now += 1 / 30
        kernel._update_body_motion_guard_locked(moved, now)
        assert kernel.body_motion_guard_early_evidence is False
        assert kernel.body_motion_guard_active is False
        assert kernel._guard_horizontal_output_locked(0.7, now) == 0.0

        now += 0.07
        assert kernel._guard_horizontal_output_locked(0.7, now) == 0.7
        assert kernel.body_motion_guard_active is False
    finally:
        kernel.close()


def test_early_limb_threshold_keeps_return_like_noise_below_trigger(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    kernel = ControlKernel(Output())
    try:
        now = 10.0
        base = pose()
        kernel._update_body_motion_guard_locked(base, now)
        # Small multi-point displacement stays below the promoted C2.3 limb
        # onset boundary and must not create an output-only transient lease.
        moved = pose()
        moved["left_wrist"]["x"] += 0.018
        moved["left_elbow"]["x"] += 0.004
        now += 1 / 30
        kernel._update_body_motion_guard_locked(moved, now)
        assert kernel.body_motion_guard_active is False
        assert kernel.body_motion_guard_early_evidence is False
        assert kernel._guard_horizontal_output_locked(0.7, now) == 0.7
    finally:
        kernel.close()


def test_coherent_vertical_threshold_promotes_body_translation_only(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    kernel = ControlKernel(Output())
    try:
        now = 10.0
        base = pose()
        kernel._update_body_motion_guard_locked(base, now)
        moved = pose()
        # Translate the torso/legs coherently in Y while preserving local limb
        # geometry. This is the path that recovers squat/onset motion removed by
        # hip-centering, and should remain output-only rather than persistent.
        for name in (
            "left_shoulder", "right_shoulder", "left_hip", "right_hip",
            "left_knee", "right_knee", "left_ankle", "right_ankle",
            "left_elbow", "right_elbow", "left_wrist", "right_wrist",
        ):
            moved[name]["y"] += 0.004
        now += 1 / 30
        kernel._update_body_motion_guard_locked(moved, now)
        assert kernel.body_motion_guard_active is False
        assert kernel.body_motion_guard_early_evidence is True
        assert kernel._guard_horizontal_output_locked(0.7, now) == 0.0
    finally:
        kernel.close()


def test_strong_early_burst_arms_only_two_postburst_veto_frames(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    kernel = ControlKernel(Output())
    try:
        now = 10.0
        current = pose()
        kernel._update_body_motion_guard_locked(current, now)
        # Move only two limb points so the early pair is strong while the
        # fastest-half average remains below the persistent raw threshold.
        for step in range(1, 5):
            moved = pose()
            moved["left_wrist"]["x"] += 0.030 * step
            moved["left_elbow"]["x"] += 0.006 * step
            now += 1 / 30
            kernel._update_body_motion_guard_locked(moved, now)
            assert kernel.body_motion_guard_early_evidence is True
            assert kernel.body_motion_guard_active is False
            current = moved

        # Falling edge after four consecutive evidence frames arms the bounded
        # post-burst veto, but the ordinary 67 ms bridge gets first priority.
        now += 1 / 30
        kernel._update_body_motion_guard_locked(current, now)
        assert kernel.body_motion_guard_early_evidence is False
        assert kernel.body_motion_guard_postburst_budget == 2
        assert kernel._guard_horizontal_output_locked(0.7, now) == 0.0
        assert kernel.body_motion_guard_postburst_budget == 2

        # After the ordinary bridge expires, exactly two non-zero frames can be
        # vetoed. The third passes immediately; there is no blanket tail hold.
        now += 0.04
        assert kernel._guard_horizontal_output_locked(0.7, now) == 0.0
        assert kernel.body_motion_guard_postburst_budget == 1
        now += 0.01
        assert kernel._guard_horizontal_output_locked(0.7, now) == 0.0
        assert kernel.body_motion_guard_postburst_budget == 0
        now += 0.01
        assert kernel._guard_horizontal_output_locked(0.7, now) == 0.7
    finally:
        kernel.close()


def test_short_early_burst_does_not_arm_postburst_veto(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    kernel = ControlKernel(Output())
    try:
        now = 10.0
        current = pose()
        kernel._update_body_motion_guard_locked(current, now)
        for step in range(1, 4):
            moved = pose()
            moved["left_wrist"]["x"] += 0.030 * step
            moved["left_elbow"]["x"] += 0.006 * step
            now += 1 / 30
            kernel._update_body_motion_guard_locked(moved, now)
            current = moved
        now += 1 / 30
        kernel._update_body_motion_guard_locked(current, now)
        assert kernel.body_motion_guard_postburst_budget == 0
        now += 0.08
        assert kernel._guard_horizontal_output_locked(0.7, now) == 0.7
    finally:
        kernel.close()


def test_distal_chain_requires_two_consecutive_supported_frames(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    kernel = ControlKernel(Output())
    try:
        now = 10.0
        base = pose()
        kernel._update_body_motion_guard_locked(base, now)

        first = pose()
        first["left_wrist"]["x"] += 0.012
        first["left_elbow"]["x"] += 0.001
        now += 1 / 30
        kernel._update_body_motion_guard_locked(first, now)
        assert kernel.body_motion_guard_raw < 2.50
        assert kernel.body_motion_guard_active is False
        assert kernel.body_motion_guard_early_evidence is False

        second = pose()
        second["left_wrist"]["x"] += 0.024
        second["left_elbow"]["x"] += 0.002
        now += 1 / 30
        kernel._update_body_motion_guard_locked(second, now)
        assert kernel.body_motion_guard_raw < 2.50
        assert kernel.body_motion_guard_active is False
        assert kernel.body_motion_guard_early_evidence is True
        assert kernel._guard_horizontal_output_locked(0.7, now) == 0.0
    finally:
        kernel.close()


def test_segment_articulation_can_cover_motion_below_distal_threshold(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    kernel = ControlKernel(Output())
    try:
        now = 10.0
        kernel._update_body_motion_guard_locked(pose(), now)
        for step in (1, 2):
            moved = pose()
            moved["left_wrist"]["x"] += 0.008 * step
            moved["left_elbow"]["x"] += 0.001 * step
            now += 1 / 30
            kernel._update_body_motion_guard_locked(moved, now)
        assert kernel.body_motion_guard_raw < 2.50
        assert kernel.body_motion_guard_active is False
        # C2.5 distal evidence remains below threshold, while the orthogonal
        # C2.6 segment-deformation path is allowed to recognize the sustained
        # wrist-versus-elbow articulation.
        assert kernel.body_motion_guard_distal_runs["left_arm"] == 0
        assert kernel.body_motion_guard_segment_runs["left_arm"] >= 2
        assert kernel.body_motion_guard_early_evidence is True
        assert kernel._guard_horizontal_output_locked(0.7, now) == 0.0
    finally:
        kernel.close()


def test_distal_chain_requires_proximal_support(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    kernel = ControlKernel(Output())
    try:
        now = 10.0
        kernel._update_body_motion_guard_locked(pose(), now)
        for step in (1, 2):
            moved = pose()
            moved["left_wrist"]["x"] += 0.012 * step
            now += 1 / 30
            kernel._update_body_motion_guard_locked(moved, now)
        assert kernel.body_motion_guard_active is False
        assert kernel.body_motion_guard_early_evidence is False
        assert kernel._guard_horizontal_output_locked(0.7, now) == 0.7
    finally:
        kernel.close()


def test_rigid_limb_translation_does_not_use_segment_articulation_path(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    kernel = ControlKernel(Output())
    try:
        now = 10.0
        kernel._update_body_motion_guard_locked(pose(), now)
        for step in (1, 2):
            moved = pose()
            # Wrist and elbow translate together, so the forearm segment vector
            # does not deform even though both landmarks have measurable speed.
            moved["left_wrist"]["x"] += 0.008 * step
            moved["left_elbow"]["x"] += 0.008 * step
            now += 1 / 30
            kernel._update_body_motion_guard_locked(moved, now)
        assert kernel.body_motion_guard_active is False
        assert kernel.body_motion_guard_distal_runs["left_arm"] == 0
        assert kernel.body_motion_guard_segment_runs["left_arm"] == 0
        assert kernel.body_motion_guard_early_evidence is False
        assert kernel._guard_horizontal_output_locked(0.7, now) == 0.7
    finally:
        kernel.close()


def _distal_confirmation_delay_at_fps(kernel, fps):
    """Return observed confirmation delay after the first supported sample."""
    base = pose()
    start = 10.0
    kernel._update_body_motion_guard_locked(base, start)
    first_supported_at = None
    # Translate wrist+elbow together at ~1.5 torso-lengths/s. This exercises
    # the C2.5 distal path without using C2.6 segment deformation or the global
    # peak>=2.40 early-pair path.
    for frame in range(1, 12):
        elapsed = frame / float(fps)
        moved = pose()
        dx = 0.375 * elapsed  # torso is 0.25 -> normalized speed ~= 1.5/s
        moved["left_wrist"]["x"] += dx
        moved["left_elbow"]["x"] += dx
        now = start + elapsed
        kernel._update_body_motion_guard_locked(moved, now)
        if first_supported_at is None:
            first_supported_at = now
        if kernel.body_motion_guard_early_evidence:
            return now - first_supported_at
    raise AssertionError(f"distal evidence did not confirm at {fps} FPS")


def test_distal_confirmation_uses_time_not_frame_count_across_fps(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    delays = {}
    for fps in (20, 30, 45, 60):
        kernel = ControlKernel(Output())
        try:
            delays[fps] = _distal_confirmation_delay_at_fps(kernel, fps)
        finally:
            kernel.close()
    # A 30 ms confirmation requirement is quantized by camera sampling. At
    # 20 FPS the next available sample is 50 ms; higher FPS must not collapse
    # the confirmation to one 16-22 ms interval merely because more frames are
    # available.
    assert all(0.030 - 1e-6 <= delay <= 0.051 for delay in delays.values())


def _drive_early_pair_for_frames(kernel, fps, frame_count, start=10.0):
    current = pose()
    kernel._update_body_motion_guard_locked(current, start)
    for frame in range(1, frame_count + 1):
        elapsed = frame / float(fps)
        moved = pose()
        # peak ~=3.0, second ~=0.6 normalized torso lengths/s: enough for the
        # global transient pair but far below persistent fastest-half raw=2.5.
        moved["left_wrist"]["x"] += 0.75 * elapsed
        moved["left_elbow"]["x"] += 0.15 * elapsed
        kernel._update_body_motion_guard_locked(moved, start + elapsed)
        assert kernel.body_motion_guard_early_evidence is True
        assert kernel.body_motion_guard_active is False
        current = moved
    end = start + (frame_count + 1) / float(fps)
    kernel._update_body_motion_guard_locked(current, end)
    assert kernel.body_motion_guard_early_evidence is False
    return end


def test_strong_burst_confirmation_is_time_based_across_fps(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    import math
    for fps in (20, 30, 45, 60):
        kernel = ControlKernel(Output())
        try:
            # ceil(0.13*fps) yields a sampled burst whose first-to-last evidence
            # duration is >=~95 ms at every target FPS.
            frames = int(math.ceil(0.13 * fps))
            _drive_early_pair_for_frames(kernel, fps, frames)
            assert kernel.body_motion_guard_postburst_budget == 2
        finally:
            kernel.close()


def test_four_frame_burst_at_60fps_is_not_mistaken_for_100ms_burst(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    kernel = ControlKernel(Output())
    try:
        _drive_early_pair_for_frames(kernel, 60, 4)
        # Four 60 FPS evidence frames span only 50 ms from first to last. The
        # old frame-count rule incorrectly treated this as equivalent to four
        # 30 FPS frames (~100 ms).
        assert kernel.body_motion_guard_postburst_budget == 0
    finally:
        kernel.close()


def test_guard_recovery_settle_uses_time_not_three_frames(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    for fps in (20, 30, 45, 60):
        kernel = ControlKernel(Output())
        try:
            kernel.body_motion_guard_active = True
            kernel.body_motion_guard_hold_until = 0.0
            kernel.body_motion_guard_score = 0.0
            now = 10.0
            first_zero_at = None
            released_at = None
            for _ in range(10):
                now += 1 / float(fps)
                if first_zero_at is None:
                    first_zero_at = now
                kernel._guard_horizontal_output_locked(0.0, now)
                if not kernel.body_motion_guard_active:
                    released_at = now
                    break
            assert released_at is not None
            elapsed = released_at - first_zero_at
            assert 0.060 - 1e-6 <= elapsed <= 0.101
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
        old = ControlKernel(Output())
        timed = ControlKernel(Output())
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
        kernel = ControlKernel(Output())
        try:
            on_delay = _sample_action_risk_transition(
                kernel, "squat", fps, 0.060, 0.095, activating=True
            )
            assert 0.060 - 1e-6 <= on_delay <= 0.101
        finally:
            kernel.close()

        kernel = ControlKernel(Output())
        try:
            off_delay = _sample_action_risk_transition(
                kernel, "squat", fps, 0.060, 0.095, activating=False
            )
            assert 0.095 - 1e-6 <= off_delay <= 0.151
        finally:
            kernel.close()


def test_clear_body_resets_action_risk_state(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    kernel = ControlKernel(Output())
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
    kernel = ControlKernel(Output())
    try:
        state = kernel.status()
        assert state["body_motion_guard_version"] == "C2.10"
        assert state["head"]["body_motion_guard_version"] == "C2.10"
    finally:
        kernel.close()


def test_output_veto_reports_only_frames_that_actually_block_x(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    kernel = ControlKernel(Output())
    try:
        kernel.body_motion_guard_early_until = 10.2
        assert kernel._guard_horizontal_output_locked(0.5, 10.0) == 0.0
        assert kernel.body_motion_guard_output_blocked is True
        assert kernel.body_motion_guard_veto_reason == "early"

        assert kernel._guard_horizontal_output_locked(0.0, 10.05) == 0.0
        assert kernel.body_motion_guard_output_blocked is False
        assert kernel.body_motion_guard_veto_reason == ""

        kernel.body_motion_guard_early_until = 0.0
        kernel.body_motion_guard_postburst_budget = 1
        kernel.body_motion_guard_postburst_until = 10.5
        assert kernel._guard_horizontal_output_locked(-0.5, 10.3) == 0.0
        assert kernel.body_motion_guard_output_blocked is True
        assert kernel.body_motion_guard_veto_reason == "postburst"

        kernel.body_motion_guard_active = True
        kernel.body_motion_guard_hold_until = 11.0
        assert kernel._guard_horizontal_output_locked(0.5, 10.4) == 0.0
        assert kernel.body_motion_guard_output_blocked is True
        assert kernel.body_motion_guard_veto_reason == "persistent"
    finally:
        kernel.close()


def test_guard_survives_brief_core_quality_drop_then_resets(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    kernel = ControlKernel(Output())
    try:
        kernel._update_body_motion_guard_locked(pose(), 10.0)
        kernel.body_motion_guard_active = True
        kernel.body_motion_guard_hold_until = 10.05
        degraded = pose()
        degraded["left_shoulder"]["score"] = 0.0

        kernel._update_body_motion_guard_locked(degraded, 10.1)
        assert kernel.body_motion_guard_active is True
        assert kernel.body_motion_guard_hold_until == pytest.approx(10.16)

        kernel._update_body_motion_guard_locked(degraded, 10.151)
        assert kernel.body_motion_guard_active is False
        assert kernel.body_motion_guard_last_at == 0.0
    finally:
        kernel.close()
