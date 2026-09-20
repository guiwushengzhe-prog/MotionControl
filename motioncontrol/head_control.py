"""Head-control v5: three comparable horizontal signal routes.

- gesture_v153: personal (or generic) PnP yaw with a pitch-aware evidence guard.
- frozen22: fixed 22-D image-feature yaw with explicitly selected pitch units.
- gesture_v188: PnP yaw corroborated by calibrated 2D/world cues.

All three use the same relative gesture: outward turn moves, a held pose stops,
and returning to a quiet neutral center rearms without reverse mouse output.
Legacy profile/API IDs are accepted; ``classic`` now selects gesture_v153.
One valid capture can be reused across the three horizontal policies. A new
estimator (pnp/ratio) or camera session still requires a new neutral capture.
"""

from __future__ import annotations

import bisect
import json
import math
import os
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


HEAD_SIGNAL_VERSION = "head-control-v5.1-calibration-compat"
HEAD_PROFILE_COMPATIBLE_VERSIONS = {
    HEAD_SIGNAL_VERSION,
    "head-control-v5.0-three-routes",
    "head-control-v4.4-gated-pitch",
    "head-control-v4.3-reference-video-tuned",
}
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
    # 常开，不再是一个开关。理由同手控鼠标：这不是偏好，是默认值本来就错了。
    "invert_x": True,
    "invert_y": False,
    # Horizontal (yaw) output policy; see HORIZONTAL_ALGORITHMS below.
    # Default to the responsive PnP route; classic is a compatibility alias.
    "horizontal_algorithm": "gesture_v153",
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
# Reacquire after repeated rejection, but require consistent valid poses before
# allowing a new tracking segment to drive control.
PNP_REACQUIRE_FAILURE_FRAMES = 3
PNP_REACQUIRE_CONFIRM_FRAMES = 3
PNP_REACQUIRE_MAX_SPREAD_DEG = 5.0

# v0.9.7 intent thresholds are expressed in normalized signal units per
# second.  Pitch is deliberately more permissive than yaw (roughly 60%) so a
# small natural nod can be used when the left-hand look gate is active.  The
# ungated controller remains a relative turn gesture; ControlKernel separately
# applies stable pitch deflection only while the player holds the look gate.
YAW_INTENT_ANGLE = 0.055
YAW_INTENT_START_VELOCITY = 0.12
YAW_INTENT_STOP_VELOCITY = 0.045
PITCH_INTENT_ANGLE = 0.035
PITCH_INTENT_START_VELOCITY = 0.070
PITCH_INTENT_STOP_VELOCITY = 0.026

# ---------------------------------------------------------------------------
# Selectable horizontal (yaw) policies.
#
# Keep the three established API IDs so old clients remain compatible.
# Their v5 versions below share the same movement/return semantics, but use
# distinct signal evidence. The removed classic mode aliases to gesture_v153.

HORIZONTAL_ALGORITHMS = ("gesture_v153", "frozen22", "gesture_v188")
V153_POLICIES = frozenset(("gesture_v153", "gesture_v188"))
FROZEN22_POLICIES = frozenset(("frozen22",))
HORIZONTAL_ALGORITHM_VERSIONS = {
    "gesture_v153": "v5.1-pnp-optional-personal",
    "frozen22": "v5.1-fixed22-stable-units",
    "gesture_v188": "v5.1-consensus-shared-calibration",
}
HORIZONTAL_ALGORITHM_LABELS = {
    "gesture_v153": "个性化 PnP（灵敏）",
    "frozen22": "固定特征 2D（独立）",
    "gesture_v188": "多信号融合（稳健）",
}


def _horizontal_policy(value: Any) -> str:
    policy = str(value).lower().strip()
    # Legacy web clients can still send the removed classic selector.
    return "gesture_v153" if policy in {"classic", "gesture"} else policy


# frozen22 is the audited R3 11-point / 22-dimensional horizontal
# measurement.  The signature is a fixed research coefficient vector: runtime
# calibration may estimate only a user's neutral center and per-channel noise.
# It must never be re-fit from a new capture.
FROZEN22_SIGNATURE_VERSION = "real-ab-equalmean-20260830-v1"
FROZEN22_SIGNATURE = (
    0.017391687225, -0.0063843505855, 0.003726558662, 0.000005274079,
    0.0004064948765, -0.000035417555825, -0.004127857981, 0.00003236540025,
    0.003492305625, -0.00002010736025, 0.000074819552875, -0.000058343073335,
    -0.003572923066, 0.0000744540638, -0.034462183665, -0.006624336096,
    -0.03443915071, -0.0047827628515, 0.0088078813755, -0.01310556813,
    0.007183393412, -0.01328455541,
)
FROZEN22_MEDIAN_WINDOW = 3
FROZEN22_MAX_CAL_SIGMA_DEG = 2.5
FROZEN22_MIN_SAMPLES = 20
FROZEN22_SIGMA_FLOOR = 1e-4
# frozen22 returns a degree-like yaw measurement.  This is only the output
# normalization span; it does not alter the audited 22-D signature.
FROZEN22_YAW_SPAN_DEG = PNP_YAW_SPAN_DEG
FROZEN22_GATE_FAST_WINDOW_S = 0.267
FROZEN22_GATE_FAST_BASE_DELTA_DEG = 1.40
FROZEN22_GATE_FAST_SIGMA_MULT = 1.50
FROZEN22_GATE_FAST_PITCH_RATIO = 0.80
FROZEN22_GATE_FAST_CONFIRM_FRAMES = 2
FROZEN22_GATE_FAST_LEASE_S = 0.200
FROZEN22_GATE_SLOW_WINDOW_S = 0.800
FROZEN22_GATE_SLOW_BASE_DELTA_DEG = 1.00
FROZEN22_GATE_SLOW_SIGMA_MULT = 1.00
FROZEN22_GATE_SLOW_WORLD_DELTA_DEG = 1.00
FROZEN22_GATE_SLOW_PITCH_RATIO = 0.80
FROZEN22_GATE_SLOW_LEASE_S = 0.200
FROZEN22_GATE_COMMITTED_FALLBACK_SCALE = 0.00
FROZEN22_GATE_HISTORY_S = 1.20
FROZEN22_GATE_MAX_SAMPLE_AGE_S = 0.12
V188_PITCH_GUARD_MIN_DEG = 1.50
V188_PITCH_GUARD_YAW_TO_PITCH = 0.35
HEAD11_NAMES = (
    "nose", "left_eye_inner", "left_eye", "left_eye_outer",
    "right_eye_inner", "right_eye", "right_eye_outer",
    "left_ear", "right_ear", "mouth_left", "mouth_right",
)

HEAD_POINT_EMA_TAU_S = 0.050
YAW_V2_VELOCITY_TAU_S = 0.070
YAW_V2_MIN_DELTA = 0.0012
YAW_V2_EVIDENCE_NEED = 0.008
YAW_V2_EVIDENCE_WINDOW_S = 0.25
YAW_V2_CENTER_ZONE = 0.036
YAW_V2_COMMIT_ANGLE = 0.075
YAW_V2_COMMIT_S = 0.14
YAW_V2_RETURN_STEP = 0.015
YAW_V2_RETURN_VELOCITY = 0.070
YAW_V2_RETURN_EVIDENCE_NEED = 0.020
YAW_V2_RETURN_EVIDENCE_DECAY = 0.70
YAW_V2_STABLE_DELTA = 0.0015
YAW_V2_STABLE_VELOCITY = 0.020
YAW_V2_STABLE_S = 0.14
YAW_V2_RESUME_VELOCITY = 0.050
YAW_V2_RESUME_S = 0.18
YAW_V2_CENTER_RELEASE_S = 0.080
YAW_V2_OPPOSITE_REARM = 0.110
YAW_V2_KEEP_VELOCITY = 0.025
YAW_V2_OUTPUT_DECAY = 0.94
YAW_V2_SPEED_KNEE = 0.16
YAW_V2_MIN_DRIVE = 0.22
# The return clutch is intentionally stricter than normal TURN activation.
# Once a committed turn starts moving back, stop the relative output early;
# the head must then settle in the calibrated centre corridor before the
# normal start detector is allowed to run again.
YAW_V2_RETURN_EARLY_RETREAT = 0.025
YAW_V2_RETURN_EARLY_TREND = 0.045
# A short counter-swing is part of a natural outward turn.  Requiring this
# much accumulated opposite-side movement prevents one small correction from
# latching RETURNING; a smaller correction may still qualify when it remains
# coherent for the time gate below.  This is a conservative default pending
# calibration against the user's live raw_x scale.
YAW_RETURN_MIN_REVERSAL_NORM = 0.055
YAW_RETURN_MIN_REVERSAL_S = 0.12
YAW_V2_RETURN_CENTER_STABLE_S = 0.14
# After the signal has visibly crossed the calibrated centre, the opposite
# side must be a deliberate, sustained turn.  The old low gate let a normal
# return overshoot (around -0.14) re-arm the other direction.
YAW_V2_OPPOSITE_REARM_AFTER_CENTER = 0.18
YAW_V2_OPPOSITE_REARM_AFTER_CENTER_VELOCITY = 0.10
YAW_V2_OPPOSITE_REARM_AFTER_CENTER_S = 0.16
YAW_V2_OPPOSITE_REARM_OUTPUT_GUARD_S = 0.28
YAW_V2_OPPOSITE_REARM_GUARD_MAX_ANGLE = 0.65

# v202 output-only safety layers.  They never feed back into the ratchet, PnP,
# filters, or slew state.
V202_REARM_MIN_VISIBLE_QUIET_S = 0.40
V202_PITCH_CLEAN_RESET_S = 0.10
V202_PITCH_VETO_MIN_PITCH_DEG = 4.00
V202_PITCH_VETO_MAX_FROZEN22_YAW_DEG = 4.00
V202_PITCH_VETO_MAX_WORLD_RIGID_YAW_DEG = 5.00

# v205 output-only recovery for a false RETURNING latch.  It never changes
# _RelativeYawAxisV153 state.  Frozen real-window prescan: this conjunction
# occurs only in v3_yaw_left_01 and v5_yaw_left_02, and in zero annotated
# return/pitch/neutral frames.
V205_SAME_SIDE_RESCUE_MIN_NORM = 0.55
V205_SAME_SIDE_RESCUE_MIN_VELOCITY = 0.50
V205_SAME_SIDE_RESCUE_MIN_FROZEN22_DEG = 2.80
V205_SAME_SIDE_RESCUE_MIN_WORLD_DEG = 7.00
V205_SAME_SIDE_RESCUE_MAX_PITCH_DEG = 6.50
V205_SAME_SIDE_RESCUE_HOLD_S = 0.067

# Six-channel 2D auxiliary yaw evidence (v115 stage candidate).
#
# These channels use only the seven landmarks already required by sparse PnP.
# x/y are first converted to pixel coordinates and de-rolled by the eye line;
# this avoids the 16:9 aspect-ratio contamination of the old simple ratio.
# The fixed scales are the median five-degree response measured on the
# randomized 3D-face generator.  They convert each channel to an approximately
# degree-like evidence value.  The next iteration will replace these fixed
# scales with per-user calibration noise/gain normalization.
MULTI2D_USE = (0, 1, 2, 3, 4, 6)
MULTI2D_SCALE = (
    0.07653495 / 5.0,
    0.18059956 / 5.0,
    0.13952917 / 5.0,
    0.12103324 / 5.0,
    0.04028254 / 5.0,
    0.03873437 / 5.0,
)
# v117 frozen cross-axis thresholds.  The six 2D channels retain their fixed
# physical yaw scale; each channel's calibration sigma only makes that channel
# harder to trust, never more sensitive.  These values were frozen before the
# 18000--18023 blind cohort.
CROSS2D_SIGMA_LOCAL = 0.60
CROSS2D_SIGMA_FAST = 0.50
CROSS2D_SIGMA_LEASE = 1.00
CROSS2D_SIGMA_BOUNDARY = 0.50
WORLD_RIGID_FIT_GROSS_MAX = 0.35

PERSONAL_PNP_YAW_GAIN = 1.20
PERSONAL_PNP_MIN_SAMPLES = 20
PERSONAL_PNP_MAX_MEDIAN_REPROJECTION = 0.025
PERSONAL_PNP_MAX_WORLD_RIGID_SIGMA_DEG = 4.80
PERSONAL_PNP_FAR_START_POWER = 0.20
PERSONAL_PNP_FAR_MAX_DEPTH_RATIO = 1.50
PERSONAL_PNP_CALIB_DEROTATE_YAW_DEG = 4.0
PERSONAL_PNP_CALIB_DEROTATE_ROLL_DEG = 2.5
PERSONAL_PNP_MAX_WORLD_PAIR_YAW_DEVIATION_DEG = 7.0

# Cross-axis safety layer (v129 frozen multi-blind candidate).
#
# Frozen only after three development cohorts (60 synthetic users) and then a
# completely unseen 18000--18023 blind cohort.  Blind strict-direction result:
#   pitch-only/pitch+roll false output reduction: 73.36%
#   simultaneous pitch+yaw correct-direction loss: 0.95 pp
#   delayed-yaw correct-direction loss: 1.45 pp
#   wrong-direction output reduction: 90.68%
# The v59c ratchet remains untouched; only its final drive is gated.
PITCH_LOCK_WORLD_SIGMA_MAX_DEG = 1.40  # legacy diagnostic only; not a v115 gate
CROSS2D_ENTER_PITCH_DEG = 4.5
CROSS2D_PROXY_TAU_S = 0.030
CROSS2D_PITCH_TAU_S = 0.050
CROSS2D_LOCAL_STABLE_S = 0.020
CROSS2D_LOCAL_STABLE_PITCH_VEL = 5.5
CROSS2D_LOCAL_STABLE_PNP_VEL = 5.5


class IntentAxis:
    """Small angle + velocity + acceleration state machine.

    ``TURN_*`` is emitted only while the signal is moving in the same
    direction as its deflection.  A held deflection becomes ``HOLD`` and is
    silent; a velocity reversal becomes ``RETURNING`` and returns zero.
    """

    def __init__(self, name: str) -> None:
        self.name = str(name)
        self.reset()

    def reset(self) -> None:
        self.state = "IDLE"
        self.previous_signal = math.nan
        self.previous_velocity = 0.0
        self.last_at = 0.0
        self.velocity = 0.0
        self.acceleration = 0.0
        self.quiet_frames = 0
        # A return-to-centre is a short safety lock, not a new opposite turn.
        # Without this latch, crossing the neutral point while the filtered
        # signal still has a tail can immediately arm the other direction.
        self.return_latched = False
        self.return_settle_s = 0.0
        self.raw_stable_s = 0.0

    @staticmethod
    def _sign(value: float) -> int:
        return 1 if value > 0 else -1 if value < 0 else 0

    def step(
        self,
        signal: float,
        now: float,
        *,
        angle_threshold: float,
        start_velocity: float,
        stop_velocity: float,
        release_threshold: float,
        stop_grace_s: float = 0.075,
    ) -> dict:
        signal = _finite(signal, 0.0)
        if not self.last_at or not math.isfinite(self.previous_signal):
            # There is no previous sample yet, so position cannot be treated as
            # velocity.  Seed the derivative state silently; motion may only be
            # inferred from the next valid sample onward.  This also prevents a
            # tracking dropout/reset from re-triggering a held off-centre pose.
            dt = 1.0 / 30.0
            velocity = 0.0
            raw_delta = 0.0
        else:
            dt = _clamp(now - self.last_at, 1.0 / 240.0, 0.20)
            raw_delta = signal - self.previous_signal
            velocity = raw_delta / dt
        acceleration = (velocity - self.previous_velocity) / max(dt, 1.0 / 240.0)
        self.previous_signal = signal
        self.previous_velocity = velocity
        self.last_at = now
        self.velocity = _clamp(velocity, -8.0, 8.0)
        self.acceleration = _clamp(acceleration, -80.0, 80.0)

        # A stopped head should stop the camera even while the filtered
        # derivative is settling.  This uses raw *change*, not raw position,
        # so a held off-centre head cannot cause drift.
        if abs(raw_delta) <= 0.0025:
            self.raw_stable_s += dt
        else:
            self.raw_stable_s = 0.0

        direction = self._sign(signal)
        moving = abs(self.velocity) >= float(start_velocity)
        reversing = abs(self.velocity) >= float(stop_velocity)
        velocity_direction = self._sign(self.velocity)
        outside = abs(signal) >= max(float(angle_threshold), float(release_threshold))

        # Once returning begins, keep output at zero until the motion itself
        # settles.  This check intentionally precedes the neutral release
        # branch: crossing zero must not become an opposite turn.
        if self.return_latched:
            settle_velocity = max(float(stop_velocity), float(stop_velocity) * 0.72)
            if abs(self.velocity) <= settle_velocity:
                self.return_settle_s += dt
            else:
                self.return_settle_s = 0.0
            if self.return_settle_s >= max(0.07, float(stop_grace_s)):
                self.return_latched = False
                self.return_settle_s = 0.0
                self.state = "IDLE" if abs(signal) <= float(release_threshold) else "HOLD"
            else:
                self.state = "RETURNING"
            self.quiet_frames = 0
            return {
                "state": self.state,
                "active": False,
                "signal": signal,
                "velocity": self.velocity,
                "acceleration": self.acceleration,
                "direction": 0,
                "return_latched": self.return_latched,
            }

        if abs(signal) <= float(release_threshold):
            self.state = "IDLE"
            self.quiet_frames = 0
            self.return_settle_s = 0.0
        elif self.state == "IDLE":
            if outside and moving and velocity_direction == direction:
                self.state = "TURN_RIGHT" if direction > 0 else "TURN_LEFT"
                self.quiet_frames = 0
        elif self.state in {"TURN_LEFT", "TURN_RIGHT"}:
            active_direction = -1 if self.state == "TURN_LEFT" else 1
            if velocity_direction == -active_direction and reversing:
                self.state = "RETURNING"
                self.return_latched = True
                self.return_settle_s = 0.0
                self.quiet_frames = 0
            elif velocity_direction == active_direction and moving:
                self.quiet_frames = 0
            elif self.raw_stable_s >= max(0.10, float(stop_grace_s)) or abs(self.velocity) < float(stop_velocity):
                # Keep one frame of grace for the filter settling tail.  At
                # normal 30 FPS this is ~33 ms, then a held head is silent.
                self.quiet_frames += 1
                if self.quiet_frames > 1:
                    self.state = "HOLD"
            else:
                self.state = "RETURNING"
                self.return_latched = True
                self.return_settle_s = 0.0
                self.quiet_frames = 0
        elif self.state == "HOLD":
            if velocity_direction == direction and moving:
                self.state = "TURN_RIGHT" if direction > 0 else "TURN_LEFT"
                self.quiet_frames = 0
            elif velocity_direction == -direction and reversing:
                self.state = "RETURNING"
                self.return_latched = True
                self.return_settle_s = 0.0
                self.quiet_frames = 0
        elif self.state == "RETURNING":
            # Older states can enter RETURNING without the branch above (for
            # example after a filter tail).  Treat them as latched too.
            self.return_latched = True
            self.return_settle_s = 0.0

        active = self.state in {"TURN_LEFT", "TURN_RIGHT"}
        return {
            "state": self.state,
            "active": active,
            "signal": signal,
            "velocity": self.velocity,
            "acceleration": self.acceleration,
            "direction": -1 if self.state == "TURN_LEFT" else 1 if self.state == "TURN_RIGHT" else 0,
            "return_latched": self.return_latched,
        }


def _depth_yaw_proxy(pose: dict[str, dict] | None) -> float:
    """Estimate horizontal face rotation from left/right depth asymmetry.

    This is deliberately only an auxiliary safety signal.  It never replaces
    solvePnP yaw.  The ratio is scale-free, so the same function works for
    normalized MediaPipe ``z`` and metric ``world_pose`` coordinates.
    """
    if not isinstance(pose, dict):
        return math.nan
    values: list[float] = []
    for left_name, right_name in (
        ("left_eye_outer", "right_eye_outer"),
        ("mouth_left", "mouth_right"),
        ("left_ear", "right_ear"),
    ):
        left = pose.get(left_name)
        right = pose.get(right_name)
        if not isinstance(left, dict) or not isinstance(right, dict):
            continue
        lx, lz = _finite(left.get("x")), _finite(left.get("z"))
        rx, rz = _finite(right.get("x")), _finite(right.get("z"))
        if not all(math.isfinite(v) for v in (lx, lz, rx, rz)):
            continue
        span_x = abs(lx - rx)
        if span_x <= 1e-6:
            continue
        # With the canonical anatomical labels used by this module, positive
        # solvePnP yaw corresponds to subject-right.  The negative sign aligns
        # left/right depth skew to that same convention.
        values.append(-math.degrees(math.atan2(lz - rz, span_x)))
    return _median(values)

