"""Regression tests for the reduced head-control-v4.4 gated-pitch path."""

from __future__ import annotations

import math
import time

import pytest

from control_kernel import ControlKernel, MP_NAMES
from head_test_support import hold_head, turn_head
from head_control import (
    CENTER_MIN_COLLECTION_S,
    CENTER_MIN_SAMPLES,
    CENTER_TARGET_SAMPLES,
    HEAD_SIGNAL_VERSION,
    HeadController,
    HeadEstimate,
    HeadPoseEstimator,
    OneEuro,
    _robust_center_and_sigma,
)


class NumericEstimator:
    """Deterministic estimator used to test controller semantics, not geometry."""

    pnp_available = True
    pnp_error = ""

    def set_pnp_model(self, *args, **kwargs):
        # v5.1 hands the estimator a calibrated model; these stubs
        # exercise controller semantics, not geometry, so it is a no-op.
        return None

    def reset(self):
        pass

    def estimate(self, pose, width, height, algorithm):
        pose = pose or {}
        if not pose.get("valid", True):
            return HeadEstimate(False, algorithm=algorithm, error="invalid")
        return HeadEstimate(
            True,
            float(pose.get("yaw", 0.0)),
            float(pose.get("pitch", 0.0)),
            float(pose.get("roll", 0.0)),
            float(pose.get("confidence", 1.0)),
            algorithm,
        )


class FakeOutput:
    def __init__(self):
        self.axes = (0.0, 0.0)
        self.history = []
        self.holds = []
        self.buttons = []
        self.sensors = {}
        self.cleared = []

    def apply(self, x, y=0.0):
        self.axes = (x, y)
        self.history.append((x, y))

    def set_holds(self, holds, **kwargs):
        self.holds = list(holds)

    def set_buttons(self, buttons, **kwargs):
        self.buttons = list(buttons)

    def set_sensor_state(self, source, buttons, **kwargs):
        self.sensors[source] = {"buttons": set(buttons), **kwargs}

    def clear_source(self, source):
        self.cleared.append(source)
        self.sensors.pop(source, None)


def point(x, y, z=0.0, score=0.98):
    return {"x": float(x), "y": float(y), "z": float(z), "score": float(score)}


def ratio_pose(nose_x=0.50, nose_y=0.45):
    """Anatomical left appears image-right in canonical unmirrored camera space."""
    pose = {name: point(0.5, 0.5) for name in MP_NAMES}
    pose.update({
        "nose": point(nose_x, nose_y),
        "left_eye_outer": point(0.56, 0.40),
        "right_eye_outer": point(0.44, 0.40),
        "mouth_left": point(0.54, 0.52),
        "mouth_right": point(0.46, 0.52),
        "left_ear": point(0.62, 0.42),
        "right_ear": point(0.38, 0.42),
        "left_shoulder": point(0.60, 0.62),
        "right_shoulder": point(0.40, 0.62),
        "left_hip": point(0.57, 0.82),
        "right_hip": point(0.43, 0.82),
    })
    return pose


def test_signal_version_and_algorithms_are_reduced():
    controller = HeadController()
    state = controller.status()
    assert HEAD_SIGNAL_VERSION == "head-control-v5.1-calibration-compat"
    assert state["available_algorithms"] == ["pnp", "ratio"]
    for obsolete in (
        "raw_pitch_face", "raw_pitch_z", "raw_pitch_fused",
        "pitch_face_weight", "pitch_z_weight", "face_pair",
        "head_recenter_required", "gamma",
    ):
        assert obsolete not in state


def test_ratio_yaw_canonical_sign_and_pitch_direction():
    est = HeadPoseEstimator()
    neutral = est.estimate(ratio_pose(), 640, 480, "ratio")
    # Subject turns right -> nose moves toward subject-right eye, which is
    # image-left in an unmirrored front camera. Canonical right must be +.
    right = est.estimate(ratio_pose(nose_x=0.47), 640, 480, "ratio")
    left = est.estimate(ratio_pose(nose_x=0.53), 640, 480, "ratio")
    down = est.estimate(ratio_pose(nose_y=0.47), 640, 480, "ratio")
    up = est.estimate(ratio_pose(nose_y=0.43), 640, 480, "ratio")
    assert all(x.valid for x in (neutral, right, left, down, up))
    assert right.yaw > neutral.yaw
    assert left.yaw < neutral.yaw
    assert down.pitch > neutral.pitch
    assert up.pitch < neutral.pitch


