"""Head-control engine for MotionControl 1.00.

This module deliberately keeps head control separate from body actions and
output backends.  It provides two estimators for A/B testing:

- ``pnp``: sparse 3D head-pose estimation with OpenCV ``solvePnP``.
- ``ratio``: a scale-free 2D face-ratio estimator that needs no OpenCV.

For PnP, the personal-model path can build a seven-point 3D face model from the
calibration-time MediaPipe world landmarks.  The world landmarks are used only
to build that model; runtime horizontal control remains the original 2D->PnP
->relative-ratchet path.  A frozen 1.20x yaw gain restores the useful response
scale after removing generic-template distortion.

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
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


HEAD_SIGNAL_VERSION = "head-control-v5.0-intent"
HEAD_HORIZONTAL_ALGORITHM_VERSION = "relative-ratchet-v188-pitch-guard"
FROZEN22_GATE_VERSION = "frozen22-adaptive-dual-timegate-v188"

# v184 diagnostic-only personal 11-point / 22D geometry probe.  It never feeds Mouse-X.
PERSONAL22_PROBE_VERSION = "personal22-perspective-probe-v184"
STAGE_A_PROBE_VERSION = "frozen22-stageA-evidence-v186"  # retained for evidence compatibility
STAGE_A_LOG_ENV = "MOTIONCONTROL_HEAD_STAGEA_LOG"
STAGE_A_LABEL_ENV = "MOTIONCONTROL_HEAD_STAGEA_LABEL_FILE"
PERSONAL22_WORLD_RESID_REL_MAX = 0.10
PERSONAL22_MIN_SAMPLES = 20
PERSONAL22_SIGMA_FLOOR = 1e-4
PERSONAL22_MAX_CAL_SIGMA_DEG = 2.5
PERSONAL22_Z_EYE_RATIO_MIN = 4.0
PERSONAL22_Z_EYE_RATIO_MAX = 40.0
PERSONAL22_MEDIAN_WINDOW = 3
HEAD11_NAMES = (
    "nose", "left_eye_inner", "left_eye", "left_eye_outer",
    "right_eye_inner", "right_eye", "right_eye_outer",
    "left_ear", "right_ear", "mouth_left", "mouth_right",
)

# v186 Stage-A candidate: fixed 11-point / 22D yaw signature learned only from
# the two archived 2026-08-30 real-video training recordings.  Each recording
# was fitted independently on the pre-existing v160 motion windows; the two
# signatures have cosine similarity 0.98599 and are averaged with equal video
# weight.  The new Stage-A capture MUST NOT update these coefficients.
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

# v187: frozen22 is promoted from a diagnostic probe to an *outer confidence
# gate* around the proven v160 Mouse-X controller.  It does not replace the
# v160 yaw source, TURN/RETURNING state machine, amplitude curve, or slew.
# Windows are time-based so the same semantics hold at 20/30/45/60 FPS.
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
# When v160 has already committed to a TURN but Full/frozen22 evidence is still
# statistically indistinguishable from noise, expose only a tiny preview rather
# than either full output or a hard zero.  This preserves an escape path for
# genuine ultra-slow turns while reducing visible drift by ~95% in that state.
FROZEN22_GATE_COMMITTED_FALLBACK_SCALE = 0.00
FROZEN22_GATE_HISTORY_S = 1.20
FROZEN22_GATE_MAX_SAMPLE_AGE_S = 0.12

# A FAST/LEASE decision can survive into a later pitch-dominant frame.  Hide
# only that current frame when the calibrated pitch excursion is clearly larger
# than the independent frozen22 yaw excursion.  The lease and ratchet state are
# deliberately preserved so a genuine horizontal turn resumes without a new
# startup delay.  SLOW evidence may still pass when its independent yaw/world
# checks genuinely support horizontal motion.
V188_PITCH_GUARD_MIN_DEG = 1.50
V188_PITCH_GUARD_YAW_TO_PITCH = 0.35

# v155 stage: a production-implementable horizontal signal learned from the
# user's real MediaPipe recordings.  It uses five 2D face-shape channels whose
# yaw response stayed directionally stable across both recordings.  The signal
# is only used when the personal PnP source fix is unavailable; a valid personal
# PnP model keeps the proven v153 path unchanged.
REAL_SHAPE_CONTROL_ENABLED = True
REAL_SHAPE_MEDIAN_WINDOW = 3
REAL_SHAPE_SIGMA_FLOOR_DEG = 0.18
REAL_SHAPE_MAX_CAL_SIGMA_DEG = 2.5
# Real-video A/B: false shape TURNs sometimes persist while the independent
# world-rigid yaw points clearly the other way.  Do not require world to prove
# a turn (that hurts very-slow yaw); only veto an already-proposed Mouse-X frame
# after three consecutive, explicit opposite-world observations.  Missing or
# noisy world data simply disables this optional veto.
REAL_SHAPE_WORLD_OPPOSE_DEG = 0.70
REAL_SHAPE_WORLD_OPPOSE_FRAMES = 3
# v156: preserve a short contradiction memory through brief zero-output gaps.
# This fixes the real A~10s pattern where world remains opposite while v59c
# briefly emits zero, which previously reset the three-frame counter.  Weak
# / near-neutral world evidence decays one vote per frame; explicit same-
# direction world evidence releases the memory immediately.
REAL_SHAPE_WORLD_CONTEXT_S = 0.20
REAL_SHAPE_WORLD_VETO_HOLD_S = 0.18
REAL_SHAPE_WORLD_SAME_RELEASE_DEG = 0.35
REAL_SHAPE_WORLD_WEAK_DECAY = 1
# v157: only the first weak tentative Mouse-X frame is hidden from the player.
# The v59c state machine still receives the frame and keeps accumulating evidence.
# A strong first-frame turn (normalized yaw velocity >= 0.16/s) bypasses this guard.
REAL_SHAPE_TENTATIVE_QUIET_VELOCITY = 0.18
# While v59c is still tentative, expose only a small preview velocity.  Internal
# evidence/state remains untouched; committed TURN immediately returns to 100%.
REAL_SHAPE_TENTATIVE_PREVIEW_MIN_SCALE = 0.12
REAL_SHAPE_TENTATIVE_PREVIEW_SLOPE_PER_S = 1.40
REAL_SHAPE_TENTATIVE_PREVIEW_MAX_SCALE = 0.55
REAL_SHAPE_TENTATIVE_PREVIEW_CAP = 0.08
# Independent one-frame world contradiction does not hard-veto a tentative
# turn; it only makes the user-visible preview much lighter.  The proven
# 0.70 deg x 3-frame contradiction veto above remains the hard stop.
REAL_SHAPE_TENTATIVE_SOFT_OPPOSE_DEG = 0.40
REAL_SHAPE_TENTATIVE_SOFT_OPPOSE_SCALE = 0.25
# Existing _multi2d_yaw_proxy channel indices:
#   0 nose-eye asymmetry, 2 nose-ear asymmetry, 3 eye-ear asymmetry,
#   4 nose horizontal shift, 6 ear-mid horizontal shift.
REAL_SHAPE_CHANNELS = (0, 2, 3, 4, 6)
# Per-degree response fitted from the two real 2026-08-30 recordings.  These
# replace the old ideal-3D-generator scale for this stage path.
REAL_SHAPE_SLOPE_PER_DEG = (0.02430, 0.05600, 0.01835, 0.01500, 0.02143)
PERSONAL_PNP_YAW_GAIN = 1.20
PERSONAL_PNP_MIN_SAMPLES = 20
PERSONAL_PNP_MAX_MEDIAN_REPROJECTION = 0.025
PERSONAL_PNP_MAX_WORLD_RIGID_SIGMA_DEG = 4.80
PERSONAL_PNP_FAR_START_POWER = 0.20
PERSONAL_PNP_FAR_MAX_DEPTH_RATIO = 1.50
PERSONAL_PNP_CALIB_DEROTATE_YAW_DEG = 4.0
PERSONAL_PNP_CALIB_DEROTATE_ROLL_DEG = 2.5
PERSONAL_PNP_MAX_WORLD_PAIR_YAW_DEVIATION_DEG = 7.0
HEAD_PROFILE_COMPAT_VERSIONS = {
    HEAD_SIGNAL_VERSION,
    "head-control-v5.0-motion-evidence",
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
    "invert_x": False,
    "invert_y": False,
    # One user-facing stability zone shared by both axes.  Automatic center
    # noise can only enlarge it, never make it smaller than this value.
    "deadzone": 0.10,
    "sensitivity_x": 58.0,
    "sensitivity_y": 46.0,
    # Vertical camera source. Both modes remain gated by the left-wrist lookGate.
    "vertical_source": "right_wrist",
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


def _real_shape_yaw_from_proxy(
    proxy: tuple[float, ...] | None,
    center: tuple[float, ...] | None,
    sigma: tuple[float, ...] | None,
) -> tuple[float, float]:
    """Return (yaw_deg, estimated_calibration_sigma_deg).

    The five selected 2D channels were re-scaled from the user's real video
    recordings rather than the previous ideal 3D-face generator.  Calibration
    noise provides per-user inverse-variance weighting.  No world landmarks are
    required at runtime.
    """
    if proxy is None or center is None or sigma is None:
        return math.nan, math.inf
    if min(len(proxy), len(center), len(sigma)) < 7:
        return math.nan, math.inf
    vals: list[float] = []
    weights: list[float] = []
    channel_sigmas: list[float] = []
    for index, slope in zip(REAL_SHAPE_CHANNELS, REAL_SHAPE_SLOPE_PER_DEG):
        p = _finite(proxy[index]); c = _finite(center[index]); sn = _finite(sigma[index])
        if not all(math.isfinite(v) for v in (p, c, sn)) or abs(slope) < 1e-9:
            continue
        y = (p - c) / slope
        sy = max(REAL_SHAPE_SIGMA_FLOOR_DEG, abs(sn / slope))
        vals.append(float(y)); channel_sigmas.append(float(sy)); weights.append(1.0 / (sy * sy))
    if len(vals) < 3 or not weights or sum(weights) <= 0.0:
        return math.nan, math.inf

    # Robust one-step reweighting.  A short MediaPipe burst often distorts one
    # or two face channels together.  The median is used only to reduce those
    # outliers; it does not create an extra deadzone or suppress slow trends.
    med = _median(vals)
    robust_weights: list[float] = []
    for value, base_w, sy in zip(vals, weights, channel_sigmas):
        r = abs(value - med) / max(sy, REAL_SHAPE_SIGMA_FLOOR_DEG)
        robust_weights.append(base_w if r <= 2.5 else base_w * (2.5 / max(r, 1e-9)))
    wsum = sum(robust_weights)
    if wsum <= 0.0:
        return math.nan, math.inf
    yaw = sum(v * w for v, w in zip(vals, robust_weights)) / wsum
    # Approximate neutral sigma of the weighted common-yaw estimate.
    cal_sigma = math.sqrt(1.0 / max(sum(weights), 1e-12))
    return float(yaw), float(cal_sigma)



def _head11_local_feature_xy(points: tuple[tuple[float, float], ...]) -> tuple[tuple[float, ...], float] | None:
    """v177-compatible 11-point local face feature: deroll, eye-center, eye-scale."""
    if len(points) != 11:
        return None
    try:
        le = (
            sum(float(points[i][0]) for i in (1, 2, 3)) / 3.0,
            sum(float(points[i][1]) for i in (1, 2, 3)) / 3.0,
        )
        re = (
            sum(float(points[i][0]) for i in (4, 5, 6)) / 3.0,
            sum(float(points[i][1]) for i in (4, 5, 6)) / 3.0,
        )
        mx, my = (le[0] + re[0]) * 0.5, (le[1] + re[1]) * 0.5
        dx, dy = re[0] - le[0], re[1] - le[1]
        scale = math.hypot(dx, dy)
        if not math.isfinite(scale) or scale < 3.0:
            return None
        angle = math.atan2(dy, dx)
        c, ss = math.cos(-angle), math.sin(-angle)
        out: list[float] = []
        for x, y in points:
            x, y = float(x), float(y)
            if not (math.isfinite(x) and math.isfinite(y)):
                return None
            rx, ry = x - mx, y - my
            out.extend(((c * rx - ss * ry) / scale, (ss * rx + c * ry) / scale))
        return tuple(out), float(scale)
    except Exception:
        return None


def _head11_local_feature(pose: dict[str, dict] | None, width: int, height: int) -> tuple[tuple[float, ...], float] | None:
    if not isinstance(pose, dict):
        return None
    if any(not _point_ok(pose.get(name), 0.20) for name in HEAD11_NAMES):
        return None
    width, height = max(2, int(width)), max(2, int(height))
    pts: list[tuple[float, float]] = []
    for name in HEAD11_NAMES:
        pt = pose.get(name)
        if not isinstance(pt, dict):
            return None
        x, y = _finite(pt.get("x")) * width, _finite(pt.get("y")) * height
        if not (math.isfinite(x) and math.isfinite(y)):
            return None
        pts.append((x, y))
    return _head11_local_feature_xy(tuple(pts))


def _world_head11_xyz(pose: dict[str, dict] | None) -> tuple[tuple[float, float, float], ...] | None:
    if not isinstance(pose, dict):
        return None
    out: list[tuple[float, float, float]] = []
    for name in HEAD11_NAMES:
        pt = pose.get(name)
        if not isinstance(pt, dict):
            return None
        xyz = (_finite(pt.get("x")), _finite(pt.get("y")), _finite(pt.get("z")))
        if not all(math.isfinite(v) for v in xyz):
            return None
        out.append(tuple(float(v) for v in xyz))
    return tuple(out)


def _head11_eye_world_span(template: tuple[tuple[float, float, float], ...] | None) -> float:
    if template is None or len(template) != 11:
        return math.nan
    le = tuple(sum(template[i][j] for i in (1, 2, 3)) / 3.0 for j in range(3))
    re = tuple(sum(template[i][j] for i in (4, 5, 6)) / 3.0 for j in range(3))
    return math.sqrt(sum((re[j] - le[j]) ** 2 for j in range(3)))


def _personal22_world_residual_rel(
    template: tuple[tuple[float, float, float], ...] | None,
    samples: list[tuple[tuple[float, float, float], ...]],
) -> float:
    """Median Kabsch non-rigid residual divided by personal 3D eye span."""
    if template is None or len(template) != 11 or not samples:
        return math.inf
    try:
        import numpy as np
        p = np.asarray(template, dtype=np.float64)
        pc = p - p.mean(axis=0, keepdims=True)
        eye = _head11_eye_world_span(template)
        if p.shape != (11, 3) or not np.isfinite(p).all() or not math.isfinite(eye) or eye <= 1e-9:
            return math.inf
        vals: list[float] = []
        for sample in samples:
            q = np.asarray(sample, dtype=np.float64)
            if q.shape != (11, 3) or not np.isfinite(q).all():
                continue
            qc = q - q.mean(axis=0, keepdims=True)
            u, _, vt = np.linalg.svd(pc.T @ qc)
            r = vt.T @ u.T
            if np.linalg.det(r) < 0.0:
                vt[-1, :] *= -1.0
                r = vt.T @ u.T
            aligned = pc @ r.T
            rms = float(np.sqrt(np.mean(np.sum((aligned - qc) ** 2, axis=1))))
            if math.isfinite(rms):
                vals.append(rms / eye)
        return float(_median(vals)) if vals else math.inf
    except Exception:
        return math.inf


def _personal22_signature_from_world(
    template: tuple[tuple[float, float, float], ...] | None,
    *, focal_px: float, eye_px: float,
) -> tuple[tuple[float, ...] | None, float, float]:
    """Generate personal 22D feature/degree yaw response with perspective projection."""
    if template is None or len(template) != 11 or not (math.isfinite(focal_px) and math.isfinite(eye_px)):
        return None, math.nan, math.nan
    eye_world = _head11_eye_world_span(template)
    if not math.isfinite(eye_world) or eye_world <= 1e-9 or eye_px < 3.0 or focal_px <= 1.0:
        return None, math.nan, eye_world
    z_est = focal_px * eye_world / eye_px
    z_ratio = z_est / eye_world
    if not (PERSONAL22_Z_EYE_RATIO_MIN <= z_ratio <= PERSONAL22_Z_EYE_RATIO_MAX):
        return None, float(z_est), float(eye_world)
    cx = sum(p[0] for p in template) / 11.0
    cy = sum(p[1] for p in template) / 11.0
    cz = sum(p[2] for p in template) / 11.0
    base = tuple((p[0] - cx, p[1] - cy, p[2] - cz) for p in template)
    def projected(deg: float) -> tuple[tuple[float, ...], float] | None:
        a = math.radians(deg); c, ss = math.cos(a), math.sin(a)
        pts: list[tuple[float, float]] = []
        for x, y, z in base:
            xr, zr = c * x + ss * z, -ss * x + c * z
            depth = z_est + zr
            if not math.isfinite(depth) or depth <= max(1e-9, eye_world * 0.5):
                return None
            pts.append((focal_px * xr / depth, focal_px * y / depth))
        return _head11_local_feature_xy(tuple(pts))
    plus, minus = projected(5.0), projected(-5.0)
    if plus is None or minus is None:
        return None, float(z_est), float(eye_world)
    sig = tuple((plus[0][i] - minus[0][i]) / 10.0 for i in range(22))
    if not all(math.isfinite(v) for v in sig) or sum(v * v for v in sig) <= 1e-12:
        return None, float(z_est), float(eye_world)
    return sig, float(z_est), float(eye_world)


def _personal22_matched_yaw(
    feature: tuple[float, ...] | None,
    center: tuple[float, ...] | None,
    sigma: tuple[float, ...] | None,
    signature: tuple[float, ...] | None,
) -> float:
    if feature is None or center is None or sigma is None or signature is None:
        return math.nan
    if min(len(feature), len(center), len(sigma), len(signature)) < 22:
        return math.nan
    num = den = 0.0
    for x, c, s, g in zip(feature[:22], center[:22], sigma[:22], signature[:22]):
        if not all(math.isfinite(float(v)) for v in (x, c, s, g)):
            return math.nan
        w = 1.0 / max(PERSONAL22_SIGMA_FLOOR, abs(float(s))) ** 2
        num += (float(x) - float(c)) * float(g) * w
        den += float(g) * float(g) * w
    return float(num / den) if den > 1e-12 else math.nan

def _coerce_world_pose(value: Any) -> dict[str, dict] | None:
    """Accept either the module's named map or a raw 33-landmark world list."""
    if isinstance(value, dict):
        return value
    if isinstance(value, list) and len(value) == 33:
        # MediaPipe Pose landmark indices needed by the depth-yaw proxy.
        index_by_name = {
            "nose": 0,
            "left_eye_inner": 1,
            "left_eye": 2,
            "left_eye_outer": 3,
            "right_eye_inner": 4,
            "right_eye": 5,
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


# Horizontal relative-mouse tuning selected by the MotionControl replay benchmark.
# The horizontal path uses two stages:
#   1) a very light EMA on the seven face landmarks before solvePnP;
#   2) a tentative -> committed relative-mouse ratchet.
#
# The tentative stage is important: centre noise can briefly look like a turn,
# but an unconfirmed turn is allowed to cancel silently.  Only a committed turn
# is allowed to enter RETURNING, preventing one false centre trigger from
# poisoning the following real movement with a long return latch.
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


class _HeadIntentAxis:
    """Convert filtered head motion into deliberate camera-turn intent.

    Position supplies camera speed, velocity decides whether the user is
    actively turning, and acceleration is used only as an early-stop hint.
    Returning toward neutral never drives the camera in the old direction.
    """

    def __init__(self, *, velocity_tau: float = 0.085, acceleration_tau: float = 0.14) -> None:
        self.velocity_tau = float(velocity_tau)
        self.acceleration_tau = float(acceleration_tau)
        self.state = "CENTER"
        self.velocity = 0.0
        self.acceleration = 0.0
        self.output = 0.0
        self._last_norm = 0.0
        self._last_raw_norm = 0.0
        self._last_velocity = 0.0
        self._last_t = 0.0
        self._slow_s = 0.0
        self._return_latched = False
        self._return_settle_s = 0.0
        self._raw_stable_s = 0.0

    def reset(self, norm: float = 0.0, now: float = 0.0) -> None:
        self.state = "CENTER"
        self.velocity = 0.0
        self.acceleration = 0.0
        self.output = 0.0
        self._last_norm = float(norm) if math.isfinite(float(norm)) else 0.0
        self._last_raw_norm = self._last_norm
        self._last_velocity = 0.0
        self._last_t = float(now) if now else 0.0
        self._slow_s = 0.0
        self._return_latched = False
        self._return_settle_s = 0.0
        self._raw_stable_s = 0.0

    @staticmethod
    def _exp_alpha(dt: float, tau: float) -> float:
        return 1.0 - math.exp(-max(1e-4, dt) / max(1e-4, tau))

    def update(
        self,
        norm: float,
        now: float,
        *,
        raw_norm: float | None = None,
        start_angle: float,
        start_velocity: float,
        keep_velocity: float,
        return_velocity: float,
        stop_grace_s: float,
        acceleration_stop: float,
        return_step: float = 0.025,
        curve_gamma: float = 1.28,
    ) -> float:
        norm = _clamp(norm, -1.0, 1.0)
        raw_norm = norm if raw_norm is None else _clamp(raw_norm, -1.0, 1.0)
        raw_delta = raw_norm - self._last_raw_norm
        dt = _clamp(now - self._last_t, 1.0 / 240.0, 0.10) if self._last_t else 1.0 / 30.0
        raw_velocity = (norm - self._last_norm) / dt
        a_v = self._exp_alpha(dt, self.velocity_tau)
        velocity = self.velocity + a_v * (raw_velocity - self.velocity)
        raw_accel = (velocity - self._last_velocity) / dt
        a_a = self._exp_alpha(dt, self.acceleration_tau)
        acceleration = self.acceleration + a_a * (raw_accel - self.acceleration)

        self._last_norm = norm
        self._last_raw_norm = raw_norm
        self._last_t = now
        self._last_velocity = velocity
        self.velocity = velocity
        self.acceleration = acceleration

        # A stationary head should stop the camera even while the filtered
        # derivative still has a small tail.  This uses only raw *change*, not
        # raw position, so model offset does not cause drift.
        if abs(raw_delta) <= max(0.0025, abs(return_step) * 0.12):
            self._raw_stable_s += dt
        else:
            self._raw_stable_s = 0.0

        release_angle = max(0.015, start_angle * 0.52)
        amount = abs(norm)

        # Once a deliberate return-to-center starts, latch that state until the
        # head motion itself settles.  Crossing the neutral point must not be
        # interpreted as a fresh turn in the opposite direction; that was the
        # source of the observed "回正时视角反向转动" behaviour.
        if self._return_latched:
            settle_velocity = max(keep_velocity, return_velocity * 0.72)
            if abs(velocity) <= settle_velocity:
                self._return_settle_s += dt
            else:
                self._return_settle_s = 0.0
            if self._return_settle_s >= max(0.07, stop_grace_s):
                self._return_latched = False
                self._return_settle_s = 0.0
                self.state = "CENTER" if amount <= start_angle else "STABLE_OFFSET"
            else:
                self.state = "RETURNING"
            self._slow_s = 0.0
            self.output = 0.0
            return 0.0

        if amount <= release_angle:
            self.state = "CENTER"
            self._slow_s = 0.0
            self._return_settle_s = 0.0
            self.output = 0.0
            return 0.0

        direction = 1.0 if norm > 0 else -1.0
        moving_same_way = direction * velocity
        accel_same_way = direction * acceleration

        # A strong raw reversal is allowed to stop immediately even if the
        # position smoother still has a same-direction tail. Tiny raw jitter is
        # ignored by return_step; this is only a high-confidence return hint.
        if direction * raw_delta <= -abs(return_step):
            self._return_latched = True
            self._return_settle_s = 0.0
            self.state = "RETURNING"
            self._slow_s = 0.0
            self.output = 0.0
            return 0.0

        # The head can still be physically to one side while already travelling
        # back toward neutral. That is a return motion, not a request to keep
        # rotating the game camera.
        if moving_same_way <= -return_velocity:
            self._return_latched = True
            self._return_settle_s = 0.0
            self.state = "RETURNING"
            self._slow_s = 0.0
            self.output = 0.0
            return 0.0

        turning_state = self.state in {"TURN_LEFT", "TURN_RIGHT"}
        if turning_state and self._raw_stable_s >= max(0.10, stop_grace_s):
            self.state = "STABLE_OFFSET"
            self._slow_s = 0.0
            self.output = 0.0
            return 0.0
        if not turning_state:
            if amount >= start_angle and moving_same_way >= start_velocity:
                self.state = "TURN_RIGHT" if direction > 0 else "TURN_LEFT"
                self._slow_s = 0.0
            else:
                self.state = "STABLE_OFFSET" if abs(velocity) < start_velocity else "RETURNING"
                self.output = 0.0
                return 0.0
        else:
            if moving_same_way >= keep_velocity:
                self._slow_s = 0.0
            else:
                self._slow_s += dt
                # Acceleration is intentionally auxiliary. A clear deceleration
                # lets us stop earlier, but acceleration is never required to
                # begin a turn because second derivatives are noisy.
                if accel_same_way <= -acceleration_stop and moving_same_way < start_velocity * 0.85:
                    self._slow_s += dt
                if self._slow_s >= stop_grace_s:
                    self.state = "STABLE_OFFSET"
                    self.output = 0.0
                    return 0.0

        shaped = _clamp((amount - start_angle) / max(1e-6, 1.0 - start_angle), 0.0, 1.0)
        # Keep a small floor after deliberate activation so a small but clear
        # head movement does not feel as if it triggered yet produced nothing.
        magnitude = max(0.10, shaped ** curve_gamma)
        self.output = direction * magnitude
        return self.output


class _RelativeYawAxis:
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
        self._return_latched=False; self._return_from_direction=0; self._return_evidence=0.0
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
        norm=_clamp(norm,-1,1); raw=norm if raw_norm is None else _clamp(raw_norm,-1,1)
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
                qdir=orig if orig else 1
                qs,_,qd,_=self._trend(now,qdir,.18); qrs,_,qrd,_=self._trend(now,qdir,.18,raw=True)
                quiet_center=abs(qs)<0.060 and abs(qrs)<0.100 and abs(qd)<0.020 and abs(qrd)<0.026
                if quiet_center:self._center_stable_s+=dt
                else:self._center_stable_s=0.0
                if self._center_stable_s>=0.14:
                    self._return_latched=False;self._return_from_direction=0;self._cross_evidence=0;self._clear_active();self._clear_motion_evidence();self.state='CENTER';self._history=[(now,norm,raw)]
                return 0.0
            self._center_stable_s=0.0
            # false-return recovery on original side needs sustained outward trend
            if orig and signal_dir==orig:
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
            raw_signal_dir=self._sign(raw,center*.55)
            crossed_far=amount>=YAW_V2_OPPOSITE_REARM or (raw_signal_dir==opp and abs(raw)>=0.115)
            if opp and crossed_far and (signal_dir==opp or raw_signal_dir==opp):
                sm,_,dm,em=self._trend(now,opp,.32);rsm,_,rdm,_=self._trend(now,opp,.32,raw=True)
                if dm>.030 and rdm>.022 and sm>.065 and rsm>.035 and em>.14:
                    self._cross_evidence+=dt
                    if self._cross_evidence>=0.05:
                        self._return_latched=False;self._return_from_direction=0;self._cross_evidence=0
                        self._active_direction=opp;self._active_age_s=.2;self._committed=True;self._turn_mode=self._classify_mode(opp,now);self._turn_baseline=max(.04,sm*.75);return self._drive(opp,norm)
                else:self._cross_evidence=max(0,self._cross_evidence-dt)
            return 0.0

        if amount<=center:
            if self._active_direction:
                d=self._active_direction
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
            if signal_dir==-d:
                if self._committed:
                    self._return_latched=True;self._return_from_direction=d;self._center_stable_s=0;self._cross_evidence=0;self._clear_active();self._clear_motion_evidence();self.state='RETURNING';self.output=0.0;return 0.0
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
            retreat=self._peak_norm-proj
            huge=(d*rd<=-.13 and retreat>=.13)
            if self._turn_mode=='FAST': ret=retreat>=.045 and s18<=-.10 and (rs18<=-.06 or s45<=-.055); need=.07
            elif self._turn_mode=='NORMAL': ret=retreat>=.055 and s45<=-.045 and rs45<=-.025; need=.12
            else: ret=retreat>=.060 and s45<=-.030 and rs45<=-.018 and (e45>.12 or r245>.18); need=.20
            if self._committed and (huge or ret): self._return_confirm_s+=dt
            else:self._return_confirm_s=max(0,self._return_confirm_s-dt*.6)
            if self._committed and (huge or self._return_confirm_s>=need):
                self._return_latched=True;self._return_from_direction=d;self._center_stable_s=0;self._cross_evidence=0;self._clear_active();self._clear_motion_evidence();self.state='RETURNING';self.output=0;return 0.0
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
            return self._drive(d,norm)

        # A completed TURN owns a real hold anchor.  An untriggered head that
        # merely drifted outside centre does *not*; it must keep evaluating the
        # regular tentative-start evidence every frame.  Conflating those two
        # states was a major slow-turn dead zone.
        if self.state=='STABLE_OFFSET' and self._held_from_turn:
            side=self._sign(self._hold_anchor) or signal_dir
            if side and side*norm < side*self._hold_anchor-max(.04,center*.8):
                sm,_,dm,_=self._trend(now,side,.30);rsm,_,rdm,_=self._trend(now,side,.30,raw=True)
                if sm<-.04 and (rsm<-.025 or dm<-.018):
                    self._return_latched=True;self._return_from_direction=side;self.state='RETURNING';self.output=0;return 0.0
            if side and signal_dir==side and side*(norm-self._hold_anchor)>=max(.035,center*.65):
                sm,_,dm,em=self._trend(now,side,.35);rsm,_,rdm,_=self._trend(now,side,.35,raw=True)
                if sm>.045 and rsm>.025 and dm>.014:return self._begin(side,now,norm)
            self.output=0;return 0.0

        # A gross one-frame jump is not a human head turn.  Quarantine its
        # filtered tail and restart trend history from the jump frame; otherwise
        # the post-spike OneEuro tail can look like a perfectly coherent turn.
        gross_jump=abs(fd)>=.12 or abs(rd)>=.15
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
            position_ok=amount>=max(center*1.10,.040)
            candidate=bool(position_ok and (fast or normal or slow))
            huge_step=abs(fd)>=.12 or abs(rd)>=.15
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
        self._pnp_model = tuple(tuple(float(v) for v in p) for p in self._MODEL)
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
        scale = math.sqrt(sum(x*x+y*y+z*z for x,y,z in centered) / 7.0)
        if not math.isfinite(scale) or scale <= 1e-6:
            return False
        # MediaPipe world coordinates are in metres.  Uniform model scale does
        # not change recovered rotation; mm keeps numerical magnitudes similar
        # to the original generic sparse-face template.
        self._pnp_model = tuple((x*1000.0, y*1000.0, z*1000.0) for x,y,z in centered)
        self.personal_model_active = True
        self.reset()
        return True

    @property
    def pnp_model(self) -> tuple[tuple[float, float, float], ...]:
        return self._pnp_model

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
        self._head_point_filter = _LandmarkEMA(HEAD_POINT_EMA_TAU_S)
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
        self.center_roll_samples: list[float] = []
        self.personal_pnp_calibration_derotate_yaw = 0.0
        self.personal_pnp_calibration_derotate_roll = 0.0
        self.personal_pnp_world_pair_yaw_deviation = math.inf
        self.personal_pnp_world_pair_yaws: tuple[float, float, float] | None = None
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
        self._vertical_pitch_filter = OneEuro(1.18, 0.075, 1.2)
        self._x_intent = _RelativeYawAxis(velocity_tau=YAW_V2_VELOCITY_TAU_S, acceleration_tau=0.11)
        self._cross_axis_lock = _CrossAxisPitchLock()
        self._vertical_intent = _HeadIntentAxis(velocity_tau=0.075, acceleration_tau=0.12)
        self.center_world_yaw = math.nan
        self.noise_world_yaw = math.inf
        self.center_norm_z_yaw = math.nan
        self.noise_norm_z_yaw = math.inf
        self.center_world_yaw_samples: list[float] = []
        self.center_norm_z_yaw_samples: list[float] = []
        self.current_world_yaw = math.nan
        self.current_norm_z_yaw = math.nan
        self.current_multi2d_proxy: tuple[float, ...] | None = None
        self.center_multi2d_proxy: tuple[float, ...] | None = None
        self.noise_multi2d_proxy: tuple[float, ...] | None = None
        self.real_shape_yaw = math.nan
        self.real_shape_yaw_median = math.nan
        self.real_shape_cal_sigma = math.inf
        self.real_shape_active = False
        self._real_shape_history: list[float] = []
        self._real_shape_world_oppose_count = 0
        self._real_shape_world_oppose_direction = 0
        self._real_shape_world_context_age_s = math.inf
        self._real_shape_world_veto_latched = False
        self._real_shape_world_veto_age_s = math.inf
        self._real_shape_world_last_t = 0.0
        self.real_shape_world_veto_active = False
        self.real_shape_tentative_quiet_active = False
        self.real_shape_tentative_preview_active = False
        self.real_shape_tentative_soft_opposition_active = False
        self.center_multi2d_proxy_samples: list[tuple[float, ...]] = []
        self.center_world_face_samples: list[tuple[tuple[float,float,float], ...]] = []
        self.center_world_face_template: tuple[tuple[float,float,float], ...] | None = None
        # v186 Stage-A frozen22 candidate state.  Diagnostic only: it never
        # feeds _RelativeYawAxis or Mouse-X.  Its direction is fixed above; the
        # current calibration supplies only neutral center and per-channel noise.
        self.frozen22_center: tuple[float, ...] | None = None
        self.frozen22_sigma: tuple[float, ...] | None = None
        self.frozen22_yaw = math.nan
        self.frozen22_yaw_median = math.nan
        self.frozen22_cal_sigma_deg = math.inf
        self.frozen22_world_resid_rel = math.inf
        self.frozen22_calibration_valid = False
        self._frozen22_history: list[float] = []
        # v187 outer confidence-gate state.  ``_base_output_x`` is the exact
        # pre-gate v160/v186 slew output; the gate never feeds back into it.
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
        self.v188_pitch_guard_active = False

        # v184 probe state.  None of these fields participate in control output.
        self.current_personal22_feature: tuple[float, ...] | None = None
        self.current_personal22_eye_px = math.nan
        self.personal22_center: tuple[float, ...] | None = None
        self.personal22_sigma: tuple[float, ...] | None = None
        self.personal22_signature: tuple[float, ...] | None = None
        self.personal22_yaw = math.nan
        self.personal22_yaw_median = math.nan
        self.personal22_cal_sigma_deg = math.inf
        self.personal22_world_resid_rel = math.inf
        self.personal22_z_est = math.nan
        self.personal22_eye_world = math.nan
        self.personal22_valid = False
        self._personal22_history: list[float] = []
        self.center_personal22_feature_samples: list[tuple[float, ...]] = []
        self.center_personal22_eye_px_samples: list[float] = []
        self.center_personal22_focal_samples: list[float] = []
        self.center_world_head11_samples: list[tuple[tuple[float,float,float], ...]] = []
        # v186 Stage-A recorder. Completely opt-in: when the environment
        # variable is absent, no file is opened and the v184/v160 control path
        # is byte-for-byte unaffected below this diagnostic layer.
        self._stage_a_log_path: Path | None = None
        self._stage_a_label_path: Path | None = None
        self._stage_a_log_handle = None
        self._stage_a_log_count = 0
        self._stage_a_label = "unlabeled"
        self._stage_a_label_mtime_ns = -1
        self._stage_a_label_check_t = 0.0
        self._stage_a_init_recorder()
        self.center_pnp_pose_samples: list[tuple[dict[str, dict], int, int]] = []
        self.personal_pnp_active = False
        self.personal_pnp_valid_samples = 0
        self.personal_pnp_median_reprojection = math.nan
        self.personal_pnp_center_depth = math.nan
        self.personal_pnp_current_depth = math.nan
        self.personal_pnp_far_depth_ratio = 1.0
        self._calibration_restore_model: tuple[tuple[float, float, float], ...] | None = None
        self._calibration_restore_personal_active = False
        self._calibration_restore_depth = math.nan
        self.current_world_rigid_yaw = math.nan
        self.current_world_rigid_fit = math.nan
        self.center_world_rigid_yaw = math.nan
        self.noise_world_rigid_yaw = math.inf
        self.vertical_gate_active = False
        self.vertical_temp_center_pitch: float | None = None
        self.vertical_filtered_pitch = math.nan
        self.vertical_norm = 0.0
        self.last_error = ""
        self.notice = ""
        self.notice_until = 0.0
        self._load_profile()

    def _stage_a_init_recorder(self) -> None:
        raw = str(os.environ.get(STAGE_A_LOG_ENV, "")).strip()
        if not raw:
            return
        try:
            candidate = Path(raw).expanduser()
            # A path without a .jsonl suffix is treated as a directory so the
            # user can set only one simple environment variable.
            if candidate.suffix.lower() != ".jsonl":
                stamp = time.strftime("%Y%m%d-%H%M%S")
                candidate = candidate / f"head_stageA_v186_{stamp}.jsonl"
            candidate.parent.mkdir(parents=True, exist_ok=True)
            self._stage_a_log_path = candidate
            label_raw = str(os.environ.get(STAGE_A_LABEL_ENV, "")).strip()
            self._stage_a_label_path = Path(label_raw).expanduser() if label_raw else candidate.with_suffix(".label.txt")
            self._stage_a_log_handle = candidate.open("a", encoding="utf-8", buffering=1)
            header = {
                "record_type": "stageA_header",
                "schema": 1,
                "probe_version": STAGE_A_PROBE_VERSION,
                "personal22_probe_version": PERSONAL22_PROBE_VERSION,
                "frozen22_signature_version": FROZEN22_SIGNATURE_VERSION,
                "frozen22_signature": list(FROZEN22_SIGNATURE),
                "horizontal_algorithm_version": HEAD_HORIZONTAL_ALGORITHM_VERSION,
                "created_unix": time.time(),
                "label_file": str(self._stage_a_label_path),
                "personal22_controls_mouse": False,
                "frozen22_controls_mouse": True,
            }
            self._stage_a_log_handle.write(json.dumps(header, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError:
            self._stage_a_log_path = None
            self._stage_a_label_path = None
            self._stage_a_log_handle = None

    def _stage_a_read_label(self, now: float) -> str:
        path = self._stage_a_label_path
        if path is None:
            return "unlabeled"
        if now - self._stage_a_label_check_t < 0.20:
            return self._stage_a_label
        self._stage_a_label_check_t = now
        try:
            st = path.stat()
            if st.st_mtime_ns != self._stage_a_label_mtime_ns:
                text = path.read_text(encoding="utf-8").strip().lower().replace(" ", "_")
                if text:
                    self._stage_a_label = text[:64]
                self._stage_a_label_mtime_ns = st.st_mtime_ns
        except OSError:
            pass
        return self._stage_a_label

    def _stage_a_record_frame(
        self,
        *,
        now: float,
        width: int,
        height: int,
        current_world_head11: tuple[tuple[float, float, float], ...] | None,
    ) -> None:
        h = self._stage_a_log_handle
        if h is None:
            return
        try:
            rec = {
                "record_type": "frame",
                "schema": 1,
                "probe_version": STAGE_A_PROBE_VERSION,
                "t_mono": round(float(now), 6),
                "t_unix": time.time(),
                "label": self._stage_a_read_label(now),
                "width": int(width),
                "height": int(height),
                "calibrated": bool(self.calibrated),
                "frozen22_signature_version": FROZEN22_SIGNATURE_VERSION,
                "frozen22_calibration_valid": bool(self.frozen22_calibration_valid),
                "frozen22_frame_valid": bool(
                    self.frozen22_calibration_valid
                    and self.current_personal22_feature is not None
                    and math.isfinite(self.frozen22_yaw_median)
                ),
                "frozen22_yaw": (float(self.frozen22_yaw) if math.isfinite(self.frozen22_yaw) else None),
                "frozen22_yaw_median": (float(self.frozen22_yaw_median) if math.isfinite(self.frozen22_yaw_median) else None),
                "frozen22_cal_sigma": (float(self.frozen22_cal_sigma_deg) if math.isfinite(self.frozen22_cal_sigma_deg) else None),
                "frozen22_world_resid_rel": (float(self.frozen22_world_resid_rel) if math.isfinite(self.frozen22_world_resid_rel) else None),
                "frozen22_gate_version": FROZEN22_GATE_VERSION,
                "frozen22_gate_source": self.frozen22_gate_source,
                "frozen22_gate_scale": float(self.frozen22_gate_scale),
                "frozen22_center": (list(self.frozen22_center) if self.frozen22_center is not None else None),
                "frozen22_sigma": (list(self.frozen22_sigma) if self.frozen22_sigma is not None else None),
                "personal22_valid": bool(self.personal22_valid),
                "personal22_yaw": (float(self.personal22_yaw) if math.isfinite(self.personal22_yaw) else None),
                "personal22_yaw_median": (float(self.personal22_yaw_median) if math.isfinite(self.personal22_yaw_median) else None),
                "personal22_cal_sigma": (float(self.personal22_cal_sigma_deg) if math.isfinite(self.personal22_cal_sigma_deg) else None),
                "personal22_world_resid_rel": (float(self.personal22_world_resid_rel) if math.isfinite(self.personal22_world_resid_rel) else None),
                "personal22_center": (list(self.personal22_center) if self.personal22_center is not None else None),
                "personal22_sigma": (list(self.personal22_sigma) if self.personal22_sigma is not None else None),
                "personal22_signature_personal": (list(self.personal22_signature) if self.personal22_signature is not None else None),
                "personal22_feature": (list(self.current_personal22_feature) if self.current_personal22_feature is not None else None),
                "world_head11": ([list(v) for v in current_world_head11] if current_world_head11 is not None else None),
                "real_shape_yaw": (float(self.real_shape_yaw) if math.isfinite(self.real_shape_yaw) else None),
                "real_shape_yaw_median": (float(self.real_shape_yaw_median) if math.isfinite(self.real_shape_yaw_median) else None),
                "real_shape_cal_sigma": (float(self.real_shape_cal_sigma) if math.isfinite(self.real_shape_cal_sigma) else None),
                "world_rigid_yaw": (float(self.current_world_rigid_yaw) if math.isfinite(self.current_world_rigid_yaw) else None),
                "world_rigid_sigma": (float(self.noise_world_rigid_yaw) if math.isfinite(self.noise_world_rigid_yaw) else None),
                "filtered_yaw": (float(self.filtered_yaw) if math.isfinite(self.filtered_yaw) else None),
                "raw_pitch": (float(self.raw.pitch) if self.raw.valid and math.isfinite(self.raw.pitch) else None),
                "center_pitch": (float(self.center_pitch) if self.calibrated and math.isfinite(self.center_pitch) else None),
                "pitch_delta": (float(self.raw.pitch - self.center_pitch) if self.raw.valid and self.calibrated and math.isfinite(self.raw.pitch) else None),
                "norm_x": float(self.norm_x),
                "output_x": float(self.output_x / 100.0),
                "intent_state": str(self._x_intent.state),
                "yaw_committed": bool(getattr(self._x_intent, "committed", False)),
                "yaw_velocity_norm_s": float(getattr(self._x_intent, "velocity", 0.0)),
                "yaw_return_latched": bool(getattr(self._x_intent, "return_latched", False)),
                "tentative_age_s": float(getattr(self._x_intent, "tentative_age_s", 0.0)),
                "world_veto": bool(self.real_shape_world_veto_active),
                "preview": bool(self.real_shape_tentative_preview_active),
                "soft_opposition": bool(self.real_shape_tentative_soft_opposition_active),
            }
            h.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
            self._stage_a_log_count += 1
            if self._stage_a_log_count % 15 == 0:
                h.flush()
        except (OSError, TypeError, ValueError):
            # Evidence collection must never be allowed to alter control.
            return

    def close_stage_a_recorder(self) -> None:
        h = self._stage_a_log_handle
        self._stage_a_log_handle = None
        if h is not None:
            try:
                h.flush()
                h.close()
            except OSError:
                pass

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
        """Build and validate the personal PnP model from this calibration.

        The same accepted calibration frames are re-solved offline with the
        personal 3D model, so one user calibration supplies both the model and
        the matching neutral yaw/pitch center.
        """
        self.personal_pnp_active = False
        self.personal_pnp_valid_samples = 0
        self.personal_pnp_median_reprojection = math.nan
        self.personal_pnp_center_depth = math.nan
        self.personal_pnp_far_depth_ratio = 1.0
        self.personal_pnp_calibration_derotate_yaw = 0.0
        self.personal_pnp_calibration_derotate_roll = 0.0
        self.personal_pnp_world_pair_yaw_deviation = math.inf
        self.personal_pnp_world_pair_yaws = None
        template = self.center_world_face_template
        if (
            self.config.get("algorithm") != "pnp"
            or template is None
            or len(self.center_pnp_pose_samples) < PERSONAL_PNP_MIN_SAMPLES
            or not math.isfinite(self.noise_world_rigid_yaw)
            or self.noise_world_rigid_yaw > PERSONAL_PNP_MAX_WORLD_RIGID_SIGMA_DEG
        ):
            return False

        pair_dev, pair_yaws = _world_pair_yaw_consistency(template)
        self.personal_pnp_world_pair_yaw_deviation = pair_dev
        self.personal_pnp_world_pair_yaws = pair_yaws
        if not math.isfinite(pair_dev) or pair_dev > PERSONAL_PNP_MAX_WORLD_PAIR_YAW_DEVIATION_DEG:
            return False

        # A personal world template captured while the user is clearly yawed or
        # rolled carries that calibration rotation inside the model itself.
        # Relative pitch can then project into false yaw even though the face
        # geometry is personal.  The generic PnP stream is already available
        # during calibration; use its robust center only as a large-pose cue.
        # Below the thresholds the personal template is left bit-for-bit
        # untouched.  Generic pitch is deliberately excluded from de-rotation.
        # World left/right depth asymmetry is a cleaner calibration-yaw cue
        # than generic sparse-PnP yaw: personal nose/ear geometry can bias the
        # latter by several degrees even while the user is truly neutral.
        # The already-robust calibration world-yaw center is therefore used for
        # yaw de-rotation; generic PnP remains useful for roll, where its
        # face-shape bias is much smaller.
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
            return False
        ys: list[float] = []; ps: list[float] = []; reps: list[float] = []; depths: list[float] = []
        for snap, w, h in self.center_pnp_pose_samples:
            e = offline.estimate(snap, w, h, "pnp")
            if not e.valid:
                continue
            ys.append(e.yaw); ps.append(e.pitch)
            if math.isfinite(e.reprojection_error): reps.append(e.reprojection_error)
            if math.isfinite(offline.pnp_depth): depths.append(offline.pnp_depth)
        if len(ys) < PERSONAL_PNP_MIN_SAMPLES:
            return False
        cy, ny = _robust_center_and_sigma(ys)
        cp, npitch = _robust_center_and_sigma(ps)
        med_rep = _median(reps) if reps else math.inf
        med_depth = _median(depths) if depths else math.nan
        if not (math.isfinite(cy) and math.isfinite(cp) and math.isfinite(ny) and math.isfinite(npitch)):
            return False
        if not math.isfinite(med_rep) or med_rep > PERSONAL_PNP_MAX_MEDIAN_REPROJECTION:
            return False
        if not math.isfinite(med_depth) or med_depth <= 1e-6:
            return False
        if not self.estimator.set_pnp_model(template):
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
        return True

    def _span(self) -> tuple[float, float]:
        if self.config["algorithm"] == "pnp":
            return PNP_YAW_SPAN_DEG, PNP_PITCH_SPAN_DEG
        return RATIO_YAW_SPAN, RATIO_PITCH_SPAN

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
        self.v188_pitch_guard_active = False

    def _frozen22_gate_sample(self, now: float, window_s: float) -> tuple[float, float, float] | None:
        """Return the latest sample at/before ``now-window_s``.

        Using time rather than a frame index keeps the evidence window stable
        when the phone or PC camera moves between 20/30/45/60 FPS.  A very stale
        sample is rejected rather than silently stretching the window.
        """
        target = now - window_s
        for t, yaw, pitch, world in reversed(self._frozen22_gate_history):
            if t <= target:
                if target - t > FROZEN22_GATE_MAX_SAMPLE_AGE_S:
                    return None
                return yaw, pitch, world
        return None

    def _frozen22_uncertainty_scale(self, vx: float, now: float) -> float:
        """Confidence-gate the already-computed v160 horizontal output.

        FAST: short-window frozen22 evidence, no world dependency.
        SLOW: long-window frozen22 + independent world agreement, only after
              the original v160 state machine has committed to TURN.
        FALLBACK: unresolved committed TURN is exposed at 3% amplitude.
        DISABLED: invalid frozen22 calibration preserves v160 exactly.
        """
        # Keep a signed copy that follows the user's configured Mouse-X
        # direction.  Pitch is only used by absolute magnitude.
        sign = -1.0 if self.config.get("invert_x") else 1.0
        fy = float(self.frozen22_yaw_median) * sign if math.isfinite(self.frozen22_yaw_median) else math.nan
        fp = float(self.filtered_pitch) if math.isfinite(self.filtered_pitch) else math.nan
        wy = float(self.current_world_rigid_yaw) * sign if math.isfinite(self.current_world_rigid_yaw) else math.nan

        if math.isfinite(fy) and math.isfinite(fp):
            self._frozen22_gate_history.append((now, fy, fp, wy))
            cutoff = now - FROZEN22_GATE_HISTORY_S
            while self._frozen22_gate_history and self._frozen22_gate_history[0][0] < cutoff:
                del self._frozen22_gate_history[0]

        self.frozen22_gate_fast_delta_deg = math.nan
        self.frozen22_gate_slow_delta_deg = math.nan
        self.frozen22_gate_slow_world_delta_deg = math.nan
        self.frozen22_gate_pitch_delta_deg = math.nan

        valid = bool(
            self.frozen22_calibration_valid
            and math.isfinite(self.frozen22_cal_sigma_deg)
            and math.isfinite(fy)
            and math.isfinite(fp)
        )
        if not valid:
            self._frozen22_gate_direction = 0
            self._frozen22_gate_fast_count = 0
            self._frozen22_gate_lease_direction = 0
            self._frozen22_gate_lease_until = 0.0
            self.frozen22_gate_scale = 1.0
            self.frozen22_gate_source = "DISABLED"
            return 1.0

        sigma = max(0.0, float(self.frozen22_cal_sigma_deg))
        fast_thr = max(FROZEN22_GATE_FAST_BASE_DELTA_DEG, FROZEN22_GATE_FAST_SIGMA_MULT * sigma)
        slow_thr = max(FROZEN22_GATE_SLOW_BASE_DELTA_DEG, FROZEN22_GATE_SLOW_SIGMA_MULT * sigma)
        self.frozen22_gate_fast_threshold_deg = fast_thr
        self.frozen22_gate_slow_threshold_deg = slow_thr

        direction = 1 if vx > 1e-12 else (-1 if vx < -1e-12 else 0)
        if direction == 0:
            # Do not erase a valid lease just because v160 briefly emits zero;
            # the lease will naturally expire.  No visible output exists here.
            self.frozen22_gate_scale = 0.0
            self.frozen22_gate_source = "ZERO"
            return 0.0

        if direction != self._frozen22_gate_direction:
            self._frozen22_gate_direction = direction
            self._frozen22_gate_fast_count = 0
            self._frozen22_gate_lease_direction = 0
            self._frozen22_gate_lease_until = 0.0

        fast_ok = False
        slow_ok = False
        old = self._frozen22_gate_sample(now, FROZEN22_GATE_FAST_WINDOW_S)
        if old is not None:
            oy, op, _ = old
            dy = fy - oy
            dp = fp - op
            self.frozen22_gate_fast_delta_deg = dy
            self.frozen22_gate_pitch_delta_deg = dp
            fast_ok = bool(
                direction * dy >= fast_thr
                and abs(dy) >= FROZEN22_GATE_FAST_PITCH_RATIO * abs(dp)
            )

        if fast_ok:
            self._frozen22_gate_fast_count += 1
        else:
            self._frozen22_gate_fast_count = max(0, self._frozen22_gate_fast_count - 1)

        committed = bool(getattr(self._x_intent, "committed", False))
        old = self._frozen22_gate_sample(now, FROZEN22_GATE_SLOW_WINDOW_S)
        world_reliable = bool(
            math.isfinite(wy)
            and math.isfinite(self.noise_world_rigid_yaw)
            and self.noise_world_rigid_yaw <= PERSONAL_PNP_MAX_WORLD_RIGID_SIGMA_DEG
        )
        if committed and old is not None and world_reliable:
            oy, op, ow = old
            if math.isfinite(ow):
                dy = fy - oy
                dp = fp - op
                dw = wy - ow
                self.frozen22_gate_slow_delta_deg = dy
                self.frozen22_gate_slow_world_delta_deg = dw
                # Keep the fast-window pitch diagnostic if available; otherwise
                # expose the slow-window value.
                if not math.isfinite(self.frozen22_gate_pitch_delta_deg):
                    self.frozen22_gate_pitch_delta_deg = dp
                slow_ok = bool(
                    direction * dy >= slow_thr
                    and direction * dw >= FROZEN22_GATE_SLOW_WORLD_DELTA_DEG
                    and abs(dy) >= FROZEN22_GATE_SLOW_PITCH_RATIO * abs(dp)
                )

        pitch_offset = (
            abs(fp - float(self.center_pitch))
            if math.isfinite(fp) and math.isfinite(float(self.center_pitch))
            else 0.0
        )
        yaw_offset = abs(fy) if math.isfinite(fy) else 0.0
        self.v188_pitch_guard_active = bool(
            pitch_offset >= V188_PITCH_GUARD_MIN_DEG
            and yaw_offset < V188_PITCH_GUARD_YAW_TO_PITCH * pitch_offset
            and not slow_ok
        )
        if self.v188_pitch_guard_active:
            self.frozen22_gate_scale = 0.0
            self.frozen22_gate_source = "PITCH_GUARD"
            return 0.0

        if self._frozen22_gate_fast_count >= FROZEN22_GATE_FAST_CONFIRM_FRAMES:
            self._frozen22_gate_lease_direction = direction
            self._frozen22_gate_lease_until = now + FROZEN22_GATE_FAST_LEASE_S
            self.frozen22_gate_scale = 1.0
            self.frozen22_gate_source = "FAST"
            return 1.0
        if slow_ok:
            self._frozen22_gate_lease_direction = direction
            self._frozen22_gate_lease_until = now + FROZEN22_GATE_SLOW_LEASE_S
            self.frozen22_gate_scale = 1.0
            self.frozen22_gate_source = "SLOW"
            return 1.0
        if self._frozen22_gate_lease_direction == direction and now <= self._frozen22_gate_lease_until:
            self.frozen22_gate_scale = 1.0
            self.frozen22_gate_source = "LEASE"
            return 1.0

        self._frozen22_gate_lease_direction = 0
        self._frozen22_gate_lease_until = 0.0
        if committed:
            self.frozen22_gate_scale = FROZEN22_GATE_COMMITTED_FALLBACK_SCALE
            self.frozen22_gate_source = "FALLBACK"
            return FROZEN22_GATE_COMMITTED_FALLBACK_SCALE
        self.frozen22_gate_scale = 0.0
        self.frozen22_gate_source = "BLOCK"
        return 0.0

    def _reset_filters(self) -> None:
        self._head_point_filter.reset()
        self._yaw_filter.reset()
        self._pitch_filter.reset()
        self._vertical_pitch_filter.reset()
        self._x_intent.reset()
        self._cross_axis_lock.reset()
        self._real_shape_history = []
        self.real_shape_yaw = math.nan
        self.real_shape_yaw_median = math.nan
        self.real_shape_active = False
        self._real_shape_world_oppose_count = 0
        self._real_shape_world_oppose_direction = 0
        self._real_shape_world_context_age_s = math.inf
        self._real_shape_world_veto_latched = False
        self._real_shape_world_veto_age_s = math.inf
        self._real_shape_world_last_t = 0.0
        self.real_shape_world_veto_active = False
        self._vertical_intent.reset()
        self.vertical_gate_active = False
        self.vertical_temp_center_pitch = None
        self.vertical_filtered_pitch = math.nan
        self.vertical_norm = 0.0
        self.filtered_yaw = math.nan
        self.filtered_pitch = math.nan
        self._axis_active_x = False
        self._axis_active_y = False
        self.output_x = self.output_y = 0.0
        self._reset_frozen22_gate()
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
        vertical_source: str | None = None,
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
        if vertical_source is not None:
            source = str(vertical_source).strip().lower()
            if source not in {"right_wrist", "head"}:
                raise ValueError("vertical_source must be right_wrist or head")
            if source != self.config.get("vertical_source"):
                self.config["vertical_source"] = source
                self.end_vertical_gate()
        self._recompute_deadzone()
        self._save_profile()

    def start_center(self, now: float | None = None, kind: str = "manual") -> None:
        now = time.monotonic() if now is None else now
        # Always collect a new center with the generic PnP geometry first.  This
        # guarantees that a calibration with missing/unreliable world landmarks
        # can safely fall back to generic PnP without a center/model mismatch.
        # If the attempt is cancelled or fails, restore the previously valid
        # personal model together with the already-preserved old center.
        if self.config.get("algorithm") == "pnp":
            self._calibration_restore_model = self.estimator.pnp_model if self.personal_pnp_active else None
            self._calibration_restore_personal_active = bool(self.personal_pnp_active)
            self._calibration_restore_depth = self.personal_pnp_center_depth if self.personal_pnp_active else math.nan
            self.estimator.set_pnp_model(None)
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
        self.center_roll_samples = []
        self.center_confidence_samples = []
        self.center_world_yaw_samples = []
        self.center_norm_z_yaw_samples = []
        self.center_multi2d_proxy_samples = []
        self.center_world_face_samples = []
        self.center_personal22_feature_samples = []
        self.center_personal22_eye_px_samples = []
        self.center_personal22_focal_samples = []
        self.center_world_head11_samples = []
        self.center_pnp_pose_samples = []
        self.current_world_rigid_yaw = math.nan
        self.current_world_rigid_fit = math.nan
        self._reset_filters()
        self.notice = "校准已开始：看向游戏屏幕中心，保持自然姿势"
        self.notice_until = now + CENTER_WALL_LIMIT_S

    def _restore_precalibration_pnp_model(self) -> None:
        if self._calibration_restore_personal_active and self._calibration_restore_model is not None:
            # pnp_model is already centered/mm; set_pnp_model recenters and
            # uniformly rescales once more, which is rotation-equivalent.
            self.estimator.set_pnp_model(self._calibration_restore_model)
            self.personal_pnp_active = True
            self.personal_pnp_center_depth = self._calibration_restore_depth
        self._calibration_restore_model = None
        self._calibration_restore_personal_active = False
        self._calibration_restore_depth = math.nan

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
        self.center_roll_samples = []
        self.center_confidence_samples = []
        self.center_world_yaw_samples = []
        self.center_norm_z_yaw_samples = []
        self.center_multi2d_proxy_samples = []
        self.center_world_face_samples = []
        self.center_personal22_feature_samples = []
        self.center_personal22_eye_px_samples = []
        self.center_personal22_focal_samples = []
        self.center_world_head11_samples = []
        self.center_pnp_pose_samples = []
        self._restore_precalibration_pnp_model()
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
                        centers.append(c2); sigmas.append(max(0.0, s2) if math.isfinite(s2) else math.inf)
                    if all(math.isfinite(v) for v in centers):
                        self.center_multi2d_proxy = tuple(centers)
                        self.noise_multi2d_proxy = tuple(sigmas)
                    else:
                        self.center_multi2d_proxy = None; self.noise_multi2d_proxy = None
                else:
                    self.center_multi2d_proxy = None; self.noise_multi2d_proxy = None

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

                # v186 Stage-A frozen 22D candidate.  The direction is fixed
                # from archived training data.  Current calibration is allowed
                # to estimate only the neutral center and per-channel noise.
                self.frozen22_center = None
                self.frozen22_sigma = None
                self.frozen22_yaw = math.nan
                self.frozen22_yaw_median = math.nan
                self.frozen22_cal_sigma_deg = math.inf
                self.frozen22_world_resid_rel = math.inf
                self.frozen22_calibration_valid = False
                self._frozen22_history = []
                if len(self.center_personal22_feature_samples) >= PERSONAL22_MIN_SAMPLES:
                    frozen_centers: list[float] = []
                    frozen_sigmas: list[float] = []
                    for j in range(22):
                        c22, s22 = _robust_center_and_sigma([v[j] for v in self.center_personal22_feature_samples])
                        frozen_centers.append(c22)
                        frozen_sigmas.append(max(PERSONAL22_SIGMA_FLOOR, s22) if math.isfinite(s22) else math.inf)
                    if all(math.isfinite(v) for v in frozen_centers) and all(math.isfinite(v) for v in frozen_sigmas):
                        fcenter, fsigma = tuple(frozen_centers), tuple(frozen_sigmas)
                        fyaws = [
                            _personal22_matched_yaw(v, fcenter, fsigma, FROZEN22_SIGNATURE)
                            for v in self.center_personal22_feature_samples
                        ]
                        _, fcal_sigma = _robust_center_and_sigma(fyaws)
                        self.frozen22_center = fcenter
                        self.frozen22_sigma = fsigma
                        self.frozen22_cal_sigma_deg = fcal_sigma if math.isfinite(fcal_sigma) else math.inf
                if len(self.center_world_head11_samples) >= PERSONAL22_MIN_SAMPLES:
                    pts11_frozen: list[tuple[float, float, float]] = []
                    for i in range(11):
                        pts11_frozen.append(tuple(_median([f[i][j] for f in self.center_world_head11_samples]) for j in range(3)))
                    template11_frozen = tuple(pts11_frozen)
                    if all(all(math.isfinite(v) for v in p) for p in template11_frozen):
                        self.frozen22_world_resid_rel = _personal22_world_residual_rel(
                            template11_frozen, self.center_world_head11_samples
                        )
                self.frozen22_calibration_valid = bool(
                    self.frozen22_center is not None
                    and self.frozen22_sigma is not None
                    and math.isfinite(self.frozen22_cal_sigma_deg)
                    and self.frozen22_cal_sigma_deg <= FROZEN22_MAX_CAL_SIGMA_DEG
                )

                # v184 diagnostic-only 11-point personal perspective signature.
                # Keep it isolated from source selection / _RelativeYawAxis.
                self.personal22_center = None
                self.personal22_sigma = None
                self.personal22_signature = None
                self.personal22_cal_sigma_deg = math.inf
                self.personal22_world_resid_rel = math.inf
                self.personal22_z_est = math.nan
                self.personal22_eye_world = math.nan
                self.personal22_valid = False
                self._personal22_history = []
                if (
                    len(self.center_personal22_feature_samples) >= PERSONAL22_MIN_SAMPLES
                    and len(self.center_world_head11_samples) >= PERSONAL22_MIN_SAMPLES
                    and len(self.center_personal22_eye_px_samples) >= PERSONAL22_MIN_SAMPLES
                    and len(self.center_personal22_focal_samples) >= PERSONAL22_MIN_SAMPLES
                ):
                    centers22: list[float] = []
                    sigmas22: list[float] = []
                    for j in range(22):
                        c22, s22 = _robust_center_and_sigma([v[j] for v in self.center_personal22_feature_samples])
                        centers22.append(c22)
                        sigmas22.append(max(PERSONAL22_SIGMA_FLOOR, s22) if math.isfinite(s22) else math.inf)
                    pts11: list[tuple[float, float, float]] = []
                    for i in range(11):
                        pts11.append(tuple(_median([f[i][j] for f in self.center_world_head11_samples]) for j in range(3)))
                    template11 = tuple(pts11)
                    if all(math.isfinite(v) for v in centers22) and all(math.isfinite(v) for v in sigmas22) and all(all(math.isfinite(v) for v in p) for p in template11):
                        eye_px = _median(self.center_personal22_eye_px_samples)
                        focal_px = _median(self.center_personal22_focal_samples)
                        sig22, z_est, eye_world = _personal22_signature_from_world(template11, focal_px=focal_px, eye_px=eye_px)
                        resid_rel = _personal22_world_residual_rel(template11, self.center_world_head11_samples)
                        if sig22 is not None:
                            ctuple, stuple = tuple(centers22), tuple(sigmas22)
                            cal_yaws = [
                                _personal22_matched_yaw(v, ctuple, stuple, sig22)
                                for v in self.center_personal22_feature_samples
                            ]
                            _, cal_sigma22 = _robust_center_and_sigma(cal_yaws)
                            self.personal22_center = ctuple
                            self.personal22_sigma = stuple
                            self.personal22_signature = sig22
                            self.personal22_cal_sigma_deg = cal_sigma22 if math.isfinite(cal_sigma22) else math.inf
                            self.personal22_world_resid_rel = resid_rel
                            self.personal22_z_est = z_est
                            self.personal22_eye_world = eye_world
                            self.personal22_valid = bool(
                                math.isfinite(resid_rel)
                                and resid_rel <= PERSONAL22_WORLD_RESID_REL_MAX
                                and math.isfinite(self.personal22_cal_sigma_deg)
                                and self.personal22_cal_sigma_deg <= PERSONAL22_MAX_CAL_SIGMA_DEG
                            )

                # v142: source-level fix.  Build the user's own 7-point PnP
                # model from calibration-time world landmarks and then recompute
                # the neutral center using the same accepted 2D calibration
                # frames.  If validation fails, keep the generic model and the
                # v129 cross-axis fallback unchanged.
                if not self._activate_personal_pnp_from_center():
                    self.estimator.set_pnp_model(None)
                    self.personal_pnp_active = False
                self._calibration_restore_model = None
                self._calibration_restore_personal_active = False
                self._calibration_restore_depth = math.nan

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
            self._restore_precalibration_pnp_model()
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
        self.center_roll_samples = []
        self.center_confidence_samples = []
        self.center_world_yaw_samples = []
        self.center_norm_z_yaw_samples = []
        self.center_multi2d_proxy_samples = []
        self.center_world_face_samples = []
        self.center_personal22_feature_samples = []
        self.center_personal22_eye_px_samples = []
        self.center_personal22_focal_samples = []
        self.center_world_head11_samples = []
        self.center_pnp_pose_samples = []
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

    def update(
        self,
        pose: dict[str, dict] | None,
        width: int,
        height: int,
        now: float | None = None,
        world_pose: dict[str, dict] | list[dict] | None = None,
    ) -> tuple[float, float]:
        now = time.monotonic() if now is None else now
        # ``world_pose`` is optional for strict backward compatibility.  It may
        # be either the named map used by this module or the phone's raw 33-point
        # MediaPipe world list.  A caller may also embed a named map under
        # ``__world_pose__`` / ``world_pose`` in the pose dictionary.
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
        personal22_now = _head11_local_feature(pose, width, height)
        if personal22_now is not None:
            self.current_personal22_feature, self.current_personal22_eye_px = personal22_now
        else:
            self.current_personal22_feature, self.current_personal22_eye_px = None, math.nan
        frozen22 = _personal22_matched_yaw(
            self.current_personal22_feature, self.frozen22_center, self.frozen22_sigma, FROZEN22_SIGNATURE
        )
        self.frozen22_yaw = frozen22
        if math.isfinite(frozen22):
            self._frozen22_history.append(float(frozen22))
            if len(self._frozen22_history) > FROZEN22_MEDIAN_WINDOW:
                del self._frozen22_history[:-FROZEN22_MEDIAN_WINDOW]
            self.frozen22_yaw_median = _median(self._frozen22_history)
        else:
            self.frozen22_yaw_median = math.nan
        p22 = _personal22_matched_yaw(
            self.current_personal22_feature, self.personal22_center, self.personal22_sigma, self.personal22_signature
        )
        self.personal22_yaw = p22
        if math.isfinite(p22):
            self._personal22_history.append(float(p22))
            if len(self._personal22_history) > PERSONAL22_MEDIAN_WINDOW:
                del self._personal22_history[:-PERSONAL22_MEDIAN_WINDOW]
            self.personal22_yaw_median = _median(self._personal22_history)
        else:
            self.personal22_yaw_median = math.nan
        shape_yaw, shape_sigma = _real_shape_yaw_from_proxy(
            self.current_multi2d_proxy, self.center_multi2d_proxy, self.noise_multi2d_proxy
        )
        self.real_shape_yaw = shape_yaw
        self.real_shape_cal_sigma = shape_sigma
        if math.isfinite(shape_yaw):
            self._real_shape_history.append(float(shape_yaw))
            if len(self._real_shape_history) > REAL_SHAPE_MEDIAN_WINDOW:
                del self._real_shape_history[:-REAL_SHAPE_MEDIAN_WINDOW]
            self.real_shape_yaw_median = _median(self._real_shape_history)
        else:
            self.real_shape_yaw_median = math.nan
        self.current_world_rigid_yaw = math.nan
        self.current_world_rigid_fit = math.nan
        current_world_face = _world_face_xyz(world_pose_map)
        current_world_head11 = _world_head11_xyz(world_pose_map)
        if self.center_world_face_template is not None and current_world_face is not None:
            ry, rfit = _world_rigid_yaw(self.center_world_face_template, current_world_face)
            if math.isfinite(ry) and math.isfinite(rfit) and rfit <= WORLD_RIGID_FIT_GROSS_MAX:
                self.current_world_rigid_yaw = ry - (self.center_world_rigid_yaw if math.isfinite(self.center_world_rigid_yaw) else 0.0)
                self.current_world_rigid_fit = rfit
        estimate_pose = pose
        if self.config["algorithm"] == "pnp":
            estimate_pose = self._head_point_filter.apply(pose, HeadPoseEstimator._PNP_NAMES, now)
        estimate = self.estimator.estimate(estimate_pose, width, height, self.config["algorithm"])
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
        self.personal_pnp_current_depth = self.estimator.pnp_depth
        self.personal_pnp_far_depth_ratio = 1.0
        if (
            self.personal_pnp_active
            and math.isfinite(self.personal_pnp_center_depth)
            and math.isfinite(self.personal_pnp_current_depth)
            and self.personal_pnp_center_depth > 1e-6
        ):
            self.personal_pnp_far_depth_ratio = _clamp(
                self.personal_pnp_current_depth / self.personal_pnp_center_depth,
                1.0, PERSONAL_PNP_FAR_MAX_DEPTH_RATIO,
            )

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
            if math.isfinite(estimate.roll):
                self.center_roll_samples.append(estimate.roll)
            self.center_confidence_samples.append(estimate.confidence)
            if math.isfinite(self.current_world_yaw):
                self.center_world_yaw_samples.append(self.current_world_yaw)
            if math.isfinite(self.current_norm_z_yaw):
                self.center_norm_z_yaw_samples.append(self.current_norm_z_yaw)
            if self.current_multi2d_proxy is not None:
                self.center_multi2d_proxy_samples.append(self.current_multi2d_proxy)
            if current_world_face is not None:
                self.center_world_face_samples.append(current_world_face)
            if self.current_personal22_feature is not None and math.isfinite(self.current_personal22_eye_px):
                self.center_personal22_feature_samples.append(self.current_personal22_feature)
                self.center_personal22_eye_px_samples.append(float(self.current_personal22_eye_px))
                self.center_personal22_focal_samples.append(float(max(int(width), int(height))))
            if current_world_head11 is not None:
                self.center_world_head11_samples.append(current_world_head11)
            snap = self._snapshot_pnp_pose(estimate_pose)
            if snap is not None:
                self.center_pnp_pose_samples.append((snap, int(width), int(height)))
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
        yaw_gain = PERSONAL_PNP_YAW_GAIN if (self.config["algorithm"] == "pnp" and self.personal_pnp_active) else 1.0
        raw_y = _clamp((estimate.pitch - self.center_pitch) / span_y, -1.0, 1.0)

        # v154 stage source selection.  A validated personal PnP keeps v153
        # exactly.  Otherwise, when the real-video-calibrated shape signal has
        # a sufficiently clean personal neutral estimate, use it for horizontal
        # motion.  This targets the real failure mode where sparse PnP drifts
        # while the player is visually still.
        self.real_shape_active = bool(
            REAL_SHAPE_CONTROL_ENABLED
            and self.config["algorithm"] == "pnp"
            and not self.personal_pnp_active
            and math.isfinite(self.real_shape_yaw_median)
            and math.isfinite(self.real_shape_cal_sigma)
            and self.real_shape_cal_sigma <= REAL_SHAPE_MAX_CAL_SIGMA_DEG
            and len(self._real_shape_history) >= min(3, REAL_SHAPE_MEDIAN_WINDOW)
        )
        if self.real_shape_active:
            source_raw_yaw = float(self.real_shape_yaw)
            source_filtered_yaw = float(self.real_shape_yaw_median)
            raw_x = _clamp(source_raw_yaw / PNP_YAW_SPAN_DEG, -1.0, 1.0)
        else:
            source_raw_yaw = estimate.yaw
            source_filtered_yaw = self._yaw_filter.apply(estimate.yaw, now)
            raw_x = _clamp(yaw_gain * (estimate.yaw - self.center_yaw) / span_x, -1.0, 1.0)

        release_y = self.effective_deadzone_y * DEADZONE_RELEASE_RATIO
        if abs(raw_y) <= release_y:
            self._pitch_filter.reset()
            filtered_pitch = self._pitch_filter.apply(self.center_pitch, now)
        else:
            filtered_pitch = self._pitch_filter.apply(estimate.pitch, now)

        # filtered_yaw remains a diagnostic in PnP degrees.  In real-shape mode
        # it is already relative to the calibrated center, so expose that value
        # directly while the controller norm uses its degree scale below.
        filtered_yaw = source_filtered_yaw
        self.filtered_yaw, self.filtered_pitch = filtered_yaw, filtered_pitch
        if self.real_shape_active:
            x = _clamp(source_filtered_yaw / PNP_YAW_SPAN_DEG, -1.0, 1.0)
        else:
            x = _clamp(yaw_gain * (source_filtered_yaw - self.center_yaw) / span_x, -1.0, 1.0)
        diagnostic_y = _clamp((filtered_pitch - self.center_pitch) / span_y, -1.0, 1.0)
        if self.config["invert_x"]:
            x = -x
        self.norm_x, self.norm_y = x, diagnostic_y

        start_x = _clamp(max(self.effective_deadzone_x * 0.58, 0.055), 0.045, 0.18)
        if self.personal_pnp_active and self.personal_pnp_far_depth_ratio > 1.0:
            # A farther face makes fixed pixel landmark noise correspond to a
            # larger angular PnP error.  Personal-model solvePnP already gives
            # a rotation-invariant Z translation, so use that instead of 2D
            # apparent face width (which shrinks during real yaw).  Only TURN
            # activation is made slightly stricter; gain/committed TURN remain.
            start_x = _clamp(
                start_x * (self.personal_pnp_far_depth_ratio ** PERSONAL_PNP_FAR_START_POWER),
                0.045, 0.18,
            )
        intent_raw_x = -raw_x if self.config["invert_x"] else raw_x
        vx = self._x_intent.update(
            x, now,
            raw_norm=intent_raw_x,
            start_angle=start_x,
            start_velocity=0.12,
            keep_velocity=0.045,
            return_velocity=0.060,
            stop_grace_s=0.075,
            acceleration_stop=1.15,
            curve_gamma=1.30,
        )

        # v156 real-data hardening.  World rigid yaw is still contradiction-
        # only: it never controls Mouse-X amplitude and is never required to
        # prove a real turn.  v156 preserves a short contradiction memory
        # through brief zero-output gaps.  This closes the real A~10s leak
        # where v59c briefly emitted zero and v155 forgot two already-observed
        # opposite-world frames.
        self.real_shape_world_veto_active = False
        gate_dt = (
            _clamp(now - self._real_shape_world_last_t, 1.0 / 240.0, 0.10)
            if self._real_shape_world_last_t else 1.0 / 30.0
        )
        self._real_shape_world_last_t = now
        self._real_shape_world_context_age_s += gate_dt
        self._real_shape_world_veto_age_s += gate_dt
        if self.real_shape_active:
            world_reliable = (
                math.isfinite(self.current_world_rigid_yaw)
                and math.isfinite(self.noise_world_rigid_yaw)
                and self.noise_world_rigid_yaw <= PERSONAL_PNP_MAX_WORLD_RIGID_SIGMA_DEG
            )
            # Direction context comes from an actual candidate first, then the
            # v59c TURN state.  If both temporarily disappear, remember the
            # last direction for at most 200 ms; memory-only frames do NOT
            # refresh that timer.
            direction = 0
            fresh_direction = False
            if abs(vx) > 1e-12:
                direction = 1 if vx > 0.0 else -1
                fresh_direction = True
            elif self._x_intent.state == "TURN_RIGHT":
                direction = 1
                fresh_direction = True
            elif self._x_intent.state == "TURN_LEFT":
                direction = -1
                fresh_direction = True
            elif (
                self._real_shape_world_oppose_direction
                and self._real_shape_world_context_age_s <= REAL_SHAPE_WORLD_CONTEXT_S
            ):
                direction = self._real_shape_world_oppose_direction

            if direction and direction != self._real_shape_world_oppose_direction:
                self._real_shape_world_oppose_direction = direction
                self._real_shape_world_oppose_count = 0
                self._real_shape_world_context_age_s = 0.0
                self._real_shape_world_veto_latched = False
                self._real_shape_world_veto_age_s = math.inf
            elif direction and fresh_direction:
                self._real_shape_world_context_age_s = 0.0

            if world_reliable and direction:
                world_yaw = float(self.current_world_rigid_yaw)
                if self.config.get("invert_x"):
                    world_yaw = -world_yaw
                projected_world = direction * world_yaw
                if projected_world <= -REAL_SHAPE_WORLD_OPPOSE_DEG:
                    self._real_shape_world_oppose_count += 1
                    self._real_shape_world_veto_age_s = 0.0
                    if self._real_shape_world_oppose_count >= REAL_SHAPE_WORLD_OPPOSE_FRAMES:
                        self._real_shape_world_veto_latched = True
                elif projected_world >= REAL_SHAPE_WORLD_SAME_RELEASE_DEG:
                    # Explicit same-direction world motion immediately clears
                    # the contradiction.  This is the fast path for a genuine
                    # turn after an earlier false-shape episode.
                    self._real_shape_world_oppose_count = 0
                    self._real_shape_world_veto_latched = False
                    self._real_shape_world_veto_age_s = math.inf
                else:
                    # Near-neutral/weak world does not erase all history in a
                    # single frame; decay one vote instead.
                    self._real_shape_world_oppose_count = max(
                        0, self._real_shape_world_oppose_count - REAL_SHAPE_WORLD_WEAK_DECAY
                    )

                if self._real_shape_world_veto_latched:
                    if (
                        self._real_shape_world_veto_age_s <= REAL_SHAPE_WORLD_VETO_HOLD_S
                        or projected_world <= -REAL_SHAPE_WORLD_OPPOSE_DEG
                    ):
                        if abs(vx) > 1e-12:
                            vx = 0.0
                            self.real_shape_world_veto_active = True
                    else:
                        self._real_shape_world_veto_latched = False
                        self._real_shape_world_oppose_count = 0
            else:
                # Missing/unreliable world must never become a runtime
                # dependency.  Disable the optional contradiction veto.
                self._real_shape_world_oppose_count = 0
                self._real_shape_world_veto_latched = False
                self._real_shape_world_veto_age_s = math.inf

            if self._real_shape_world_context_age_s > REAL_SHAPE_WORLD_CONTEXT_S:
                self._real_shape_world_oppose_count = 0
                self._real_shape_world_oppose_direction = 0
                self._real_shape_world_veto_latched = False
                self._real_shape_world_veto_age_s = math.inf
        else:
            self._real_shape_world_oppose_count = 0
            self._real_shape_world_oppose_direction = 0
            self._real_shape_world_context_age_s = math.inf
            self._real_shape_world_veto_latched = False
            self._real_shape_world_veto_age_s = math.inf

        # v157 real-data stability hardening.  Do not expose the very first
        # weak tentative frame to Mouse-X.  Internal v59c evidence/state is not
        # reset or delayed, so a genuine slow turn proceeds normally from the
        # next frame; strong first-frame motion bypasses this guard entirely.
        self.real_shape_tentative_quiet_active = False
        self.real_shape_tentative_preview_active = False
        self.real_shape_tentative_soft_opposition_active = False
        if (
            self.real_shape_active
            and abs(vx) > 1e-12
            and not bool(getattr(self._x_intent, "committed", False))
        ):
            tentative_age = float(getattr(self._x_intent, "tentative_age_s", 0.0))
            tentative_velocity = abs(float(getattr(self._x_intent, "velocity", 0.0)))
            if tentative_age <= 1e-6 and tentative_velocity < REAL_SHAPE_TENTATIVE_QUIET_VELOCITY:
                vx = 0.0
                self.real_shape_tentative_quiet_active = True
            else:
                preview_scale = min(
                    REAL_SHAPE_TENTATIVE_PREVIEW_MAX_SCALE,
                    REAL_SHAPE_TENTATIVE_PREVIEW_MIN_SCALE
                    + REAL_SHAPE_TENTATIVE_PREVIEW_SLOPE_PER_S * max(0.0, tentative_age),
                )
                vx *= preview_scale
                if abs(vx) > REAL_SHAPE_TENTATIVE_PREVIEW_CAP:
                    vx = math.copysign(REAL_SHAPE_TENTATIVE_PREVIEW_CAP, vx)

                # Soft contradiction: one independent world-rigid frame in the
                # opposite direction is not enough to reject the TURN, but it
                # is enough to make an *uncommitted* user-visible preview much
                # lighter.  This does not alter v59c state/evidence.
                world_reliable = (
                    math.isfinite(self.current_world_rigid_yaw)
                    and math.isfinite(self.noise_world_rigid_yaw)
                    and self.noise_world_rigid_yaw <= PERSONAL_PNP_MAX_WORLD_RIGID_SIGMA_DEG
                )
                if world_reliable and abs(vx) > 1e-12:
                    preview_direction = 1.0 if vx > 0.0 else -1.0
                    preview_world_yaw = float(self.current_world_rigid_yaw)
                    if self.config.get("invert_x"):
                        preview_world_yaw = -preview_world_yaw
                    if preview_direction * preview_world_yaw <= -REAL_SHAPE_TENTATIVE_SOFT_OPPOSE_DEG:
                        vx *= REAL_SHAPE_TENTATIVE_SOFT_OPPOSE_SCALE
                        self.real_shape_tentative_soft_opposition_active = True
                self.real_shape_tentative_preview_active = True

        if (self.personal_pnp_active or self.real_shape_active) and self.config["algorithm"] == "pnp":
            # Personal PnP and the real-video shape source are both source-level
            # horizontal estimators.  Do not stack the old PITCH_LOCK on them;
            # its proxy evidence is derived from the same 2D geometry and would
            # double-count the stage signal.
            self._cross_axis_lock.reset()
        else:
            vx = self._cross_axis_lock.apply(
                vx,
                pnp_yaw=filtered_yaw,
                pitch_delta_deg=estimate.pitch - self.center_pitch,
                proxy_values=self.current_multi2d_proxy,
                proxy_center=self.center_multi2d_proxy,
                proxy_sigma=self.noise_multi2d_proxy,
                signal_sign=(-1.0 if self.config["invert_x"] else 1.0),
                world_rigid_yaw=self.current_world_rigid_yaw,
                world_rigid_sigma=self.noise_world_rigid_yaw,
                now=now,
            )
        target_x = vx * float(self.config["sensitivity_x"])
        dt = _clamp(now - self._last_update, 0.0, 0.08) if self._last_update else 1.0 / 30.0
        self._last_update = now
        # Preserve the exact pre-gate v160/v186 slew as an internal baseline.
        # Confidence scaling is an outer safety layer and never feeds back into
        # TURN state, RETURNING, gain, or future slew calculations.
        self._base_output_x = self._slew(self._base_output_x, target_x, dt)
        gate_scale = self._frozen22_uncertainty_scale(self._base_output_x, now)
        self.output_x = self._base_output_x * gate_scale
        # Mouse Y is intentionally produced only through vertical_gate_output().
        # This keeps accidental pitch motion harmless unless the left hand has
        # explicitly opened the vertical look gate.
        if not self.vertical_gate_active:
            self.output_y = 0.0
        self._stage_a_record_frame(
            now=now, width=width, height=height, current_world_head11=current_world_head11
        )
        return self.output_x / 100.0, 0.0

    def begin_vertical_gate(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.vertical_gate_active = True
        self.vertical_temp_center_pitch = None
        self.vertical_filtered_pitch = math.nan
        self.vertical_norm = 0.0
        self.output_y = 0.0
        self._vertical_pitch_filter.reset()
        self._vertical_intent.reset(0.0, now)
        self._capture_vertical_center_if_possible(now)

    def _capture_vertical_center_if_possible(self, now: float) -> bool:
        if self.vertical_temp_center_pitch is not None:
            return True
        if not (self.raw.valid and math.isfinite(self.raw.pitch)):
            return False
        # Capture the actual current pitch at gate entry, not the global
        # calibration center. This makes each vertical-control gesture relative
        # to the player's natural head pose at the moment they authorize it.
        self.vertical_temp_center_pitch = float(self.raw.pitch)
        self._vertical_pitch_filter.reset()
        self.vertical_filtered_pitch = self._vertical_pitch_filter.apply(self.vertical_temp_center_pitch, now)
        self._vertical_intent.reset(0.0, now)
        return True

    def end_vertical_gate(self) -> None:
        self.vertical_gate_active = False
        self.vertical_temp_center_pitch = None
        self.vertical_filtered_pitch = math.nan
        self.vertical_norm = 0.0
        self.output_y = 0.0
        self._vertical_pitch_filter.reset()
        self._vertical_intent.reset()

    def vertical_gate_output(self, active: bool, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        if (
            not active
            or self.config.get("vertical_source") != "head"
            or not self.config.get("enabled")
            or not self.calibrated
        ):
            if self.vertical_gate_active:
                self.end_vertical_gate()
            return 0.0
        if not self.vertical_gate_active:
            self.begin_vertical_gate(now)
            return 0.0
        if not self._capture_vertical_center_if_possible(now):
            return 0.0
        if not (self.raw.valid and math.isfinite(self.raw.pitch)):
            self.output_y = 0.0
            self.vertical_norm = 0.0
            self._vertical_intent.reset(0.0, now)
            return 0.0

        _, span_y = self._span()
        filtered = self._vertical_pitch_filter.apply(float(self.raw.pitch), now)
        self.vertical_filtered_pitch = filtered
        norm = _clamp((filtered - float(self.vertical_temp_center_pitch)) / span_y, -1.0, 1.0)
        if self.config.get("invert_y"):
            norm = -norm
        self.vertical_norm = norm

        # Pitch is deliberately easier to activate than yaw. The left-hand gate
        # already provides the strong false-trigger barrier, so vertical control
        # can use a smaller angle/velocity threshold and a little more gain.
        start_y = _clamp(max(0.035, self.effective_deadzone_y * 0.32), 0.025, 0.10)
        raw_norm = _clamp((float(self.raw.pitch) - float(self.vertical_temp_center_pitch)) / span_y, -1.0, 1.0)
        if self.config.get("invert_y"):
            raw_norm = -raw_norm
        vy = self._vertical_intent.update(
            norm, now,
            raw_norm=raw_norm,
            start_angle=start_y,
            start_velocity=0.070,
            keep_velocity=0.026,
            return_velocity=0.035,
            stop_grace_s=0.060,
            acceleration_stop=0.80,
            curve_gamma=1.18,
        )
        self.output_y = _clamp(vy * float(self.config["sensitivity_y"]) * 1.60, -100.0, 100.0)
        return self.output_y / 100.0

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
            "horizontal_algorithm_version": HEAD_HORIZONTAL_ALGORITHM_VERSION,
            "personal22_probe_version": PERSONAL22_PROBE_VERSION,
            "personal22_controls_mouse": False,
            "stage_a_probe_version": STAGE_A_PROBE_VERSION,
            "stage_a_recording": bool(self._stage_a_log_handle is not None),
            "stage_a_log_path": (str(self._stage_a_log_path) if self._stage_a_log_path is not None else None),
            "stage_a_label_path": (str(self._stage_a_label_path) if self._stage_a_label_path is not None else None),
            "stage_a_frame_count": int(self._stage_a_log_count),
            "stage_a_label": str(self._stage_a_label),
            "frozen22_signature_version": FROZEN22_SIGNATURE_VERSION,
            "frozen22_controls_mouse": True,
            "frozen22_calibration_valid": bool(self.frozen22_calibration_valid),
            "frozen22_yaw_deg": (round(float(self.frozen22_yaw), 5) if math.isfinite(self.frozen22_yaw) else None),
            "frozen22_yaw_median_deg": (round(float(self.frozen22_yaw_median), 5) if math.isfinite(self.frozen22_yaw_median) else None),
            "frozen22_cal_sigma_deg": (round(float(self.frozen22_cal_sigma_deg), 5) if math.isfinite(self.frozen22_cal_sigma_deg) else None),
            "frozen22_world_resid_rel": (round(float(self.frozen22_world_resid_rel), 5) if math.isfinite(self.frozen22_world_resid_rel) else None),
            "frozen22_gate_version": FROZEN22_GATE_VERSION,
            "frozen22_gate_source": self.frozen22_gate_source,
            "frozen22_gate_scale": round(float(self.frozen22_gate_scale), 4),
            "frozen22_gate_fast_delta_deg": (round(float(self.frozen22_gate_fast_delta_deg), 5) if math.isfinite(self.frozen22_gate_fast_delta_deg) else None),
            "frozen22_gate_slow_delta_deg": (round(float(self.frozen22_gate_slow_delta_deg), 5) if math.isfinite(self.frozen22_gate_slow_delta_deg) else None),
            "frozen22_gate_slow_world_delta_deg": (round(float(self.frozen22_gate_slow_world_delta_deg), 5) if math.isfinite(self.frozen22_gate_slow_world_delta_deg) else None),
            "frozen22_gate_pitch_delta_deg": (round(float(self.frozen22_gate_pitch_delta_deg), 5) if math.isfinite(self.frozen22_gate_pitch_delta_deg) else None),
            "frozen22_gate_fast_threshold_deg": (round(float(self.frozen22_gate_fast_threshold_deg), 5) if math.isfinite(self.frozen22_gate_fast_threshold_deg) else None),
            "frozen22_gate_slow_threshold_deg": (round(float(self.frozen22_gate_slow_threshold_deg), 5) if math.isfinite(self.frozen22_gate_slow_threshold_deg) else None),
            "pre_gate_output_x": round(float(self._base_output_x), 3),
            "personal22_valid": bool(self.personal22_valid),
            "personal22_yaw_deg": (round(float(self.personal22_yaw), 5) if math.isfinite(self.personal22_yaw) else None),
            "personal22_yaw_median_deg": (round(float(self.personal22_yaw_median), 5) if math.isfinite(self.personal22_yaw_median) else None),
            "personal22_cal_sigma_deg": (round(float(self.personal22_cal_sigma_deg), 5) if math.isfinite(self.personal22_cal_sigma_deg) else None),
            "personal22_world_resid_rel": (round(float(self.personal22_world_resid_rel), 5) if math.isfinite(self.personal22_world_resid_rel) else None),
            "personal22_world_resid_rel_limit": PERSONAL22_WORLD_RESID_REL_MAX,
            "personal22_z_est": (round(float(self.personal22_z_est), 6) if math.isfinite(self.personal22_z_est) else None),
            "personal22_eye_world": (round(float(self.personal22_eye_world), 6) if math.isfinite(self.personal22_eye_world) else None),
            "personal22_cal_samples_2d": len(self.center_personal22_feature_samples) if self.calibrating else None,
            "personal22_cal_samples_world": len(self.center_world_head11_samples) if self.calibrating else None,
            "personal_pnp_active": bool(self.personal_pnp_active),
            "real_shape_stage_enabled": bool(REAL_SHAPE_CONTROL_ENABLED),
            "real_shape_active": bool(self.real_shape_active),
            "real_shape_yaw_deg": (round(float(self.real_shape_yaw), 5) if math.isfinite(self.real_shape_yaw) else None),
            "real_shape_yaw_median_deg": (round(float(self.real_shape_yaw_median), 5) if math.isfinite(self.real_shape_yaw_median) else None),
            "real_shape_cal_sigma_deg": (round(float(self.real_shape_cal_sigma), 5) if math.isfinite(self.real_shape_cal_sigma) else None),
            "real_shape_median_window": int(REAL_SHAPE_MEDIAN_WINDOW),
            "real_shape_world_veto_active": bool(self.real_shape_world_veto_active),
            "real_shape_tentative_quiet_active": bool(self.real_shape_tentative_quiet_active),
            "real_shape_tentative_quiet_velocity": REAL_SHAPE_TENTATIVE_QUIET_VELOCITY,
            "real_shape_tentative_preview_active": bool(self.real_shape_tentative_preview_active),
            "real_shape_tentative_soft_opposition_active": bool(self.real_shape_tentative_soft_opposition_active),
            "real_shape_tentative_preview_min_scale": REAL_SHAPE_TENTATIVE_PREVIEW_MIN_SCALE,
            "real_shape_tentative_preview_slope_per_s": REAL_SHAPE_TENTATIVE_PREVIEW_SLOPE_PER_S,
            "real_shape_tentative_preview_max_scale": REAL_SHAPE_TENTATIVE_PREVIEW_MAX_SCALE,
            "real_shape_tentative_preview_cap": REAL_SHAPE_TENTATIVE_PREVIEW_CAP,
            "real_shape_tentative_soft_oppose_deg": REAL_SHAPE_TENTATIVE_SOFT_OPPOSE_DEG,
            "real_shape_tentative_soft_oppose_scale": REAL_SHAPE_TENTATIVE_SOFT_OPPOSE_SCALE,
            "real_shape_world_oppose_count": int(self._real_shape_world_oppose_count),
            "real_shape_world_oppose_deg": REAL_SHAPE_WORLD_OPPOSE_DEG,
            "real_shape_world_oppose_frames": REAL_SHAPE_WORLD_OPPOSE_FRAMES,
            "real_shape_world_context_s": REAL_SHAPE_WORLD_CONTEXT_S,
            "real_shape_world_veto_hold_s": REAL_SHAPE_WORLD_VETO_HOLD_S,
            "real_shape_world_veto_latched": bool(self._real_shape_world_veto_latched),
            "real_shape_world_context_age_s": (
                round(float(self._real_shape_world_context_age_s), 4)
                if math.isfinite(self._real_shape_world_context_age_s) else None
            ),
            "personal_pnp_yaw_gain": PERSONAL_PNP_YAW_GAIN if self.personal_pnp_active else 1.0,
            "personal_pnp_calibration_derotate_yaw": round(float(self.personal_pnp_calibration_derotate_yaw), 3),
            "personal_pnp_calibration_derotate_roll": round(float(self.personal_pnp_calibration_derotate_roll), 3),
            "personal_pnp_world_pair_yaw_deviation": (round(float(self.personal_pnp_world_pair_yaw_deviation), 3) if math.isfinite(self.personal_pnp_world_pair_yaw_deviation) else None),
            "personal_pnp_world_pair_yaws": ([round(float(v), 3) for v in self.personal_pnp_world_pair_yaws] if self.personal_pnp_world_pair_yaws is not None else None),
            "personal_pnp_world_pair_yaw_limit": PERSONAL_PNP_MAX_WORLD_PAIR_YAW_DEVIATION_DEG,
            "personal_pnp_valid_samples": int(self.personal_pnp_valid_samples),
            "personal_pnp_world_sigma_limit_deg": PERSONAL_PNP_MAX_WORLD_RIGID_SIGMA_DEG,
            "personal_pnp_reprojection_limit": PERSONAL_PNP_MAX_MEDIAN_REPROJECTION,
            "personal_pnp_median_reprojection": (
                round(float(self.personal_pnp_median_reprojection), 6)
                if math.isfinite(self.personal_pnp_median_reprojection) else None
            ),
            "personal_pnp_center_depth": (round(float(self.personal_pnp_center_depth), 3) if math.isfinite(self.personal_pnp_center_depth) else None),
            "personal_pnp_current_depth": (round(float(self.personal_pnp_current_depth), 3) if math.isfinite(self.personal_pnp_current_depth) else None),
            "personal_pnp_far_depth_ratio": round(float(self.personal_pnp_far_depth_ratio), 4),
            "personal_pnp_far_start_power": PERSONAL_PNP_FAR_START_POWER,
            "cross_axis_mode": ("PERSONAL_PNP_SOURCE_FIX" if self.personal_pnp_active else ("REALDATA_SHAPE_V160" if self.real_shape_active else "V129_FALLBACK")),
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
            "vertical_source": str(self.config.get("vertical_source", "right_wrist")),
            "intent_x_state": self._x_intent.state,
            "yaw_velocity_norm_s": round(self._x_intent.velocity, 4),
            "yaw_acceleration_norm_s2": round(self._x_intent.acceleration, 4),
            "yaw_relative_drive": round(float(self._x_intent.output), 4),
            "yaw_motion_evidence": round(float(getattr(self._x_intent, "motion_evidence", 0.0)), 5),
            "yaw_return_evidence": round(float(getattr(self._x_intent, "return_evidence", 0.0)), 5),
            "yaw_relative_center_zone": round(float(getattr(self._x_intent, "center_zone", 0.0)), 5),
            "yaw_return_latched": bool(getattr(self._x_intent, "return_latched", False)),
            "yaw_committed": bool(getattr(self._x_intent, "committed", False)),
            "yaw_tentative_age_s": round(float(getattr(self._x_intent, "tentative_age_s", 0.0)), 4),
            "pitch_lock_active": bool(self._cross_axis_lock.locked),
            "pitch_lock_yaw_released": bool(self._cross_axis_lock.yaw_released),
            "pitch_lock_confirm_s": round(float(self._cross_axis_lock.confirm_s), 4),
            "cross_axis_lock_mode": str(getattr(self._cross_axis_lock, "last_mode", "")),
            "cross_axis_local_ready": bool(getattr(self._cross_axis_lock, "local_ready", False)),
            "cross_axis_lease_s": round(float(getattr(self._cross_axis_lock, "lease_s", 0.0)), 4),
            "cross_axis_global_votes": int(getattr(self._cross_axis_lock, "global_votes", 0)),
            "cross_axis_local_votes": int(getattr(self._cross_axis_lock, "local_votes", 0)),
            "cross_axis_pnp_velocity_deg_s": round(float(getattr(self._cross_axis_lock, "pnp_velocity_deg_s", 0.0)), 3),
            "cross_axis_pnp_local_deg": (round(float(self._cross_axis_lock.pnp_local_deg), 4) if math.isfinite(getattr(self._cross_axis_lock, "pnp_local_deg", math.nan)) else None),
            "cross_axis_pnp_net_300ms_deg": (round(float(self._cross_axis_lock.pnp_net_300ms_deg), 4) if math.isfinite(getattr(self._cross_axis_lock, "pnp_net_300ms_deg", math.nan)) else None),
            "cross_axis_local_ratchet_remaining_s": round(max(0.0, float(getattr(self._cross_axis_lock, "local_ratchet_until", 0.0)) - now), 4),
            "cross_axis_pitch_velocity_deg_s": round(float(getattr(self._cross_axis_lock, "pitch_velocity_deg_s", 0.0)), 3),
            "multi2d_calibrated": bool(self.center_multi2d_proxy is not None and self.noise_multi2d_proxy is not None),
            "multi2d_noise_deg": [
                round(float(v), 4) if math.isfinite(v) else None
                for v in getattr(self._cross_axis_lock, "noise_deg", [])
            ],
            "world_rigid_yaw": (round(float(self.current_world_rigid_yaw), 5) if math.isfinite(self.current_world_rigid_yaw) else None),
            "world_rigid_sigma": (round(float(self.noise_world_rigid_yaw), 5) if math.isfinite(self.noise_world_rigid_yaw) else None),
            "world_rigid_fit": (round(float(self.current_world_rigid_fit), 5) if math.isfinite(self.current_world_rigid_fit) else None),
            "world_rigid_local_deg": (round(float(self._cross_axis_lock.world_local_deg), 5) if math.isfinite(getattr(self._cross_axis_lock, "world_local_deg", math.nan)) else None),
            "world_rigid_net_200ms_deg": (round(float(self._cross_axis_lock.world_net_200ms_deg), 5) if math.isfinite(getattr(self._cross_axis_lock, "world_net_200ms_deg", math.nan)) else None),
            "world_rigid_net_300ms_deg": (round(float(self._cross_axis_lock.world_net_300ms_deg), 5) if math.isfinite(getattr(self._cross_axis_lock, "world_net_300ms_deg", math.nan)) else None),
            "world_yaw_proxy": (
                round(float(self.current_world_yaw), 5)
                if math.isfinite(self.current_world_yaw) else None
            ),
            "world_yaw_center": (
                round(float(self.center_world_yaw), 5)
                if math.isfinite(self.center_world_yaw) else None
            ),
            "world_yaw_sigma": (
                round(float(self.noise_world_yaw), 5)
                if math.isfinite(self.noise_world_yaw) else None
            ),
            "world_yaw_reliable": bool(
                math.isfinite(self.center_world_yaw)
                and math.isfinite(self.noise_world_yaw)
                and self.noise_world_yaw <= PITCH_LOCK_WORLD_SIGMA_MAX_DEG
            ),
            "normalized_z_yaw_proxy": (
                round(float(self.current_norm_z_yaw), 5)
                if math.isfinite(self.current_norm_z_yaw) else None
            ),
            "normalized_z_yaw_center": (
                round(float(self.center_norm_z_yaw), 5)
                if math.isfinite(self.center_norm_z_yaw) else None
            ),
            "normalized_z_yaw_sigma": (
                round(float(self.noise_norm_z_yaw), 5)
                if math.isfinite(self.noise_norm_z_yaw) else None
            ),
            "head_point_smoothing_tau_s": round(float(HEAD_POINT_EMA_TAU_S), 4),
            "vertical_intent_state": self._vertical_intent.state,
            "pitch_velocity_norm_s": round(self._vertical_intent.velocity, 4),
            "pitch_acceleration_norm_s2": round(self._vertical_intent.acceleration, 4),
            "vertical_gate_active": bool(self.vertical_gate_active),
            "vertical_temp_center_pitch": (
                round(float(self.vertical_temp_center_pitch), 5)
                if self.vertical_temp_center_pitch is not None else None
            ),
            "vertical_filtered_pitch": (
                round(float(self.vertical_filtered_pitch), 5)
                if math.isfinite(self.vertical_filtered_pitch) else None
            ),
            "vertical_head_norm": round(float(self.vertical_norm), 4),
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
            if payload.get("signal_version") not in HEAD_PROFILE_COMPAT_VERSIONS:
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
                "vertical_source": (
                    str(params.get("vertical_source", DEFAULT_CONFIG["vertical_source"]))
                    if str(params.get("vertical_source", DEFAULT_CONFIG["vertical_source"])) in {"right_wrist", "head"}
                    else DEFAULT_CONFIG["vertical_source"]
                ),
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
            self.center_world_yaw = math.nan
            self.noise_world_yaw = math.inf
            self.center_norm_z_yaw = math.nan
            self.noise_norm_z_yaw = math.inf
            self.center_multi2d_proxy = None
            self.noise_multi2d_proxy = None
            self.center_multi2d_proxy_samples = []
            self.center_world_face_samples = []
            self.center_world_face_template = None
            self.center_pnp_pose_samples = []
            self.personal_pnp_active = False
            self.personal_pnp_valid_samples = 0
            self.personal_pnp_median_reprojection = math.nan
            self.personal_pnp_center_depth = math.nan
            self.personal_pnp_current_depth = math.nan
            self.personal_pnp_far_depth_ratio = 1.0
            self._calibration_restore_model = None
            self._calibration_restore_personal_active = False
            self._calibration_restore_depth = math.nan
            self.estimator.set_pnp_model(None)
            self.current_world_rigid_yaw = math.nan
            self.current_world_rigid_fit = math.nan
            self.center_world_rigid_yaw = math.nan
            self.noise_world_rigid_yaw = math.inf
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
                "personal_pnp_active": bool(self.personal_pnp_active),
                "personal_pnp_yaw_gain": PERSONAL_PNP_YAW_GAIN if self.personal_pnp_active else 1.0,
                "personal_pnp_valid_samples": int(self.personal_pnp_valid_samples),
                "personal_pnp_median_reprojection": (self.personal_pnp_median_reprojection if math.isfinite(self.personal_pnp_median_reprojection) else None),
                "world_yaw": self.center_world_yaw if math.isfinite(self.center_world_yaw) else None,
                "world_yaw_sigma": self.noise_world_yaw if math.isfinite(self.noise_world_yaw) else None,
                "normalized_z_yaw": self.center_norm_z_yaw if math.isfinite(self.center_norm_z_yaw) else None,
                "normalized_z_yaw_sigma": (
                    self.noise_norm_z_yaw if math.isfinite(self.noise_norm_z_yaw) else None
                ),
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