# Six-channel 2D auxiliary yaw evidence (v115 stage candidate).
#
# These channels use only the seven landmarks already required by sparse PnP.
# x/y are first converted to pixel coordinates and de-rolled by the eye line;
# this avoids the 16:9 aspect-ratio contamination of the old simple ratio.
# The fixed scales are the median five-degree response measured on the
# randomized 3D-face generator.  They convert each channel to an approximately
# degree-like evidence value.  The next iteration will replace these fixed
# scales with per-user calibration noise/gain normalization.
MULTI2D_USE = (0, 1, 2, 3, 4, 6)
MULTI2D_SCALE = (
    0.07653495 / 5.0,
    0.18059956 / 5.0,
    0.13952917 / 5.0,
    0.12103324 / 5.0,
    0.04028254 / 5.0,
    0.03873437 / 5.0,
)
# v117 frozen cross-axis thresholds.  The six 2D channels retain their fixed
# physical yaw scale; each channel's calibration sigma only makes that channel
# harder to trust, never more sensitive.  These values were frozen before the
# 18000--18023 blind cohort.
CROSS2D_SIGMA_LOCAL = 0.60
CROSS2D_SIGMA_FAST = 0.50
CROSS2D_SIGMA_LEASE = 1.00
CROSS2D_SIGMA_BOUNDARY = 0.50
WORLD_RIGID_FIT_GROSS_MAX = 0.35

def _matmul3(a: tuple[tuple[float, float, float], ...], b: tuple[tuple[float, float, float], ...]) -> tuple[tuple[float, float, float], ...]:
    return tuple(
        tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3))
        for i in range(3)
    )


def _euler_rotation_matrix_deg(yaw: float = 0.0, pitch: float = 0.0, roll: float = 0.0) -> tuple[tuple[float, float, float], ...]:
    '''Rz(roll) * Ry(yaw) * Rx(pitch), matching HeadPoseEstimator.'''
    y = math.radians(float(yaw)); p = math.radians(float(pitch)); r = math.radians(float(roll))
    cy, sy = math.cos(y), math.sin(y); cp, sp = math.cos(p), math.sin(p); cr, sr = math.cos(r), math.sin(r)
    rx = ((1.0, 0.0, 0.0), (0.0, cp, -sp), (0.0, sp, cp))
    ry = ((cy, 0.0, sy), (0.0, 1.0, 0.0), (-sy, 0.0, cy))
    rz = ((cr, -sr, 0.0), (sr, cr, 0.0), (0.0, 0.0, 1.0))
    return _matmul3(_matmul3(rz, ry), rx)


def _derotate_personal_template(
    template: tuple[tuple[float, float, float], ...],
    *, yaw_deg: float = 0.0, roll_deg: float = 0.0,
) -> tuple[tuple[float, float, float], ...]:
    '''Remove an obvious calibration-time head yaw/roll from a personal 3D face.

    The generic PnP estimate is used only when the calibration pose is clearly
    biased.  Mild/neutral calibrations are deliberately left untouched, so the
    proven personal-face geometry remains exact for normal users.  For row
    vectors, a calibration template P @ R.T is de-rotated by multiplying R.
    Pitch is intentionally not corrected because generic sparse-PnP pitch has
    materially larger face-shape bias than yaw/roll in the stress benchmark.
    '''
    if len(template) != 7:
        return template
    r = _euler_rotation_matrix_deg(yaw_deg, 0.0, roll_deg)
    cx = sum(pt[0] for pt in template) / 7.0; cy = sum(pt[1] for pt in template) / 7.0; cz = sum(pt[2] for pt in template) / 7.0
    out = []
    for px, py, pz in template:
        x, y, z = px - cx, py - cy, pz - cz
        out.append((
            x * r[0][0] + y * r[1][0] + z * r[2][0],
            x * r[0][1] + y * r[1][1] + z * r[2][1],
            x * r[0][2] + y * r[1][2] + z * r[2][2],
        ))
    return tuple(out)


def _world_pair_yaw_consistency(
    template: tuple[tuple[float, float, float], ...] | None,
) -> tuple[float, tuple[float, float, float] | None]:
    '''Measure eye/mouth/ear agreement on calibration-time world yaw.

    A rigid head yaw moves all three left/right pairs together. A persistent
    corrupt landmark instead makes one pair disagree strongly. This catches
    systematic bad geometry without punishing random frame noise that the
    robust multi-frame template median already averages down.
    '''
    if template is None or len(template) != 7:
        return math.inf, None
    vals: list[float] = []
    for li, ri in ((1, 2), (3, 4), (5, 6)):
        lx, ly, lz = template[li]; rx, ry, rz = template[ri]
        dx, dy = float(lx) - float(rx), float(ly) - float(ry)
        span = math.hypot(dx, dy)
        if not all(math.isfinite(float(v)) for v in (lx, ly, lz, rx, ry, rz)) or span <= 1e-9:
            return math.inf, None
        vals.append(-math.degrees(math.atan2(float(lz) - float(rz), span)))
    med = _median(vals)
    if not math.isfinite(med):
        return math.inf, None
    return float(max(abs(v - med) for v in vals)), (float(vals[0]), float(vals[1]), float(vals[2]))

def _world_face_xyz(pose: dict[str, dict] | None) -> tuple[tuple[float, float, float], ...] | None:
    if not isinstance(pose, dict): return None
    vals=[]
    for name in HeadPoseEstimator._PNP_NAMES if "HeadPoseEstimator" in globals() else (
        "nose","left_eye_outer","right_eye_outer","mouth_left","mouth_right","left_ear","right_ear"):
        pt=pose.get(name)
        if not isinstance(pt,dict): return None
        xyz=(_finite(pt.get("x")),_finite(pt.get("y")),_finite(pt.get("z")))
        if not all(math.isfinite(v) for v in xyz): return None
        vals.append(xyz)
    return tuple(vals)


def _head11_local_feature_xy(
    points: tuple[tuple[float, float], ...],
) -> tuple[tuple[float, ...], float] | None:
    """Build the audited frozen22 derolled, eye-centred 22-D feature.

    The point order and normalization intentionally mirror the R3 reference:
    three landmarks per eye are averaged, the eye line is derolled, and all
    coordinates are divided by the pixel eye span.  No runtime coefficient
    fitting belongs here.
    """
    if len(points) != len(HEAD11_NAMES):
        return None
    try:
        left_eye = (
            sum(float(points[i][0]) for i in (1, 2, 3)) / 3.0,
            sum(float(points[i][1]) for i in (1, 2, 3)) / 3.0,
        )
        right_eye = (
            sum(float(points[i][0]) for i in (4, 5, 6)) / 3.0,
            sum(float(points[i][1]) for i in (4, 5, 6)) / 3.0,
        )
        mx = (left_eye[0] + right_eye[0]) * 0.5
        my = (left_eye[1] + right_eye[1]) * 0.5
        dx = right_eye[0] - left_eye[0]
        dy = right_eye[1] - left_eye[1]
        scale = math.hypot(dx, dy)
        if not math.isfinite(scale) or scale < 3.0:
            return None
        angle = math.atan2(dy, dx)
        c, s = math.cos(-angle), math.sin(-angle)
        out: list[float] = []
        for x, y in points:
            x, y = float(x), float(y)
            if not (math.isfinite(x) and math.isfinite(y)):
                return None
            rx, ry = x - mx, y - my
            out.extend(((c * rx - s * ry) / scale, (s * rx + c * ry) / scale))
        return tuple(out), float(scale)
    except Exception:
        return None


def _head11_local_feature(
    pose: dict[str, dict] | None,
    width: int,
    height: int,
) -> tuple[tuple[float, ...], float] | None:
    """Return the fixed 11-point feature in image pixels and its eye span."""
    if not isinstance(pose, dict):
        return None
    if any(not _point_ok(pose.get(name), 0.20) for name in HEAD11_NAMES):
        return None
    width, height = max(2, int(width)), max(2, int(height))
    points: list[tuple[float, float]] = []
    for name in HEAD11_NAMES:
        point = pose.get(name)
        if not isinstance(point, dict):
            return None
        x = _finite(point.get("x")) * width
        y = _finite(point.get("y")) * height
        if not (math.isfinite(x) and math.isfinite(y)):
            return None
        points.append((x, y))
    return _head11_local_feature_xy(tuple(points))


def _world_head11_xyz(
    pose: dict[str, dict] | None,
) -> tuple[tuple[float, float, float], ...] | None:
    """Extract the same 11 landmarks from optional metric world pose."""
    if not isinstance(pose, dict):
        return None
    out: list[tuple[float, float, float]] = []
    for name in HEAD11_NAMES:
        point = pose.get(name)
        if not isinstance(point, dict):
            return None
        xyz = (_finite(point.get("x")), _finite(point.get("y")), _finite(point.get("z")))
        if not all(math.isfinite(value) for value in xyz):
            return None
        out.append(tuple(float(value) for value in xyz))
    return tuple(out)


def _head11_eye_world_span(
    template: tuple[tuple[float, float, float], ...] | None,
) -> float:
    if template is None or len(template) != len(HEAD11_NAMES):
        return math.nan
    left = tuple(sum(template[i][j] for i in (1, 2, 3)) / 3.0 for j in range(3))
    right = tuple(sum(template[i][j] for i in (4, 5, 6)) / 3.0 for j in range(3))
    return math.sqrt(sum((right[j] - left[j]) ** 2 for j in range(3)))


def _frozen22_world_residual_rel(
    template: tuple[tuple[float, float, float], ...] | None,
    samples: list[tuple[tuple[float, float, float], ...]],
) -> float:
    """Report optional non-rigid world-face residual for diagnostics only."""
    if template is None or len(template) != len(HEAD11_NAMES) or not samples:
        return math.inf
    try:
        import numpy as np

        p = np.asarray(template, dtype=np.float64)
        if p.shape != (11, 3) or not np.isfinite(p).all():
            return math.inf
        eye = _head11_eye_world_span(template)
        if not math.isfinite(eye) or eye <= 1e-9:
            return math.inf
        pc = p - p.mean(axis=0, keepdims=True)
        values: list[float] = []
        for sample in samples:
            q = np.asarray(sample, dtype=np.float64)
            if q.shape != (11, 3) or not np.isfinite(q).all():
                continue
            qc = q - q.mean(axis=0, keepdims=True)
            u, _, vt = np.linalg.svd(pc.T @ qc)
            # Keep the same Kabsch reflection correction as the reference.
            rotation = vt.T @ u.T
            if np.linalg.det(rotation) < 0.0:
                vt[-1, :] *= -1.0
                rotation = vt.T @ u.T
            aligned = pc @ rotation.T
            rms = float(np.sqrt(np.mean(np.sum((aligned - qc) ** 2, axis=1))))
            if math.isfinite(rms):
                values.append(rms / eye)
        return float(_median(values)) if values else math.inf
    except Exception:
        return math.inf


def _personal22_matched_yaw(
    feature: tuple[float, ...] | None,
    center: tuple[float, ...] | None,
    sigma: tuple[float, ...] | None,
    signature: tuple[float, ...] | None,
) -> float:
    """Project a calibrated 22-D feature onto the fixed frozen22 signature."""
    if feature is None or center is None or sigma is None or signature is None:
        return math.nan
    if min(len(feature), len(center), len(sigma), len(signature)) < 22:
        return math.nan
    numerator = denominator = 0.0
    for value, neutral, noise, coefficient in zip(
        feature[:22], center[:22], sigma[:22], signature[:22]
    ):
        if not all(math.isfinite(float(item)) for item in (value, neutral, noise, coefficient)):
            return math.nan
        weight = 1.0 / max(FROZEN22_SIGMA_FLOOR, abs(float(noise))) ** 2
        numerator += (float(value) - float(neutral)) * float(coefficient) * weight
        denominator += float(coefficient) * float(coefficient) * weight
    return float(numerator / denominator) if denominator > 1e-12 else math.nan

def _world_rigid_yaw(
    template: tuple[tuple[float, float, float], ...] | None,
    current: tuple[tuple[float, float, float], ...] | None,
) -> tuple[float, float]:
    """Rigid 3D yaw relative to the calibrated personal face template.

    Kabsch alignment removes translation and personal facial asymmetry before
    measuring rotation.  ``fit`` is RMS residual normalized by face scale and
    is diagnostic/gross-corruption protection only; world yaw never drives the
    mouse directly and is used only as a final cross-axis rescue signal.
    """
    if template is None or current is None or len(template) != 7 or len(current) != 7:
        return math.nan, math.nan
    try:
        import numpy as np
        p = np.asarray(template, dtype=np.float64)
        q = np.asarray(current, dtype=np.float64)
        if p.shape != (7, 3) or q.shape != (7, 3) or not (np.isfinite(p).all() and np.isfinite(q).all()):
            return math.nan, math.nan
        pc = p - p.mean(axis=0, keepdims=True)
        qc = q - q.mean(axis=0, keepdims=True)
        h = pc.T @ qc
        u, _, vt = np.linalg.svd(h)
        r = vt.T @ u.T
        if np.linalg.det(r) < 0.0:
            vt[-1, :] *= -1.0
            r = vt.T @ u.T
        sy = math.sqrt(float(r[0, 0]) ** 2 + float(r[1, 0]) ** 2)
        yaw = math.degrees(math.atan2(-float(r[2, 0]), sy))
        aligned = pc @ r.T
        rms = float(np.sqrt(np.mean(np.sum((aligned - qc) ** 2, axis=1))))
        scale = float(np.sqrt(np.mean(np.sum(pc ** 2, axis=1))))
        fit = rms / max(scale, 1e-9)
        return (yaw, fit) if math.isfinite(yaw) and math.isfinite(fit) else (math.nan, math.nan)
    except Exception:
        return math.nan, math.nan


def _multi2d_proxy_xy_metric(points: tuple[tuple[float,float,float], ...]) -> tuple[float,...] | None:
    if len(points)!=7:return None
    # same anatomical order as _PNP_NAMES
    names=("nose","left_eye_outer","right_eye_outer","mouth_left","mouth_right","left_ear","right_ear")
    xy0={n:(points[i][0],points[i][1]) for i,n in enumerate(names)}
    le,re=xy0["left_eye_outer"],xy0["right_eye_outer"];mx=(le[0]+re[0])*.5;my=(le[1]+re[1])*.5
    ang=math.atan2(re[1]-le[1],re[0]-le[0])
    while ang>math.pi*.5:ang-=math.pi
    while ang<-math.pi*.5:ang+=math.pi
    c,ss=math.cos(-ang),math.sin(-ang);xy={}
    for n,(x,y) in xy0.items():
        dx,dy=x-mx,y-my;xy[n]=(c*dx-ss*dy,ss*dx+c*dy)
    def D(a,b):return math.hypot(xy[a][0]-xy[b][0],xy[a][1]-xy[b][1])
    eps=1e-9;n="nose";ew=max(eps,D("left_eye_outer","right_eye_outer"))
    return (
        math.log((D(n,"left_eye_outer")+eps)/(D(n,"right_eye_outer")+eps)),
        math.log((D(n,"mouth_left")+eps)/(D(n,"mouth_right")+eps)),
        math.log((D(n,"left_ear")+eps)/(D(n,"right_ear")+eps)),
        math.log((D("left_eye_outer","left_ear")+eps)/(D("right_eye_outer","right_ear")+eps)),
        -xy[n][0]/ew,
        ((xy["mouth_left"][0]+xy["mouth_right"][0])*.5)/ew,
        ((xy["left_ear"][0]+xy["right_ear"][0])*.5)/ew,
    )

def _personal_multi2d_gain(template: tuple[tuple[float,float,float], ...] | None) -> tuple[float,...] | None:
    if template is None or len(template)!=7:return None
    cx=sum(p[0] for p in template)/7;cy=sum(p[1] for p in template)/7;cz=sum(p[2] for p in template)/7
    base=[(p[0]-cx,p[1]-cy,p[2]-cz) for p in template]
    def rot(deg):
        a=math.radians(deg);c,ss=math.cos(a),math.sin(a);return tuple((c*x+ss*z,y,-ss*x+c*z) for x,y,z in base)
    plus=_multi2d_proxy_xy_metric(rot(5.0));minus=_multi2d_proxy_xy_metric(rot(-5.0))
    if plus is None or minus is None:return None
    fixed7=(0.07653495/5,0.18059956/5,0.13952917/5,0.12103324/5,0.04028254/5,0.01303513/5,0.03873437/5)
    out=[]
    for i in range(7):
        slope=(plus[i]-minus[i])/10.0
        f=fixed7[i]
        out.append(slope if math.isfinite(slope) and slope>f*.15 and slope<f*5.0 else f)
    return tuple(out)

def _multi2d_yaw_proxy(pose: dict[str, dict] | None, width: int, height: int) -> tuple[float, ...] | None:
    if not isinstance(pose, dict):
        return None
    names = (
        "nose", "left_eye_outer", "right_eye_outer", "mouth_left",
        "mouth_right", "left_ear", "right_ear",
    )
    if any(not _point_ok(pose.get(name), 0.25) for name in names):
        return None
    width = max(2, int(width)); height = max(2, int(height))
    le = pose["left_eye_outer"]; re = pose["right_eye_outer"]
    x1, y1 = _finite(le.get("x")) * width, _finite(le.get("y")) * height
    x2, y2 = _finite(re.get("x")) * width, _finite(re.get("y")) * height
    if not all(math.isfinite(v) for v in (x1, y1, x2, y2)):
        return None
    mx, my = (x1 + x2) * 0.5, (y1 + y2) * 0.5
    angle = math.atan2(y2 - y1, x2 - x1)
    while angle > math.pi * 0.5: angle -= math.pi
    while angle < -math.pi * 0.5: angle += math.pi
    c, ss = math.cos(-angle), math.sin(-angle)
    xy: dict[str, tuple[float, float]] = {}
    for name in names:
        pt = pose[name]
        x, y = _finite(pt.get("x")) * width, _finite(pt.get("y")) * height
        if not (math.isfinite(x) and math.isfinite(y)):
            return None
        dx, dy = x - mx, y - my
        xy[name] = (c * dx - ss * dy, ss * dx + c * dy)
    def dist(a: str, b: str) -> float:
        return math.hypot(xy[a][0] - xy[b][0], xy[a][1] - xy[b][1])
    eps = 1e-6
    ew = max(eps, dist("left_eye_outer", "right_eye_outer"))
    n = "nose"
    values = (
        math.log((dist(n, "left_eye_outer") + eps) / (dist(n, "right_eye_outer") + eps)),
        math.log((dist(n, "mouth_left") + eps) / (dist(n, "mouth_right") + eps)),
        math.log((dist(n, "left_ear") + eps) / (dist(n, "right_ear") + eps)),
        math.log((dist("left_eye_outer", "left_ear") + eps) / (dist("right_eye_outer", "right_ear") + eps)),
        -xy[n][0] / ew,
        ((xy["mouth_left"][0] + xy["mouth_right"][0]) * 0.5) / ew,
        ((xy["left_ear"][0] + xy["right_ear"][0]) * 0.5) / ew,
    )
    return tuple(float(v) for v in values)

def _coerce_world_pose(value: Any) -> dict[str, dict] | None:
    """Accept either the module's named map or a raw 33-landmark world list."""
    if isinstance(value, dict):
        return value
    if isinstance(value, list) and len(value) == 33:
        # MediaPipe Pose landmark indices needed by the depth-yaw proxy.
        index_by_name = {
            "nose": 0,
            "left_eye_outer": 3,
            "right_eye_outer": 6,
            "left_ear": 7,
            "right_ear": 8,
            "mouth_left": 9,
            "mouth_right": 10,
        }
        result: dict[str, dict] = {}
        for name, index in index_by_name.items():
            point = value[index]
            if isinstance(point, dict):
                result[name] = point
        return result if result else None
    return None

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


class _LandmarkEMA:
    """Tiny time-aware EMA for head landmarks before pose estimation.

    Differentiating noisy solvePnP yaw amplifies sub-pixel landmark jitter.
    Smoothing the seven observations *before* solvePnP preserves their geometric
    relationship and reduces that amplification with only about one video frame
    of added lag at normal camera rates. Scores are never fabricated or raised.
    """

    def __init__(self, tau_s: float = HEAD_POINT_EMA_TAU_S) -> None:
        self.tau_s = max(1e-4, float(tau_s))
        self._points: dict[str, tuple[float, float]] = {}
        self._last_t = 0.0

    def reset(self) -> None:
        self._points.clear()
        self._last_t = 0.0

    def apply(self, pose: dict[str, dict] | None, names: tuple[str, ...], now: float) -> dict[str, dict] | None:
        if not isinstance(pose, dict):
            self.reset()
            return pose
        if not self._last_t or now <= self._last_t or now - self._last_t > 0.25:
            alpha = 1.0
        else:
            dt = _clamp(now - self._last_t, 1.0 / 240.0, 0.10)
            alpha = 1.0 - math.exp(-dt / self.tau_s)
        out = dict(pose)
        for name in names:
            point = pose.get(name)
            if not isinstance(point, dict):
                self._points.pop(name, None)
                continue
            x = _finite(point.get("x"))
            y = _finite(point.get("y"))
            if not (math.isfinite(x) and math.isfinite(y)):
                self._points.pop(name, None)
                continue
            previous = self._points.get(name)
            if previous is None or alpha >= 0.999999:
                sx, sy = x, y
            else:
                sx = previous[0] + alpha * (x - previous[0])
                sy = previous[1] + alpha * (y - previous[1])
            self._points[name] = (sx, sy)
            copy_point = dict(point)
            copy_point["x"] = sx
            copy_point["y"] = sy
            out[name] = copy_point
        self._last_t = float(now)
        return out