def _euler_matrix(np, yaw, pitch, roll=0.0):
    y, p, r = map(math.radians, (yaw, pitch, roll))
    rx = np.array([[1, 0, 0], [0, math.cos(p), -math.sin(p)], [0, math.sin(p), math.cos(p)]], dtype=float)
    ry = np.array([[math.cos(y), 0, math.sin(y)], [0, 1, 0], [-math.sin(y), 0, math.cos(y)]], dtype=float)
    rz = np.array([[math.cos(r), -math.sin(r), 0], [math.sin(r), math.cos(r), 0], [0, 0, 1]], dtype=float)
    return rz @ ry @ rx


def _project_pnp_pose(estimator, yaw=0.0, pitch=0.0, roll=0.0, width=640, height=480):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    model = np.asarray(estimator._MODEL, dtype=np.float64)
    R = _euler_matrix(np, yaw, pitch, roll)
    rvec, _ = cv2.Rodrigues(R)
    tvec = np.asarray([[0.0], [0.0], [600.0]], dtype=np.float64)
    focal = float(max(width, height))
    camera = np.asarray([[focal, 0.0, width * 0.5], [0.0, focal, height * 0.5], [0.0, 0.0, 1.0]], dtype=np.float64)
    pts, _ = cv2.projectPoints(model, rvec, tvec, camera, np.zeros((4, 1), dtype=np.float64))
    pts = pts.reshape(-1, 2)
    pose = {name: point(0.5, 0.5) for name in MP_NAMES}
    for name, xy in zip(estimator._PNP_NAMES, pts):
        pose[name] = point(xy[0] / width, xy[1] / height)
    return pose


@pytest.mark.parametrize(
    "yaw,pitch,expected_yaw_sign,expected_pitch_sign",
    [
        (15.0, 0.0, 1, 0),
        (-15.0, 0.0, -1, 0),
        (0.0, 10.0, 0, 1),
        (0.0, -10.0, 0, -1),
    ],
)
def test_pnp_recovers_canonical_direction(yaw, pitch, expected_yaw_sign, expected_pitch_sign):
    est = HeadPoseEstimator()
    if not est.pnp_available:
        pytest.skip(est.pnp_error)
    pose = _project_pnp_pose(est, yaw=yaw, pitch=pitch)
    est.reset()
    result = est.estimate(pose, 640, 480, "pnp")
    assert result.valid, result.error
    assert result.reprojection_error < 0.02
    if expected_yaw_sign:
        assert math.copysign(1.0, result.yaw) == expected_yaw_sign
        assert abs(result.yaw - yaw) < 2.0
    else:
        assert abs(result.yaw) < 2.0
    if expected_pitch_sign:
        assert math.copysign(1.0, result.pitch) == expected_pitch_sign
        assert abs(result.pitch - pitch) < 2.0
    else:
        assert abs(result.pitch) < 2.0



def test_pnp_cold_start_prefers_non_flipped_branch_on_typical_face():
    est = HeadPoseEstimator()
    if not est.pnp_available:
        pytest.skip(est.pnp_error)
    pose = ratio_pose(nose_y=0.45)
    # Make the full sparse PnP geometry look like a conventional front face.
    pose.update({
        "left_eye_outer": point(0.55, 0.40),
        "right_eye_outer": point(0.45, 0.40),
        "mouth_left": point(0.53, 0.50),
        "mouth_right": point(0.47, 0.50),
        "left_ear": point(0.61, 0.43),
        "right_ear": point(0.39, 0.43),
    })
    result = est.estimate(pose, 640, 480, "pnp")
    assert result.valid, result.error
    assert abs(result.roll) < 30.0
    assert abs(result.yaw) < 20.0
    assert abs(result.pitch) < 35.0


