"""Clean head-control engine for MotionControl 0.9.3.

This module deliberately keeps head control separate from body actions and
output backends.  It provides two estimators for A/B testing:

- ``pnp``: sparse 3D head-pose estimation with OpenCV ``solvePnP``.
- ``ratio``: a scale-free 2D face-ratio estimator that needs no OpenCV.

Both estimators feed the same stable *view velocity* mapper:

    head deflection from the captured neutral center -> camera velocity

Returning the head to neutral therefore stops camera motion without undoing the
view that was already accumulated in the game.  The mapper uses a One Euro
filter, a noise-aware hysteresis deadzone and a slew-rate limiter.  It does not
use the previous pitch-face/z weighted fusion and it never silently swaps
between different face geometries.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


HEAD_SIGNAL_VERSION = "head-control-v4.3-reference-video-tuned"
HEAD_ALGORITHMS = ("pnp", "ratio")

# Center capture is intentionally the only calibration flow.  The calibration
# should measure the estimator's *real* neutral jitter instead of demanding a
# mathematically motionless user.  After a short post-voice settle period we
# collect a forgiving multi-second window, keep ordinary model jitter, reject
# only obvious pose jumps / deliberate head turns, and use robust median/MAD to
# learn both neutral center and the runtime deadzone.
# Reference-video tuned defaults (2026-08-21):
# 720x1280 @ 30 FPS, 12.43 s.  The neutral standing section remained visually
# stable for ~7.6 s.  Phone runtime telemetry in the same workflow was ~16.7
# pose FPS, so calibration is now sample-aware instead of assuming a fixed FPS.
CENTER_PREPARE_S = 1.00
CENTER_MIN_COLLECTION_S = 2.20
CENTER_TARGET_SAMPLES = 32
CENTER_WALL_LIMIT_S = 6.00
CENTER_MIN_SAMPLES = 20
CENTER_MIN_CONFIDENCE = 0.35
CENTER_OUTLIER_WARMUP_SAMPLES = 8
CENTER_ROLLING_WINDOW = 21

# These are deliberately loose rejection gates.  They are not "stillness"
# thresholds.  Normal frame-to-frame MediaPipe/PnP jitter must remain in the
# sample set because it is exactly what calibration needs to measure.
PNP_CALIBRATION_OUTLIER_YAW_DEG = 10.0
PNP_CALIBRATION_OUTLIER_PITCH_DEG = 7.0
PNP_CALIBRATION_REFERENCE_SIGMA_YAW_DEG = 1.8
PNP_CALIBRATION_REFERENCE_SIGMA_PITCH_DEG = 1.5

RATIO_CALIBRATION_OUTLIER_YAW = 0.15
RATIO_CALIBRATION_OUTLIER_PITCH = 0.10
RATIO_CALIBRATION_REFERENCE_SIGMA_YAW = 0.040
RATIO_CALIBRATION_REFERENCE_SIGMA_PITCH = 0.035

# Control defaults.  These are *signal spans* used to normalize deflection to
# roughly +/-1; they are not claimed to be exact physical FOV limits.
PNP_YAW_SPAN_DEG = 18.0
PNP_PITCH_SPAN_DEG = 12.0
RATIO_YAW_SPAN = 0.34
RATIO_PITCH_SPAN = 0.16

DEFAULT_CONFIG = {
    "algorithm": "pnp",
    "enabled": True,
    "invert_x": False,
    "invert_y": False,
    # One user-facing stability zone shared by both axes.  Automatic center
    # noise can only enlarge it, never make it smaller than this value.
    "deadzone": 0.10,
    "sensitivity_x": 58.0,
    "sensitivity_y": 46.0,
}

# Internal shaping; deliberately not exposed as a large tuning panel.
CURVE_GAMMA = 1.55
DEADZONE_RELEASE_RATIO = 0.62
OUTPUT_SLEW_PERCENT_PER_S = 420.0
MAX_PNP_STEP_DEG = 35.0


def _finite(value: Any, default: float = math.nan) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _score(point: dict | None) -> float:
    if not isinstance(point, dict):
        return 0.0
    for key in ("score", "visibility", "presence"):
        if key in point:
            value = _finite(point.get(key), 0.0)
            if math.isfinite(value):
                return value
    return 0.0


def _point_ok(point: dict | None, threshold: float = 0.40) -> bool:
    return bool(
        isinstance(point, dict)
        and _score(point) >= threshold
        and math.isfinite(_finite(point.get("x")))
        and math.isfinite(_finite(point.get("y")))
    )


def _mid(a: dict, b: dict) -> tuple[float, float]:
    return ((_finite(a["x"]) + _finite(b["x"])) * 0.5, (_finite(a["y"]) + _finite(b["y"])) * 0.5)


def _distance_xy(a: dict, b: dict) -> float:
    return math.hypot(_finite(a["x"]) - _finite(b["x"]), _finite(a["y"]) - _finite(b["y"]))


def _median(values: list[float]) -> float:
    clean = sorted(v for v in values if math.isfinite(v))
    if not clean:
        return math.nan
    n = len(clean)
    m = n // 2
    return clean[m] if n % 2 else (clean[m - 1] + clean[m]) * 0.5


def _quantile(values: list[float], q: float) -> float:
    clean = sorted(v for v in values if math.isfinite(v))
    if not clean:
        return math.nan
    if len(clean) == 1:
        return clean[0]
    pos = _clamp(q, 0.0, 1.0) * (len(clean) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return clean[lo]
    frac = pos - lo
    return clean[lo] * (1.0 - frac) + clean[hi] * frac


def _robust_center_and_sigma(values: list[float]) -> tuple[float, float]:
    clean = [v for v in values if math.isfinite(v)]
    med = _median(clean)
    if not math.isfinite(med):
        return math.nan, math.nan

    def spread(xs: list[float], center: float) -> float:
        deviations = [abs(v - center) for v in xs if math.isfinite(v)]
        mad = _median(deviations)
        sigma_mad = max(0.0, 1.4826 * mad) if math.isfinite(mad) else 0.0
        q25 = _quantile(xs, 0.25)
        q75 = _quantile(xs, 0.75)
        sigma_iqr = max(0.0, (q75 - q25) / 1.349) if math.isfinite(q25) and math.isfinite(q75) else 0.0
        # MAD can collapse to zero for quantized/bimodal estimator jitter even
        # when the signal clearly has spread.  IQR is an equally robust fallback.
        return max(sigma_mad, sigma_iqr)

    sigma = spread(clean, med)
    if sigma <= 1e-12:
        return med, 0.0
    kept = [v for v in clean if abs(v - med) <= 3.5 * sigma]
    if len(kept) >= max(5, len(clean) // 2):
        med = _median(kept)
        sigma = spread(kept, med)
    return med, sigma


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


class _LowPass:
    def __init__(self) -> None:
        self.value = math.nan

    def reset(self) -> None:
        self.value = math.nan

    def apply(self, value: float, alpha: float) -> float:
        if not math.isfinite(self.value):
            self.value = value
        else:
            self.value = alpha * value + (1.0 - alpha) * self.value
        return self.value


class OneEuro:
    """Small, dependency-free One Euro filter."""

    def __init__(self, min_cutoff: float = 1.15, beta: float = 0.045, d_cutoff: float = 1.0) -> None:
        self.min_cutoff = float(min_cutoff)
        self.beta = float(beta)
        self.d_cutoff = float(d_cutoff)
        self._x = _LowPass()
        self._dx = _LowPass()
        self._last_raw = math.nan
        self._last_t = 0.0

    @staticmethod
    def _alpha(cutoff: float, dt: float) -> float:
        cutoff = max(1e-4, float(cutoff))
        dt = max(1e-4, float(dt))
        tau = 1.0 / (2.0 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def reset(self) -> None:
        self._x.reset()
        self._dx.reset()
        self._last_raw = math.nan
        self._last_t = 0.0

    def apply(self, value: float, now: float) -> float:
        value = float(value)
        if not math.isfinite(value):
            return math.nan
        if not self._last_t or not math.isfinite(self._last_raw):
            self._last_t = now
            self._last_raw = value
            self._x.value = value
            self._dx.value = 0.0
            return value
        dt = _clamp(now - self._last_t, 1.0 / 240.0, 0.10)
        derivative = (value - self._last_raw) / dt
        edx = self._dx.apply(derivative, self._alpha(self.d_cutoff, dt))
        cutoff = self.min_cutoff + self.beta * abs(edx)
        filtered = self._x.apply(value, self._alpha(cutoff, dt))
        self._last_t = now
        self._last_raw = value
        return filtered


@dataclass
class HeadEstimate:
    valid: bool
    yaw: float = math.nan
    pitch: float = math.nan
    roll: float = math.nan
    confidence: float = 0.0
    algorithm: str = ""
    error: str = ""
    reprojection_error: float = math.nan


class HeadPoseEstimator:
    """A/B estimator: OpenCV PnP or a 2D ratio proxy."""

    _PNP_NAMES = (
        "nose",
        "left_eye_outer",
        "right_eye_outer",
        "mouth_left",
        "mouth_right",
        "left_ear",
        "right_ear",
    )
    _RATIO_NAMES = ("nose", "left_eye_outer", "right_eye_outer", "mouth_left", "mouth_right")

    # Sparse generic face template in the same canonical camera convention as
    # MediaPipe Pose.  The subject faces the camera, so the subject's anatomical
    # LEFT appears on the image RIGHT in an unmirrored camera frame.  Therefore
    # anatomical-left landmarks use +X here.  Y points down, and more-negative Z
    # is closer to the camera; the nose tip protrudes in front of the eye plane.
    #
    # This seemingly small convention matters: swapping left/right in the 3D
    # template is enough to make a mathematically valid solvePnP result produce
    # the wrong game-control yaw direction.
    _MODEL = (
        (0.0, 0.0, -32.0),      # nose tip
        (31.0, -24.0, 0.0),     # subject left eye outer
        (-31.0, -24.0, 0.0),    # subject right eye outer
        (25.0, 27.0, -3.0),     # subject left mouth
        (-25.0, 27.0, -3.0),    # subject right mouth
        (67.0, -4.0, 18.0),     # subject left ear (farther back)
        (-67.0, -4.0, 18.0),    # subject right ear
    )

    def __init__(self) -> None:
        self._rvec = None
        self._tvec = None
        self._last_pnp: tuple[float, float, float] | None = None
        self.pnp_available = self._probe_pnp()
        self.pnp_error = "" if self.pnp_available else "OpenCV/numpy 不可用"

    @staticmethod
    def _probe_pnp() -> bool:
        try:
            import cv2  # noqa: F401
            import numpy  # noqa: F401
            return True
        except Exception:
            return False

    def reset(self) -> None:
        self._rvec = None
        self._tvec = None
        self._last_pnp = None

    @classmethod
    def required_points(cls, algorithm: str) -> tuple[str, ...]:
        return cls._PNP_NAMES if algorithm == "pnp" else cls._RATIO_NAMES

    @classmethod
    def validity(cls, pose: dict[str, dict] | None, algorithm: str) -> tuple[bool, list[str]]:
        if not isinstance(pose, dict):
            return False, ["人体"]
        missing = [name for name in cls.required_points(algorithm) if not _point_ok(pose.get(name), 0.40)]
        return not missing, missing

    def estimate(self, pose: dict[str, dict] | None, width: int, height: int, algorithm: str) -> HeadEstimate:
        algorithm = algorithm if algorithm in HEAD_ALGORITHMS else "pnp"
        ok, missing = self.validity(pose, algorithm)
        if not ok:
            return HeadEstimate(False, algorithm=algorithm, error="缺少关键点：" + ",".join(missing))
        if algorithm == "pnp":
            return self._estimate_pnp(pose or {}, width, height)
        return self._estimate_ratio(pose or {})

    @staticmethod
    def _rotation_to_euler_deg(R) -> tuple[float, float, float]:
        # R = Rz(roll) * Ry(yaw) * Rx(pitch), right-handed camera axes.
        sy = math.sqrt(float(R[0, 0]) ** 2 + float(R[1, 0]) ** 2)
        singular = sy < 1e-6
        if not singular:
            pitch = math.atan2(float(R[2, 1]), float(R[2, 2]))
            yaw = math.atan2(-float(R[2, 0]), sy)
            roll = math.atan2(float(R[1, 0]), float(R[0, 0]))
        else:
            pitch = math.atan2(-float(R[1, 2]), float(R[1, 1]))
            yaw = math.atan2(-float(R[2, 0]), sy)
            roll = 0.0
        return tuple(math.degrees(v) for v in (yaw, pitch, roll))

    def _estimate_pnp(self, pose: dict[str, dict], width: int, height: int) -> HeadEstimate:
        if not self.pnp_available:
            return HeadEstimate(False, algorithm="pnp", error=self.pnp_error or "PnP 不可用")
        try:
            import cv2
            import numpy as np

            width = max(2, int(width))
            height = max(2, int(height))
            image_points = np.asarray(
                [[_finite(pose[name]["x"]) * width, _finite(pose[name]["y"]) * height] for name in self._PNP_NAMES],
                dtype=np.float64,
            )
            model_points = np.asarray(self._MODEL, dtype=np.float64)
            focal = float(max(width, height))
            camera = np.asarray(
                [[focal, 0.0, width * 0.5], [0.0, focal, height * 0.5], [0.0, 0.0, 1.0]],
                dtype=np.float64,
            )
            dist = np.zeros((4, 1), dtype=np.float64)
            use_guess = self._rvec is not None and self._tvec is not None
            # The iterative solver can choose a 180-degree roll branch when
            # started cold from a sparse near-planar face.  SQPnP gives a much
            # more reliable first-frame global solution; subsequent frames are
            # refined iteratively from the previous valid pose for continuity.
            if use_guess:
                ok, rvec, tvec = cv2.solvePnP(
                    model_points, image_points, camera, dist,
                    self._rvec, self._tvec, useExtrinsicGuess=True,
                    flags=cv2.SOLVEPNP_ITERATIVE,
                )
            else:
                first_flag = getattr(cv2, "SOLVEPNP_SQPNP", cv2.SOLVEPNP_EPNP)
                ok, rvec, tvec = cv2.solvePnP(
                    model_points, image_points, camera, dist, flags=first_flag
                )
            if not ok:
                return HeadEstimate(False, algorithm="pnp", error="solvePnP 未收敛")
            R, _ = cv2.Rodrigues(rvec)
            yaw, pitch, roll = self._rotation_to_euler_deg(R)
            if not all(math.isfinite(v) for v in (yaw, pitch, roll)):
                return HeadEstimate(False, algorithm="pnp", error="PnP 输出非有限值")
            if abs(yaw) > 90.0 or abs(pitch) > 70.0 or abs(roll) > 75.0:
                return HeadEstimate(False, algorithm="pnp", error="PnP 姿态超出可信范围")
            if self._last_pnp is not None:
                if max(abs(yaw - self._last_pnp[0]), abs(pitch - self._last_pnp[1])) > MAX_PNP_STEP_DEG:
                    # Do not accept a one-frame branch flip.  The next frame
                    # can still converge using the previous valid guess.
                    return HeadEstimate(False, algorithm="pnp", error="PnP 单帧跳变被拒绝")
            projected, _ = cv2.projectPoints(model_points, rvec, tvec, camera, dist)
            projected = projected.reshape(-1, 2)
            rmse_px = float(np.sqrt(np.mean(np.sum((projected - image_points) ** 2, axis=1))))
            face_px = max(8.0, float(np.linalg.norm(image_points[5] - image_points[6])))
            reprojection = rmse_px / face_px
            if reprojection > 0.22:
                return HeadEstimate(
                    False, algorithm="pnp",
                    error=f"PnP 重投影误差过大：{reprojection:.3f}",
                    reprojection_error=reprojection,
                )

            self._rvec, self._tvec = rvec.copy(), tvec.copy()
            self._last_pnp = (yaw, pitch, roll)
            landmark_conf = sum(_score(pose[name]) for name in self._PNP_NAMES) / len(self._PNP_NAMES)
            geometry_conf = _clamp(1.0 - reprojection / 0.22, 0.20, 1.0)
            confidence = landmark_conf * geometry_conf
            # With the anatomical-left/right model above, positive yaw means
            # subject-right and positive pitch means down.  This is the kernel
            # canonical convention: left/up negative, right/down positive.
            return HeadEstimate(True, yaw, pitch, roll, confidence, "pnp", reprojection_error=reprojection)
        except Exception as exc:
            self.pnp_error = str(exc)
            return HeadEstimate(False, algorithm="pnp", error=f"PnP 失败：{exc}")

    def _estimate_ratio(self, pose: dict[str, dict]) -> HeadEstimate:
        nose = pose["nose"]
        le = pose["left_eye_outer"]
        re = pose["right_eye_outer"]
        lm = pose["mouth_left"]
        rm = pose["mouth_right"]
        eye_width = _distance_xy(le, re)
        eye_mid_x, eye_mid_y = _mid(le, re)
        mouth_mid_x, mouth_mid_y = _mid(lm, rm)
        if eye_width < 0.015 or abs(mouth_mid_y - eye_mid_y) < 0.010:
            return HeadEstimate(False, algorithm="ratio", error="脸部比例尺度过小")
        dl = _distance_xy(nose, le)
        dr = _distance_xy(nose, re)
        if dl < 1e-4 or dr < 1e-4:
            return HeadEstimate(False, algorithm="ratio", error="鼻眼距离无效")
        # Canonical yaw: person-left negative, person-right positive for the
        # MediaPipe anatomical labels.  If a particular camera stack reverses
        # the user's desired direction, the explicit invert_x option is the
        # single supported user override.
        # In an unmirrored front-facing image, the subject's anatomical LEFT
        # eye is on image-right.  Turning to the subject's right moves the nose
        # toward the anatomical right eye, so d_left / d_right grows.
        # Canonical sign: left negative, right positive.
        yaw = math.log((dl + 1e-4) / (dr + 1e-4))
        # Dimensionless vertical geometry.  Translation and camera distance
        # mostly cancel because both numerator and denominator are face-local.
        pitch = (_finite(nose["y"]) - eye_mid_y) / (mouth_mid_y - eye_mid_y)
        # A small roll diagnostic from the eye line; not used for control.
        roll = math.degrees(math.atan2(_finite(re["y"]) - _finite(le["y"]), _finite(re["x"]) - _finite(le["x"])))
        confidence = sum(_score(pose[name]) for name in self._RATIO_NAMES) / len(self._RATIO_NAMES)
        return HeadEstimate(True, yaw, pitch, roll, confidence, "ratio")


class HeadController:
    """Clean center-only head controller."""

    def __init__(self, profile_path: Path | None = None) -> None:
        self.estimator = HeadPoseEstimator()
        self.profile_path = profile_path
        self.config = dict(DEFAULT_CONFIG)
        self.center_yaw = 0.0
        self.center_pitch = 0.0
        self.noise_yaw = 0.0
        self.noise_pitch = 0.0
        self.calibrated = False
        self.calibrating = False
        self.center_pending = True
        self.center_kind = ""
        self.center_started = 0.0
        self.center_prepare_until = 0.0
        self.center_deadline = 0.0
        self.center_phase = ""
        self.center_valid_s = 0.0
        self.center_invalid_count = 0
        self.center_rejected_count = 0
        self.center_observed_count = 0
        self.center_quality = "未校准"
        self.center_yaw_samples: list[float] = []
        self.center_pitch_samples: list[float] = []
        self.center_confidence_samples: list[float] = []
        self.raw = HeadEstimate(False, algorithm=self.config["algorithm"])
        self.filtered_yaw = math.nan
        self.filtered_pitch = math.nan
        self.norm_x = 0.0
        self.norm_y = 0.0
        self.output_x = 0.0
        self.output_y = 0.0
        self.effective_deadzone_x = float(self.config["deadzone"])
        self.effective_deadzone_y = float(self.config["deadzone"])
        self._axis_active_x = False
        self._axis_active_y = False
        self._last_update = 0.0
        self._yaw_filter = OneEuro(1.10, 0.050, 1.0)
        self._pitch_filter = OneEuro(1.05, 0.060, 1.0)
        self.last_error = ""
        self.notice = ""
        self.notice_until = 0.0
        self._load_profile()

    def _span(self) -> tuple[float, float]:
        if self.config["algorithm"] == "pnp":
            return PNP_YAW_SPAN_DEG, PNP_PITCH_SPAN_DEG
        return RATIO_YAW_SPAN, RATIO_PITCH_SPAN

    def _reset_filters(self) -> None:
        self._yaw_filter.reset()
        self._pitch_filter.reset()
        self.filtered_yaw = math.nan
        self.filtered_pitch = math.nan
        self._axis_active_x = False
        self._axis_active_y = False
        self.output_x = self.output_y = 0.0
        self.norm_x = self.norm_y = 0.0
        self._last_update = 0.0

    def reset_tracking(self) -> None:
        self.estimator.reset()
        self._reset_filters()
        self.raw = HeadEstimate(False, algorithm=self.config["algorithm"])
        self.last_error = ""

    def configure(
        self,
        *,
        algorithm: str | None = None,
        deadzone: float | None = None,
        sensitivity_x: float | None = None,
        sensitivity_y: float | None = None,
        enabled: bool | None = None,
        invert_x: bool | None = None,
        invert_y: bool | None = None,
    ) -> None:
        if algorithm is not None:
            algorithm = str(algorithm).lower().strip()
            if algorithm not in HEAD_ALGORITHMS:
                raise ValueError("head algorithm must be pnp or ratio")
            if algorithm != self.config["algorithm"]:
                self.config["algorithm"] = algorithm
                self.calibrated = False
                self.center_pending = True
                self.cancel_center("算法已切换，请重新设置中心")
                self.estimator.reset()
                self._reset_filters()
        if deadzone is not None:
            self.config["deadzone"] = _clamp(deadzone, 0.03, 0.25)
        if sensitivity_x is not None:
            self.config["sensitivity_x"] = _clamp(sensitivity_x, 20.0, 120.0)
        if sensitivity_y is not None:
            self.config["sensitivity_y"] = _clamp(sensitivity_y, 15.0, 100.0)
        if enabled is not None:
            self.config["enabled"] = bool(enabled)
            if not self.config["enabled"]:
                self._reset_filters()
        if invert_x is not None:
            self.config["invert_x"] = bool(invert_x)
        if invert_y is not None:
            self.config["invert_y"] = bool(invert_y)
        self._recompute_deadzone()
        self._save_profile()

    def start_center(self, now: float | None = None, kind: str = "manual") -> None:
        now = time.monotonic() if now is None else now
        self.calibrating = True
        self.center_pending = False
        self.center_kind = str(kind or "manual")
        self.center_started = now
        self.center_prepare_until = now + CENTER_PREPARE_S
        self.center_deadline = now + CENTER_WALL_LIMIT_S
        self.center_phase = "prepare"
        self.center_valid_s = 0.0
        self.center_invalid_count = 0
        self.center_rejected_count = 0
        self.center_observed_count = 0
        self.center_yaw_samples = []
        self.center_pitch_samples = []
        self.center_confidence_samples = []
        self._reset_filters()
        self.notice = "校准已开始：看向游戏屏幕中心，保持自然姿势"
        self.notice_until = now + CENTER_WALL_LIMIT_S

    def timeout_center(self, reason: str = "中心记录超时") -> None:
        """Finish safely at the wall limit; enough valid samples still succeed."""
        if not self.calibrating:
            return
        if len(self.center_yaw_samples) >= CENTER_MIN_SAMPLES:
            self._finish_center(True)
        else:
            self._finish_center(
                False,
                f"校准失败：有效姿态样本不足 {len(self.center_yaw_samples)}/{CENTER_MIN_SAMPLES}",
            )

    def cancel_center(self, reason: str = "中心设置已取消") -> None:
        self.calibrating = False
        self.center_kind = ""
        self.center_phase = ""
        self.center_started = self.center_prepare_until = self.center_deadline = 0.0
        self.center_valid_s = 0.0
        self.center_invalid_count = 0
        self.center_rejected_count = 0
        self.center_observed_count = 0
        self.center_yaw_samples = []
        self.center_pitch_samples = []
        self.center_confidence_samples = []
        self.notice = reason
        self.notice_until = time.monotonic() + 2.5
        self._reset_filters()

    def _calibration_quality_limits(self) -> tuple[float, float]:
        if self.config["algorithm"] == "pnp":
            return (
                PNP_CALIBRATION_REFERENCE_SIGMA_YAW_DEG,
                PNP_CALIBRATION_REFERENCE_SIGMA_PITCH_DEG,
            )
        return (
            RATIO_CALIBRATION_REFERENCE_SIGMA_YAW,
            RATIO_CALIBRATION_REFERENCE_SIGMA_PITCH,
        )

    def _calibration_outlier_limits(self) -> tuple[float, float]:
        if self.config["algorithm"] == "pnp":
            return PNP_CALIBRATION_OUTLIER_YAW_DEG, PNP_CALIBRATION_OUTLIER_PITCH_DEG
        return RATIO_CALIBRATION_OUTLIER_YAW, RATIO_CALIBRATION_OUTLIER_PITCH

    def _is_obvious_calibration_outlier(self, estimate: HeadEstimate) -> bool:
        """Reject only gross movement/jumps; never use this as a stillness gate."""
        if len(self.center_yaw_samples) < CENTER_OUTLIER_WARMUP_SAMPLES:
            return False
        window = max(5, int(CENTER_ROLLING_WINDOW))
        yaw_ref = _median(self.center_yaw_samples[-window:])
        pitch_ref = _median(self.center_pitch_samples[-window:])
        if not (math.isfinite(yaw_ref) and math.isfinite(pitch_ref)):
            return False
        yaw_limit, pitch_limit = self._calibration_outlier_limits()
        return (
            abs(estimate.yaw - yaw_ref) > yaw_limit
            or abs(estimate.pitch - pitch_ref) > pitch_limit
        )

    def _finish_center(self, success: bool, reason: str = "") -> None:
        if success:
            yaw, yaw_sigma = _robust_center_and_sigma(self.center_yaw_samples)
            pitch, pitch_sigma = _robust_center_and_sigma(self.center_pitch_samples)
            ref_yaw_sigma, ref_pitch_sigma = self._calibration_quality_limits()
            if not (math.isfinite(yaw) and math.isfinite(pitch)):
                success = False
                reason = reason or "中心样本无效"
            else:
                # High neutral noise no longer makes calibration fail.  It is
                # valuable information: keep the robust center and let the
                # measured noise enlarge the runtime deadzone.
                self.center_yaw = yaw
                self.center_pitch = pitch
                self.noise_yaw = max(0.0, yaw_sigma if math.isfinite(yaw_sigma) else 0.0)
                self.noise_pitch = max(0.0, pitch_sigma if math.isfinite(pitch_sigma) else 0.0)
                self.calibrated = True
                self.center_pending = False
                ratio_y = self.noise_yaw / max(ref_yaw_sigma, 1e-9)
                ratio_p = self.noise_pitch / max(ref_pitch_sigma, 1e-9)
                worst = max(ratio_y, ratio_p)
                if worst <= 0.35:
                    self.center_quality = "优秀"
                elif worst <= 0.70:
                    self.center_quality = "良好"
                elif worst <= 1.35:
                    self.center_quality = "可用"
                else:
                    self.center_quality = "噪声较大"
                self._recompute_deadzone()
                if self.center_quality == "噪声较大":
                    self.notice = "校准完成 · 噪声较大，已自动扩大稳定区；需要更灵敏可重新校准"
                else:
                    self.notice = f"校准完成 · 质量{self.center_quality}"
                self._save_profile()
        if not success:
            if self.calibrated:
                # A failed *recalibration* must not downgrade or overwrite the
                # already-good center.  Resume the previous center after the
                # attempt and tell the user exactly what happened.
                self.notice = (reason or "重新校准失败") + "；继续使用上一次有效中心"
            else:
                self.center_quality = "失败"
                self.notice = reason or "中心记录失败；保持安全零输出"
                self.center_pending = True
        self.calibrating = False
        self.center_kind = ""
        self.center_phase = ""
        self.center_started = self.center_prepare_until = self.center_deadline = 0.0
        self.center_valid_s = 0.0
        self.center_yaw_samples = []
        self.center_pitch_samples = []
        self.center_confidence_samples = []
        self.notice_until = time.monotonic() + 3.0
        self._reset_filters()

    def _recompute_deadzone(self) -> None:
        span_x, span_y = self._span()
        base = float(self.config["deadzone"])
        auto_x = (3.2 * self.noise_yaw / span_x + 0.015) if span_x > 0 else base
        auto_y = (3.2 * self.noise_pitch / span_y + 0.015) if span_y > 0 else base
        self.effective_deadzone_x = _clamp(max(base, auto_x), 0.03, 0.28)
        self.effective_deadzone_y = _clamp(max(base, auto_y), 0.03, 0.30)

    def _axis_curve(self, norm: float, axis: str) -> float:
        if axis == "x":
            deadzone = self.effective_deadzone_x
            active = self._axis_active_x
        else:
            deadzone = self.effective_deadzone_y
            active = self._axis_active_y
        release = deadzone * DEADZONE_RELEASE_RATIO
        amount = abs(norm)
        if active:
            if amount <= release:
                active = False
                value = 0.0
            else:
                threshold = release
                shaped = _clamp((amount - threshold) / max(1e-6, 1.0 - threshold), 0.0, 1.0)
                value = math.copysign(shaped ** CURVE_GAMMA, norm)
        else:
            if amount < deadzone:
                value = 0.0
            else:
                active = True
                shaped = _clamp((amount - deadzone) / max(1e-6, 1.0 - deadzone), 0.0, 1.0)
                value = math.copysign(shaped ** CURVE_GAMMA, norm)
        if axis == "x":
            self._axis_active_x = active
        else:
            self._axis_active_y = active
        return value

    @staticmethod
    def _slew(current: float, target: float, dt: float) -> float:
        if target == 0.0:
            # Neutral must be exact, not a slow decay that causes cursor drift.
            return 0.0
        step = OUTPUT_SLEW_PERCENT_PER_S * max(0.0, min(0.08, dt))
        delta = _clamp(target - current, -step, step)
        return current + delta

    def update(self, pose: dict[str, dict] | None, width: int, height: int, now: float | None = None) -> tuple[float, float]:
        now = time.monotonic() if now is None else now
        estimate = self.estimator.estimate(pose, width, height, self.config["algorithm"])
        self.raw = estimate
        if not estimate.valid:
            self.last_error = estimate.error
            self._reset_filters()
            if self.calibrating:
                self.center_observed_count += 1
                if now >= self.center_deadline:
                    self.timeout_center("中心记录超时：姿态流中断")
                elif now >= self.center_prepare_until:
                    self.center_phase = "collect"
                    self.center_valid_s = max(0.0, now - self.center_prepare_until)
                    self.center_invalid_count += 1
                    self.notice = "正在采集：这一帧姿态无效，继续自然看向屏幕即可"
            return 0.0, 0.0
        self.last_error = ""

        # A missing center never auto-starts calibration.  The player first
        # moves to the real play position and then clicks or says "开始校准".
        # Calibration is intentionally forgiving: after the short post-voice
        # prepare period, ordinary estimator jitter is collected rather than
        # treated as user movement.  Only low-confidence frames and gross
        # turns/jumps are skipped; accepted history is never reset.
        if self.calibrating:
            self.center_observed_count += 1
            if now >= self.center_deadline:
                self.timeout_center("中心记录超时")
                return 0.0, 0.0
            if now < self.center_prepare_until:
                self.center_phase = "prepare"
                self.notice = "准备校准：看向游戏屏幕中心，自然站/坐，不要看摄像头"
                return 0.0, 0.0

            self.center_phase = "collect"
            self.center_valid_s = max(0.0, now - self.center_prepare_until)
            if estimate.confidence < CENTER_MIN_CONFIDENCE:
                self.center_invalid_count += 1
                self.notice = "正在采集：当前脸部置信度偏低，继续自然正视即可"
                return 0.0, 0.0

            if self._is_obvious_calibration_outlier(estimate):
                self.center_rejected_count += 1
                self.notice = "正在采集：已忽略一次明显转头/跳点，不会重新开始"
                return 0.0, 0.0

            self.center_yaw_samples.append(estimate.yaw)
            self.center_pitch_samples.append(estimate.pitch)
            self.center_confidence_samples.append(estimate.confidence)
            self.notice = "正在记录自然中心：轻微模型抖动是正常的，不需要刻意僵住"
            if (
                self.center_valid_s >= CENTER_MIN_COLLECTION_S
                and len(self.center_yaw_samples) >= CENTER_TARGET_SAMPLES
            ):
                self._finish_center(True)
            return 0.0, 0.0

        if not self.calibrated or not self.config["enabled"]:
            self._reset_filters()
            return 0.0, 0.0

        span_x, span_y = self._span()
        raw_x = _clamp((estimate.yaw - self.center_yaw) / span_x, -1.0, 1.0)
        raw_y = _clamp((estimate.pitch - self.center_pitch) / span_y, -1.0, 1.0)

        # A low-pass filter prevents jitter while the head is moving, but a
        # normal low-pass also keeps a stale tail after the user has physically
        # returned to center.  That tail feels like mouse drift.  When the RAW
        # signal is convincingly back inside the hysteresis release zone, snap
        # that axis to the learned center and reset only that filter.  This keeps
        # moving response smooth while making neutral an immediate exact stop.
        release_x = self.effective_deadzone_x * DEADZONE_RELEASE_RATIO
        release_y = self.effective_deadzone_y * DEADZONE_RELEASE_RATIO
        if abs(raw_x) <= release_x:
            self._yaw_filter.reset()
            filtered_yaw = self._yaw_filter.apply(self.center_yaw, now)
            self._axis_active_x = False
        else:
            filtered_yaw = self._yaw_filter.apply(estimate.yaw, now)
        if abs(raw_y) <= release_y:
            self._pitch_filter.reset()
            filtered_pitch = self._pitch_filter.apply(self.center_pitch, now)
            self._axis_active_y = False
        else:
            filtered_pitch = self._pitch_filter.apply(estimate.pitch, now)

        self.filtered_yaw, self.filtered_pitch = filtered_yaw, filtered_pitch
        x = _clamp((filtered_yaw - self.center_yaw) / span_x, -1.0, 1.0)
        y = _clamp((filtered_pitch - self.center_pitch) / span_y, -1.0, 1.0)
        if self.config["invert_x"]:
            x = -x
        if self.config["invert_y"]:
            y = -y
        self.norm_x, self.norm_y = x, y

        vx = self._axis_curve(x, "x")
        vy = self._axis_curve(y, "y")
        target_x = vx * float(self.config["sensitivity_x"])
        target_y = vy * float(self.config["sensitivity_y"])
        dt = _clamp(now - self._last_update, 0.0, 0.08) if self._last_update else 1.0 / 30.0
        self._last_update = now
        self.output_x = self._slew(self.output_x, target_x, dt)
        self.output_y = self._slew(self.output_y, target_y, dt)
        return self.output_x / 100.0, self.output_y / 100.0

    def status(self, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else now
        if self.calibrating:
            elapsed = max(0.0, now - self.center_started)
            remaining = max(0.0, self.center_deadline - now)
            phase_labels = {
                "prepare": "准备中",
                "collect": "记录自然中心",
            }
            quality = phase_labels.get(self.center_phase, "正在校准")
        elif self.calibrated:
            elapsed = None
            remaining = None
            quality = f"个人中心 · 质量{self.center_quality}"
        else:
            elapsed = None
            remaining = None
            quality = "等待校准（可说“开始校准”）"
        return {
            "signal_version": HEAD_SIGNAL_VERSION,
            "algorithm": self.config["algorithm"],
            "available_algorithms": list(HEAD_ALGORITHMS),
            "pnp_available": bool(self.estimator.pnp_available),
            "pnp_error": self.estimator.pnp_error or None,
            "enabled": bool(self.config["enabled"]),
            "invert_x": bool(self.config["invert_x"]),
            "invert_y": bool(self.config["invert_y"]),
            "deadzone": round(float(self.config["deadzone"]), 4),
            "effective_deadzone_x": round(self.effective_deadzone_x, 4),
            "effective_deadzone_y": round(self.effective_deadzone_y, 4),
            "sensitivity_x": round(float(self.config["sensitivity_x"]), 2),
            "sensitivity_y": round(float(self.config["sensitivity_y"]), 2),
            "calibrated": bool(self.calibrated),
            "calibrating": bool(self.calibrating),
            "center_capture_pending": bool(self.center_pending),
            "center_capture_kind": self.center_kind,
            "center_phase": self.center_phase,
            "center_invalid_count": int(self.center_invalid_count),
            "center_rejected_count": int(self.center_rejected_count),
            "center_observed_count": int(self.center_observed_count),
            "center_quality": self.center_quality,
            "center_elapsed_s": round(elapsed, 2) if elapsed is not None else None,
            "center_remaining_s": round(remaining, 2) if remaining is not None else None,
            "center_valid_s": round(self.center_valid_s, 3),
            "center_required_s": CENTER_MIN_COLLECTION_S,
            "center_target_samples": CENTER_TARGET_SAMPLES,
            "center_min_samples": CENTER_MIN_SAMPLES,
            "center_sample_count": len(self.center_yaw_samples),
            "quality": quality,
            "notice": self.notice if now <= self.notice_until else "",
            "raw_yaw": self.raw.yaw if self.raw.valid and math.isfinite(self.raw.yaw) else None,
            "raw_pitch": self.raw.pitch if self.raw.valid and math.isfinite(self.raw.pitch) else None,
            "raw_roll": self.raw.roll if self.raw.valid and math.isfinite(self.raw.roll) else None,
            "confidence": round(self.raw.confidence, 4) if self.raw.valid else 0.0,
            "reprojection_error": (
                round(self.raw.reprojection_error, 5)
                if self.raw.valid and math.isfinite(self.raw.reprojection_error) else None
            ),
            "estimate_valid": bool(self.raw.valid),
            "estimate_error": self.raw.error or self.last_error or None,
            "filtered_yaw": self.filtered_yaw if math.isfinite(self.filtered_yaw) else None,
            "filtered_pitch": self.filtered_pitch if math.isfinite(self.filtered_pitch) else None,
            "center_yaw": self.center_yaw if self.calibrated else None,
            "center_pitch": self.center_pitch if self.calibrated else None,
            "noise_yaw": round(self.noise_yaw, 6),
            "noise_pitch": round(self.noise_pitch, 6),
            "normalized_x": round(self.norm_x, 4),
            "normalized_y": round(self.norm_y, 4),
            "output_x": round(self.output_x, 3),
            "output_y": round(self.output_y, 3),
            # Compact compatibility aliases used by the existing web status.
            "calibration_notice_text": self.notice if now <= self.notice_until else "",
        }

    def _load_profile(self) -> None:
        if not self.profile_path:
            return
        try:
            payload = json.loads(Path(self.profile_path).read_text(encoding="utf-8"))
            if payload.get("signal_version") != HEAD_SIGNAL_VERSION:
                return
            params = payload.get("params") or {}
            algorithm = str(params.get("algorithm", "pnp"))
            if algorithm not in HEAD_ALGORITHMS:
                return
            self.config.update({
                "algorithm": algorithm,
                "enabled": bool(params.get("enabled", True)),
                "invert_x": bool(params.get("invert_x", False)),
                "invert_y": bool(params.get("invert_y", False)),
                "deadzone": _clamp(params.get("deadzone", DEFAULT_CONFIG["deadzone"]), 0.03, 0.25),
                "sensitivity_x": _clamp(params.get("sensitivity_x", DEFAULT_CONFIG["sensitivity_x"]), 20.0, 120.0),
                "sensitivity_y": _clamp(params.get("sensitivity_y", DEFAULT_CONFIG["sensitivity_y"]), 15.0, 100.0),
            })
            # Persist tuning parameters, but deliberately require a fresh neutral
            # center after each program launch.  Camera height, player position
            # and natural posture can change between sessions, and voice-triggered
            # calibration makes this one-time step cheap.  Saved center data is
            # retained in the file for diagnostics only.
            self.calibrated = False
            self.center_pending = True
            self.center_quality = "未校准"
            self.noise_yaw = 0.0
            self.noise_pitch = 0.0
            self._recompute_deadzone()
        except (OSError, ValueError, TypeError):
            return

    def _save_profile(self) -> None:
        if not self.profile_path:
            return
        path = Path(self.profile_path)
        payload = {
            "signal_version": HEAD_SIGNAL_VERSION,
            "saved_at_unix": time.time(),
            "params": dict(self.config),
            "center": {
                "yaw": self.center_yaw if self.calibrated else None,
                "pitch": self.center_pitch if self.calibrated else None,
                "noise_yaw": self.noise_yaw,
                "noise_pitch": self.noise_pitch,
            },
        }
        temp = path.with_suffix(path.suffix + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, path)
        except OSError:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