class _CrossAxisPitchLock:
    """v129 cross-axis safety layer after the untouched v59c yaw ratchet.

    PnP remains the only horizontal mouse source.  During large pitch, six
    independent 2D face-asymmetry channels are converted to approximate yaw
    degrees and judged against both physical thresholds and their own neutral
    calibration noise.  A frozen low/high-pitch local baseline catches yaw that
    starts *after* the player has already looked up/down.

    World-pose rigid yaw is deliberately late and weak: it can only rescue a
    frame already rejected by the 2D/PnP safety layer.  It never creates mouse
    motion by itself and disappears safely when world_pose is unavailable.
    """
    def __init__(self) -> None:
        self.reset()

    @staticmethod
    def _alpha(dt: float, tau: float) -> float:
        return 1.0 - math.exp(-max(1e-4, dt) / max(1e-4, tau))

    @staticmethod
    def _count(values: list[float], thresholds: list[float] | tuple[float, ...] | float) -> int:
        if isinstance(thresholds, (int, float)):
            t = float(thresholds)
            return sum(1 for v in values if v >= t)
        return sum(1 for v, t in zip(values, thresholds) if v >= t)

    @staticmethod
    def _old_value(history: list[tuple[float, Any]], now: float, window: float, fallback: Any) -> Any:
        pts = [x for x in history if now - x[0] <= window + 1e-9]
        return pts[0][1] if pts else fallback

    def reset(self) -> None:
        self.locked = False
        self.yaw_released = False
        self.confirm_s = 0.0
        self.filtered_proxy: list[float] | None = None
        self.filtered_pitch = math.nan
        self._last_pnp = math.nan
        self._last_t = 0.0
        self.local_ready = False
        self.local_stable_s = 0.0
        self.local_anchor = [0.0] * 6
        self.lease_s = 0.0
        self.lease_direction = 0
        self.last_mode = "FALLBACK"
        self.global_votes = 0
        self.local_votes = 0
        self.pnp_velocity_deg_s = 0.0
        self.pitch_velocity_deg_s = 0.0
        self.noise_deg = [math.inf] * 6
        self.proxy_history: list[tuple[float, list[float]]] = []
        self.world_history: list[tuple[float, float]] = []
        self.world_local_anchor = math.nan
        self._last_world_t = 0.0
        self.world_local_deg = math.nan
        self.world_net_200ms_deg = math.nan
        self.world_net_300ms_deg = math.nan
        # v129 delayed-yaw bridge: a low/high-pitch PnP-local anchor plus a
        # very short direction-bound ratchet.  It never creates a direction;
        # it can only preserve an already-existing v59c drive for <=60 ms.
        self.local_pnp_anchor = math.nan
        self.pnp_history: list[tuple[float, float]] = []
        self.local_ratchet_until = 0.0
        self.local_ratchet_direction = 0
        self.pnp_local_deg = math.nan
        self.pnp_net_300ms_deg = math.nan

    def _append_proxy_history(self, now: float, ch: list[float]) -> None:
        self.proxy_history.append((float(now), ch.copy()))
        cutoff = now - 0.32
        while len(self.proxy_history) > 2 and self.proxy_history[0][0] < cutoff:
            self.proxy_history.pop(0)

    def _append_world_history(self, now: float, world_yaw: float) -> None:
        if not math.isfinite(world_yaw):
            self.world_history.clear()
            self._last_world_t = 0.0
            self.world_local_anchor = math.nan
            return
        # Never compare across a meaningful world-pose dropout.
        if self._last_world_t and now - self._last_world_t > 0.12:
            self.world_history.clear()
            self.world_local_anchor = math.nan
        self._last_world_t = now
        self.world_history.append((float(now), float(world_yaw)))
        cutoff = now - 0.32
        while len(self.world_history) > 2 and self.world_history[0][0] < cutoff:
            self.world_history.pop(0)

    def _append_pnp_history(self, now: float, pnp_yaw: float) -> None:
        self.pnp_history.append((float(now), float(pnp_yaw)))
        cutoff = now - 0.35
        while len(self.pnp_history) > 2 and self.pnp_history[0][0] < cutoff:
            self.pnp_history.pop(0)

    def apply(
        self,
        drive: float,
        *,
        pnp_yaw: float,
        pitch_delta_deg: float,
        proxy_values: tuple[float, ...] | None,
        proxy_center: tuple[float, ...] | None,
        proxy_sigma: tuple[float, ...] | None = None,
        now: float = 0.0,
        signal_sign: float = 1.0,
        world_rigid_yaw: float = math.nan,
        world_rigid_sigma: float = math.inf,
        **_: Any,
    ) -> float:
        # Missing calibrated 2D evidence means exact v59c behavior.
        if (
            proxy_values is None or proxy_center is None or proxy_sigma is None
            or len(proxy_values) < 7 or len(proxy_center) < 7 or len(proxy_sigma) < 7
            or not math.isfinite(pnp_yaw)
        ):
            self.reset()
            self.last_mode = "FALLBACK"
            return drive

        dt = _clamp(now - self._last_t, 1.0 / 240.0, 0.10) if self._last_t else 1.0 / 30.0
        self._last_t = now
        raw = [float(proxy_values[i]) for i in MULTI2D_USE]
        cen = [float(proxy_center[i]) for i in MULTI2D_USE]
        sig_raw = [float(proxy_sigma[i]) for i in MULTI2D_USE]
        if not all(math.isfinite(v) for v in raw + cen):
            self.reset(); self.last_mode = "FALLBACK"; return drive

        # Infinite/invalid sigma disables only that channel rather than the full
        # safety layer.  A valid center plus at least a few stable channels is
        # still useful; thresholds naturally become +inf for a bad channel.
        noise_deg: list[float] = []
        for sig, scale in zip(sig_raw, MULTI2D_SCALE):
            noise_deg.append(max(0.0, sig) / max(scale, 1e-9) if math.isfinite(sig) else math.inf)
        self.noise_deg = noise_deg

        ap = self._alpha(dt, CROSS2D_PROXY_TAU_S)
        if self.filtered_proxy is None:
            self.filtered_proxy = raw.copy()
        else:
            for i in range(6):
                self.filtered_proxy[i] += ap * (raw[i] - self.filtered_proxy[i])

        aq = self._alpha(dt, CROSS2D_PITCH_TAU_S)
        old_pitch = self.filtered_pitch
        if not math.isfinite(self.filtered_pitch):
            self.filtered_pitch = float(pitch_delta_deg)
        else:
            self.filtered_pitch += aq * (float(pitch_delta_deg) - self.filtered_pitch)
        pitch_v = (self.filtered_pitch - old_pitch) / dt if math.isfinite(old_pitch) else 0.0
        aligned_pnp = float(pnp_yaw) * float(signal_sign)
        pnp_v = (aligned_pnp - self._last_pnp) / dt if math.isfinite(self._last_pnp) else 0.0
        self._last_pnp = aligned_pnp
        self.pitch_velocity_deg_s = pitch_v
        self.pnp_velocity_deg_s = pnp_v

        # Fixed physical scale.  Personal calibration noise may only demand
        # stronger evidence; it never makes a channel more sensitive.
        ch = [
            float(signal_sign) * (self.filtered_proxy[j] - cen[j]) / max(1e-9, MULTI2D_SCALE[j])
            for j in range(6)
        ]
        self._append_proxy_history(now, ch)

        wy = float(world_rigid_yaw) * float(signal_sign) if math.isfinite(world_rigid_yaw) else math.nan
        self._append_world_history(now, wy)

        local_was_ready = self.local_ready
        if abs(self.filtered_pitch) < CROSS2D_ENTER_PITCH_DEG:
            self.local_stable_s = 0.0
            self.local_ready = False
            self.local_anchor = ch.copy()
            self.world_local_anchor = wy if math.isfinite(wy) else math.nan
        elif not self.local_ready:
            if abs(pitch_v) <= CROSS2D_LOCAL_STABLE_PITCH_VEL and abs(pnp_v) <= CROSS2D_LOCAL_STABLE_PNP_VEL:
                self.local_stable_s += dt
                if self.local_stable_s >= CROSS2D_LOCAL_STABLE_S:
                    self.local_ready = True
                    self.local_anchor = ch.copy()
                    self.world_local_anchor = wy if math.isfinite(wy) else math.nan
            else:
                self.local_stable_s = max(0.0, self.local_stable_s - dt * 0.6)
                self.local_anchor = ch.copy()
                self.world_local_anchor = wy if math.isfinite(wy) else math.nan
        elif not math.isfinite(self.world_local_anchor) and math.isfinite(wy):
            # A resumed world stream starts a fresh safe local anchor; it cannot
            # immediately rescue a frame from stale pre-dropout history.
            self.world_local_anchor = wy

        # Freeze the PnP-local horizontal center at the same moment as the 2D
        # low/high-pitch baseline.  While the baseline is not yet ready it keeps
        # following PnP, so the pitch-induced false yaw becomes the local zero.
        if abs(self.filtered_pitch) < CROSS2D_ENTER_PITCH_DEG or not local_was_ready:
            self.local_pnp_anchor = aligned_pnp
        self._append_pnp_history(now, aligned_pnp)

        d = 1 if drive > 1e-12 else -1 if drive < -1e-12 else 0
        inside = abs(self.filtered_pitch) >= CROSS2D_ENTER_PITCH_DEG and d != 0
        if not inside:
            self.locked = False
            self.yaw_released = False
            self.confirm_s = 0.0
            self.local_ratchet_until = 0.0
            self.local_ratchet_direction = 0
            self.pnp_local_deg = math.nan
            self.pnp_net_300ms_deg = math.nan
            if d == 0:
                self.lease_s = 0.0
                self.lease_direction = 0
            self.last_mode = "V59C"
            return drive

        S = [d * v for v in ch]
        L = [d * (v - a) for v, a in zip(ch, self.local_anchor)]
        pd = d * pnp_v
        old_ch = self._old_value(self.proxy_history, now, 0.20, ch)
        N = [d * (v - ov) for v, ov in zip(ch, old_ch)]

        TG = [2.8] * 6
        TL = [max(0.4, CROSS2D_SIGMA_LOCAL * n) for n in noise_deg]
        TF = [max(0.5, CROSS2D_SIGMA_FAST * n) for n in noise_deg]
        TT = [max(1.0, CROSS2D_SIGMA_LEASE * n) for n in noise_deg]
        TLM = [max(0.8, CROSS2D_SIGMA_LEASE * n) for n in noise_deg]
        TB = [max(0.3, CROSS2D_SIGMA_BOUNDARY * n) for n in noise_deg]

        self.global_votes = self._count(S, TG)
        self.local_votes = self._count(L, TL)

        # Frozen conservative core.
        core_global = self._count(S, TG) >= 3
        core_local = (
            self.local_ready and self._count(L, TL) >= 3
            and self._count(S, 0.0) >= 3 and pd >= 1.0
        )
        core_fast = pd >= 6.0 and self._count(S, TF) >= 3
        core = core_global or core_local or core_fast

        # Sigma-aware action lease.  The lease is intentionally short and
        # direction-bound; a return or reversal cancels it immediately.
        trigger = (
            pd >= 1.5 and abs(pitch_v) <= 3.0 and (
                (self._count(S, TT) >= 3 and self._count(S, 0.0) >= 3)
                or (
                    self.local_ready and self._count(L, TLM) >= 3
                    and self._count(L, 0.1) >= 4 and self._count(S, 1.0) >= 3
                )
            )
        )
        if trigger and not core:
            self.lease_s = 0.40
            self.lease_direction = d
        elif self.lease_s > 0.0:
            self.lease_s = max(0.0, self.lease_s - dt)
            if d == 0 or d != self.lease_direction or pd < -0.5:
                self.lease_s = 0.0
                self.lease_direction = 0

        passed = core or (self.lease_s > 0.0 and d == self.lease_direction)
        mode = "CORE" if core else "LEASE" if passed else "LOCK"

        # Non-latching 2D boundary recovery.
        if not passed and pd >= -0.5 and self._count(L, TB) >= 4:
            passed = True; mode = "BOUNDARY"

        # 200 ms multi-channel trend rescue.  Each participating channel must
        # move at least 0.5 deg and at least 0.25 sigma in the same direction.
        if not passed and self.local_ready and abs(pitch_v) <= 6.0 and pd >= 0.0:
            votes = 0
            for value, sigma in zip(N, noise_deg):
                denom = max(sigma, 0.15)
                if value >= 0.5 and value / denom >= 0.25:
                    votes += 1
            if votes >= 4:
                passed = True; mode = "TREND2D"

        # Strong local two-channel proof, useful just after a frozen low/high
        # pitch baseline when global evidence has not yet grown.
        if not passed and self.local_ready and pd >= 0.25 and abs(pitch_v) <= 4.0:
            ll = sorted(L)
            if ll[-1] >= 3.0 and ll[-2] >= 1.0:
                passed = True; mode = "LOCAL2D"

        # World-pose final adjudication.  It is intentionally evaluated only
        # after all 2D/PnP paths reject the frame.
        wsig = max(float(world_rigid_sigma), 0.05) if math.isfinite(world_rigid_sigma) else math.inf
        wlocal = d * (wy - self.world_local_anchor) if (math.isfinite(wy) and math.isfinite(self.world_local_anchor)) else math.nan
        self.world_local_deg = wlocal
        if (
            not passed and math.isfinite(wlocal) and math.isfinite(wsig)
            and pd >= 1.0 and abs(pitch_v) <= 6.0
            and wlocal >= 1.6 and wlocal / wsig >= 1.5
        ):
            passed = True; mode = "WORLD_LOCAL"

        def world_net(window: float) -> float:
            if not math.isfinite(wy) or not self.world_history:
                return math.nan
            old = self._old_value(self.world_history, now, window, wy)
            return d * (wy - float(old))

        wn30 = world_net(0.30)
        wn20 = world_net(0.20)
        self.world_net_300ms_deg = wn30
        self.world_net_200ms_deg = wn20
        if (
            not passed and math.isfinite(wn30) and math.isfinite(wn20)
            and pd >= 0.5 and abs(pitch_v) <= 4.0
            and wn30 >= 1.5 and wn20 >= 0.8
        ):
            passed = True; mode = "WORLD_TREND"

        # ------------------------------------------------------------------
        # v129 local-PnP ratchet + final anti-direction adjudication.
        # Frozen after 18000/19000/20000 development validation, then passed
        # an unseen randomized-timing 21000--21023 cohort.
        # ------------------------------------------------------------------
        p_local = d * (aligned_pnp - self.local_pnp_anchor) if math.isfinite(self.local_pnp_anchor) else 0.0
        old_pnp = self._old_value(self.pnp_history, now, 0.30, aligned_pnp)
        p_net30 = d * (aligned_pnp - float(old_pnp))
        self.pnp_local_deg = p_local
        self.pnp_net_300ms_deg = p_net30
        s_sorted = sorted(S, reverse=True)
        l_sorted = sorted(L, reverse=True)
        s2 = s_sorted[1] if len(s_sorted) >= 2 else -math.inf
        l2 = l_sorted[1] if len(l_sorted) >= 2 else -math.inf

        # Maintain/cancel before evaluating a fresh trigger.  The ratchet is
        # intentionally only 60 ms and must keep moving outward locally; a
        # reversal, strong pitch motion, or loss of the local baseline kills it.
        ratchet_active = self.local_ratchet_direction != 0 and now <= self.local_ratchet_until
        if ratchet_active and (
            d != self.local_ratchet_direction or d == 0 or not self.local_ready
            or p_local < 0.02 or pd < -2.0 or abs(pitch_v) > 9.0
        ):
            self.local_ratchet_until = 0.0
            self.local_ratchet_direction = 0
            ratchet_active = False

        ratchet_trigger = (
            d != 0 and self.local_ready and p_local >= 0.30 and l2 >= 0.40
            and s2 >= 0.0 and p_net30 >= 0.30 and pd >= 0.0
            and abs(pitch_v) <= 6.0
        )
        if ratchet_trigger:
            self.local_ratchet_until = now + 0.06
            self.local_ratchet_direction = d
            ratchet_active = True

        if not passed and ratchet_active and d == self.local_ratchet_direction:
            passed = True
            mode = "LOCAL_RATCHET"

        # A drive can still be geometrically inconsistent even after an older
        # rescue branch.  Suppress only when several independent cues are weak,
        # or when reliable world-local yaw explicitly points against the drive.
        world_z = (wlocal / wsig) if math.isfinite(wlocal) and math.isfinite(wsig) else 0.0
        weak_motion = self.local_ready and s2 < 2.2 and p_net30 < 0.40 and pd < 0.50
        anti_world = self.local_ready and world_z < -1.0 and p_net30 < 0.70
        if passed and (weak_motion or anti_world):
            passed = False
            mode = "VETO_WORLD" if anti_world else "VETO_WEAK"

        # Final single-frame recovery, deliberately non-latching.  It requires
        # both current outward PnP velocity and accumulated 300 ms displacement;
        # neither cue alone is allowed to reopen Mouse X.
        if not passed and self.local_ready and pd >= 1.50 and p_net30 >= 0.20:
            passed = True
            mode = "MOTION_RESCUE"

        self.locked = not passed
        self.yaw_released = passed
        self.confirm_s = self.lease_s
        self.last_mode = mode
        return drive if passed else 0.0