def test_pnp_pose_is_stable_under_camera_translation():
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    est = HeadPoseEstimator()
    if not est.pnp_available:
        pytest.skip(est.pnp_error)
    width, height = 640, 480
    model = np.asarray(est._MODEL, dtype=np.float64)
    R = _euler_matrix(np, 12.0, -8.0, 3.0)
    rvec, _ = cv2.Rodrigues(R)
    focal = float(max(width, height))
    camera = np.asarray([[focal, 0.0, width * 0.5], [0.0, focal, height * 0.5], [0.0, 0.0, 1.0]], dtype=np.float64)
    dist = np.zeros((4, 1), dtype=np.float64)
    outputs = []
    for tvec in (
        np.asarray([[0.0], [0.0], [600.0]]),
        np.asarray([[45.0], [-25.0], [600.0]]),
        np.asarray([[-35.0], [30.0], [760.0]]),
    ):
        pts, _ = cv2.projectPoints(model, rvec, tvec, camera, dist)
        pts = pts.reshape(-1, 2)
        pose = {name: point(0.5, 0.5) for name in MP_NAMES}
        for name, xy in zip(est._PNP_NAMES, pts):
            pose[name] = point(xy[0] / width, xy[1] / height)
        est.reset()
        result = est.estimate(pose, width, height, "pnp")
        assert result.valid, result.error
        outputs.append((result.yaw, result.pitch, result.roll))
    for yaw, pitch, roll in outputs:
        assert yaw == pytest.approx(12.0, abs=2.0)
        assert pitch == pytest.approx(-8.0, abs=2.0)
        assert roll == pytest.approx(3.0, abs=2.0)

def test_one_euro_static_signal_does_not_drift():
    f = OneEuro()
    values = [f.apply(7.25, 1.0 + i / 30.0) for i in range(90)]
    assert max(abs(v - 7.25) for v in values) < 1e-9


def test_robust_center_rejects_extreme_outlier():
    center, sigma = _robust_center_and_sigma([0.0, 0.01, -0.01, 0.005, -0.005, 0.0, 4.0])
    assert abs(center) < 0.03
    assert 0.0 <= sigma < 0.05


def test_center_capture_collects_real_jitter_without_stillness_gate(tmp_path):
    c = HeadController(tmp_path / "head.json")
    c.estimator = NumericEstimator()
    c.center_pending = True
    c.start_center(now=10.0, kind="voice")
    # Post-voice prepare frames must never contribute samples.
    assert c.update({"yaw": 2.0, "pitch": -1.0}, 640, 480, now=10.50) == (0.0, 0.0)
    assert c.center_phase == "prepare"
    assert c.center_yaw_samples == []

    # Normal estimator jitter is intentionally kept.  There is no contiguous
    # 0.55 s stillness gate and no reset when the values wobble frame to frame.
    t = 11.02
    for i in range(60):
        yaw = 2.0 + (0.32 if i % 2 else -0.32)
        pitch = -1.0 + (0.24 if i % 3 else -0.24)
        c.update({"yaw": yaw, "pitch": pitch}, 640, 480, now=t)
        t += 0.05
        if not c.calibrating:
            break
    assert c.calibrated
    assert not c.calibrating
    assert c.center_quality in {"优秀", "良好", "可用", "噪声较大"}
    assert abs(c.center_yaw - 2.0) < 0.45
    assert abs(c.center_pitch + 1.0) < 0.35
    assert c.noise_yaw > 0.0 and c.noise_pitch > 0.0
    assert c.effective_deadzone_x >= c.config["deadzone"]
    assert c.effective_deadzone_y >= c.config["deadzone"]


def test_reference_video_phone_cadence_completes_without_stillness_gate(tmp_path):
    """Reference-video default: ~16.7 pose FPS with ordinary wobble must finish."""
    c = HeadController(tmp_path / "head.json")
    c.estimator = NumericEstimator()
    c.start_center(now=5.0, kind="voice")
    dt = 1.0 / 16.74
    t = 5.0 + 1.01
    accepted = 0
    # Model normal neutral wobble plus occasional unusable/gross frames.  The
    # cadence mirrors the phone telemetry seen with the supplied reference clip.
    for i in range(90):
        if i % 17 == 8:
            pose = {"valid": False}
        elif i in (25, 58):
            pose = {"yaw": 18.0, "pitch": 10.0}
        else:
            pose = {
                "yaw": 1.8 + (0.34 if i % 2 else -0.34),
                "pitch": -0.9 + (0.26 if i % 3 else -0.26),
                "confidence": 0.92,
            }
            accepted += 1
        c.update(pose, 720, 1280, now=t)
        t += dt
        if not c.calibrating:
            break
    assert c.calibrated
    assert not c.calibrating
    assert c.center_valid_s == 0.0  # reset after completion
    assert accepted >= CENTER_TARGET_SAMPLES
    assert abs(c.center_yaw - 1.8) < 0.5
    assert abs(c.center_pitch + 0.9) < 0.4


def test_calibration_ignores_gross_turn_without_erasing_good_samples(tmp_path):
    c = HeadController(tmp_path / "head.json")
    c.estimator = NumericEstimator()
    c.start_center(now=1.0)
    t = 2.02
    # Warm up the rolling center with valid neutral samples.
    for i in range(10):
        c.update({"yaw": 1.0 + 0.05 * (i % 2), "pitch": -0.5}, 640, 480, now=t)
        t += 0.05
    kept = len(c.center_yaw_samples)
    # A deliberate large turn is skipped, but already accepted history stays.
    c.update({"yaw": 25.0, "pitch": 12.0}, 640, 480, now=t)
    assert len(c.center_yaw_samples) == kept
    assert c.center_rejected_count == 1
    assert c.calibrating


def test_calibration_low_confidence_frames_do_not_restart_or_erase_history(tmp_path):
    c = HeadController(tmp_path / "head.json")
    c.estimator = NumericEstimator()
    c.start_center(now=1.0)
    t = 2.02
    for i in range(12):
        c.update({"yaw": 0.5, "pitch": -0.2}, 640, 480, now=t)
        t += 0.05
    kept = len(c.center_yaw_samples)
    c.update({"yaw": 0.5, "pitch": -0.2, "confidence": 0.1}, 640, 480, now=t)
    assert len(c.center_yaw_samples) == kept
    assert c.center_invalid_count == 1
    assert c.calibrating


def test_noisy_capture_succeeds_and_converts_noise_into_deadzone(tmp_path):
    c = HeadController(tmp_path / "head.json")
    c.estimator = NumericEstimator()
    c.start_center(now=1.0)
    t = 2.02
    # Deliberately noisy but still far below the gross-turn rejection gate.
    for i in range(70):
        yaw = 3.0 + (1.7 if i % 2 else -1.7)
        pitch = -2.0 + (1.25 if i % 3 else -1.25)
        c.update({"yaw": yaw, "pitch": pitch}, 640, 480, now=t)
        t += 0.05
        if not c.calibrating:
            break
    assert c.calibrated
    assert c.center_quality in {"可用", "噪声较大"}
    assert c.effective_deadzone_x > c.config["deadzone"]
    assert c.effective_deadzone_y > c.config["deadzone"]


def test_wall_timeout_succeeds_if_enough_valid_samples_exist(tmp_path):
    c = HeadController(tmp_path / "head.json")
    c.estimator = NumericEstimator()
    c.start_center(now=1.0)
    # The wall-clock watchdog is allowed to finish a low-FPS/intermittent run
    # as long as robust estimation has enough accepted frames.
    c.center_yaw_samples = [1.0 + 0.01 * (i % 2) for i in range(CENTER_MIN_SAMPLES)]
    c.center_pitch_samples = [-0.5 + 0.01 * (i % 3) for i in range(CENTER_MIN_SAMPLES)]
    c.center_confidence_samples = [0.9] * CENTER_MIN_SAMPLES
    c.timeout_center("timeout")
    assert c.calibrated
    assert not c.calibrating


def test_missing_center_does_not_auto_calibrate_before_player_requests_it(tmp_path):
    c = HeadController(tmp_path / "head.json")
    c.estimator = NumericEstimator()
    assert c.center_pending and not c.calibrated
    for i in range(20):
        assert c.update({"yaw": 0.0, "pitch": 0.0}, 640, 480, now=1.0 + i * 0.03) == (0.0, 0.0)
    assert not c.calibrating
    assert not c.calibrated
    assert c.status()["quality"].startswith("等待校准")


def test_center_timeout_is_finite_and_safe(tmp_path):
    c = HeadController(tmp_path / "head.json")
    c.estimator = NumericEstimator()
    c.center_pending = False
    c.start_center(now=1.0)
    c.timeout_center("timeout")
    assert not c.calibrating
    assert not c.calibrated
    assert c.center_pending
    assert c.output_x == 0.0 and c.output_y == 0.0