class _RelativeYawAxisV153:
    """Action-adaptive horizontal relative-mouse ratchet.

    v3 keeps the proven v2 safety architecture (tentative/committed TURN,
    RETURNING clutch, opposite-side re-arm) but changes how a committed TURN is
    maintained and stopped.  Per-frame PnP derivatives are too noisy to be a
    reliable lease.  Instead each TURN learns its own robust outward velocity
    baseline from multi-scale trends.  Brief PnP stalls therefore do not chop a
    real turn into pulses; HOLD is entered only after the current trend remains
    substantially below this turn's own baseline.
    """

    def __init__(self, *, velocity_tau: float = YAW_V2_VELOCITY_TAU_S, acceleration_tau: float = 0.11) -> None:
        self.velocity_tau=float(velocity_tau); self.acceleration_tau=float(acceleration_tau); self.reset()

    @staticmethod
    def _sign(v: float, eps: float=0.0) -> int:
        return 1 if v>eps else -1 if v<-eps else 0

    @staticmethod
    def _exp_alpha(dt: float, tau: float) -> float:
        return 1.0-math.exp(-max(1e-4,dt)/max(1e-4,tau))

    def reset(self, norm: float=0.0, now: float=0.0) -> None:
        norm=float(norm) if math.isfinite(float(norm)) else 0.0
        self.state='CENTER'; self.velocity=0.0; self.acceleration=0.0; self.output=0.0
        self._last_t=float(now) if now else 0.0; self._last_norm=norm; self._last_raw_norm=norm
        self._motion_evidence=0.0; self._evidence_direction=0; self._evidence_age_s=0.0
        self._active_direction=0; self._active_age_s=0.0; self._committed=False
        self._return_latched=False; self._return_from_direction=0; self._return_center_seen=False; self._return_evidence=0.0
        self._opposite_rearm_guard_until=0.0
        self._resume_s=0.0; self._cross_evidence=0.0; self._center_zone=YAW_V2_CENTER_ZONE
        self._history=[]; self._peak_norm=abs(norm); self._turn_baseline=0.0; self._baseline_ready_s=0.0
        self._stop_s=0.0; self._turn_mode=''; self._center_stable_s=0.0; self._active_center_s=0.0; self._return_confirm_s=0.0
        self._hold_anchor=norm; self._held_from_turn=False; self._jump_pending_dir=0; self._jump_pending_s=0.0; self._jump_quarantine_until=0.0; self._startup_guard=False

    def _clear_motion_evidence(self) -> None:
        self._motion_evidence=0.0; self._evidence_direction=0; self._evidence_age_s=0.0

    def _clear_active(self) -> None:
        self._active_direction=0; self._active_age_s=0.0; self._committed=False
        self._return_evidence=0.0; self._turn_baseline=0.0; self._baseline_ready_s=0.0
        self._stop_s=0.0; self._turn_mode=''; self._active_center_s=0.0; self._return_confirm_s=0.0; self._startup_guard=False

    @property
    def motion_evidence(self): return self._motion_evidence
    @property
    def return_evidence(self): return self._return_evidence
    @property
    def center_zone(self): return self._center_zone
    @property
    def return_latched(self): return self._return_latched
    @property
    def committed(self): return self._committed
    @property
    def tentative_age_s(self): return self._active_age_s if self._active_direction and not self._committed else 0.0

    def _append_history(self, now: float, norm: float, raw: float) -> None:
        self._history.append((float(now),float(norm),float(raw)))
        cutoff=now-1.15
        while len(self._history)>2 and self._history[0][0]<cutoff: self._history.pop(0)

    def _trend(self, now: float, direction: int, window: float, *, raw: bool=False) -> tuple[float,float,float,float]:
        idx=2 if raw else 1
        pts=[p for p in self._history if now-p[0]<=window+1e-9]
        if len(pts)<3: return 0.0,0.0,0.0,0.0
        t0=pts[0][0]; xs=[p[0]-t0 for p in pts]; ys=[direction*p[idx] for p in pts]
        mx=sum(xs)/len(xs); my=sum(ys)/len(ys); den=sum((x-mx)**2 for x in xs)
        if den<=1e-9:return 0.0,0.0,ys[-1]-ys[0],0.0
        slope=sum((x-mx)*(y-my) for x,y in zip(xs,ys))/den
        sst=sum((y-my)**2 for y in ys)
        sse=sum((y-(my+slope*(x-mx)))**2 for x,y in zip(xs,ys))
        r2=max(0.0,min(1.0,1.0-sse/sst)) if sst>1e-10 else 0.0
        net=ys[-1]-ys[0]; path=sum(abs(ys[i]-ys[i-1]) for i in range(1,len(ys)))
        eff=abs(net)/path if path>1e-9 else 0.0
        return float(slope),float(r2),float(net),float(eff)

    def _classify_mode(self, direction: int, now: float) -> str:
        s18,_,d18,_=self._trend(now,direction,.18); r18,_,rd18,_=self._trend(now,direction,.18,raw=True)
        s45,_,d45,_=self._trend(now,direction,.45); r45,_,rd45,_=self._trend(now,direction,.45,raw=True)
        if d18>=.045 and rd18>=.035 and s18>=.34 and r18>=.24 and d45>=.030 and rd45>=.022 and s45>=.16 and r45>=.09: return 'FAST'
        if d45>=.018 and rd45>=.014 and s45>=.050 and r45>=.025: return 'NORMAL'
        return 'SLOW'

    def _drive(self, direction: int, norm: float) -> float:
        speed=max(0.0,self._turn_baseline,abs(self.velocity)*0.45)
        speed_drive=speed/(speed+YAW_V2_SPEED_KNEE) if speed>0 else 0.0
        angle_boost=.10*_clamp((abs(norm)-self._center_zone)/.28,0.0,1.0)
        mag=_clamp(max(YAW_V2_MIN_DRIVE,speed_drive+angle_boost),0.0,1.0)
        self.state='TURN_RIGHT' if direction>0 else 'TURN_LEFT'; self.output=direction*mag; return self.output

    def _begin(self,direction:int,now:float,norm:float) -> float:
        self._active_direction=direction; self._active_age_s=0.0; self._committed=False; self._active_center_s=0.0; self._held_from_turn=False
        self._return_center_seen=False
        self._peak_norm=direction*norm; self._turn_baseline=0.0; self._baseline_ready_s=0.0
        self._stop_s=0.0; self._return_confirm_s=0.0; self._turn_mode=self._classify_mode(direction,now)
        # Targeted uncertainty guard: a strong medium filtered trend combined
        # with almost no raw short-window net progress is a signature of the
        # high-noise short-flick wrong-direction cases.  Do not generalize this
        # to all tentative starts; normal slow turns retain immediate response.
        s18,_,d18,e18=self._trend(now,direction,.18); rs18,_,rd18,re18=self._trend(now,direction,.18,raw=True)
        s45,_,d45,e45=self._trend(now,direction,.45)
        self._startup_guard=bool(
            self._turn_mode!='FAST' and s45>.10 and d45>.035
            and (rd18<.006 or re18<.035)
        )
        self._clear_motion_evidence()
        if self._startup_guard:
            self.state='TURN_RIGHT' if direction>0 else 'TURN_LEFT'; self.output=0.0; return 0.0
        return self._drive(direction,norm)

    def update(self,norm:float,now:float,*,raw_norm:float|None=None,start_angle:float,start_velocity:float,
               keep_velocity:float,return_velocity:float,stop_grace_s:float,acceleration_stop:float,
               return_step:float=.025,curve_gamma:float=1.28)->float:
        del start_velocity,keep_velocity,return_velocity,stop_grace_s,acceleration_stop,curve_gamma
        norm=_clamp(norm,-4,4); raw=norm if raw_norm is None else _clamp(raw_norm,-4,4)
        dt=_clamp(now-self._last_t,1/240,.10) if self._last_t else 1/30
        fd=norm-self._last_norm; rd=raw-self._last_raw_norm
        inst=fd/dt; a=self._exp_alpha(dt,self.velocity_tau); prev=self.velocity; self.velocity+=a*(inst-self.velocity)
        self.acceleration=(self.velocity-prev)/dt
        self._last_norm=norm;self._last_raw_norm=raw;self._last_t=now;self._append_history(now,norm,raw)
        signal_dir=self._sign(norm,self._center_zone*.65); delta_dir=self._sign(fd,YAW_V2_MIN_DELTA); raw_dir=self._sign(rd,YAW_V2_MIN_DELTA)
        amount=abs(norm); center=max(self._center_zone,min(.055,start_angle*.68))

        if self._return_latched:
            self.state='RETURNING';self.output=0.0;orig=self._return_from_direction;opp=-orig if orig else 0
            if amount<=center:
                # Crossing the centre is not the same as *stopping* at centre.
                # Keep the return clutch latched while short-window motion is
                # still significant; otherwise a small overshoot can be
                # misread as a brand-new opposite turn.
                self._return_center_seen=True
                self._cross_evidence=0.0
                qdir=orig if orig else 1
                qs,_,qd,_=self._trend(now,qdir,.18); qrs,_,qrd,_=self._trend(now,qdir,.18,raw=True)
                quiet_center=abs(qs)<0.060 and abs(qrs)<0.100 and abs(qd)<0.020 and abs(qrd)<0.026
                if quiet_center:self._center_stable_s+=dt
                else:self._center_stable_s=0.0
                if self._center_stable_s>=YAW_V2_RETURN_CENTER_STABLE_S:
                    self._return_latched=False;self._return_from_direction=0;self._return_center_seen=False;self._cross_evidence=0;self._clear_active();self._clear_motion_evidence();self.state='CENTER';self._history=[(now,norm,raw)]
                return 0.0
            self._center_stable_s=0.0
            # Do not resume the original side before the calibrated centre has
            # been reached and settled.  A returning head often bounces back
            # on the same side; treating that bounce as a continuation is the
            # source of the stray movement this clutch is meant to suppress.
            # Before the centre is observed, retain the existing false-return
            # recovery: a clear, sustained return toward the original side can
            # be the filter briefly reversing during the same intentional turn.
            if not self._return_center_seen and orig and signal_dir==orig:
                sm,_,dm,em=self._trend(now,orig,.30); rsm,_,rdm,_=self._trend(now,orig,.30,raw=True)
                recovery_pos=max(center*1.15,.050)
                sshort,_,dshort,_=self._trend(now,orig,.18)
                rshort,_,rdshort,_=self._trend(now,orig,.18,raw=True)
                fast_recovery=amount>=recovery_pos and sshort>.040 and rshort>.120 and dshort>.004 and rdshort>.006
                medium_recovery=amount>=recovery_pos and dm>.018 and rdm>.012 and sm>.045 and rsm>.025
                if fast_recovery or medium_recovery:
                    self._resume_s+=dt
                    if self._resume_s>=.08:
                        self._return_latched=False;self._active_direction=orig;self._active_age_s=.2;self._committed=True
                        self._turn_mode=self._classify_mode(orig,now);self._turn_baseline=max(.035,sm*.75);self._resume_s=0;return self._drive(orig,norm)
                else:self._resume_s=max(0,self._resume_s-dt)
            # A reverse may only re-arm after the centre has actually been
            # observed.  A one-frame jump that never presents a centre sample
            # therefore remains muted as well.
            if not self._return_center_seen:
                self._cross_evidence=0.0
                return 0.0

            # v5.3: after a real centre crossing, allow a *deliberate* new turn
            # on the opposite side to re-arm without requiring the user to stop
            # dead in the narrow centre corridor first.  This fixes the old
            # RETURNING deadlock while keeping ordinary return overshoot silent:
            # the opposite pose must be far enough from centre, keep moving
            # outward, and remain coherent for a sustained interval.
            if opp and signal_dir==opp:
                s18,_,d18,e18=self._trend(now,opp,.18)
                rs18,_,rd18,re18=self._trend(now,opp,.18,raw=True)
                opposite_speed=opp*self.velocity
                deliberate_pos=amount>=max(YAW_V2_OPPOSITE_REARM_AFTER_CENTER, center*2.4)
                deliberate_motion=(
                    opposite_speed>=YAW_V2_OPPOSITE_REARM_AFTER_CENTER_VELOCITY
                    and d18>=.020 and rd18>=.012
                    and s18>=.080 and rs18>=.045
                    and (e18>=.18 or re18>=.14)
                )
                if deliberate_pos and deliberate_motion:
                    self._cross_evidence+=dt
                else:
                    self._cross_evidence=max(0.0,self._cross_evidence-dt*1.5)

                # v5.3 adaptive confirmation: do not shorten the safety dwell
                # near the centre.  Only once the new-side turn is clearly past
                # ordinary return overshoot (>= 0.25 normalized deflection) may
                # strong outward speed/trend reduce the required dwell.
                opposite_rearm_s=YAW_V2_OPPOSITE_REARM_AFTER_CENTER_S
                if amount>=.25 and deliberate_motion:
                    speed_score=_clamp((opposite_speed-.30)/.90,0.0,1.0)
                    trend_score=_clamp((min(s18,rs18)-.15)/.70,0.0,1.0)
                    confidence=.65*speed_score+.35*trend_score
                    opposite_rearm_s=_clamp(
                        YAW_V2_OPPOSITE_REARM_AFTER_CENTER_S-.070*confidence,
                        .090,YAW_V2_OPPOSITE_REARM_AFTER_CENTER_S,
                    )
                if self._cross_evidence>=opposite_rearm_s:
                    self._return_latched=False
                    self._return_from_direction=0
                    self._return_center_seen=False
                    self._center_stable_s=0.0
                    self._cross_evidence=0.0
                    self._resume_s=0.0
                    self._clear_active()
                    self._clear_motion_evidence()
                    # Keep only the recent opposite-side history so the new
                    # action is classified from the new gesture, not from the
                    # preceding return from the old side.
                    cutoff=now-.30
                    self._history=[h for h in self._history if h[0]>=cutoff]
                    return self._begin(opp,now,norm)
            else:
                self._cross_evidence=max(0.0,self._cross_evidence-dt*2.0)
            return 0.0

        if amount<=center:
            if self._active_direction:
                d=self._active_direction
                if self._committed:
                    # Preserve evidence that a committed turn actually
                    # visited the calibrated centre, even if the clutch is
                    # entered on the following opposite-side sample.
                    self._return_center_seen=True
                self._active_center_s+=dt
                grace=.35 if self._committed else .18
                if self._committed and self._active_center_s<=.09:
                    s90,_,_,_=self._trend(now,d,.90); rs90,_,_,_=self._trend(now,d,.90,raw=True)
                    if s90>.045 and rs90>.008:
                        full=self._drive(d,norm); self.output=full*.42; return self.output
                if self._active_center_s<grace:
                    self.state='TURN_RIGHT' if d>0 else 'TURN_LEFT'; self.output=0.0; return 0.0
            self.state='CENTER';self.output=0;self._clear_active();self._clear_motion_evidence();return 0.0

        if self._active_direction:
            d=self._active_direction;self._active_age_s+=dt;self._active_center_s=0.0;proj=d*norm
            # If the filtered signal has actually crossed outside the centre on
            # the opposite side, never continue driving the old direction.
            # Committed turns enter the existing RETURNING clutch; tentative
            # turns simply cancel.
            if signal_dir==-d or d*raw < -center:
                if self._committed:
                    center_seen=self._return_center_seen
                    self._return_latched=True;self._return_from_direction=d;self._return_center_seen=center_seen;self._center_stable_s=0;self._cross_evidence=0;self._clear_active();self._clear_motion_evidence();self.state='RETURNING';self.output=0.0;return 0.0
                self._clear_active();self._clear_motion_evidence();self.state='STABLE_OFFSET';self.output=0.0;return 0.0
            if self._startup_guard:
                self._startup_guard=False
                full=self._drive(d,norm)
                self.output=_clamp(full*1.35,-1.0,1.0)
                return self.output
            if proj>self._peak_norm:self._peak_norm=proj
            s18,r218,d18,e18=self._trend(now,d,.18);rs18,_,rd18,_=self._trend(now,d,.18,raw=True)
            s45,r245,d45,e45=self._trend(now,d,.45);rs45,_,rd45,_=self._trend(now,d,.45,raw=True)
            s90,r290,d90,e90=self._trend(now,d,.90);rs90,_,rd90,_=self._trend(now,d,.90,raw=True)
            # A committed turn must surrender the mouse as soon as a clear
            # return trend starts.  Waiting for the signal to cross the centre
            # leaves the old direction driving throughout most of the user's
            # physical return.  The retreat and short-window slope together
            # reject a single noisy sample while stopping the real return
            # before it can be mistaken for a reverse turn.
            retreat=self._peak_norm-proj
            early_return=(
                self._committed
                and retreat>=YAW_V2_RETURN_EARLY_RETREAT
                and d*fd<=-YAW_V2_MIN_DELTA
                and s18<=-YAW_V2_RETURN_EARLY_TREND
            )
            if early_return:
                # Do not let a single small back-swing seize the clutch.  A
                # sustained, coherent reversal is still accepted through the
                # time gate, while a larger reversal is accepted immediately.
                self._return_confirm_s += dt
            else:
                self._return_confirm_s = max(0.0, self._return_confirm_s - dt * 0.6)
            early_return_confirmed = (
                early_return
                and (
                    retreat >= YAW_RETURN_MIN_REVERSAL_NORM
                    or (
                        self._return_confirm_s >= YAW_RETURN_MIN_REVERSAL_S
                        and retreat >= YAW_V2_RETURN_EARLY_RETREAT
                    )
                )
            )
            if early_return_confirmed:
                self._return_latched=True;self._return_from_direction=d;self._return_center_seen=False;self._center_stable_s=0;self._cross_evidence=0;self._clear_active();self._clear_motion_evidence();self.state='RETURNING';self.output=0.0;return 0.0
            # commit is position/time plus coherent trend; no single derivative ticket
            if not self._committed and (proj>=YAW_V2_COMMIT_ANGLE or (self._active_age_s>=.16 and proj>=center*1.35)):
                self._committed=True; self._turn_mode=self._classify_mode(d,now)
            # action baseline: conservative upward update, never one-frame max
            candidates=[v for v in (s18,s45,s90,rs45) if v>0]
            cur=statistics.median(candidates) if candidates else 0.0
            if cur>0:
                if self._turn_baseline<=0:self._turn_baseline=cur
                else:
                    cap=max(self._turn_baseline*1.35,self._turn_baseline+.018)
                    target=min(cur,cap); alpha=1-math.exp(-dt/.75); self._turn_baseline+=alpha*(target-self._turn_baseline)
                self._baseline_ready_s+=dt
            # immediate huge retreat kept for direct 9->6-style compatibility; ordinary PnP needs trend confirmation
            huge=(d*rd<=-.13 and retreat>=YAW_RETURN_MIN_REVERSAL_NORM)
            if self._turn_mode=='FAST': ret=retreat>=.045 and s18<=-.10 and (rs18<=-.06 or s45<=-.055); need=.07
            elif self._turn_mode=='NORMAL': ret=retreat>=.055 and s45<=-.045 and rs45<=-.025; need=.12
            else: ret=retreat>=.060 and s45<=-.030 and rs45<=-.018 and (e45>.12 or r245>.18); need=.20
            if self._committed and (huge or ret): self._return_confirm_s+=dt
            else:self._return_confirm_s=max(0,self._return_confirm_s-dt*.6)
            if self._committed and (huge or self._return_confirm_s>=need):
                self._return_latched=True;self._return_from_direction=d;self._return_center_seen=False;self._center_stable_s=0;self._cross_evidence=0;self._clear_active();self._clear_motion_evidence();self.state='RETURNING';self.output=0;return 0.0
            # A stationary raw source must not inherit a long-window speed
            # lease. Keep the hold anchor so later outward motion can resume.
            if self._committed and abs(rd18) < .004 and abs(rs18) < .025 and abs(d18) < .008:
                self._hold_anchor=norm; self._held_from_turn=True
                self._clear_active(); self._clear_motion_evidence()
                self.state='STABLE_OFFSET'; self.output=0.0
                self._history=[(now,norm,raw)]
                return 0.0
            # Continue/stop relative to this action's own learned speed. Freeze mode for the whole turn.
            base=max(.012,self._turn_baseline)
            if self._turn_mode=='FAST': cur_speed=max(s18,s45*.7); ratio=.24; floor=.035; stop_need=.16
            elif self._turn_mode=='NORMAL': cur_speed=max(s45,s18*.35,s90*0.60); ratio=.25; floor=.018; stop_need=0.40
            else: cur_speed=max(s90,s45*.45); ratio=.20; floor=.006; stop_need=.68
            moving=cur_speed>max(floor,base*ratio) or (self._baseline_ready_s<.35 and cur_speed>floor*.7)
            if moving:self._stop_s=max(0,self._stop_s-dt*.8)
            else:self._stop_s+=dt
            if self._committed and self._stop_s>=stop_need:
                self._hold_anchor=norm;self._held_from_turn=True;self._clear_active();self._clear_motion_evidence();self.state='STABLE_OFFSET';self.output=0;self._history=[(now,norm,raw)];return 0.0
            full=self._drive(d,norm)
            if now<self._opposite_rearm_guard_until:
                self.output=0.0
                return 0.0
            return full

        # A completed TURN owns a real hold anchor.  An untriggered head that
        # merely drifted outside centre does *not*; it must keep evaluating the
        # regular tentative-start evidence every frame.  Conflating those two
        # states was a major slow-turn dead zone.
        if self.state=='STABLE_OFFSET' and self._held_from_turn:
            side=self._sign(self._hold_anchor) or signal_dir
            if side and side*norm < side*self._hold_anchor-max(.04,center*.8):
                sm,_,dm,_=self._trend(now,side,.30);rsm,_,rdm,_=self._trend(now,side,.30,raw=True)
                if sm<-.04 and (rsm<-.025 or dm<-.018):
                    self._return_latched=True;self._return_from_direction=side;self._return_center_seen=False;self._center_stable_s=0;self._cross_evidence=0;self.state='RETURNING';self.output=0;return 0.0
            if side and signal_dir==side and side*(norm-self._hold_anchor)>=max(.035,center*.65):
                sm,_,dm,em=self._trend(now,side,.35);rsm,_,rdm,_=self._trend(now,side,.35,raw=True)
                if sm>.045 and rsm>.025 and dm>.014:return self._begin(side,now,norm)
            self.output=0;return 0.0

        # Scale spike rejection with frame duration: an ordinary quick turn
        # at 15 FPS must not be rejected by a threshold tuned for 60 FPS.
        # Quarantine truly extreme jumps and their filter tail.
        gross_jump=abs(fd)>=max(.32,12.0*dt) or abs(rd)>=max(.40,15.0*dt)
        if gross_jump:
            self._jump_quarantine_until=now+.22
            self._history=[(now,norm,raw)]
            self._jump_pending_dir=0;self._jump_pending_s=0.0
            self.output=0.0;self.state='STABLE_OFFSET';self._held_from_turn=False
            return 0.0
        if now<self._jump_quarantine_until:
            self.output=0.0;self.state='STABLE_OFFSET';self._held_from_turn=False
            return 0.0

        # Tentative start: fast/normal use short/medium trends; extreme slow gets a long-window path.
        d=signal_dir
        if d:
            s18,_,d18,e18=self._trend(now,d,.18);rs18,_,rd18,_=self._trend(now,d,.18,raw=True)
            s45,r245,d45,e45=self._trend(now,d,.45);rs45,_,rd45,_=self._trend(now,d,.45,raw=True)
            s90,r290,d90,e90=self._trend(now,d,.90);rs90,_,rd90,_=self._trend(now,d,.90,raw=True)
            fast=d18>.028 and rd18>.020 and s18>.18 and rs18>.12
            normal=d45>.018 and rd45>.012 and s45>.040 and rs45>.020 and (e45>.16 or r245>.15)
            slow=d90>.012 and rd90>.006 and s90>.010 and rs90>-.004 and (e90>.12 or r290>.20)
            position_ok=amount>=max(center*1.10, start_angle)
            candidate=bool(position_ok and (fast or normal or slow))
            huge_step=gross_jump
            if candidate and huge_step:
                if self._jump_pending_dir==d:self._jump_pending_s+=dt
                else:self._jump_pending_dir=d;self._jump_pending_s=dt
                if self._jump_pending_s>=.075:
                    self._jump_pending_dir=0;self._jump_pending_s=0;return self._begin(d,now,norm)
            elif candidate:
                self._jump_pending_dir=0;self._jump_pending_s=0;return self._begin(d,now,norm)
            else:
                self._jump_pending_dir=0;self._jump_pending_s=0
        self.state='STABLE_OFFSET';self._held_from_turn=False;self.output=0;return 0.0

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
    # Independent 2D nose/eye yaw proxy.  It is used only to corroborate the
    # neutral state; PnP remains the single owner of angle magnitude and sign.
    # This avoids turning a camera/mirror ambiguity into a global sign patch.
    yaw_proxy: float = math.nan


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
        self.reset()
        self._pnp_model = tuple(tuple(float(v) for v in p) for p in self._MODEL)
        # Raw, centred MediaPipe-world template in metres.  Keep it separate
        # from _pnp_model, whose personal representation is converted to mm.
        # Re-feeding _pnp_model through set_pnp_model() would otherwise multiply
        # an already-converted model by 1000 on every restore/recalibration.
        self._personal_source_model: tuple[tuple[float, float, float], ...] | None = None
        self.personal_model_active = False
        self.pnp_available = self._probe_pnp()
        self.pnp_error = "" if self.pnp_available else "OpenCV/numpy 不可用"

    def set_pnp_model(self, model: tuple[tuple[float, float, float], ...] | None = None) -> bool:
        """Set an instance-specific seven-point PnP face model.

        ``None`` restores the proven generic model.  A personal model is built
        only from the calibration-time MediaPipe world landmarks; world data is
        never consumed by the per-frame mouse-control path after activation.
        """
        if model is None:
            self._pnp_model = tuple(tuple(float(v) for v in p) for p in self._MODEL)
            self._personal_source_model = None
            self.personal_model_active = False
            self.reset()
            return True
        try:
            pts = tuple(tuple(float(v) for v in p) for p in model)
        except Exception:
            return False
        if len(pts) != 7 or any(len(p) != 3 or not all(math.isfinite(v) for v in p) for p in pts):
            return False
        cx = sum(p[0] for p in pts) / 7.0
        cy = sum(p[1] for p in pts) / 7.0
        cz = sum(p[2] for p in pts) / 7.0
        centered = tuple((p[0]-cx, p[1]-cy, p[2]-cz) for p in pts)
        scale = math.sqrt(sum(x*x+y*y+z*z for x, y, z in centered) / 7.0)
        if not math.isfinite(scale) or scale <= 1e-6:
            return False
        # MediaPipe world coordinates are in metres.  Uniform model scale does
        # not change recovered rotation; mm keeps numerical magnitudes similar
        # to the original generic sparse-face template.
        self._personal_source_model = tuple(tuple(float(v) for v in point) for point in centered)
        self._pnp_model = tuple((x*1000.0, y*1000.0, z*1000.0) for x, y, z in centered)
        self.personal_model_active = True
        self.reset()
        return True

    @property
    def pnp_model(self) -> tuple[tuple[float, float, float], ...]:
        return self._pnp_model

    @property
    def personal_source_model(self) -> tuple[tuple[float, float, float], ...] | None:
        """Centred personal template in the metre-space expected by set_pnp_model()."""
        return self._personal_source_model

    @property
    def pnp_depth(self) -> float:
        """Current solvePnP camera-Z translation in model units."""
        try:
            z = float(self._tvec[2, 0]) if self._tvec is not None else math.nan
        except Exception:
            return math.nan
        return z if math.isfinite(z) and z > 0.0 else math.nan

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
        self._pnp_failure_frames = 0
        self._pnp_reacquiring = False
        self._pnp_reacquire_pose: tuple[float, float, float] | None = None
        self._pnp_reacquire_count = 0

    def _pnp_failure(self, error: str, reprojection_error: float = math.nan) -> HeadEstimate:
        self._pnp_failure_frames += 1
        self._pnp_reacquire_pose = None
        self._pnp_reacquire_count = 0
        if self._pnp_failure_frames >= PNP_REACQUIRE_FAILURE_FRAMES:
            # Discard both the solver guess and its stale jump reference.
            # The following frames must pass geometry and stability checks.
            self._rvec = self._tvec = None
            self._last_pnp = None
            self._pnp_reacquiring = True
        return HeadEstimate(
            False, algorithm="pnp", error=error,
            reprojection_error=reprojection_error,
        )

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
            error = "缺少关键点：" + ",".join(missing)
            if algorithm == "pnp":
                return self._pnp_failure(error)
            return HeadEstimate(False, algorithm=algorithm, error=error)
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
            model_points = np.asarray(self._pnp_model, dtype=np.float64)
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
                    self._rvec.copy(), self._tvec.copy(), useExtrinsicGuess=True,
                    flags=cv2.SOLVEPNP_ITERATIVE,
                )
            else:
                first_flag = getattr(cv2, "SOLVEPNP_SQPNP", cv2.SOLVEPNP_EPNP)
                ok, rvec, tvec = cv2.solvePnP(
                    model_points, image_points, camera, dist, flags=first_flag
                )
            if not ok:
                return self._pnp_failure("solvePnP 未收敛")
            R, _ = cv2.Rodrigues(rvec)
            yaw, pitch, roll = self._rotation_to_euler_deg(R)
            if not all(math.isfinite(v) for v in (yaw, pitch, roll)):
                return self._pnp_failure("PnP 输出非有限值")
            if abs(yaw) > 90.0 or abs(pitch) > 70.0 or abs(roll) > 75.0:
                return self._pnp_failure("PnP 姿态超出可信范围")
            if self._last_pnp is not None:
                if max(abs(yaw - self._last_pnp[0]), abs(pitch - self._last_pnp[1])) > MAX_PNP_STEP_DEG:
                    # Do not accept a one-frame branch flip.  The next frame
                    # can still converge using the previous valid guess.
                    return self._pnp_failure("PnP 单帧跳变被拒绝")
            projected, _ = cv2.projectPoints(model_points, rvec, tvec, camera, dist)
            projected = projected.reshape(-1, 2)
            rmse_px = float(np.sqrt(np.mean(np.sum((projected - image_points) ** 2, axis=1))))
            face_px = max(8.0, float(np.linalg.norm(image_points[5] - image_points[6])))
            reprojection = rmse_px / face_px
            if not math.isfinite(reprojection) or reprojection > 0.22:
                return self._pnp_failure(
                    f"PnP 重投影误差过大：{reprojection:.3f}", reprojection,
                )

            self._rvec, self._tvec = rvec.copy(), tvec.copy()
            self._pnp_failure_frames = 0
            if self._pnp_reacquiring:
                candidate = (yaw, pitch, roll)
                reference = self._pnp_reacquire_pose
                if reference is None or max(
                    abs(value - previous) for value, previous in zip(candidate, reference)
                ) > PNP_REACQUIRE_MAX_SPREAD_DEG:
                    self._pnp_reacquire_pose = candidate
                    self._pnp_reacquire_count = 1
                else:
                    self._pnp_reacquire_count += 1
                if self._pnp_reacquire_count < PNP_REACQUIRE_CONFIRM_FRAMES:
                    return HeadEstimate(False, algorithm="pnp", error="PnP 重新跟踪：等待姿态稳定")
                self._pnp_reacquiring = False
                self._pnp_reacquire_pose = None
                self._pnp_reacquire_count = 0
            self._last_pnp = (yaw, pitch, roll)
            landmark_conf = sum(_score(pose[name]) for name in self._PNP_NAMES) / len(self._PNP_NAMES)
            geometry_conf = _clamp(1.0 - reprojection / 0.22, 0.20, 1.0)
            confidence = landmark_conf * geometry_conf
            # With the anatomical-left/right model above, positive yaw means
            # subject-right and positive pitch means down.  This is the kernel
            # canonical convention: left/up negative, right/down positive.
            ratio = self._estimate_ratio(pose)
            yaw_proxy = ratio.yaw if ratio.valid and math.isfinite(ratio.yaw) else math.nan
            return HeadEstimate(
                True, yaw, pitch, roll, confidence, "pnp",
                reprojection_error=reprojection, yaw_proxy=yaw_proxy,
            )
        except Exception as exc:
            self.pnp_error = str(exc)
            return self._pnp_failure(f"PnP 失败：{exc}")

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
        return HeadEstimate(True, yaw, pitch, roll, confidence, "ratio", yaw_proxy=yaw)