def _ready_controller(tmp_path, *, yaw=0.0, pitch=0.0):
    c = HeadController(tmp_path / "head.json")
    c.estimator = NumericEstimator()
    c.center_pending = False
    c.calibrated = True
    c.center_yaw = yaw
    c.center_pitch = pitch
    c.noise_yaw = c.noise_pitch = 0.0
    c._recompute_deadzone()
    c._reset_filters()
    return c


def test_turning_produces_output_and_returning_to_centre_stops(tmp_path):
    """v5.1 reacts to a turn, not to a standing offset.

    v4.4 mapped deflection straight to velocity, so a yaw that appeared from
    one frame to the next produced movement.  v5.1 looks for the turn itself:
    that same instant jump reads as a teleport and yields nothing, while a turn
    spread over a few frames ramps up as expected.
    """
    c = _ready_controller(tmp_path)
    x, _y, now = turn_head(c, 9.0, start_at=1.0)
    assert x > 0

    # Returning physically to neutral must snap out the filter tail and stop.
    turn_head(c, 0.0, start_yaw=9.0, start_at=now)
    x0, y0, _ = hold_head(c, 0.0, start_at=now + 0.3)
    assert x0 == 0.0 and y0 == 0.0
    assert c.output_x == 0.0 and c.output_y == 0.0


def test_pnp_yaw_proxy_is_diagnostic_and_never_rewrites_or_suppresses(tmp_path):
    class ProxyEstimator:
        pnp_available = True
        pnp_error = ""

        def set_pnp_model(self, *args, **kwargs):
            # v5.1 hands the estimator a calibrated model; these stubs
            # exercise controller semantics, not geometry, so it is a no-op.
            return None

        def reset(self):
            pass

        def estimate(self, pose, width, height, algorithm):
            return HeadEstimate(
                True,
                yaw=float(pose["yaw"]),
                pitch=0.0,
                roll=0.0,
                confidence=1.0,
                algorithm="pnp",
                yaw_proxy=float(pose["proxy"]),
            )

    c = _ready_controller(tmp_path)
    c.estimator = ProxyEstimator()
    c.center_yaw_proxy = 0.0
    c.noise_yaw_proxy = 0.0
    # Disagreement is diagnostic only: the proxy must not become another
    # hidden/global invert switch. PnP remains the direction authority.
    # Turned rather than teleported: v5.1 reads the turn, not the offset.
    now = 1.0
    for index in range(1, 7):
        share = index / 6
        x, _ = c.update({"yaw": -30.0 * share, "proxy": 0.20 * share}, 640, 480, now=now)
        now += 1 / 30
    assert x < 0.0
    assert c.status(1.0)["raw_yaw"] < 0.0
    assert c.status(1.0)["control_yaw"] < 0.0
    assert c.status(1.0)["yaw_guard_state"] == "proxy_motion"
    # A proxy-neutral disagreement is diagnostic only; it must not silently
    # erase an otherwise valid PnP signal.
    c._reset_filters()
    now = 2.0
    for index in range(1, 7):
        share = index / 6
        x, _ = c.update({"yaw": 28.0 * share, "proxy": 0.002 * share}, 640, 480, now=now)
        now += 1 / 30
    assert x > 0.0
    assert c.status(2.0)["yaw_guard_state"] == "proxy_neutral"


def test_noise_aware_deadzone_prevents_small_jitter(tmp_path):
    c = _ready_controller(tmp_path)
    c.noise_yaw = 0.8  # deg; deliberately noisy center for PnP
    c.noise_pitch = 0.5
    c._recompute_deadzone()
    assert c.effective_deadzone_x > c.config["deadzone"]
    assert c.effective_deadzone_y > c.config["deadzone"]
    # Small jitter around neutral must produce exact zero.
    for i, yaw in enumerate((0.2, -0.3, 0.35, -0.1, 0.25)):
        assert c.update({"yaw": yaw, "pitch": 0.15}, 640, 480, now=2.0 + i * 0.03) == (0.0, 0.0)


def test_invalid_estimate_immediately_zeroes_output(tmp_path):
    c = _ready_controller(tmp_path)
    # Establish real output first: a standing offset alone produces none under
    # the v5.1 intent model, so there would be nothing to zero.
    x, y, now = turn_head(c, 9.0, start_at=1.0, pitch=5.0)
    assert (x, y) != (0.0, 0.0)
    assert c.update({"valid": False}, 640, 480, now=now) == (0.0, 0.0)
    assert c.output_x == 0.0 and c.output_y == 0.0


def test_algorithm_switch_invalidates_old_center(tmp_path):
    c = _ready_controller(tmp_path)
    c.configure(algorithm="ratio")
    assert c.config["algorithm"] == "ratio"
    assert not c.calibrated
    assert c.center_pending
    assert c.output_x == 0.0 and c.output_y == 0.0


def test_only_explicit_user_invert_changes_axis_sign(tmp_path):
    c = _ready_controller(tmp_path)
    normal, _y, _now = turn_head(c, 9.0, start_at=1.0)
    assert normal > 0
    c._reset_filters()
    c.configure(invert_x=True)
    c.calibrated = True
    c.center_pending = False
    inverted, _y, _now = turn_head(c, 9.0, start_at=2.0)
    assert inverted < 0
    assert "invert_yaw" not in c.status()


def test_mobile_mirror_is_normalized_exactly_once():
    landmarks = [point(0.5, 0.5) for _ in range(33)]
    idx = MP_NAMES.index("left_eye_outer")
    landmarks[idx] = point(0.72, 0.40)
    message = {"coordinates_mirrored": True, "poses": [{"pose": landmarks}]}
    pose = ControlKernel.pose_map_from_message(message)
    assert pose is not None
    assert pose["left_eye_outer"]["x"] == pytest.approx(0.28)
    message["coordinates_mirrored"] = False
    pose2 = ControlKernel.pose_map_from_message(message)
    assert pose2["left_eye_outer"]["x"] == pytest.approx(0.72)


def test_kernel_watchdog_releases_head_and_source(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    output = FakeOutput()
    kernel = ControlKernel(output, watchdog_timeout=0.12)
    try:
        kernel.configure_head(algorithm="ratio")
        kernel.handle_pose_map("mobile_pose:test", ratio_pose())
        assert kernel.status()["active_body_source"] == "mobile_pose:test"
        time.sleep(0.20)
        state = kernel.status()
        assert state["active_body_source"] is None
        assert output.axes == (0.0, 0.0)
    finally:
        kernel.close()


def test_kernel_status_exposes_clean_diagnostics_only(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    output = FakeOutput()
    kernel = ControlKernel(output)
    try:
        kernel.configure_head(algorithm="ratio")
        kernel.handle_pose_map("test", ratio_pose())
        head = kernel.status()["head"]
        for key in (
            "raw_yaw", "raw_pitch", "raw_roll", "filtered_yaw", "filtered_pitch",
            "normalized_x", "normalized_y", "output_x", "output_y", "confidence",
        ):
            assert key in head
        for obsolete in (
            "raw_pitch_face", "raw_pitch_z", "raw_pitch_fused", "face_pair",
            "pitch_face_weight", "pitch_z_weight", "head_recenter_required", "gamma",
        ):
            assert obsolete not in head
    finally:
        kernel.close()


def test_saved_center_is_diagnostic_only_and_new_process_requires_fresh_calibration(tmp_path):
    path = tmp_path / "head.json"
    c = _ready_controller(tmp_path, yaw=3.0, pitch=-2.0)
    c.profile_path = path
    c.center_quality = "优秀"
    c._save_profile()
    reloaded = HeadController(path)
    assert reloaded.config["algorithm"] == c.config["algorithm"]
    assert reloaded.center_pending is True
    assert reloaded.calibrated is False
    assert reloaded.update({"yaw": 3.0, "pitch": -2.0}, 640, 480, now=1.0) == (0.0, 0.0)
    assert reloaded.calibrating is False


def test_insufficient_recalibration_does_not_overwrite_existing_good_center(tmp_path):
    c = _ready_controller(tmp_path, yaw=2.0, pitch=-1.0)
    old = (c.center_yaw, c.center_pitch)
    c.center_quality = "优秀"
    c.start_center(now=10.0, kind="voice")
    # A truly failed recalibration is now about insufficient usable frames, not
    # ordinary neutral jitter.  The prior good center must remain active.
    c.center_yaw_samples = [2.1] * (CENTER_MIN_SAMPLES - 1)
    c.center_pitch_samples = [-0.9] * (CENTER_MIN_SAMPLES - 1)
    c.timeout_center("timeout")
    assert c.calibrated is True
    assert (c.center_yaw, c.center_pitch) == old
    assert c.center_quality == "优秀"
    assert "继续使用上一次有效中心" in c.notice