class HeadController:
    """Clean center-only head controller."""

    # Immutable calibration values that must stay paired with the restored
    # PnP model and Frozen22 center when a replacement calibration fails.
    _CALIBRATION_AUX_FIELDS = (
        "_calibration_algorithm", "_personal_policy_store",
        "center_multi2d_proxy", "noise_multi2d_proxy",
        "center_world_face_template", "center_world_rigid_yaw", "noise_world_rigid_yaw",
        "center_world_yaw", "noise_world_yaw", "center_norm_z_yaw", "noise_norm_z_yaw",
        "personal_pnp_valid_samples", "personal_pnp_median_reprojection",
        "personal_pnp_rejection_reason", "personal_pnp_calibration_derotate_yaw",
        "personal_pnp_calibration_derotate_roll", "personal_pnp_world_pair_yaw_deviation",
        "personal_pnp_world_pair_yaws", "personal_pnp_center_depth",
        "personal_pnp_current_depth", "personal_pnp_far_depth_ratio",
    )

    def __init__(self, profile_path: Path | None = None) -> None:
        self.estimator = HeadPoseEstimator()
        self.profile_path = profile_path
        self.config = dict(DEFAULT_CONFIG)
        self.center_yaw = 0.0
        self.center_pitch = 0.0
        self.center_yaw_proxy = math.nan
        self.noise_yaw = 0.0
        self.noise_pitch = 0.0
        self.noise_yaw_proxy = 0.0
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
        self.center_yaw_proxy_samples: list[float] = []
        self.center_confidence_samples: list[float] = []
        self.raw = HeadEstimate(False, algorithm=self.config["algorithm"])
        self.filtered_yaw = math.nan
        self.filtered_pitch = math.nan
        self.norm_x = 0.0
        self.norm_y = 0.0
        # Filtered raw signals remain available even before a center is
        # calibrated.  The kernel uses ``signal_pitch`` to establish a fresh
        # temporary center when the left-hand look gate opens.
        self.signal_yaw = math.nan
        self.signal_pitch = math.nan
        self.control_yaw = math.nan
        self.yaw_proxy = math.nan
        self.yaw_proxy_delta = math.nan
        self.yaw_guard_state = "fallback"
        self.yaw_velocity = 0.0
        self.yaw_acceleration = 0.0
        self.pitch_velocity = 0.0
        self.pitch_acceleration = 0.0
        self.yaw_intent_state = "IDLE"
        self.pitch_intent_state = "IDLE"
        self.output_x = 0.0
        self.output_y = 0.0
        self.effective_deadzone_x = float(self.config["deadzone"])
        self.effective_deadzone_y = float(self.config["deadzone"])
        self._axis_active_x = False
        self._axis_active_y = False
        self._last_update = 0.0
        self._yaw_filter = OneEuro(1.10, 0.050, 1.0)
        self._pitch_filter = OneEuro(1.05, 0.060, 1.0)
        self._yaw_intent = IntentAxis("yaw")
        self._pitch_intent = IntentAxis("pitch")
        # v153 personal-PnP policy state (selectable via horizontal_algorithm).
        self._head_point_filter = _LandmarkEMA(HEAD_POINT_EMA_TAU_S)
        self._x_intent_v153 = _RelativeYawAxisV153(velocity_tau=YAW_V2_VELOCITY_TAU_S, acceleration_tau=0.11)
        self._cross_axis_lock = _CrossAxisPitchLock()
        self.current_multi2d_proxy: tuple[float, ...] | None = None
        self.center_multi2d_proxy: tuple[float, ...] | None = None
        self.noise_multi2d_proxy: tuple[float, ...] | None = None
        self.center_multi2d_proxy_samples: list[tuple[float, ...]] = []
        self.center_world_face_samples: list[tuple[tuple[float, float, float], ...]] = []
        self.center_world_face_template: tuple[tuple[float, float, float], ...] | None = None
        # frozen22 fixed-signature measurement state.  Only the neutral center
        # and per-channel noise are learned at runtime; the 22 coefficients are
        # the audited R3 constants above.
        self.current_personal22_feature: tuple[float, ...] | None = None
        self.current_personal22_eye_px = math.nan
        self.frozen22_missing_points: tuple[str, ...] = HEAD11_NAMES
        self.frozen22_center: tuple[float, ...] | None = None
        self.frozen22_sigma: tuple[float, ...] | None = None
        self.frozen22_yaw = math.nan
        self.frozen22_yaw_median = math.nan
        self.frozen22_cal_sigma_deg = math.inf
        self.frozen22_world_resid_rel = math.inf
        self.frozen22_calibration_valid = False
        self._frozen22_history: list[float] = []
        self._base_output_x = 0.0
        self._frozen22_gate_history: list[tuple[float, float, float, float]] = []
        self._frozen22_gate_direction = 0
        self._frozen22_gate_fast_count = 0
        self._frozen22_gate_lease_direction = 0
        self._frozen22_gate_lease_until = 0.0
        self.frozen22_gate_scale = 1.0
        self.frozen22_gate_source = "DISABLED"
        self.frozen22_gate_fast_delta_deg = math.nan
        self.frozen22_gate_slow_delta_deg = math.nan
        self.frozen22_gate_slow_world_delta_deg = math.nan
        self.frozen22_gate_pitch_delta_deg = math.nan
        self.frozen22_gate_fast_threshold_deg = math.nan
        self.frozen22_gate_slow_threshold_deg = math.nan
        # v188 fail-safe dropout memory.  Only an already-confirmed, previously
        # allowed turn may bridge a very short auxiliary-signal dropout.
        self._frozen22_gate_last_valid_t = -math.inf
        self._frozen22_gate_last_valid_direction = 0
        self._frozen22_gate_last_valid_scale = 0.0
        self.v188_pitch_guard_active = False
        self._reset_output_guards()
        self.center_personal22_feature_samples: list[tuple[float, ...]] = []
        self.center_world_head11_samples: list[tuple[tuple[float, float, float], ...]] = []
        self._calibration_restore_frozen22: tuple[
            tuple[float, ...] | None,
            tuple[float, ...] | None,
            float,
            float,
            bool,
        ] | None = None
        self.center_pnp_pose_samples: list[tuple[dict[str, dict], int, int]] = []
        self.personal_pnp_active = False
        self.personal_pnp_valid_samples = 0
        self.personal_pnp_median_reprojection = math.nan
        self.personal_pnp_rejection_reason = "未尝试"
        self._calibration_restore_model: tuple[tuple[float, float, float], ...] | None = None
        self._calibration_restore_personal_active = False
        self._calibration_restore_center_state: dict[str, Any] | None = None
        self._calibration_restore_aux_state: dict[str, Any] | None = None
        self.current_world_rigid_yaw = math.nan
        self.current_world_rigid_fit = math.nan
        self.center_world_rigid_yaw = math.nan
        self.noise_world_rigid_yaw = math.inf
        self.center_world_yaw = math.nan
        self.noise_world_yaw = math.inf
        self.center_norm_z_yaw = math.nan
        self.noise_norm_z_yaw = math.inf
        self.center_world_yaw_samples: list[float] = []
        self.center_norm_z_yaw_samples: list[float] = []
        self.current_world_yaw = math.nan
        self.current_norm_z_yaw = math.nan
        self.center_roll_samples: list[float] = []
        self.personal_pnp_calibration_derotate_yaw = 0.0
        self.personal_pnp_calibration_derotate_roll = 0.0
        self.personal_pnp_world_pair_yaw_deviation = math.inf
        self.personal_pnp_world_pair_yaws: tuple[float, float, float] | None = None
        self.personal_pnp_center_depth = math.nan
        self.personal_pnp_current_depth = math.nan
        self.personal_pnp_far_depth_ratio = 1.0
        self._calibration_restore_depth = math.nan
        self._generic_center = (math.nan, 0.0, math.nan, 0.0)
        self._calibration_algorithm: str | None = None
        self._personal_policy_store: dict[str, dict] = {}
        self.last_error = ""
        self.notice = ""
        self.notice_until = 0.0
        self._last_intent_drive = 0.0
        self._last_evidence_scale = 0.0
        self._last_target_x = 0.0
        self._load_profile()

    def _effective_estimator_algorithm(self) -> str:
        # raw_yaw/raw_pitch/signal_pitch keep the units advertised to the
        # existing kernel. Fixed22 has a separate, explicitly labelled yaw
        # source; selecting it must not secretly change PnP degrees to ratios.
        return self.config["algorithm"]

    def _span(self) -> tuple[float, float]:
        if self.config.get("horizontal_algorithm") in FROZEN22_POLICIES:
            pitch_span = PNP_PITCH_SPAN_DEG if self._effective_estimator_algorithm() == "pnp" else RATIO_PITCH_SPAN
            return FROZEN22_YAW_SPAN_DEG, pitch_span
        if self.config["algorithm"] == "pnp":
            return PNP_YAW_SPAN_DEG, PNP_PITCH_SPAN_DEG
        return RATIO_YAW_SPAN, RATIO_PITCH_SPAN

    def _reset_output_guards(self) -> None:
        """Forget output provenance and leases when a tracking segment ends."""
        self.v202_pitch_output_veto_active = False
        self.v202_return_output_veto_active = False
        self._v202_prev_return_latched = False
        self._v202_return_from_direction = 0
        self._v202_last_visible_output_direction = 0
        self._v202_last_visible_output_t = -math.inf
        self._v202_rearm_block_direction = 0
        self._v202_rearm_quiet_hold_direction = 0
        self._v202_pitch_clean_s = 0.0
        self._v202_pitch_clean_reset_ready = False
        self._v205_same_side_rescue_s = 0.0
        self._v205_same_side_rescue_last_t = 0.0
        self.v205_same_side_rescue_active = False
        self._cue_direction = 0
        self._cue_since = 0.0
        self._cue_frames = 0
        self._cue_lease_until = 0.0
        self._cue_last_pitch = math.nan
        self._cue_last_at = 0.0

    def _reset_filters(self) -> None:
        self._yaw_filter.reset()
        self._pitch_filter.reset()
        self._head_point_filter.reset()
        self._x_intent_v153.reset()
        self._reset_frozen22_gate()
        self._reset_output_guards()
        self.v188_pitch_guard_active = False
        self._cross_axis_lock.reset()
        self.filtered_yaw = math.nan
        self.filtered_pitch = math.nan
        self.signal_yaw = math.nan
        self.signal_pitch = math.nan
        self.control_yaw = math.nan
        self.yaw_proxy = math.nan
        self.yaw_proxy_delta = math.nan
        self.yaw_guard_state = "fallback"
        self.yaw_velocity = 0.0
        self.yaw_acceleration = 0.0
        self.pitch_velocity = 0.0
        self.pitch_acceleration = 0.0
        self.yaw_intent_state = "IDLE"
        self.pitch_intent_state = "IDLE"
        self.yaw_evidence = 0.0
        self.yaw_trend = 0.0
        self.yaw_engagement = 0.0
        self.yaw_engage_floor = 0.0
        self._yaw_intent.reset()
        self._pitch_intent.reset()
        self._axis_active_x = False
        self._axis_active_y = False
        self.current_personal22_feature = None
        self.current_personal22_eye_px = math.nan
        self.frozen22_missing_points = HEAD11_NAMES
        self.frozen22_yaw = math.nan
        self.frozen22_yaw_median = math.nan
        self._frozen22_history.clear()
        self.output_x = self.output_y = 0.0
        self.norm_x = self.norm_y = 0.0
        self._last_intent_drive = 0.0
        self._last_evidence_scale = 0.0
        self._last_target_x = 0.0
        self._last_update = 0.0

    def reset_tracking(self) -> None:
        self.estimator.reset()
        self._reset_filters()
        self.raw = HeadEstimate(False, algorithm=self.config["algorithm"])
        self.last_error = ""

    @staticmethod
    def _snapshot_pnp_pose(pose: dict[str, dict] | None) -> dict[str, dict] | None:
        if not isinstance(pose, dict):
            return None
        out: dict[str, dict] = {}
        for name in HeadPoseEstimator._PNP_NAMES:
            pt = pose.get(name)
            if not isinstance(pt, dict):
                return None
            x, y = _finite(pt.get("x")), _finite(pt.get("y"))
            if not (math.isfinite(x) and math.isfinite(y)):
                return None
            out[name] = {"x": x, "y": y, "score": max(0.40, _score(pt))}
        return out

    def _activate_personal_pnp_from_center(self) -> bool:
        """v153: build and validate the personal PnP model from this calibration.

        The accepted calibration frames are re-solved offline with the personal
        3D model, so one user calibration supplies both the model and the
        matching neutral center.  Gate order: world rigid sigma, eye/mouth/ear
        pair consistency, then optional de-rotation of a clearly biased
        calibration pose (world-yaw cue for yaw, generic PnP for roll).
        """
        self.personal_pnp_active = False
        self.personal_pnp_valid_samples = 0
        self.personal_pnp_median_reprojection = math.nan
        self.personal_pnp_rejection_reason = "校准数据检查中"
        self.personal_pnp_center_depth = math.nan
        self.personal_pnp_far_depth_ratio = 1.0
        self.personal_pnp_calibration_derotate_yaw = 0.0
        self.personal_pnp_calibration_derotate_roll = 0.0
        self.personal_pnp_world_pair_yaw_deviation = math.inf
        self.personal_pnp_world_pair_yaws = None
        template = self.center_world_face_template
        set_model = getattr(self.estimator, "set_pnp_model", None)
        if (
            set_model is None
            or self.config.get("algorithm") != "pnp"
            or template is None
            or len(self.center_pnp_pose_samples) < PERSONAL_PNP_MIN_SAMPLES
            or not math.isfinite(self.noise_world_rigid_yaw)
            or self.noise_world_rigid_yaw > PERSONAL_PNP_MAX_WORLD_RIGID_SIGMA_DEG
        ):
            if template is None or len(self.center_world_face_samples) < PERSONAL_PNP_MIN_SAMPLES:
                self.personal_pnp_rejection_reason = "世界脸部模板样本不足"
            elif len(self.center_pnp_pose_samples) < PERSONAL_PNP_MIN_SAMPLES:
                self.personal_pnp_rejection_reason = "校准姿态样本不足"
            elif not math.isfinite(self.noise_world_rigid_yaw):
                self.personal_pnp_rejection_reason = "世界坐标稳定性不足"
            elif self.noise_world_rigid_yaw > PERSONAL_PNP_MAX_WORLD_RIGID_SIGMA_DEG:
                self.personal_pnp_rejection_reason = "世界坐标抖动过大"
            else:
                self.personal_pnp_rejection_reason = "当前估计器不支持个人模型"
            return False

        pair_dev, pair_yaws = _world_pair_yaw_consistency(template)
        self.personal_pnp_world_pair_yaw_deviation = pair_dev
        self.personal_pnp_world_pair_yaws = pair_yaws
        if not math.isfinite(pair_dev) or pair_dev > PERSONAL_PNP_MAX_WORLD_PAIR_YAW_DEVIATION_DEG:
            self.personal_pnp_rejection_reason = "世界脸部左右点不一致"
            return False

        generic_roll, _ = _robust_center_and_sigma(self.center_roll_samples)
        pair_center_yaw = _median(list(pair_yaws)) if pair_yaws is not None else math.nan
        corr_yaw = pair_center_yaw if math.isfinite(pair_center_yaw) and abs(pair_center_yaw) >= PERSONAL_PNP_CALIB_DEROTATE_YAW_DEG else 0.0
        corr_roll = generic_roll if math.isfinite(generic_roll) and abs(generic_roll) >= PERSONAL_PNP_CALIB_DEROTATE_ROLL_DEG else 0.0
        if corr_yaw or corr_roll:
            template = _derotate_personal_template(template, yaw_deg=corr_yaw, roll_deg=corr_roll)
            self.personal_pnp_calibration_derotate_yaw = float(corr_yaw)
            self.personal_pnp_calibration_derotate_roll = float(corr_roll)

        offline = HeadPoseEstimator()
        if not offline.set_pnp_model(template):
            self.personal_pnp_rejection_reason = "个人脸部模型无效"
            return False
        ys: list[float] = []
        ps: list[float] = []
        reps: list[float] = []
        depths: list[float] = []
        for snap, w, h in self.center_pnp_pose_samples:
            e = offline.estimate(snap, w, h, "pnp")
            if not e.valid:
                continue
            ys.append(e.yaw)
            ps.append(e.pitch)
            if math.isfinite(e.reprojection_error):
                reps.append(e.reprojection_error)
            if math.isfinite(offline.pnp_depth):
                depths.append(offline.pnp_depth)
        if len(ys) < PERSONAL_PNP_MIN_SAMPLES:
            self.personal_pnp_rejection_reason = "个人模型有效重投影样本不足"
            return False
        cy, ny = _robust_center_and_sigma(ys)
        cp, npitch = _robust_center_and_sigma(ps)
        med_rep = _median(reps) if reps else math.inf
        med_depth = _median(depths) if depths else math.nan
        if not (math.isfinite(cy) and math.isfinite(cp) and math.isfinite(ny) and math.isfinite(npitch)):
            self.personal_pnp_rejection_reason = "个人模型中心计算失败"
            return False
        if not math.isfinite(med_rep) or med_rep > PERSONAL_PNP_MAX_MEDIAN_REPROJECTION:
            self.personal_pnp_rejection_reason = "个人模型重投影误差过大"
            return False
        if not math.isfinite(med_depth) or med_depth <= 1e-6:
            self.personal_pnp_rejection_reason = "个人模型深度无效"
            return False
        if not set_model(template):
            self.personal_pnp_rejection_reason = "个人模型安装失败"
            return False
        self.center_yaw = cy
        self.center_pitch = cp
        self.noise_yaw = max(0.0, ny)
        self.noise_pitch = max(0.0, npitch)
        self.personal_pnp_active = True
        self.personal_pnp_valid_samples = len(ys)
        self.personal_pnp_median_reprojection = med_rep
        self.personal_pnp_center_depth = med_depth
        self.personal_pnp_current_depth = med_depth
        self.personal_pnp_far_depth_ratio = 1.0
        self.personal_pnp_rejection_reason = ""
        return True

    def _restore_precalibration_pnp_model(self) -> None:
        set_model = getattr(self.estimator, "set_pnp_model", None)
        restored = False
        if self._calibration_restore_personal_active and self._calibration_restore_model is not None:
            if set_model is not None:
                restored = bool(set_model(self._calibration_restore_model))
            if restored:
                self.personal_pnp_active = True
                self.personal_pnp_center_depth = self._calibration_restore_depth
                self.personal_pnp_rejection_reason = ""
        if self._calibration_restore_aux_state is not None:
            for name, value in self._calibration_restore_aux_state.items():
                setattr(self, name, value)
        if self._calibration_restore_center_state is not None:
            state = self._calibration_restore_center_state
            self.center_yaw = float(state["center_yaw"])
            self.noise_yaw = float(state["noise_yaw"])
            self.center_pitch = float(state["center_pitch"])
            self.noise_pitch = float(state["noise_pitch"])
            self.center_yaw_proxy = float(state["center_yaw_proxy"])
            self.noise_yaw_proxy = float(state["noise_yaw_proxy"])
            self._generic_center = tuple(state["generic_center"])
            self.center_quality = str(state["center_quality"])
            self.calibrated = bool(state["calibrated"])
            self.center_pending = bool(state["center_pending"])
            self._recompute_deadzone()
        self._calibration_restore_model = None
        self._calibration_restore_personal_active = False
        self._calibration_restore_depth = math.nan
        self._calibration_restore_center_state = None
        self._calibration_restore_aux_state = None

    def _restore_precalibration_frozen22(self) -> None:
        saved = self._calibration_restore_frozen22
        if saved is not None:
            (
                self.frozen22_center,
                self.frozen22_sigma,
                self.frozen22_cal_sigma_deg,
                self.frozen22_world_resid_rel,
                self.frozen22_calibration_valid,
            ) = saved
            self._recompute_deadzone()
        self._calibration_restore_frozen22 = None

    def _apply_policy_model(self) -> None:
        """Install or remove the personal PnP model when the policy changes.

        The estimator is shared by every horizontal policy, so a personal
        model and its recomputed neutral center may only drive the policy
        that built it; every other policy gets the generic model and the
        generic center.
        """
        policy = str(self.config.get("horizontal_algorithm", DEFAULT_CONFIG["horizontal_algorithm"]))
        set_model = getattr(self.estimator, "set_pnp_model", None)
        personal_active = bool(getattr(self.estimator, "personal_model_active", False))
        entry = self._personal_policy_store.get(policy)
        if entry is None and policy in V153_POLICIES:
            # Both PnP policies consume the same source model and center from
            # this calibration. The cache is replaced on each new calibration.
            entry = next((
                self._personal_policy_store[name] for name in V153_POLICIES
                if name in self._personal_policy_store
            ), None)
        if (
            set_model is not None
            and entry is not None
            and entry.get("model") is not None
            and entry.get("center") is not None
            and set_model(entry["model"])
        ):
            self.personal_pnp_active = True
            self.center_yaw, self.noise_yaw, self.center_pitch, self.noise_pitch = entry["center"]
            self.personal_pnp_center_depth = entry.get("depth", math.nan)
            self.personal_pnp_rejection_reason = ""
            self._recompute_deadzone()
            return
        if personal_active or self.personal_pnp_active:
            if set_model is not None:
                set_model(None)
        self.personal_pnp_active = False
        self.personal_pnp_center_depth = math.nan
        if policy == "classic" or policy in FROZEN22_POLICIES:
            # Classic is an intentional compatibility policy, not a rejected
            # personal model.  Frozen22 has its own fixed 2D signature and does
            # not use the personal PnP model either.
            self.personal_pnp_rejection_reason = ""
        elif not self.personal_pnp_rejection_reason:
            self.personal_pnp_rejection_reason = "未启用个人模型"
        self.center_yaw, self.noise_yaw, self.center_pitch, self.noise_pitch = self._generic_center
        self._recompute_deadzone()

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
        horizontal_algorithm: str | None = None,
    ) -> None:
        if horizontal_algorithm is not None:
            value = _horizontal_policy(horizontal_algorithm)
            if value not in HORIZONTAL_ALGORITHMS:
                raise ValueError(
                    "horizontal_algorithm must be one of: " + ", ".join(HORIZONTAL_ALGORITHMS)
                )
            if value != self.config["horizontal_algorithm"]:
                if self.calibrating:
                    self.cancel_center("横向算法已切换，请重新设置中心")
                reusable = bool(
                    self.calibrated
                    and self._calibration_algorithm == self.config["algorithm"]
                    and all(math.isfinite(v) for v in self._generic_center)
                )
                self.config["horizontal_algorithm"] = value
                self._reset_filters()
                self._apply_policy_model()
                self.calibrated = reusable
                fixed_needs_center = value in FROZEN22_POLICIES and not self.frozen22_calibration_valid
                self.center_pending = not reusable or fixed_needs_center
                if not reusable:
                    self.center_quality = "未校准"
                    self.notice = "当前没有兼容的中心，请先校准"
                elif fixed_needs_center:
                    self.notice = "固定特征校准样本不足，请重新校准；其他模式的中心仍保留"
                else:
                    self.notice = "模式已切换，沿用本次校准；转头即可测试"
                self.notice_until = time.monotonic() + 4.0
        if algorithm is not None:
            algorithm = str(algorithm).lower().strip()
            if algorithm not in HEAD_ALGORITHMS:
                raise ValueError("head algorithm must be pnp or ratio")
            if algorithm != self.config["algorithm"]:
                # Roll back the old calibration before invalidating it. A
                # rollback must never re-enable a center in different units.
                self.cancel_center("算法已切换，请重新设置中心")
                self.config["algorithm"] = algorithm
                self.calibrated = False
                self.center_pending = True
                self.center_quality = "未校准"
                self.frozen22_calibration_valid = False
                self._personal_policy_store.clear()
                self._calibration_algorithm = None
                self._generic_center = (math.nan, 0.0, math.nan, 0.0)
                self.estimator.set_pnp_model(None)
                self.personal_pnp_active = False
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
        if self.calibrating:
            # A repeated start must not replace the original rollback state
            # with the temporary generic-model state of the ongoing attempt.
            self.cancel_center()
        self._calibration_restore_frozen22 = (
            self.frozen22_center,
            self.frozen22_sigma,
            self.frozen22_cal_sigma_deg,
            self.frozen22_world_resid_rel,
            self.frozen22_calibration_valid,
        )
        self._calibration_restore_center_state = {
            "center_yaw": self.center_yaw,
            "noise_yaw": self.noise_yaw,
            "center_pitch": self.center_pitch,
            "noise_pitch": self.noise_pitch,
            "center_yaw_proxy": self.center_yaw_proxy,
            "noise_yaw_proxy": self.noise_yaw_proxy,
            "generic_center": tuple(self._generic_center),
            "center_quality": self.center_quality,
            "calibrated": self.calibrated,
            "center_pending": self.center_pending,
        }
        self._calibration_restore_aux_state = {
            name: getattr(self, name) for name in self._CALIBRATION_AUX_FIELDS
        }
        # Always collect a new center with the generic PnP geometry first.  If
        # the attempt is cancelled or fails, restore the previously valid
        # personal model together with the already-preserved old center.
        if self.config.get("algorithm") == "pnp":
            # Save the metre-space source template, never the estimator's
            # already-mm-scaled internal model.
            self._calibration_restore_model = (
                getattr(self.estimator, "personal_source_model", None)
                if self.personal_pnp_active else None
            )
            self._calibration_restore_personal_active = bool(
                self.personal_pnp_active and self._calibration_restore_model is not None
            )
            self._calibration_restore_depth = self.personal_pnp_center_depth if self.personal_pnp_active else math.nan
            set_model = getattr(self.estimator, "set_pnp_model", None)
            if set_model is not None:
                set_model(None)
            self.personal_pnp_active = False
            self.personal_pnp_far_depth_ratio = 1.0
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
        self.center_yaw_proxy_samples = []
        self.center_confidence_samples = []
        self.center_world_yaw_samples = []
        self.center_norm_z_yaw_samples = []
        self.center_multi2d_proxy_samples = []
        self.center_world_face_samples = []
        self.center_personal22_feature_samples = []
        self.center_world_head11_samples = []
        self.center_pnp_pose_samples = []
        self.center_roll_samples = []
        self.current_world_rigid_yaw = math.nan
        self.current_world_rigid_fit = math.nan
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
        self.center_yaw_proxy_samples = []
        self.center_confidence_samples = []
        self.center_world_yaw_samples = []
        self.center_norm_z_yaw_samples = []
        self.center_multi2d_proxy_samples = []
        self.center_world_face_samples = []
        self.center_personal22_feature_samples = []
        self.center_world_head11_samples = []
        self.center_pnp_pose_samples = []
        self.center_roll_samples = []
        self._restore_precalibration_pnp_model()
        self._restore_precalibration_frozen22()
        self.notice = reason
        self.notice_until = time.monotonic() + 2.5
        self._reset_filters()

    def _calibration_quality_limits(self) -> tuple[float, float]:
        if self._effective_estimator_algorithm() == "pnp":
            return (
                PNP_CALIBRATION_REFERENCE_SIGMA_YAW_DEG,
                PNP_CALIBRATION_REFERENCE_SIGMA_PITCH_DEG,
            )
        return (
            RATIO_CALIBRATION_REFERENCE_SIGMA_YAW,
            RATIO_CALIBRATION_REFERENCE_SIGMA_PITCH,
        )

    def _calibration_outlier_limits(self) -> tuple[float, float]:
        if self._effective_estimator_algorithm() == "pnp":
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
            proxy, proxy_sigma = _robust_center_and_sigma(self.center_yaw_proxy_samples)
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
                self.center_yaw_proxy = proxy if math.isfinite(proxy) else math.nan
                self.noise_yaw = max(0.0, yaw_sigma if math.isfinite(yaw_sigma) else 0.0)
                self.noise_pitch = max(0.0, pitch_sigma if math.isfinite(pitch_sigma) else 0.0)
                self.noise_yaw_proxy = max(0.0, proxy_sigma if math.isfinite(proxy_sigma) else 0.0)

                if len(self.center_world_yaw_samples) >= CENTER_MIN_SAMPLES:
                    world_center, world_sigma = _robust_center_and_sigma(self.center_world_yaw_samples)
                    self.center_world_yaw = world_center
                    self.noise_world_yaw = (
                        max(0.0, world_sigma) if math.isfinite(world_sigma) else math.inf
                    )
                else:
                    self.center_world_yaw = math.nan
                    self.noise_world_yaw = math.inf

                if len(self.center_norm_z_yaw_samples) >= CENTER_MIN_SAMPLES:
                    z_center, z_sigma = _robust_center_and_sigma(self.center_norm_z_yaw_samples)
                    self.center_norm_z_yaw = z_center
                    self.noise_norm_z_yaw = (
                        max(0.0, z_sigma) if math.isfinite(z_sigma) else math.inf
                    )
                else:
                    self.center_norm_z_yaw = math.nan
                    self.noise_norm_z_yaw = math.inf

                if len(self.center_multi2d_proxy_samples) >= CENTER_MIN_SAMPLES:
                    centers: list[float] = []
                    sigmas: list[float] = []
                    for j in range(7):
                        c2, s2 = _robust_center_and_sigma([v[j] for v in self.center_multi2d_proxy_samples])
                        centers.append(c2)
                        sigmas.append(max(0.0, s2) if math.isfinite(s2) else math.inf)
                    if all(math.isfinite(v) for v in centers):
                        self.center_multi2d_proxy = tuple(centers)
                        self.noise_multi2d_proxy = tuple(sigmas)
                    else:
                        self.center_multi2d_proxy = None
                        self.noise_multi2d_proxy = None
                else:
                    self.center_multi2d_proxy = None
                    self.noise_multi2d_proxy = None

                self.center_world_face_template = None
                self.center_world_rigid_yaw = math.nan
                self.noise_world_rigid_yaw = math.inf
                if len(self.center_world_face_samples) >= CENTER_MIN_SAMPLES:
                    pts = []
                    for i in range(7):
                        coords = []
                        for j in range(3):
                            coords.append(_median([f[i][j] for f in self.center_world_face_samples]))
                        pts.append(tuple(coords))
                    if all(all(math.isfinite(v) for v in p) for p in pts):
                        self.center_world_face_template = tuple(pts)
                        rigid_samples: list[float] = []
                        for face in self.center_world_face_samples:
                            ry, fit = _world_rigid_yaw(self.center_world_face_template, face)
                            if math.isfinite(ry) and math.isfinite(fit) and fit <= WORLD_RIGID_FIT_GROSS_MAX:
                                rigid_samples.append(ry)
                        if len(rigid_samples) >= CENTER_MIN_SAMPLES:
                            rc, rs = _robust_center_and_sigma(rigid_samples)
                            if math.isfinite(rc) and math.isfinite(rs):
                                self.center_world_rigid_yaw = rc
                                self.noise_world_rigid_yaw = max(0.0, rs)

                # frozen22 uses the exact fixed 11-point/22-D signature from
                # the audited R3 reference.  Only neutral center and channel
                # noise are estimated from this user's calibration window.
                self.frozen22_center = None
                self.frozen22_sigma = None
                self.frozen22_yaw = math.nan
                self.frozen22_yaw_median = math.nan
                self.frozen22_cal_sigma_deg = math.inf
                self.frozen22_world_resid_rel = math.inf
                self.frozen22_calibration_valid = False
                self._frozen22_history = []
                if len(self.center_personal22_feature_samples) >= FROZEN22_MIN_SAMPLES:
                    centers22: list[float] = []
                    sigmas22: list[float] = []
                    for index in range(22):
                        center22, sigma22 = _robust_center_and_sigma(
                            [sample[index] for sample in self.center_personal22_feature_samples]
                        )
                        centers22.append(center22)
                        sigmas22.append(
                            max(FROZEN22_SIGMA_FLOOR, sigma22)
                            if math.isfinite(sigma22) else math.inf
                        )
                    if all(math.isfinite(value) for value in centers22) and all(
                        math.isfinite(value) for value in sigmas22
                    ):
                        frozen_center = tuple(centers22)
                        frozen_sigma = tuple(sigmas22)
                        neutral_yaws = [
                            _personal22_matched_yaw(
                                sample, frozen_center, frozen_sigma, FROZEN22_SIGNATURE
                            )
                            for sample in self.center_personal22_feature_samples
                        ]
                        _, calibration_sigma = _robust_center_and_sigma(neutral_yaws)
                        self.frozen22_center = frozen_center
                        self.frozen22_sigma = frozen_sigma
                        self.frozen22_cal_sigma_deg = (
                            calibration_sigma if math.isfinite(calibration_sigma) else math.inf
                        )
                if len(self.center_world_head11_samples) >= FROZEN22_MIN_SAMPLES:
                    world_template = tuple(
                        tuple(
                            _median([sample[i][j] for sample in self.center_world_head11_samples])
                            for j in range(3)
                        )
                        for i in range(11)
                    )
                    if all(all(math.isfinite(value) for value in point) for point in world_template):
                        self.frozen22_world_resid_rel = _frozen22_world_residual_rel(
                            world_template, self.center_world_head11_samples
                        )
                self.frozen22_calibration_valid = bool(
                    self.frozen22_center is not None
                    and self.frozen22_sigma is not None
                    and math.isfinite(self.frozen22_cal_sigma_deg)
                    and self.frozen22_cal_sigma_deg <= FROZEN22_MAX_CAL_SIGMA_DEG
                )

                # Generic center snapshot: the center every non-v153 policy uses.
                self._generic_center = (self.center_yaw, self.noise_yaw, self.center_pitch, self.noise_pitch)
                self._calibration_algorithm = self._effective_estimator_algorithm()
                # Never pair a new 2D/world center with a personal model from
                # an older capture. Rollback restores the whole previous cache.
                self._personal_policy_store = {}
                active_policy = str(self.config.get("horizontal_algorithm", DEFAULT_CONFIG["horizontal_algorithm"]))
                personal_required = active_policy in V153_POLICIES
                had_previous_personal = bool(
                    self._calibration_restore_personal_active
                    and self._calibration_restore_model is not None
                )
                personal_ok = False
                if personal_required:
                    personal_ok = self._activate_personal_pnp_from_center()
                if personal_required and personal_ok:
                    self._personal_policy_store[active_policy] = {
                        "model": self.estimator.personal_source_model,
                        "center": (self.center_yaw, self.noise_yaw, self.center_pitch, self.noise_pitch),
                        "depth": self.personal_pnp_center_depth,
                    }
                elif personal_required and had_previous_personal:
                    # Transactional recalibration: never silently downgrade an
                    # already-valid personal policy because the new world-pose
                    # portion failed.  Restore the previous model, matching
                    # centre and Frozen22 calibration as one coherent state.
                    rejection = self.personal_pnp_rejection_reason or "新个人模型校准失败"
                    self._restore_precalibration_pnp_model()
                    self._restore_precalibration_frozen22()
                    self.notice = f"{rejection}；继续使用上一次有效个人校准"
                else:
                    set_model = getattr(self.estimator, "set_pnp_model", None)
                    if set_model is not None:
                        set_model(None)
                    self.personal_pnp_active = False
                    self._calibration_restore_model = None
                    self._calibration_restore_personal_active = False
                    self._calibration_restore_depth = math.nan
                    self._calibration_restore_center_state = None
                    self._calibration_restore_aux_state = None
                    self._calibration_restore_frozen22 = None
                # Successful replacement consumes the rollback snapshot.
                if personal_ok or not personal_required:
                    self._calibration_restore_model = None
                    self._calibration_restore_personal_active = False
                    self._calibration_restore_depth = math.nan
                    self._calibration_restore_center_state = None
                    self._calibration_restore_aux_state = None
                    self._calibration_restore_frozen22 = None
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
                if "继续使用上一次有效个人校准" not in self.notice:
                    if self.center_quality == "噪声较大":
                        self.notice = "校准完成 · 噪声较大，已自动扩大稳定区；需要更灵敏可重新校准"
                    else:
                        self.notice = f"校准完成 · 质量{self.center_quality}"
                    if personal_required and not self.personal_pnp_active and self.config["algorithm"] == "pnp":
                        self.notice += "；个人模型未采用，已使用通用 PnP"
                self._save_profile()
        if not success:
            self._restore_precalibration_pnp_model()
            self._restore_precalibration_frozen22()
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
        self.center_yaw_proxy_samples = []
        self.center_confidence_samples = []
        self.center_world_yaw_samples = []
        self.center_norm_z_yaw_samples = []
        self.center_multi2d_proxy_samples = []
        self.center_world_face_samples = []
        self.center_personal22_feature_samples = []
        self.center_world_head11_samples = []
        self.center_pnp_pose_samples = []
        self.center_roll_samples = []
        self.notice_until = time.monotonic() + 3.0
        self._reset_filters()

    def _reset_frozen22_gate(self) -> None:
        self._base_output_x = 0.0
        self._frozen22_gate_history = []
        self._frozen22_gate_direction = 0
        self._frozen22_gate_fast_count = 0
        self._frozen22_gate_lease_direction = 0
        self._frozen22_gate_lease_until = 0.0
        self.frozen22_gate_scale = 1.0
        self.frozen22_gate_source = "DISABLED"
        self.frozen22_gate_fast_delta_deg = math.nan
        self.frozen22_gate_slow_delta_deg = math.nan
        self.frozen22_gate_slow_world_delta_deg = math.nan
        self.frozen22_gate_pitch_delta_deg = math.nan
        self.frozen22_gate_fast_threshold_deg = math.nan
        self.frozen22_gate_slow_threshold_deg = math.nan
        # v188 fail-safe dropout memory.  Only an already-confirmed, previously
        # allowed turn may bridge a very short auxiliary-signal dropout.
        self._frozen22_gate_last_valid_t = -math.inf
        self._frozen22_gate_last_valid_direction = 0
        self._frozen22_gate_last_valid_scale = 0.0
        self.v188_pitch_guard_active = False

    def _frozen22_gate_sample(self, now: float, window_s: float) -> tuple[float, float, float] | None:
        target = now - window_s
        for t, yaw, pitch, world in reversed(self._frozen22_gate_history):
            if t <= target:
                if target - t > FROZEN22_GATE_MAX_SAMPLE_AGE_S:
                    return None
                return yaw, pitch, world
        return None

    def _horizontal_evidence_scale(self, drive: float, now: float, policy: str) -> float:
        """Check calibrated direction evidence once, before output shaping.

        Large genuine yaw can qualify from displacement, including slow turns;
        it does not need a 1.4-degree change inside a particular short window.
        Pitch uses a stricter cross-axis check even with a personal PnP model.
        No auxiliary signal invents a movement: the shared ratchet owns drive.
        """
        self.v188_pitch_guard_active = False
        self.v202_pitch_output_veto_active = False
        self.v202_return_output_veto_active = False
        self.v205_same_side_rescue_active = False
        if policy in FROZEN22_POLICIES:
            self.frozen22_gate_scale = 1.0
            self.frozen22_gate_source = "FIXED22_2D"
            return 1.0

        sign = -1.0 if self.config["invert_x"] else 1.0
        is_pnp = self._effective_estimator_algorithm() == "pnp"
        degree_scale = 1.0 if is_pnp else PNP_YAW_SPAN_DEG / RATIO_YAW_SPAN
        pitch_scale = 1.0 if is_pnp else PNP_PITCH_SPAN_DEG / RATIO_PITCH_SPAN
        main = sign * (self.control_yaw - self.center_yaw) * degree_scale
        pitch = (self.raw.pitch - self.center_pitch) * pitch_scale
        dt = now - self._cue_last_at if self._cue_last_at else 0.0
        pitch_velocity = (
            (pitch - self._cue_last_pitch) / dt
            if 0.0 < dt <= 0.25 and math.isfinite(self._cue_last_pitch) else 0.0
        )
        self._cue_last_pitch, self._cue_last_at = pitch, now
        pitch_active = abs(pitch) >= 2.5 or (abs(pitch) >= 1.0 and abs(pitch_velocity) >= 8.0)
        direction = 1 if drive > 1e-12 else -1 if drive < -1e-12 else (1 if main > 0 else -1 if main < 0 else 0)

        def stop(reason: str) -> float:
            self._cue_direction = 0
            self._cue_frames = 0
            self._cue_lease_until = 0.0
            self.frozen22_gate_scale = 0.0
            self.frozen22_gate_source = reason
            return 0.0

        if self._x_intent_v153.return_latched:
            return stop("RETURNING")
        if not direction or not math.isfinite(main):
            return stop("ZERO")
        if policy == "gesture_v153" and not pitch_active:
            self.frozen22_gate_scale = 1.0
            self.frozen22_gate_source = "PNP"
            return 1.0

        main_threshold = max(0.65, 2.0 * self.noise_yaw * degree_scale)
        main_ok = direction * main >= main_threshold
        f22_valid = bool(
            self.frozen22_calibration_valid and math.isfinite(self.frozen22_yaw_median)
            and math.isfinite(self.frozen22_cal_sigma_deg)
        )
        f22 = sign * self.frozen22_yaw_median if f22_valid else math.nan
        f22_threshold = max(0.60, 2.0 * self.frozen22_cal_sigma_deg) if f22_valid else math.inf
        f22_ok = f22_valid and direction * f22 >= f22_threshold
        world_valid = bool(
            math.isfinite(self.current_world_rigid_yaw)
            and math.isfinite(self.noise_world_rigid_yaw)
            and self.noise_world_rigid_yaw <= 2.5
        )
        world = sign * self.current_world_rigid_yaw if world_valid else math.nan
        world_threshold = max(0.75, 2.0 * self.noise_world_rigid_yaw) if world_valid else math.inf
        world_ok = world_valid and direction * world >= world_threshold
        world_opposite = world_valid and direction * world <= -world_threshold

        votes = 0
        proxy_valid = bool(
            self.current_multi2d_proxy is not None and self.center_multi2d_proxy is not None
            and self.noise_multi2d_proxy is not None
            and min(len(self.current_multi2d_proxy), len(self.center_multi2d_proxy), len(self.noise_multi2d_proxy)) >= 7
        )
        if proxy_valid:
            for index, scale in zip(MULTI2D_USE, MULTI2D_SCALE):
                delta = sign * (self.current_multi2d_proxy[index] - self.center_multi2d_proxy[index]) / scale
                sigma = self.noise_multi2d_proxy[index] / scale
                if math.isfinite(delta) and math.isfinite(sigma) and direction * delta >= max(0.65, 2.0 * sigma):
                    votes += 1
        proxy_ok = votes >= 3
        self._cross_axis_lock.global_votes = votes

        if pitch_active:
            # A reliable 3D cue near neutral is evidence of pitch-only motion,
            # not a missing signal to bypass. With no 3D cue, require two 2D
            # checks; correlated image errors remain an empirical limitation.
            confirmed = main_ok and (world_ok if world_valid else (f22_ok and proxy_ok))
            if not confirmed:
                self.v188_pitch_guard_active = True
                self.v202_pitch_output_veto_active = True
                self._cross_axis_lock.last_mode = "PITCH_UNCONFIRMED"
                return stop("PITCH_UNCONFIRMED")
            source = "PITCH_WORLD" if world_ok else "PITCH_2D"
        else:
            if world_opposite:
                return stop("DIRECTION_CONFLICT")
            confirmed = main_ok and (f22_ok or world_ok or proxy_ok)
            source = "CONSENSUS_WORLD" if world_ok else "CONSENSUS_FIXED22" if f22_ok else "CONSENSUS_2D"

        if direction != self._cue_direction:
            self._cue_direction = direction
            self._cue_since = now
            self._cue_frames = 0
            self._cue_lease_until = 0.0
        if confirmed:
            self._cue_frames += 1
            if self._cue_frames >= 2 and now - self._cue_since >= 0.045:
                self._cue_lease_until = now + 0.10
                self.frozen22_gate_scale = 1.0
                self.frozen22_gate_source = source
                self._cross_axis_lock.last_mode = source
                return 1.0
        elif now <= self._cue_lease_until and main_ok and not pitch_active and not world_opposite:
            # Bridge only a brief loss of corroboration in the same direction.
            self.frozen22_gate_scale = 1.0
            self.frozen22_gate_source = "CONSENSUS_GRACE"
            return 1.0
        else:
            return stop("AUX_UNCONFIRMED")
        self.frozen22_gate_scale = 0.0
        self.frozen22_gate_source = "CONFIRMING"
        return 0.0

    def _recompute_deadzone(self) -> None:
        span_x, span_y = self._span()
        base = float(self.config["deadzone"])
        yaw_noise = self.noise_yaw
        if self.config.get("horizontal_algorithm") in FROZEN22_POLICIES:
            yaw_noise = self.frozen22_cal_sigma_deg if math.isfinite(self.frozen22_cal_sigma_deg) else 0.0
        auto_x = (3.2 * yaw_noise / span_x + 0.015) if span_x > 0 else base
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

    def _diagnostic_yaw(self, estimate: HeadEstimate) -> float:
        """Record an independent face-local yaw diagnostic without steering.

        The two-video audit found that PnP and image-space nose/eye geometry
        can agree yet still conflict with an isolated manual left/right label.
        Therefore the proxy must not rewrite direction or silently suppress a
        PnP value.  Angle magnitude and canonical sign remain owned by PnP;
        the user's explicit ``invert_x`` remains the sole direction override.
        """
        self.yaw_proxy = estimate.yaw_proxy
        self.yaw_proxy_delta = math.nan
        self.yaw_guard_state = "fallback"
        if (
            self.config["algorithm"] != "pnp"
            or not math.isfinite(estimate.yaw_proxy)
            or not math.isfinite(self.center_yaw_proxy)
        ):
            return estimate.yaw

        proxy_delta = estimate.yaw_proxy - self.center_yaw_proxy
        self.yaw_proxy_delta = proxy_delta
        # This diagnostic threshold classifies whether the independent proxy
        # also sees motion.  It never changes the value returned to control.
        # Calibration noise only affects the diagnostic classification.
        proxy_gate = max(RATIO_YAW_SPAN * 0.035, self.noise_yaw_proxy * 2.8)
        self.yaw_guard_state = "proxy_neutral" if abs(proxy_delta) <= proxy_gate else "proxy_motion"
        return estimate.yaw

    @staticmethod
    def _slew(current: float, target: float, dt: float) -> float:
        if target == 0.0:
            # Neutral must be exact, not a slow decay that causes cursor drift.
            return 0.0
        if current * target < 0.0:
            # A physical direction reversal must stop the old direction on the
            # crossing frame.  The opposite direction may start next frame;
            # never slew through a residual wrong-sign mouse/gamepad command.
            return 0.0
        step = OUTPUT_SLEW_PERCENT_PER_S * max(0.0, min(0.08, dt))
        delta = _clamp(target - current, -step, step)
        return current + delta

    def update(
        self,
        pose: dict[str, dict] | None,
        width: int,
        height: int,
        now: float | None = None,
        world_pose: dict[str, dict] | list[dict] | None = None,
    ) -> tuple[float, float]:
        now = time.monotonic() if now is None else now
        self._last_intent_drive = 0.0
        self._last_evidence_scale = 0.0
        self._last_target_x = 0.0
        # ``world_pose`` is optional for strict backward compatibility.  It may
        # be either the named map used by this module or the phone's raw
        # 33-point MediaPipe world list.  A caller may also embed a named map
        # under ``__world_pose__`` / ``world_pose`` in the pose dictionary.
        if world_pose is None and isinstance(pose, dict):
            embedded = pose.get("__world_pose__")
            if not isinstance(embedded, (dict, list)):
                embedded = pose.get("world_pose")
            if isinstance(embedded, (dict, list)):
                world_pose = embedded
        world_pose_map = _coerce_world_pose(world_pose)
        self.current_world_yaw = _depth_yaw_proxy(world_pose_map)
        self.current_norm_z_yaw = _depth_yaw_proxy(pose)
        self.current_multi2d_proxy = _multi2d_yaw_proxy(pose, width, height)
        self.frozen22_missing_points = tuple(
            name for name in HEAD11_NAMES
            if not _point_ok(pose.get(name) if isinstance(pose, dict) else None, 0.20)
        )
        frozen_feature = _head11_local_feature(pose, width, height)
        if frozen_feature is None:
            self.current_personal22_feature = None
            self.current_personal22_eye_px = math.nan
            self.frozen22_yaw = math.nan
            self.frozen22_yaw_median = math.nan
            self._frozen22_history.clear()
        else:
            self.current_personal22_feature, self.current_personal22_eye_px = frozen_feature
            frozen_yaw = _personal22_matched_yaw(
                self.current_personal22_feature,
                self.frozen22_center,
                self.frozen22_sigma,
                FROZEN22_SIGNATURE,
            )
            self.frozen22_yaw = frozen_yaw
            if math.isfinite(frozen_yaw):
                self._frozen22_history.append(float(frozen_yaw))
                if len(self._frozen22_history) > FROZEN22_MEDIAN_WINDOW:
                    del self._frozen22_history[:-FROZEN22_MEDIAN_WINDOW]
                self.frozen22_yaw_median = _median(self._frozen22_history)
            else:
                self.frozen22_yaw_median = math.nan
        self.current_world_rigid_yaw = math.nan
        self.current_world_rigid_fit = math.nan
        current_world_face = _world_face_xyz(world_pose_map)
        if self.center_world_face_template is not None and current_world_face is not None:
            ry, rfit = _world_rigid_yaw(self.center_world_face_template, current_world_face)
            if math.isfinite(ry) and math.isfinite(rfit) and rfit <= WORLD_RIGID_FIT_GROSS_MAX:
                self.current_world_rigid_yaw = ry - (self.center_world_rigid_yaw if math.isfinite(self.center_world_rigid_yaw) else 0.0)
                self.current_world_rigid_fit = rfit
        policy = str(self.config.get("horizontal_algorithm", DEFAULT_CONFIG["horizontal_algorithm"]))
        estimate_pose = pose
        if policy in V153_POLICIES:
            estimate_pose = self._head_point_filter.apply(pose, HeadPoseEstimator._PNP_NAMES, now)
        estimate = self.estimator.estimate(estimate_pose, width, height, self._effective_estimator_algorithm())
        self.raw = estimate
        fixed_yaw_available = bool(
            policy in FROZEN22_POLICIES and self.calibrated and not self.calibrating
            and self.frozen22_calibration_valid and math.isfinite(self.frozen22_yaw_median)
        )
        if not estimate.valid and not fixed_yaw_available:
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
        self.last_error = "" if estimate.valid else estimate.error

        span_x, span_y = self._span()

        if self.calibrated and policy in FROZEN22_POLICIES:
            # The fixed-signature projection is already neutral-centred; do
            # not feed the unrelated sparse-PnP yaw into this policy.
            control_yaw = self.frozen22_yaw_median
            span_x = FROZEN22_YAW_SPAN_DEG
        else:
            control_yaw = self._diagnostic_yaw(estimate) if self.calibrated else estimate.yaw
        self.control_yaw = control_yaw

        # Keep filtered raw signals alive for both calibrated horizontal
        # control and the kernel's gated head-pitch mode.  Before calibration
        # we still filter the absolute estimate, but never emit output.
        if self.calibrated:
            filtered_yaw = self._yaw_filter.apply(control_yaw, now)
            if not estimate.valid:
                # Fixed22 may continue horizontally, but unavailable PnP pitch
                # is not a zero-degree sample and must never drive vertical aim.
                self._pitch_filter.reset()
                self._pitch_intent.reset()
                self._axis_active_y = False
                filtered_pitch = math.nan
            else:
                raw_y_for_release = _clamp((estimate.pitch - self.center_pitch) / span_y, -1.0, 1.0)
                release_y = self.effective_deadzone_y * DEADZONE_RELEASE_RATIO
                if abs(raw_y_for_release) <= release_y:
                    self._pitch_filter.reset()
                    filtered_pitch = self._pitch_filter.apply(self.center_pitch, now)
                else:
                    filtered_pitch = self._pitch_filter.apply(estimate.pitch, now)
        else:
            filtered_yaw = self._yaw_filter.apply(estimate.yaw, now)
            filtered_pitch = self._pitch_filter.apply(estimate.pitch, now)

        self.filtered_yaw, self.filtered_pitch = filtered_yaw, filtered_pitch
        self.signal_yaw, self.signal_pitch = filtered_yaw, filtered_pitch

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
            if math.isfinite(estimate.yaw_proxy):
                self.center_yaw_proxy_samples.append(estimate.yaw_proxy)
            self.center_confidence_samples.append(estimate.confidence)
            if math.isfinite(self.current_world_yaw):
                self.center_world_yaw_samples.append(self.current_world_yaw)
            if math.isfinite(self.current_norm_z_yaw):
                self.center_norm_z_yaw_samples.append(self.current_norm_z_yaw)
            if self.current_multi2d_proxy is not None:
                self.center_multi2d_proxy_samples.append(self.current_multi2d_proxy)
            if current_world_face is not None:
                self.center_world_face_samples.append(current_world_face)
            if self.current_personal22_feature is not None:
                self.center_personal22_feature_samples.append(self.current_personal22_feature)
            current_world_head11 = _world_head11_xyz(world_pose_map)
            if current_world_head11 is not None:
                self.center_world_head11_samples.append(current_world_head11)
            snap = self._snapshot_pnp_pose(estimate_pose)
            if snap is not None:
                self.center_pnp_pose_samples.append((snap, int(width), int(height)))
            if math.isfinite(estimate.roll):
                self.center_roll_samples.append(estimate.roll)
            self.notice = "正在记录自然中心：轻微模型抖动是正常的，不需要刻意僵住"
            if (
                self.center_valid_s >= CENTER_MIN_COLLECTION_S
                and len(self.center_yaw_samples) >= CENTER_TARGET_SAMPLES
            ):
                self._finish_center(True)
            return 0.0, 0.0

        if not self.calibrated or not self.config["enabled"]:
            self._yaw_intent.reset()
            self._pitch_intent.reset()
            self._x_intent_v153.reset()
            self._cross_axis_lock.reset()
            self.yaw_intent_state = self.pitch_intent_state = "IDLE"
            self.output_x = self.output_y = 0.0
            self.norm_x = self.norm_y = 0.0
            return 0.0, 0.0

        yaw_gain = (
            PERSONAL_PNP_YAW_GAIN
            if (policy in V153_POLICIES and self.config["algorithm"] == "pnp" and self.personal_pnp_active)
            else 1.0
        )
        frozen22_ready = bool(
            self.frozen22_calibration_valid and math.isfinite(self.frozen22_yaw_median)
        )
        sign = -1.0 if self.config["invert_x"] else 1.0
        if policy in FROZEN22_POLICIES:
            raw_x = sign * self.frozen22_yaw_median / span_x if frozen22_ready else 0.0
            intent_raw_x = raw_x
        else:
            raw_x = sign * yaw_gain * (self.signal_yaw - self.center_yaw) / span_x
            intent_raw_x = sign * yaw_gain * (control_yaw - self.center_yaw) / span_x
        raw_x = _clamp(raw_x, -4.0, 4.0)
        intent_raw_x = _clamp(intent_raw_x, -4.0, 4.0)
        raw_y = (
            _clamp((self.signal_pitch - self.center_pitch) / span_y, -1.0, 1.0)
            if estimate.valid and math.isfinite(self.signal_pitch) else 0.0
        )
        if self.config["invert_y"]:
            raw_y = -raw_y

        self.personal_pnp_current_depth = getattr(self.estimator, "pnp_depth", math.nan)
        self.personal_pnp_far_depth_ratio = 1.0
        start_x = _clamp(max(self.effective_deadzone_x * 0.58, 0.055), 0.045, 0.18)
        if (
            self.personal_pnp_active and math.isfinite(self.personal_pnp_center_depth)
            and math.isfinite(self.personal_pnp_current_depth) and self.personal_pnp_center_depth > 1e-6
        ):
            self.personal_pnp_far_depth_ratio = _clamp(
                self.personal_pnp_current_depth / self.personal_pnp_center_depth,
                1.0, PERSONAL_PNP_FAR_MAX_DEPTH_RATIO,
            )
            start_x *= self.personal_pnp_far_depth_ratio ** PERSONAL_PNP_FAR_START_POWER

        if policy in FROZEN22_POLICIES and not frozen22_ready:
            self._x_intent_v153.reset()
            vx = 0.0
            self.frozen22_gate_source = "FIXED22_NOT_READY"
            self.frozen22_gate_scale = 0.0
        else:
            vx = self._x_intent_v153.update(
                raw_x, now, raw_norm=intent_raw_x, start_angle=start_x,
                start_velocity=0.12, keep_velocity=0.045, return_velocity=0.060,
                stop_grace_s=0.075, acceleration_stop=1.15, curve_gamma=1.30,
            )
            self._last_intent_drive = vx
            self._last_evidence_scale = self._horizontal_evidence_scale(vx, now, policy)
            vx *= self._last_evidence_scale
        self.yaw_intent_state = self._x_intent_v153.state
        self.yaw_velocity = float(self._x_intent_v153.velocity)
        self.yaw_acceleration = float(self._x_intent_v153.acceleration)
        if estimate.valid:
            pitch_intent = self._pitch_intent.step(
                raw_y, now,
                angle_threshold=PITCH_INTENT_ANGLE,
                start_velocity=PITCH_INTENT_START_VELOCITY,
                stop_velocity=PITCH_INTENT_STOP_VELOCITY,
                release_threshold=self.effective_deadzone_y * DEADZONE_RELEASE_RATIO,
            )
        else:
            self._pitch_intent.reset()
            pitch_intent = {"state": "IDLE", "active": False, "velocity": 0.0, "acceleration": 0.0}
        self.pitch_velocity = pitch_intent["velocity"]
        self.pitch_acceleration = pitch_intent["acceleration"]
        self.pitch_intent_state = pitch_intent["state"]
        self.norm_x, self.norm_y = raw_x, raw_y

        # Ungated head control remains a repeatable relative gesture: turn to
        # move, hold to stop, and return to center without undoing the view.
        # The separately gated vertical mode in ControlKernel is absolute
        # deflection while the player's left hand explicitly holds the clutch.
        if pitch_intent["active"]:
            vy = self._axis_curve(raw_y, "y")
        else:
            self._axis_active_y = False
            vy = 0.0
        target_x = vx * float(self.config["sensitivity_x"])
        self._last_target_x = target_x
        target_y = vy * float(self.config["sensitivity_y"])
        dt = _clamp(now - self._last_update, 0.0, 0.08) if self._last_update else 1.0 / 30.0
        self._last_update = now
        self._base_output_x = 0.0
        self.output_x = self._slew(self.output_x, target_x, dt)
        self.output_y = self._slew(self.output_y, target_y, dt)
        return self.output_x / 100.0, self.output_y / 100.0

    def _horizontal_diagnostics(self) -> dict:
        """Describe the controller stage, not whether the OS received a mouse event."""
        policy = self.config["horizontal_algorithm"]
        fixed = policy in FROZEN22_POLICIES
        frame_valid = bool(
            self.frozen22_calibration_valid and math.isfinite(self.frozen22_yaw_median)
        ) if fixed else bool(self.raw.valid)
        ready = bool(self.calibrated and (not fixed or self.frozen22_calibration_valid))
        if not self.config["enabled"]:
            code, message = "DISABLED", "头控已关闭"
        elif self.calibrating:
            code, message = "CALIBRATING", "正在校准，暂不输出"
        elif not self.calibrated:
            code, message = "NEEDS_CALIBRATION", "尚未校准中心；原始角度有值也不会输出，请先校准"
        elif fixed and not self.frozen22_calibration_valid:
            code, message = "FIXED22_NEEDS_CALIBRATION", "固定特征校准未通过或样本不足，请重新校准"
        elif not frame_valid:
            code = "FIXED22_MISSING_POINTS" if fixed else "ESTIMATE_INVALID"
            message = "固定特征所需关键点不完整" if fixed else (self.raw.error or "当前姿态无效")
        elif abs(self.output_x) > 1e-12:
            code, message = "OUTPUT_ACTIVE", "头控模块已有横向输出；若鼠标不动，请检查上层输出链路"
        elif self._x_intent_v153.return_latched:
            code, message = "RETURNING", "正在回正；回到中心短暂停稳后重新触发"
        elif abs(self._last_intent_drive) <= 1e-12:
            if self.yaw_intent_state in {"CENTER", "IDLE"}:
                code, message = "NEUTRAL", "处于中心区，等待转头"
            else:
                code, message = "WAITING_FOR_MOTION", "姿态已停止或转头证据不足；单个偏头角度不会持续输出"
        elif self._last_evidence_scale <= 0.0:
            code = self.frozen22_gate_source
            message = {
                "PITCH_UNCONFIRMED": "俯仰动作中的横向证据不足，已拦截",
                "AUX_UNCONFIRMED": "融合模式尚无足够辅助依据；可切换个性化 PnP 对比",
                "DIRECTION_CONFLICT": "不同来源的方向冲突，已拦截",
                "CONFIRMING": "正在确认连续同向证据",
            }.get(code, "横向输出被证据检查拦截")
        else:
            code, message = "OUTPUT_TRANSITION", "正在等待有效帧间隔或完成方向切换"
        yaw_unit = "degrees" if self.config["algorithm"] == "pnp" else "ratio"
        active_value = self.frozen22_yaw_median if fixed else self.control_yaw
        return {
            "horizontal_frame_valid": frame_valid,
            "horizontal_control_ready": bool(ready and self.config["enabled"] and not self.calibrating),
            "horizontal_block_reason": code,
            "horizontal_block_message": message,
            "raw_yaw_units": yaw_unit,
            "raw_pitch_units": yaw_unit,
            "horizontal_signal_source": "frozen22" if fixed else self.config["algorithm"],
            "horizontal_signal_value": active_value if math.isfinite(active_value) else None,
            "horizontal_signal_units": "degree_like" if fixed else yaw_unit,
            "intent_drive_x": round(float(self._last_intent_drive), 6),
            "evidence_scale_x": round(float(self._last_evidence_scale), 6),
            "target_output_x": round(float(self._last_target_x), 6),
            "using_generic_pnp": bool(self.config["algorithm"] == "pnp" and not self.personal_pnp_active),
            "calibration_algorithm": self._calibration_algorithm,
        }

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
            quality = f"已校准 · 质量{self.center_quality}"
        else:
            elapsed = None
            remaining = None
            quality = "等待校准（可说“开始校准”）"
        policy_name = str(self.config.get("horizontal_algorithm", DEFAULT_CONFIG["horizontal_algorithm"]))
        if policy_name in FROZEN22_POLICIES and not self.calibrating:
            if self.frozen22_missing_points:
                # 这行字第一次开机就摆在"准备开玩"里。少了哪些点属于诊断，
                # 已经在 frozen22_missing_points 里；这里只说人该做什么。
                quality = "看不清脸，请正对摄像头"
            elif self.calibrated and not self.frozen22_calibration_valid:
                quality = "Frozen22 校准无效，请正视并保持稳定后重试"
        horizontal_calibrated = bool(
            self.calibrated
            and (policy_name not in FROZEN22_POLICIES or self.frozen22_calibration_valid)
        )
        yaw_latched = bool(getattr(self._x_intent_v153, "return_latched", False))
        horizontal_version = HORIZONTAL_ALGORITHM_VERSIONS[policy_name]
        return {
            **self._horizontal_diagnostics(),
            "signal_version": HEAD_SIGNAL_VERSION,
            "horizontal_algorithm": policy_name,
            "horizontal_algorithm_version": horizontal_version,
            "available_horizontal_algorithms": list(HORIZONTAL_ALGORITHMS),
            "horizontal_algorithm_labels": dict(HORIZONTAL_ALGORITHM_LABELS),
            "horizontal_algorithm_label": HORIZONTAL_ALGORITHM_LABELS[policy_name],
            "horizontal_behavior": "转动时移动，偏头停住时停止，回到中心短暂停稳后重新触发",
            "active_pitch_estimator": self._effective_estimator_algorithm(),
            "frozen22_signature_version": FROZEN22_SIGNATURE_VERSION,
            "frozen22_controls_mouse": bool(policy_name in FROZEN22_POLICIES),
            "v188_pitch_guard_active": bool(getattr(self, "v188_pitch_guard_active", False)),
            "v202_pitch_output_veto_active": bool(getattr(self, "v202_pitch_output_veto_active", False)),
            "v202_return_output_veto_active": bool(getattr(self, "v202_return_output_veto_active", False)),
            "v202_rearm_block_direction": int(getattr(self, "_v202_rearm_block_direction", 0)),
            "v202_rearm_quiet_hold_direction": int(getattr(self, "_v202_rearm_quiet_hold_direction", 0)),
            "v202_pitch_clean_reset_ready": bool(getattr(self, "_v202_pitch_clean_reset_ready", False)),
            "v205_same_side_rescue_active": bool(getattr(self, "v205_same_side_rescue_active", False)),
            "horizontal_calibrated": horizontal_calibrated,
            "frozen22_calibration_valid": bool(self.frozen22_calibration_valid),
            "frozen22_missing_points": list(self.frozen22_missing_points),
            "frozen22_frame_valid": bool(
                self.frozen22_calibration_valid
                and self.current_personal22_feature is not None
                and math.isfinite(self.frozen22_yaw_median)
            ),
            "frozen22_yaw_deg": (
                round(float(self.frozen22_yaw), 5)
                if math.isfinite(self.frozen22_yaw) else None
            ),
            "frozen22_yaw_median_deg": (
                round(float(self.frozen22_yaw_median), 5)
                if math.isfinite(self.frozen22_yaw_median) else None
            ),
            "frozen22_cal_sigma_deg": (
                round(float(self.frozen22_cal_sigma_deg), 5)
                if math.isfinite(self.frozen22_cal_sigma_deg) else None
            ),
            "frozen22_world_resid_rel": (
                round(float(self.frozen22_world_resid_rel), 5)
                if math.isfinite(self.frozen22_world_resid_rel) else None
            ),
            "frozen22_cal_samples": (
                len(self.center_personal22_feature_samples) if self.calibrating else None
            ),
            "personal_pnp_active": bool(self.personal_pnp_active),
            "personal_pnp_template_quality": (
                "active" if self.personal_pnp_active
                else "unavailable" if policy_name not in V153_POLICIES
                else "rejected" if self.personal_pnp_rejection_reason not in {"", "未尝试", "校准数据检查中", "未启用个人模型"}
                else "unavailable"
            ),
            "personal_pnp_rejection_reason": (
                self.personal_pnp_rejection_reason or None
            ) if policy_name in V153_POLICIES else None,
            "frozen22_gate_source": self.frozen22_gate_source,
            "frozen22_gate_scale": round(float(self.frozen22_gate_scale), 5),
            "frozen22_gate_fast_delta_deg": (
                round(float(self.frozen22_gate_fast_delta_deg), 5)
                if math.isfinite(self.frozen22_gate_fast_delta_deg) else None
            ),
            "frozen22_gate_slow_delta_deg": (
                round(float(self.frozen22_gate_slow_delta_deg), 5)
                if math.isfinite(self.frozen22_gate_slow_delta_deg) else None
            ),
            "frozen22_gate_slow_world_delta_deg": (
                round(float(self.frozen22_gate_slow_world_delta_deg), 5)
                if math.isfinite(self.frozen22_gate_slow_world_delta_deg) else None
            ),
            "frozen22_gate_pitch_delta_deg": (
                round(float(self.frozen22_gate_pitch_delta_deg), 5)
                if math.isfinite(self.frozen22_gate_pitch_delta_deg) else None
            ),
            "personal_pnp_yaw_gain": PERSONAL_PNP_YAW_GAIN if self.personal_pnp_active else 1.0,
            "personal_pnp_valid_samples": int(self.personal_pnp_valid_samples),
            "personal_pnp_median_reprojection": (
                round(float(self.personal_pnp_median_reprojection), 6)
                if math.isfinite(self.personal_pnp_median_reprojection) else None
            ),
            "personal_pnp_calibration_derotate_yaw": round(float(self.personal_pnp_calibration_derotate_yaw), 3),
            "personal_pnp_calibration_derotate_roll": round(float(self.personal_pnp_calibration_derotate_roll), 3),
            "personal_pnp_world_pair_yaw_deviation": (
                round(float(self.personal_pnp_world_pair_yaw_deviation), 3)
                if math.isfinite(self.personal_pnp_world_pair_yaw_deviation) else None
            ),
            "personal_pnp_world_pair_yaw_limit": PERSONAL_PNP_MAX_WORLD_PAIR_YAW_DEVIATION_DEG,
            "personal_pnp_center_depth": (
                round(float(self.personal_pnp_center_depth), 3)
                if math.isfinite(self.personal_pnp_center_depth) else None
            ),
            "personal_pnp_far_depth_ratio": round(float(self.personal_pnp_far_depth_ratio), 4),
            "cross_axis_mode": "FIXED22_2D" if policy_name in FROZEN22_POLICIES else "V5_PITCH_AWARE_CONSENSUS",
            "cross_axis_lock_mode": str(getattr(self._cross_axis_lock, "last_mode", "")),
            "multi2d_calibrated": bool(self.center_multi2d_proxy is not None and self.noise_multi2d_proxy is not None),
            "world_rigid_yaw": (
                round(float(self.current_world_rigid_yaw), 5)
                if math.isfinite(self.current_world_rigid_yaw) else None
            ),
            "world_rigid_sigma": (
                round(float(self.noise_world_rigid_yaw), 5)
                if math.isfinite(self.noise_world_rigid_yaw) else None
            ),
            "world_yaw_proxy": (
                round(float(self.current_world_yaw), 5)
                if math.isfinite(self.current_world_yaw) else None
            ),
            "world_yaw_reliable": bool(
                math.isfinite(self.center_world_yaw)
                and math.isfinite(self.noise_world_yaw)
                and self.noise_world_yaw <= PITCH_LOCK_WORLD_SIGMA_MAX_DEG
            ),
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
            "raw_yaw_proxy": self.raw.yaw_proxy if self.raw.valid and math.isfinite(self.raw.yaw_proxy) else None,
            "confidence": round(self.raw.confidence, 4) if self.raw.valid else 0.0,
            "reprojection_error": (
                round(self.raw.reprojection_error, 5)
                if self.raw.valid and math.isfinite(self.raw.reprojection_error) else None
            ),
            "estimate_valid": bool(self.raw.valid),
            "estimate_error": self.raw.error or self.last_error or None,
            "filtered_yaw": self.filtered_yaw if math.isfinite(self.filtered_yaw) else None,
            "filtered_pitch": self.filtered_pitch if math.isfinite(self.filtered_pitch) else None,
            "signal_yaw": self.signal_yaw if math.isfinite(self.signal_yaw) else None,
            "control_yaw": self.control_yaw if math.isfinite(self.control_yaw) else None,
            "yaw_proxy_delta": self.yaw_proxy_delta if math.isfinite(self.yaw_proxy_delta) else None,
            "yaw_guard_state": self.yaw_guard_state,
            "signal_pitch": self.signal_pitch if math.isfinite(self.signal_pitch) else None,
            "yaw_velocity": round(float(self.yaw_velocity), 4),
            "yaw_acceleration": round(float(self.yaw_acceleration), 4),
            "pitch_velocity": round(float(self.pitch_velocity), 4),
            "pitch_acceleration": round(float(self.pitch_acceleration), 4),
            "yaw_intent_state": self.yaw_intent_state,
            "pitch_intent_state": self.pitch_intent_state,
            "yaw_return_latched": bool(yaw_latched),
            "pitch_return_latched": bool(self._pitch_intent.return_latched),
            "yaw_intent_active": self.yaw_intent_state in {"TURN_LEFT", "TURN_RIGHT"},
            "pitch_intent_active": self.pitch_intent_state in {"TURN_LEFT", "TURN_RIGHT"},
            "yaw_intent_angle_threshold": YAW_INTENT_ANGLE,
            "yaw_intent_velocity_threshold": YAW_INTENT_START_VELOCITY,
            "pitch_intent_angle_threshold": PITCH_INTENT_ANGLE,
            "pitch_intent_velocity_threshold": PITCH_INTENT_START_VELOCITY,
            "center_yaw": self.center_yaw if self.calibrated else None,
            "center_pitch": self.center_pitch if self.calibrated else None,
            "center_yaw_proxy": self.center_yaw_proxy if self.calibrated and math.isfinite(self.center_yaw_proxy) else None,
            "noise_yaw": round(self.noise_yaw, 6),
            "noise_pitch": round(self.noise_pitch, 6),
            "noise_yaw_proxy": round(self.noise_yaw_proxy, 6),
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
            # Accept earlier tuning parameters, but always capture a fresh
            # center for the current camera and active v5 signal route.
            if payload.get("signal_version") not in HEAD_PROFILE_COMPATIBLE_VERSIONS:
                return
            params = payload.get("params") or {}
            algorithm = str(params.get("algorithm", "pnp"))
            if algorithm not in HEAD_ALGORITHMS:
                return
            horizontal_algorithm = str(
                params.get("horizontal_algorithm", DEFAULT_CONFIG["horizontal_algorithm"])
            ).lower().strip()
            # Profiles written before the v153 policy used a few transient
            # names (for example ``gesture``).  Do not let those silently
            # select a different signal path; use the production v153 policy
            # unless the persisted value is one of the explicit choices.
            if horizontal_algorithm in {"gesture", "classic"}:
                horizontal_algorithm = "gesture_v153"
            if horizontal_algorithm not in HORIZONTAL_ALGORITHMS:
                horizontal_algorithm = DEFAULT_CONFIG["horizontal_algorithm"]
            self.config.update({
                "algorithm": algorithm,
                "enabled": bool(params.get("enabled", True)),
                # 存盘里那个值故意不读：它已经不是设置了。老档案里存着 false，
                # 照读会让改过默认值的这台机器行为跟没改一样。
                "invert_x": True,
                "invert_y": bool(params.get("invert_y", False)),
                "horizontal_algorithm": horizontal_algorithm,
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
            self.center_yaw_proxy = math.nan
            self.noise_yaw = 0.0
            self.noise_pitch = 0.0
            self.noise_yaw_proxy = 0.0
            self._personal_policy_store = {}
            self.personal_pnp_active = False
            self.personal_pnp_valid_samples = 0
            self.personal_pnp_median_reprojection = math.nan
            self.personal_pnp_center_depth = math.nan
            self.personal_pnp_current_depth = math.nan
            self.personal_pnp_far_depth_ratio = 1.0
            set_model = getattr(self.estimator, "set_pnp_model", None)
            if set_model is not None:
                set_model(None)
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
                "yaw_proxy": self.center_yaw_proxy if self.calibrated and math.isfinite(self.center_yaw_proxy) else None,
                "noise_yaw": self.noise_yaw,
                "noise_pitch": self.noise_pitch,
                "noise_yaw_proxy": self.noise_yaw_proxy,
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
