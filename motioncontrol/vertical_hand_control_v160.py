"""MotionControl body-relative right-hand vertical-look controller, v19.

v6 keeps v5's low-latency position semantics and adds two protections that
matter in real camera use:

1. camera/body image-scale compensation: neutral wrist/shoulder geometry is
   corrected when the user moves nearer/farther from the camera.  Torso height
   and shoulder width are used as independent scale witnesses when available;
2. short-loss continuity: a brief wrist-confidence dropout still outputs zero
   while tracking is bad, but a consistent reacquisition can resume from a
   conservative fraction of the pre-loss state instead of ramping from zero.

Long tracking loss remains fail-safe and requires return to neutral before
control is re-armed.  The public update/reset/status contract stays compatible
with earlier VerticalHandController versions.
"""
from __future__ import annotations

import math
import statistics
from collections import deque
from typing import Any


def _clamp(value: Any, low: float, high: float) -> float:
    return max(float(low), min(float(high), float(value)))


def _score(point: dict[str, Any] | None) -> float:
    if not point:
        return 0.0
    value = point.get("score", point.get("visibility", point.get("presence", 0.0)))
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _finite(point: dict[str, Any] | None, key: str) -> bool:
    if not point:
        return False
    try:
        return math.isfinite(float(point.get(key, math.nan)))
    except (TypeError, ValueError):
        return False


def _finite_y(point: dict[str, Any] | None) -> bool:
    return _finite(point, "y")


def _mad(values: list[float]) -> float:
    if not values:
        return 0.0
    median = statistics.median(values)
    return float(statistics.median(abs(value - median) for value in values))


def _distance(a: dict[str, Any] | None, b: dict[str, Any] | None) -> float | None:
    if not a or not b or _score(a) < 0.48 or _score(b) < 0.48:
        return None
    if not (_finite(a, "x") and _finite(a, "y") and _finite(b, "x") and _finite(b, "y")):
        return None
    return math.hypot(float(a["x"]) - float(b["x"]), float(a["y"]) - float(b["y"]))


class VerticalHandController:
    VERSION = "vertical-hand-v160-context-free-strong-three-witness-fastpath-fixed"
    _MIN_SCORE = 0.48
    _TRAVEL_SCALE = 0.72
    _NOMINAL_TORSO_Y = 0.22

    _ANCHOR_WINDOW = 8
    _ANCHOR_MIN_SAMPLES = 6
    _ANCHOR_MIN_SECONDS = 0.40

    _FILTER_TAU_ATTACK = 0.028
    _FILTER_TAU_RELEASE = 0.034
    _MAX_STEP_PER_SECOND = 8.5

    _LONG_LOSS_SECONDS = 0.28
    _REACQUIRE_GOOD_FRAMES = 1
    _SHORT_RESEED_FRACTION = 0.58

    # Near-threshold motion must persist, while decisive motion remains immediate.
    _ENTER_CONFIRM_FRAMES = 3
    _STRONG_ENTER_MULT = 1.38
    # Wrist-only jumps that violate the local arm geometry are treated as pose outliers.
    _KINEMATIC_MIN_SCORE = 0.55

    # Slow-motion intent is the remaining safety-sensitive entry path.  Do not
    # let one long-window coincidence open the gate: accumulate evidence in
    # seconds so the behavior is FPS-independent.
    _LONG_ARM_CONFIRM_SECONDS = 0.030
    _LONG_ARM_GAP_GRACE_SECONDS = 0.055
    _SCALE_VETO_HOLD_SECONDS = 0.12
    # A one- or two-frame confidence dropout must not erase the 0.6 s slow-arm
    # history. Output is still fail-safe zero while tracking is bad; only the
    # evidence memory survives briefly.
    _SLOW_HISTORY_LOSS_GRACE_SECONDS = 0.11
    _HISTORY_BRIDGE_SECONDS = 0.18
    _HISTORY_BRIDGE_GOOD_FRAMES = 5
    _VERIFIED_ACTIVE_RELEASE_GRACE_SECONDS = 0.22

    def __init__(self) -> None:
        self.anchor_y: float | None = None
        self.anchor_rel_y: float | None = None
        self.anchor_samples: deque[float] = deque(maxlen=self._ANCHOR_WINDOW)
        self.anchor_times: deque[float] = deque(maxlen=self._ANCHOR_WINDOW)
        self.anchor_started_at: float | None = None
        self.anchor_torso_samples: deque[float] = deque(maxlen=self._ANCHOR_WINDOW)
        self.anchor_shoulder_width_samples: deque[float] = deque(maxlen=self._ANCHOR_WINDOW)
        self.anchor_hip_width_samples: deque[float] = deque(maxlen=self._ANCHOR_WINDOW)
        self.anchor_torso_y: float | None = None
        self.anchor_shoulder_width: float | None = None
        self.anchor_hip_width: float | None = None

        self.filtered = 0.0
        self.filter_last_at = 0.0
        self.noise_sigma = 0.0015
        self.active = False

        self.torso_scale = 1.0
        self.camera_scale_ratio = 1.0
        self.scale_measurements: deque[float] = deque(maxlen=2)
        self.three_scale_consensus_streak = 0
        # v154: hip width is only a safety witness while the hand is still
        # neutral.  A short quarantine prevents a suspicious torso+shoulder
        # scale commit from creating hand motion, without letting hip geometry
        # interfere once genuine hand displacement is already established.
        self.hip_scale_history: deque[float] = deque(maxlen=3)
        self.anchor_hip_width_noise_rel = 1.0
        self.hip_scale_quarantine_until = 0.0
        self.last_rel_y: float | None = None
        self.trend_samples: deque[tuple[float, float]] = deque(maxlen=7)

        self.tracking_lost_at: float | None = None
        self.reacquire_good_frames = 0
        self.reacquire_rel_samples: deque[float] = deque(maxlen=3)
        self.await_neutral_after_loss = False
        self.neutral_rearm_samples: deque[float] = deque(maxlen=5)

        self.preloss_filtered = 0.0
        self.preloss_active = False
        self.preloss_corrected_rel: float | None = None

        self.intent_confirm_frames = 0
        self.motion_proof_streak = 0
        self.last_elbow_rel: float | None = None
        self.last_forearm_len: float | None = None
        self.kinematic_rejects = 0
        self.last_wrist_image_y: float | None = None
        self.last_elbow_image_y: float | None = None
        self.long_arm_samples: deque[tuple[float, float, float, float, float]] = deque(maxlen=36)
        self.neutral_residuals: deque[float] = deque(maxlen=40)
        self.long_arm_evidence_s = 0.0
        self.long_arm_evidence_sign = 0
        self.long_arm_evidence_last_at = 0.0
        self.scale_veto_until = 0.0
        self.scale_pressure_samples: deque[tuple[float, float, float]] = deque(maxlen=4)
        self.scale_context_until = 0.0
        self.long_arm_quality_streak = 0
        self.kinematic_reject_streak = 0
        self.kinematic_reject_sign = 0
        self.motion_intent_until = 0.0
        self.motion_intent_sign = 0
        self.current_wrist_body_step_norm = 0.0
        self.current_elbow_body_step_norm = 0.0
        self.ambiguous_acquisition_until = 0.0
        self.ambiguous_acquisition_sign = 0
        self.verified_active_until = 0.0
        self.verified_active_sign = 0
        self.release_grace_started_at: float | None = None
        self.slow_candidate_until = 0.0
        self.slow_candidate_sign = 0
        self.position_evidence_started_at: float | None = None
        self.position_evidence_sign = 0
        self.position_evidence_frames = 0
        self.position_evidence_start_abs_raw = 0.0
        # v64: keep only a non-outputting "far motion candidate" across a very
        # short pose-collapse interval.  It never grants control by itself.
        self.gap_candidate_until = 0.0
        self.gap_candidate_sign = 0
        self.gap_candidate_abs_raw = 0.0
        self.gap_candidate_bad_frames = 0
        self.recent_hq_until = 0.0
        self.recent_hq_sign = 0
        self.recent_hq_history: deque[tuple[float, int]] = deque(maxlen=16)
        self.prev_entry_raw: float | None = None
        self.history_bridge_until = 0.0
        self.history_bridge_good_frames = 0
        self.history_bridge_sign = 0
        self.last_enter_deadzone: float | None = None
        self.preloss_entry_raw: float | None = None
        self.preloss_entry_deadzone: float | None = None
        self.entry_raw_history: deque[tuple[float, float]] = deque(maxlen=24)

    def reset(self, now: float | None = None) -> None:
        self.__init__()
        self.filter_last_at = 0.0 if now is None else float(now)

    def _snapshot(self, output: float = 0.0, **extra: Any) -> dict[str, Any]:
        snap = {
            "output": float(output),
            "anchor_y": self.anchor_y,
            "anchor_rel_y": self.anchor_rel_y,
            "anchor_samples": len(self.anchor_samples),
            "filtered": float(self.filtered),
            "filter_last_at": float(self.filter_last_at),
            "noise_sigma": float(self.noise_sigma),
            "torso_scale": float(self.torso_scale),
            "camera_scale_ratio": float(self.camera_scale_ratio),
            "anchor_hip_width": self.anchor_hip_width,
            "anchor_hip_width_noise_rel": float(self.anchor_hip_width_noise_rel),
            "hip_scale_quarantine_until": float(self.hip_scale_quarantine_until),
            "active": bool(self.active),
            "await_neutral_after_loss": bool(self.await_neutral_after_loss),
            "intent_confirm_frames": int(self.intent_confirm_frames),
            "motion_proof_streak": int(self.motion_proof_streak),
            "kinematic_rejects": int(self.kinematic_rejects),
            "long_arm_evidence_s": float(self.long_arm_evidence_s),
            "scale_veto_until": float(self.scale_veto_until),
            "scale_context_until": float(self.scale_context_until),
            "long_arm_quality_streak": int(self.long_arm_quality_streak),
            "kinematic_reject_streak": int(self.kinematic_reject_streak),
            "motion_intent_until": float(self.motion_intent_until),
            "wrist_body_step_norm": float(self.current_wrist_body_step_norm),
            "elbow_body_step_norm": float(self.current_elbow_body_step_norm),
            "ambiguous_acquisition_until": float(self.ambiguous_acquisition_until),
            "ambiguous_acquisition_sign": int(self.ambiguous_acquisition_sign),
            "verified_active_until": float(self.verified_active_until),
            "verified_active_sign": int(self.verified_active_sign),
            "slow_candidate_until": float(self.slow_candidate_until),
            "slow_candidate_sign": int(self.slow_candidate_sign),
            "position_evidence_sign": int(self.position_evidence_sign),
            "position_evidence_frames": int(self.position_evidence_frames),
            "position_evidence_start_abs_raw": float(self.position_evidence_start_abs_raw),
            "gap_candidate_until": float(self.gap_candidate_until),
            "gap_candidate_sign": int(self.gap_candidate_sign),
            "gap_candidate_abs_raw": float(self.gap_candidate_abs_raw),
            "gap_candidate_bad_frames": int(self.gap_candidate_bad_frames),
            "recent_hq_until": float(self.recent_hq_until),
            "recent_hq_sign": int(self.recent_hq_sign),
            "recent_hq_prior_hits": int(sum(1 for t, _ in self.recent_hq_history if t <= self.filter_last_at and self.filter_last_at - t <= 0.40)),
            "history_bridge_until": float(self.history_bridge_until),
            "history_bridge_good_frames": int(self.history_bridge_good_frames),
            "history_bridge_sign": int(self.history_bridge_sign),
            "preloss_entry_raw": self.preloss_entry_raw,
            "preloss_entry_deadzone": self.preloss_entry_deadzone,
        }
        snap.update(extra)
        return snap

    def status(self) -> dict[str, Any]:
        return {"version": self.VERSION, **self._snapshot(self.filtered)}

    def _body_geometry(self, pose_map: dict[str, dict[str, Any]]) -> tuple[float | None, float | None, float | None]:
        rs = pose_map.get("right_shoulder")
        if _score(rs) < self._MIN_SCORE or not _finite_y(rs):
            return None, None, None

        ls = pose_map.get("left_shoulder")
        shoulders = [float(rs["y"])]
        if _score(ls) >= self._MIN_SCORE and _finite_y(ls):
            shoulders.append(float(ls["y"]))
        shoulder_y = float(statistics.fmean(shoulders))

        hips: list[float] = []
        for name in ("right_hip", "left_hip"):
            point = pose_map.get(name)
            if _score(point) >= self._MIN_SCORE and _finite_y(point):
                hips.append(float(point["y"]))
        torso_y = None
        if hips:
            candidate = abs(statistics.fmean(hips) - shoulder_y)
            if 0.07 <= candidate <= 0.45:
                torso_y = float(candidate)

        shoulder_width = _distance(ls, rs)
        if shoulder_width is not None and not (0.06 <= shoulder_width <= 0.55):
            shoulder_width = None
        return shoulder_y, torso_y, shoulder_width

    def _anchor_travel(self, cfg: dict[str, Any]) -> float:
        authored = max(0.065, float(cfg.get("range_y", 0.18)) * self._TRAVEL_SCALE)
        if not bool(cfg.get("body_scale_normalization", True)) or self.anchor_torso_y is None:
            return authored
        nominal = _clamp(cfg.get("nominal_torso_y", self._NOMINAL_TORSO_Y), 0.12, 0.34)
        return max(0.045, authored * _clamp(self.anchor_torso_y / nominal, 0.65, 1.55))

    def _collect_anchor_geometry(self, torso_y: float | None, shoulder_width: float | None, hip_width: float | None) -> None:
        if torso_y is not None:
            self.anchor_torso_samples.append(float(torso_y))
        if shoulder_width is not None:
            self.anchor_shoulder_width_samples.append(float(shoulder_width))
        if hip_width is not None:
            self.anchor_hip_width_samples.append(float(hip_width))

    def _update_anchor(self, rel_y: float, now: float, cfg: dict[str, Any], torso_y: float | None, shoulder_width: float | None, hip_width: float | None) -> bool:
        if self.anchor_started_at is None:
            self.anchor_started_at = now
        self.anchor_samples.append(rel_y)
        self.anchor_times.append(now)
        self._collect_anchor_geometry(torso_y, shoulder_width, hip_width)

        if len(self.anchor_samples) < self._ANCHOR_MIN_SAMPLES:
            return False
        if now - self.anchor_started_at < self._ANCHOR_MIN_SECONDS:
            return False

        vals = list(self.anchor_samples)
        k = max(2, len(vals) // 3)
        first = float(statistics.median(vals[:k]))
        last = float(statistics.median(vals[-k:]))
        center = float(statistics.median(vals[-self._ANCHOR_MIN_SAMPLES:]))
        sigma = max(0.0007, 1.4826 * _mad(vals))

        # Estimate initial travel from the currently observed body scale.  It is
        # only committed after the hand itself has passed the stationary test.
        if self.anchor_torso_samples:
            provisional_torso = float(statistics.median(self.anchor_torso_samples))
            nominal = _clamp(cfg.get("nominal_torso_y", self._NOMINAL_TORSO_Y), 0.12, 0.34)
            authored = max(0.065, float(cfg.get("range_y", 0.18)) * self._TRAVEL_SCALE)
            travel = authored * _clamp(provisional_torso / nominal, 0.65, 1.55)
        else:
            travel = max(0.065, float(cfg.get("range_y", 0.18)) * self._TRAVEL_SCALE)

        robust_shift = abs(last - first)
        max_shift = max(2.7 * sigma, 0.055 * travel)
        dispersion_ok = _mad(vals) <= max(0.0045, 0.060 * travel)
        if robust_shift > max_shift or not dispersion_ok:
            return False

        self.anchor_rel_y = center
        self.anchor_torso_y = float(statistics.median(self.anchor_torso_samples)) if self.anchor_torso_samples else None
        self.anchor_shoulder_width = float(statistics.median(self.anchor_shoulder_width_samples)) if self.anchor_shoulder_width_samples else None
        self.anchor_hip_width = float(statistics.median(self.anchor_hip_width_samples)) if self.anchor_hip_width_samples else None
        if self.anchor_hip_width and len(self.anchor_hip_width_samples) >= 4:
            self.anchor_hip_width_noise_rel = _clamp(
                1.4826 * _mad(list(self.anchor_hip_width_samples)) / max(1e-6, self.anchor_hip_width),
                0.0, 1.0,
            )
        else:
            self.anchor_hip_width_noise_rel = 1.0
        self.hip_scale_history.clear()
        self.hip_scale_quarantine_until = 0.0
        nominal = _clamp(cfg.get("nominal_torso_y", self._NOMINAL_TORSO_Y), 0.12, 0.34)
        self.torso_scale = _clamp((self.anchor_torso_y or nominal) / nominal, 0.65, 1.55)
        self.camera_scale_ratio = 1.0
        self.scale_measurements.clear()

        self.noise_sigma = _clamp(sigma, 0.0007, 0.012)
        self.filtered = 0.0
        self.filter_last_at = now
        self.active = False
        self.last_rel_y = center
        self.trend_samples.clear()
        return True

    def _scale_ratio(
        self,
        torso_y: float | None,
        shoulder_width: float | None,
        hip_width: float | None = None,
        now: float | None = None,
        hand_neutral_hint: bool = False,
    ) -> tuple[float, str]:
        if self.anchor_rel_y is None:
            return 1.0, "unanchored"

        current_time = self.filter_last_at if now is None else float(now)
        hip_scale_now = None
        if self.anchor_hip_width and hip_width:
            hip_scale_now = _clamp(hip_width / self.anchor_hip_width, 0.62, 1.62)
            self.hip_scale_history.append(float(hip_scale_now))

        candidates: list[tuple[str, float]] = []
        if self.anchor_torso_y and torso_y:
            candidates.append(("torso", _clamp(torso_y / self.anchor_torso_y, 0.62, 1.62)))
        if self.anchor_shoulder_width and shoulder_width:
            candidates.append(("shoulders", _clamp(shoulder_width / self.anchor_shoulder_width, 0.62, 1.62)))
        if not candidates:
            return self.camera_scale_ratio, "held"

        mode = candidates[0][0]
        if len(candidates) >= 2:
            _a_name, a = candidates[0]
            _b_name, b = candidates[1]
            da = math.log(max(1e-6, a / self.camera_scale_ratio))
            db = math.log(max(1e-6, b / self.camera_scale_ratio))
            witness_gap = abs(da - db)
            # True camera-distance change makes torso height and shoulder width
            # move in the same direction by nearly the same percentage.  A
            # crouch mostly changes torso height; yaw mostly changes shoulder
            # width.  Do not let those one-witness deformations move the zero.
            max_move = max(abs(da), abs(db))
            min_move = min(abs(da), abs(db))
            magnitude_match = max_move > math.log(1.006) and min_move >= 0.32 * max_move
            coherent_motion = da * db > 0.0 and witness_gap <= math.log(1.055) and magnitude_match

            # v154: detect a very specific bad scale commit while the hand is
            # still neutral.  Torso and shoulders must be >=1.5% from their
            # calibrated geometry in the same absolute direction, while two
            # recent hip-width observations are >=3% in the opposite absolute
            # direction.  The event starts a short quarantine; hip itself never
            # supplies a scale value.
            strong_absolute_contradiction = False
            if (
                coherent_motion
                and hand_neutral_hint
                and self.anchor_hip_width_noise_rel <= 0.040
                and len(self.hip_scale_history) >= 2
            ):
                la_abs = math.log(max(1e-6, a))
                lb_abs = math.log(max(1e-6, b))
                if la_abs * lb_abs > 0.0 and min(abs(la_abs), abs(lb_abs)) >= math.log(1.015):
                    expected_sign = 1 if (la_abs + lb_abs) > 0.0 else -1
                    recent_hip_abs = [math.log(max(1e-6, h)) for h in list(self.hip_scale_history)[-2:]]
                    strong_absolute_contradiction = all(
                        (1 if lh > 0.0 else (-1 if lh < 0.0 else 0)) == -expected_sign
                        and abs(lh) >= math.log(1.030)
                        for lh in recent_hip_abs
                    )
            if strong_absolute_contradiction:
                # v156: the scale coordinate was just declared unreliable.
                # Residuals collected before and after this event belong to
                # different coordinate regimes; mixing them inflates MAD and
                # later widens the user's adaptive deadzone.  Keep the current
                # noise_sigma continuous, but restart the residual window.
                if current_time >= self.hip_scale_quarantine_until:
                    # v157: do not throw away an almost-complete neutral-noise
                    # window silently.  With >=10 samples we already have useful
                    # robust evidence, but avoid a large one-shot jump because the
                    # coordinate regime is about to change.  Carry forward at most
                    # +0.0007 of additional sigma, then restart the residual window.
                    if len(self.neutral_residuals) >= 10:
                        _vals = list(self.neutral_residuals)
                        _estimate = _clamp(1.4826 * _mad(_vals), 0.0007, 0.012)
                        if _estimate >= 2.0 * max(0.0007, self.noise_sigma):
                            _handoff = min(_estimate, self.noise_sigma + 0.0007)
                            self.noise_sigma = max(self.noise_sigma, _handoff)
                    self.neutral_residuals.clear()
                self.hip_scale_quarantine_until = max(self.hip_scale_quarantine_until, current_time + 0.16)

            hip_quarantine = current_time < self.hip_scale_quarantine_until
            if coherent_motion and not hip_quarantine:
                self.three_scale_consensus_streak = 0
                measurement = math.sqrt(a * b)
                mode = "consensus_motion"
                # True perspective scale affects both witnesses at once.  Once
                # consensus is established, follow it quickly; the identity
                # deadband below prevents tiny landmark-scale jitter from
                # being injected into the hand signal.
                gain = 0.94
            elif coherent_motion and hip_quarantine:
                self.three_scale_consensus_streak = 0
                measurement = self.camera_scale_ratio
                mode = "hip_neutral_quarantine"
                gain = 0.0
            else:
                # v149: shoulder width can temporarily lag a genuine near/far
                # motion because the right arm perturbs shoulder landmarks.
                # Use hip width as a third, arm-independent witness *only* when
                # torso height and hip width both depart calibration in the same
                # direction by >=3%.  Unlike v148's veto-only experiment, this
                # fixes the camera scale itself so real hand motion remains
                # available during zoom.  Choose the witness closer to 1.0 as
                # a conservative estimate, avoiding hip-noise overshoot.
                hip_scale = hip_scale_now
                torso_scale = a if _a_name == "torso" else (b if _b_name == "torso" else None)
                three_witness_consensus = False
                if torso_scale is not None and hip_scale is not None:
                    lt = math.log(max(1e-6, torso_scale))
                    lh = math.log(max(1e-6, hip_scale))
                    # Shoulder may lag a true zoom because of arm coupling, but
                    # it should at least drift in the same direction.  Crouch
                    # and yaw produce a much larger spread among the three
                    # independent body-scale witnesses.
                    logs = [lt, lh, math.log(max(1e-6, b if _b_name == "shoulders" else a))]
                    ls3 = logs[2]
                    same_direction = lt * lh > 0.0 and lt * ls3 > 0.0
                    torso_hip_enough = min(abs(lt), abs(lh)) >= math.log(1.030)
                    shoulder_support = abs(ls3) >= math.log(1.010)
                    spread_ok = max(logs) - min(logs) <= math.log(1.080)
                    three_witness_consensus = bool(same_direction and torso_hip_enough and shoulder_support and spread_ok)
                if three_witness_consensus:
                    self.three_scale_consensus_streak += 1
                else:
                    self.three_scale_consensus_streak = 0

                # v159: a special first-frame fast path for the case that
                # motivated the third witness in the first place: shoulder width
                # lagged, so the old two-witness scale-pressure path has *not*
                # already established a scale context, but torso/shoulder/hip now
                # all show a strong perspective-consistent change.  Persistent
                # yaw/crouch deformations usually establish two-witness context
                # first and therefore still require the normal 2-frame rule.
                strong_three_first_frame = False
                if three_witness_consensus and torso_scale is not None and hip_scale is not None:
                    no_prior_scale_context = current_time >= self.scale_context_until
                    torso_mag = abs(lt)
                    shoulder_mag = abs(ls3)
                    hip_mag = abs(lh)
                    strong_three_first_frame = bool(
                        self.three_scale_consensus_streak == 1
                        and no_prior_scale_context
                        and min(torso_mag, shoulder_mag, hip_mag) >= math.log(1.040)
                        and (max(logs) - min(logs)) <= math.log(1.080)
                        and torso_mag >= min(shoulder_mag, hip_mag)
                    )

                if three_witness_consensus and (self.three_scale_consensus_streak >= 2 or strong_three_first_frame):
                    # Median in log/ratio order rejects either a lagging
                    # shoulder or a noisy hip without over-shooting the scale.
                    # Requiring two consecutive three-witness frames prevents a
                    # deep crouch from briefly masquerading as perspective zoom.
                    measurement = float(statistics.median([torso_scale, hip_scale, b if _b_name == "shoulders" else a]))
                    mode = "consensus_motion"
                    gain = 0.90
                else:
                    # When the witnesses disagree strongly, this is normally body
                    # deformation (crouch/yaw), not camera distance.
                    la = abs(math.log(max(1e-6, a)))
                    lb = abs(math.log(max(1e-6, b)))
                    near_anchor = math.log(1.030)
                    clearly_deformed = math.log(1.060)
                    if min(la, lb) <= near_anchor and max(la, lb) >= clearly_deformed:
                        measurement = a if la <= lb else b
                        mode = "deformation_recover"
                        gain = 0.16
                    else:
                        measurement = self.camera_scale_ratio
                        mode = "deformation_hold"
                        gain = 0.0
        else:
            mode, measurement = candidates[0]
            gain = 0.02

        self.scale_measurements.append(float(measurement))
        if mode == "consensus_motion":
            robust = float(measurement)
        else:
            robust = float(statistics.median(self.scale_measurements))
        self.camera_scale_ratio += gain * (robust - self.camera_scale_ratio)
        self.camera_scale_ratio = _clamp(self.camera_scale_ratio, 0.65, 1.55)
        return self.camera_scale_ratio, mode

    def _adaptive_deadzone(self, base: float, travel: float) -> tuple[float, float]:
        noise_term = 2.85 * self.noise_sigma / max(1e-6, travel)
        enter = _clamp(base + noise_term, base, 0.23)
        release = _clamp(base + 1.15 * self.noise_sigma / max(1e-6, travel), base * 0.70, enter * 0.78)
        return enter, release

    def _trend_intent(self, raw: float, base_deadzone: float, travel: float, now: float, corrected_rel: float) -> bool:
        self.trend_samples.append((now, corrected_rel))
        if len(self.trend_samples) < 4:
            return False
        pts = list(self.trend_samples)
        mid = len(pts) // 2
        y0 = statistics.median(y for _, y in pts[:mid])
        y1 = statistics.median(y for _, y in pts[mid:])
        t0 = statistics.fmean(t for t, _ in pts[:mid])
        t1 = statistics.fmean(t for t, _ in pts[mid:])
        dt = max(1e-4, t1 - t0)
        disp = float(y1 - y0)
        min_disp = max(3.0 * self.noise_sigma, 0.026 * travel)
        min_speed = 0.12 * travel
        return abs(disp) >= min_disp and abs(disp) / dt >= min_speed and abs(raw) >= base_deadzone * 0.82 and disp * raw > 0.0

    def _kinematic_filter(
        self,
        pose_map: dict[str, dict[str, Any]],
        corrected_rel: float,
        reference_y: float,
        effective_scale_ratio: float,
        travel: float,
    ) -> tuple[float, bool, bool]:
        """Return filtered wrist-relative position, reject flag, and motion proof.

        Motion proof comes from image-space wrist+elbow displacement, not from
        the shoulder-relative signal itself.  That distinction matters: a bad
        shoulder landmark can move both wrist-relative and elbow-relative
        coordinates even though the arm did not move in the camera image.
        """
        self.current_wrist_body_step_norm = 0.0
        self.current_elbow_body_step_norm = 0.0
        wrist = pose_map.get("right_wrist")
        elbow = pose_map.get("right_elbow")
        if (
            _score(wrist) < self._KINEMATIC_MIN_SCORE
            or _score(elbow) < self._KINEMATIC_MIN_SCORE
            or not _finite_y(elbow)
            or not _finite_y(wrist)
        ):
            self.last_elbow_rel = None
            self.last_forearm_len = None
            self.last_wrist_image_y = None
            self.last_elbow_image_y = None
            return corrected_rel, False, False

        wrist_image_y = float(wrist["y"])
        elbow_image_y = float(elbow["y"])
        forearm_len = _distance(wrist, elbow)
        if forearm_len is None:
            return corrected_rel, False, False
        elbow_rel = (elbow_image_y - reference_y) / max(1e-6, effective_scale_ratio)
        if self.last_rel_y is not None:
            self.current_wrist_body_step_norm = abs(corrected_rel - self.last_rel_y) / max(1e-6, travel)
        if self.last_elbow_rel is not None:
            self.current_elbow_body_step_norm = abs(elbow_rel - self.last_elbow_rel) / max(1e-6, travel)

        motion_proof = False
        if self.last_wrist_image_y is not None and self.last_elbow_image_y is not None:
            wdy = wrist_image_y - self.last_wrist_image_y
            edy = elbow_image_y - self.last_elbow_image_y
            min_wrist_move = max(2.4 * self.noise_sigma, 0.016 * travel)
            motion_proof = (
                abs(wdy) >= min_wrist_move
                and wdy * edy > 0.0
                and abs(edy) >= max(1.5 * self.noise_sigma, 0.20 * abs(wdy))
            )

        rejected = False
        if self.last_rel_y is not None and self.last_elbow_rel is not None and self.last_forearm_len is not None:
            wrist_jump = corrected_rel - self.last_rel_y
            elbow_jump = elbow_rel - self.last_elbow_rel
            jump_threshold = max(5.6 * self.noise_sigma, 0.060 * travel)
            geometry_change = abs(math.log(max(1e-6, forearm_len / self.last_forearm_len)))
            elbow_uncorroborated = abs(elbow_jump) <= max(3.0 * self.noise_sigma, 0.28 * abs(wrist_jump))
            geometry_implausible = geometry_change >= math.log(1.075)
            toward_neutral = False
            if self.anchor_rel_y is not None:
                old_mag = abs(self.last_rel_y - self.anchor_rel_y)
                new_mag = abs(corrected_rel - self.anchor_rel_y)
                # Returning toward neutral is intrinsically safer than moving
                # farther outward: accepting it can only reduce the commanded
                # magnitude.  Never let the outlier guard trap an active hand
                # away from zero.
                toward_neutral = new_mag <= max(0.0, old_mag - max(1.5 * self.noise_sigma, 0.012 * travel))
            # A neutralward jump is usually safer than an outward jump, but
            # while ACTIVE a single implausible "half-return" far from neutral
            # creates visible view jitter and poisons the next-frame baseline.
            # Quarantine only the far-from-neutral, uncorroborated geometry
            # spike; genuine return near neutral remains immediate.
            far_neutralward_spike = bool(
                toward_neutral
                and self.active
                and new_mag >= max(0.30 * travel, 0.52 * old_mag)
            )
            should_reject_jump = bool(
                abs(wrist_jump) >= jump_threshold
                and elbow_uncorroborated
                and geometry_implausible
                and ((not toward_neutral) or far_neutralward_spike)
            )
            if should_reject_jump:
                reject_sign = 1 if wrist_jump > 0.0 else -1
                if self.kinematic_reject_sign == reject_sign:
                    self.kinematic_reject_streak += 1
                else:
                    self.kinematic_reject_sign = reject_sign
                    self.kinematic_reject_streak = 1

                if self.kinematic_reject_streak < 2:
                    # First implausible jump is quarantined exactly as before.
                    corrected_rel = self.last_rel_y
                    rejected = True
                    motion_proof = False
                    self.kinematic_rejects += 1
                else:
                    # Do not freeze a sustained same-direction real gesture.
                    # The second frame may update position, but it receives no
                    # motion-proof credit.  The outer gate still needs another
                    # independent proof/decisive frame, so a two-frame false
                    # localization burst cannot directly create output.
                    rejected = False
                    motion_proof = False
            else:
                self.kinematic_reject_streak = 0
                self.kinematic_reject_sign = 0
        else:
            self.kinematic_reject_streak = 0
            self.kinematic_reject_sign = 0

        # Keep independent arm-geometry baselines current even when the wrist
        # command position is quarantined.  Freezing all baselines makes one
        # false reject self-reinforcing: every following genuine frame is then
        # compared against increasingly stale elbow/forearm geometry.
        self.last_elbow_rel = elbow_rel
        self.last_forearm_len = float(forearm_len)
        self.last_wrist_image_y = wrist_image_y
        self.last_elbow_image_y = elbow_image_y
        return corrected_rel, rejected, motion_proof

    def _long_arm_intent(
        self,
        pose_map: dict[str, dict[str, Any]],
        now: float,
        corrected_rel: float,
        raw: float,
        travel: float,
    ) -> bool:
        """Detect very slow genuine arm motion over ~0.55 s.

        Short windows are optimal for ordinary gestures but cannot separate a
        very slow wrist drift from correlated landmark noise.  This second
        timescale requires the wrist, elbow and shoulder-relative wrist signal
        to agree over a much longer window.  It therefore restores slow-motion
        coverage without weakening the instantaneous static gate.
        """
        wrist = pose_map.get("right_wrist")
        elbow = pose_map.get("right_elbow")
        shoulder = pose_map.get("right_shoulder")
        if (
            _score(wrist) < self._KINEMATIC_MIN_SCORE
            or _score(elbow) < self._KINEMATIC_MIN_SCORE
            or _score(shoulder) < self._KINEMATIC_MIN_SCORE
            or not _finite_y(wrist)
            or not _finite_y(elbow)
            or not _finite_y(shoulder)
        ):
            self.long_arm_samples.clear()
            return False
        self.long_arm_samples.append((now, float(wrist["y"]), float(elbow["y"]), float(shoulder["y"]), corrected_rel))
        while self.long_arm_samples and now - self.long_arm_samples[0][0] > 0.62:
            self.long_arm_samples.popleft()
        pts = list(self.long_arm_samples)
        if len(pts) < 6 or pts[-1][0] - pts[0][0] < 0.28:
            return False
        k = max(2, len(pts) // 3)
        early = pts[:k]
        late = pts[-k:]
        def med(index: int, values):
            return float(statistics.median(item[index] for item in values))
        dw = med(1, late) - med(1, early)
        de = med(2, late) - med(2, early)
        ds = med(3, late) - med(3, early)
        dr = med(4, late) - med(4, early)
        der = de - ds
        dt = max(1e-4, statistics.fmean(item[0] for item in late) - statistics.fmean(item[0] for item in early))
        min_rel = max(1.65 * self.noise_sigma, 0.035 * travel)
        min_wrist = max(1.45 * self.noise_sigma, 0.028 * travel)
        coherent_arm = dw * de > 0.0 and abs(de) >= 0.14 * abs(dw)
        # Whole-body sway moves elbow and shoulder together; a genuine arm
        # raise changes elbow relative to the shoulder as well.
        relative_elbow = der * dr > 0.0 and abs(der) >= max(0.75 * self.noise_sigma, 0.08 * abs(dr))
        coherent_signal = dr * raw > 0.0
        return (
            coherent_arm
            and relative_elbow
            and coherent_signal
            and abs(dr) >= min_rel
            and abs(dw) >= min_wrist
            and abs(dr) / dt >= 0.045 * travel
        )
    def _scale_motion_pressure(self, torso_y: float | None, shoulder_width: float | None, now: float) -> tuple[bool, float]:
        """Detect sustained likely camera-distance motion before scale commit.

        A single noisy shoulder-width/torso frame must never veto a slow arm
        gesture.  Store a tiny history and use the median of the recent body
        scale witnesses; genuine zoom persists, landmark scale noise usually
        does not.
        """
        if not (self.anchor_torso_y and torso_y and self.anchor_shoulder_width and shoulder_width):
            return False, 0.0
        tr = _clamp(torso_y / self.anchor_torso_y, 0.62, 1.62)
        sr = _clamp(shoulder_width / self.anchor_shoulder_width, 0.62, 1.62)
        lt = math.log(max(1e-6, tr))
        ls = math.log(max(1e-6, sr))
        self.scale_pressure_samples.append((now, lt, ls))
        recent = [item for item in self.scale_pressure_samples if now - item[0] <= 0.12]
        if len(recent) < 2:
            return False, 0.0
        use = recent[-3:]
        mt = float(statistics.median(item[1] for item in use))
        ms = float(statistics.median(item[2] for item in use))
        minimum = math.log(1.030)
        same_direction = mt * ms > 0.0
        enough = min(abs(mt), abs(ms)) >= minimum
        gap_ok = abs(mt - ms) <= math.log(1.12)
        strength = min(abs(mt), abs(ms))
        return bool(same_direction and enough and gap_ok), float(strength)

    def _long_arm_quality(self, travel: float) -> tuple[bool, float, float, float, float]:
        """Stricter quality test for sub-deadzone slow-arm intent.

        Besides overall wrist/body trend, require the wrist to move relative to
        the elbow.  Correlated body/pose drift often moves wrist and elbow
        together; a real slow arm gesture changes the internal arm geometry.
        """
        pts = list(self.long_arm_samples)
        if len(pts) < 6 or pts[-1][0] - pts[0][0] < 0.28:
            return False, 0.0, 0.0, 0.0, 0.0
        k = max(2, len(pts) // 3)
        early = pts[:k]
        late = pts[-k:]
        med = lambda index, values: float(statistics.median(item[index] for item in values))
        dr = med(4, late) - med(4, early)
        dwer = (med(1, late) - med(2, late)) - (med(1, early) - med(2, early))
        dt = max(1e-4, statistics.fmean(item[0] for item in late) - statistics.fmean(item[0] for item in early))
        values = [item[4] for item in pts]
        times = [item[0] for item in pts]
        ym = statistics.fmean(values)
        tm = statistics.fmean(times)
        cov = sum((t - tm) * (y - ym) for t, y in zip(times, values))
        vt = sum((t - tm) ** 2 for t in times)
        vy = sum((y - ym) ** 2 for y in values)
        r2 = (cov * cov / (vt * vy)) if vt > 1e-12 and vy > 1e-12 else 0.0
        disp_norm = abs(dr) / max(1e-6, travel)
        speed_norm = abs(dr) / dt / max(1e-6, travel)
        forearm_norm = abs(dwer) / max(1e-6, travel)
        current_raw_sign = 0
        if pts:
            current_raw = (pts[-1][4] - (self.anchor_rel_y or pts[-1][4])) / max(1e-6, travel)
            current_raw_sign = 1 if current_raw > 0.0 else (-1 if current_raw < 0.0 else 0)
        quality = (
            r2 >= 0.64
            and disp_norm >= 0.090
            and speed_norm >= 0.18
            and forearm_norm >= 0.075
            and dwer * dr > 0.0
            and current_raw_sign != 0
            and dr * current_raw_sign > 0.0
        )
        return bool(quality), float(r2), float(disp_norm), float(speed_norm), float(forearm_norm)

    def _update_long_arm_evidence(
        self,
        *,
        long_arm_intent: bool,
        raw: float,
        base_deadzone: float,
        now: float,
        scale_veto: bool,
    ) -> bool:
        """Turn long-window slow-motion evidence into a sequential decision."""
        sign = 1 if raw > 0.0 else (-1 if raw < 0.0 else 0)
        floor = base_deadzone * 0.76
        if scale_veto or not long_arm_intent or abs(raw) < floor or sign == 0:
            # A tiny grace interval allows one noisy false frame inside a real
            # slow gesture without forcing the accumulator back to zero.
            if now - self.long_arm_evidence_last_at > self._LONG_ARM_GAP_GRACE_SECONDS:
                self.long_arm_evidence_s = max(0.0, self.long_arm_evidence_s - 0.050)
                if self.long_arm_evidence_s <= 1e-6:
                    self.long_arm_evidence_sign = 0
            if scale_veto:
                self.long_arm_evidence_s = 0.0
                self.long_arm_evidence_sign = 0
            return False

        if self.long_arm_evidence_sign not in (0, sign):
            self.long_arm_evidence_s = 0.0
        dt = 0.0
        if self.long_arm_evidence_last_at > 0.0:
            dt = _clamp(now - self.long_arm_evidence_last_at, 1.0 / 120.0, 0.060)
        else:
            dt = 1.0 / 60.0
        self.long_arm_evidence_sign = sign
        self.long_arm_evidence_s += dt
        self.long_arm_evidence_last_at = now
        return self.long_arm_evidence_s >= self._LONG_ARM_CONFIRM_SECONDS

    def update(self, pose_map: dict[str, dict[str, Any]], now: float, config: dict[str, Any] | None = None) -> dict[str, Any]:
        cfg = config or {}
        now = float(now)
        point_name = str(cfg.get("point", "right_wrist"))
        wrist = pose_map.get(point_name)
        reference_y, torso_y, shoulder_width = self._body_geometry(pose_map)
        hip_width = _distance(pose_map.get("left_hip"), pose_map.get("right_hip"))
        if hip_width is not None and not (0.05 <= hip_width <= 0.60):
            hip_width = None
        good = bool(wrist and _score(wrist) >= self._MIN_SCORE and _finite_y(wrist) and reference_y is not None)

        if not good:
            if self.tracking_lost_at is None:
                self.tracking_lost_at = now
                self.preloss_filtered = self.filtered
                self.preloss_active = self.active
                self.preloss_corrected_rel = self.last_rel_y
                self.preloss_entry_raw = self.prev_entry_raw
                self.preloss_entry_deadzone = self.last_enter_deadzone
            elif now - self.tracking_lost_at >= self._LONG_LOSS_SECONDS:
                self.await_neutral_after_loss = True
            self.reacquire_good_frames = 0
            self.reacquire_rel_samples.clear()
            self.filtered = 0.0
            self.active = False
            self.filter_last_at = now
            self.trend_samples.clear()
            # v70: separate data memory from decision authority.  On the
            # first frame of a short confidence gap, preserve the long-window
            # geometry for at most 180 ms, but *always* reset sequential entry
            # evidence.  After recovery, five fresh good frames are required
            # before any pre-gap history may help open the gate.  A new,
            # separate short dropout starts a fresh bridge window.
            loss_age = 0.0 if self.tracking_lost_at is None else now - self.tracking_lost_at
            if loss_age <= 1e-9:
                self.history_bridge_until = now + self._HISTORY_BRIDGE_SECONDS
                self.history_bridge_good_frames = 0
                bridge_sign = 0
                if self.long_arm_samples:
                    last_corr = self.long_arm_samples[-1][4]
                    delta = last_corr - (self.anchor_rel_y or last_corr)
                    bridge_sign = 1 if delta > 0.0 else (-1 if delta < 0.0 else 0)
                self.history_bridge_sign = bridge_sign
            self.long_arm_evidence_s = 0.0
            self.long_arm_evidence_sign = 0
            self.long_arm_evidence_last_at = 0.0
            self.long_arm_quality_streak = 0
            if (
                loss_age >= self._LONG_LOSS_SECONDS
                or now >= self.history_bridge_until
            ):
                self.long_arm_samples.clear()
                self.history_bridge_until = 0.0
                self.history_bridge_good_frames = 0
                self.history_bridge_sign = 0
            self.intent_confirm_frames = 0
            self.motion_proof_streak = 0
            # Position-only evidence must be strictly contiguous.  A confidence
            # dropout breaks the physical observation sequence; bridging it can
            # join two unrelated static-noise excursions into a false gesture.
            self.position_evidence_started_at = None
            self.position_evidence_sign = 0
            self.position_evidence_frames = 0
            self.position_evidence_start_abs_raw = 0.0
            if now < self.gap_candidate_until:
                self.gap_candidate_bad_frames += 1
                if self.gap_candidate_bad_frames > 2:
                    self.gap_candidate_until = 0.0
                    self.gap_candidate_sign = 0
                    self.gap_candidate_abs_raw = 0.0
            else:
                self.gap_candidate_until = 0.0
                self.gap_candidate_sign = 0
                self.gap_candidate_abs_raw = 0.0
                self.gap_candidate_bad_frames = 0
            return self._snapshot()

        wrist_y = float(wrist["y"])
        rel_y = wrist_y - float(reference_y)
        self.anchor_y = wrist_y

        if self.anchor_rel_y is None:
            self._update_anchor(rel_y, now, cfg, torso_y, shoulder_width, hip_width)
            return self._snapshot(reference_y=reference_y)

        # v154: scale safety can use hip contradiction only before the hand has
        # materially left neutral.  Compute this hint from the unscaled signal
        # so a bad scale estimate cannot influence its own permission.
        scale_hint_travel = self._anchor_travel(cfg)
        scale_hint_base_deadzone = _clamp(cfg.get("deadzone", 0.08), 0.04, 0.22)
        scale_hint_enter, _ = self._adaptive_deadzone(scale_hint_base_deadzone, scale_hint_travel)
        unscaled_raw_hint = (rel_y - self.anchor_rel_y) / max(1e-6, scale_hint_travel)
        hand_neutral_for_scale = abs(unscaled_raw_hint) <= max(0.060, 0.88 * scale_hint_enter)
        scale_ratio, scale_mode = self._scale_ratio(
            torso_y, shoulder_width, hip_width, now=now, hand_neutral_hint=hand_neutral_for_scale
        ) if bool(cfg.get("body_scale_normalization", True)) else (1.0, "disabled")
        # Landmark geometry itself jitters by roughly the same order as tiny
        # apparent (<~1.5%) scale changes.  Compensating those tiny changes
        # injects scale-estimator noise into the wrist signal.  Keep the fast
        # v6 estimator for real near/far motion, but make its neutral region
        # exactly identity.  A genuine scale change must first become large
        # enough to matter to hand control before it is allowed to move zero.
        scale_deadband = _clamp(cfg.get("camera_scale_deadband", 0.015), 0.006, 0.035)
        if abs(math.log(max(1e-6, scale_ratio))) <= math.log(1.0 + scale_deadband):
            effective_scale_ratio = 1.0
            if scale_mode != "disabled":
                scale_mode = "identity_deadband"
        else:
            effective_scale_ratio = scale_ratio

        # v91: fuse shoulder-center with an independent hip-derived shoulder
        # estimate whenever the torso still matches the calibrated geometry.
        # This reduces shoulder landmark noise and right-arm shoulder coupling
        # without adding temporal lag.  Crouch/deformation immediately falls
        # back to the authored shoulder reference.
        fused_reference = False
        scale_near_calibration = abs(math.log(max(1e-6, effective_scale_ratio))) <= math.log(1.035)
        if (
            self.active
            and scale_near_calibration
            and scale_mode != "consensus_motion"
            and self.anchor_torso_y is not None
            and torso_y is not None
        ):
            hips_y = []
            for hip_name in ("right_hip", "left_hip"):
                hip = pose_map.get(hip_name)
                if _score(hip) >= self._MIN_SCORE and _finite_y(hip):
                    hips_y.append(float(hip["y"]))
            if hips_y:
                expected_torso = self.anchor_torso_y * effective_scale_ratio
                torso_match = abs(torso_y - expected_torso) <= max(0.018, 0.085 * expected_torso)
                if torso_match:
                    hip_reference = float(statistics.fmean(hips_y)) - expected_torso
                    shoulder_reference = float(reference_y)
                    reference_y = 0.75 * shoulder_reference + 0.25 * hip_reference
                    rel_y = wrist_y - float(reference_y)
                    fused_reference = True

        corrected_rel = rel_y / max(1e-6, effective_scale_ratio)
        travel = self._anchor_travel(cfg)
        corrected_rel, kinematic_reject, motion_proof = self._kinematic_filter(
            pose_map, corrected_rel, float(reference_y), effective_scale_ratio, travel
        )
        base_deadzone = _clamp(cfg.get("deadzone", 0.08), 0.04, 0.22)
        provisional_raw = (corrected_rel - self.anchor_rel_y) / max(1e-6, travel)
        # Calibration sees only a handful of frames.  Continue learning the
        # user's true neutral landmark dispersion, but only deep inside the
        # neutral region where genuine control intent is very unlikely.  Noise
        # can rise quickly (lighting/pose quality changed) and decays only very
        # slowly, preventing the gate from becoming optimistic again too soon.
        # v155: a scale quarantine intentionally freezes camera-scale updates.
        # Residuals measured while that alternate coordinate state is active do
        # not represent the user's normal Pose noise and must not train the
        # adaptive neutral-noise model.  Otherwise a successful safety veto can
        # inflate noise_sigma and later make ACTIVE release too eager.
        learning_scale_quarantine = now < self.hip_scale_quarantine_until
        if (
            not self.active
            and not kinematic_reject
            and not learning_scale_quarantine
            and abs(provisional_raw) <= max(0.055, 0.70 * base_deadzone)
        ):
            self.neutral_residuals.append(corrected_rel - self.anchor_rel_y)
            if len(self.neutral_residuals) >= 12:
                estimated = _clamp(1.4826 * _mad(list(self.neutral_residuals)), 0.0007, 0.012)
                gain = 0.18 if estimated > self.noise_sigma else 0.006
                self.noise_sigma = _clamp(self.noise_sigma + gain * (estimated - self.noise_sigma), 0.0007, 0.012)
        enter_deadzone, release_deadzone = self._adaptive_deadzone(base_deadzone, travel)
        raw = _clamp(provisional_raw, -1.0, 1.0)
        # v142: independent HQ slow-arm entry needs a short, genuine
        # same-direction history.  Two abrupt correlated wrist excursions can
        # look excellent to the long-window fit; they must not self-certify a
        # slow gesture before a direction has actually persisted.
        self.entry_raw_history.append((now, raw))
        while self.entry_raw_history and now - self.entry_raw_history[0][0] > 0.22:
            self.entry_raw_history.popleft()
        raw_sign_for_hq = 1 if raw > 0.0 else (-1 if raw < 0.0 else 0)
        meaningful_hq = [(t, r) for t, r in self.entry_raw_history if abs(r) >= 0.020]
        same_side_hq = [
            (t, r) for t, r in meaningful_hq
            if (1 if r > 0.0 else -1) == raw_sign_for_hq
        ]
        same_side_fraction_hq = len(same_side_hq) / max(1, len(meaningful_hq))
        same_side_started_hq = now
        for t_hist, r_hist in reversed(self.entry_raw_history):
            if abs(r_hist) < 0.020:
                continue
            if (1 if r_hist > 0.0 else -1) != raw_sign_for_hq:
                break
            same_side_started_hq = t_hist
        same_side_duration_hq = max(0.0, now - same_side_started_hq)
        hq_direction_stable = bool(
            raw_sign_for_hq != 0
            and same_side_fraction_hq >= 0.55
            and same_side_duration_hq >= 0.075
        )
        # Defaults are available to every early-return diagnostic path.
        position_evidence_s = (
            0.0 if self.position_evidence_started_at is None
            else max(0.0, now - self.position_evidence_started_at)
        )
        sustained_position = bool(
            self.position_evidence_frames >= 3 and position_evidence_s >= 0.055
        )
        if self.history_bridge_until > 0.0:
            if now >= self.history_bridge_until:
                self.long_arm_samples.clear()
                self.long_arm_evidence_s = 0.0
                self.long_arm_evidence_sign = 0
                self.long_arm_evidence_last_at = 0.0
                self.long_arm_quality_streak = 0
                self.history_bridge_until = 0.0
                self.history_bridge_good_frames = 0
                self.history_bridge_sign = 0
            elif self.tracking_lost_at is not None:
                # First recovered good frame.
                self.history_bridge_good_frames = min(
                    self._HISTORY_BRIDGE_GOOD_FRAMES,
                    self.history_bridge_good_frames + 1,
                )
            else:
                self.history_bridge_good_frames = min(
                    self._HISTORY_BRIDGE_GOOD_FRAMES,
                    self.history_bridge_good_frames + 1,
                )
        bridge_entry_ready = bool(
            self.history_bridge_until <= 0.0
            or self.history_bridge_good_frames >= self._HISTORY_BRIDGE_GOOD_FRAMES
        )

        long_arm_intent = self._long_arm_intent(pose_map, now, corrected_rel, raw, travel)
        long_arm_high_quality, long_arm_r2, long_arm_disp_norm, long_arm_speed_norm, long_arm_forearm_norm = self._long_arm_quality(travel)
        scale_motion_pressure, scale_pressure_strength = self._scale_motion_pressure(torso_y, shoulder_width, now)
        consensus_far = (
            scale_mode == "consensus_motion"
            and abs(math.log(max(1e-6, scale_ratio))) >= math.log(1.035)
        )
        confirmed_scale_motion = bool(scale_motion_pressure or consensus_far)
        if confirmed_scale_motion:
            self.scale_context_until = max(self.scale_context_until, now + 0.55)
            self.scale_veto_until = max(self.scale_veto_until, now + self._SCALE_VETO_HOLD_SECONDS)

        # When a real zoom has just been established, crossing through the
        # calibrated 1.0 scale creates a short witness-desynchronization zone:
        # torso height can already be <1 while shoulder width is still ~1 (or
        # vice versa).  Preserve veto across that bridge while either witness
        # remains meaningfully away from calibration.  This rule is context-
        # dependent, so an isolated crouch/yaw cannot trigger it by itself.
        scale_context_far = False
        if now < self.scale_context_until and self.anchor_torso_y and torso_y and self.anchor_shoulder_width and shoulder_width:
            tr_ctx = _clamp(torso_y / self.anchor_torso_y, 0.62, 1.62)
            sr_ctx = _clamp(shoulder_width / self.anchor_shoulder_width, 0.62, 1.62)
            max_departure = max(abs(math.log(max(1e-6, tr_ctx))), abs(math.log(max(1e-6, sr_ctx))))
            scale_context_far = max_departure >= math.log(1.040)
            if scale_context_far:
                self.scale_veto_until = max(self.scale_veto_until, now + 0.11)
        scale_veto = now < self.scale_veto_until

        # v119: slow-entry branches must not authenticate while torso height
        # and shoulder width disagree strongly.  This is the characteristic
        # geometry of crouch/yaw deformation, not camera scale or arm intent.
        body_witness_disagreement = 0.0
        if (
            self.anchor_torso_y is not None
            and torso_y is not None
            and self.anchor_shoulder_width is not None
            and shoulder_width is not None
        ):
            torso_ratio_entry = torso_y / self.anchor_torso_y
            shoulder_ratio_entry = shoulder_width / self.anchor_shoulder_width
            body_witness_disagreement = abs(
                math.log(max(1e-6, torso_ratio_entry / max(1e-6, shoulder_ratio_entry)))
            )
        slow_body_deformation_veto = body_witness_disagreement >= 0.12

        long_arm_confirmed = self._update_long_arm_evidence(
            long_arm_intent=long_arm_intent,
            raw=raw,
            base_deadzone=base_deadzone,
            now=now,
            scale_veto=scale_veto,
        )
        if scale_veto or not long_arm_high_quality:
            self.long_arm_quality_streak = 0
        else:
            # High-quality slow-arm evidence is intentionally independent of
            # the legacy long_arm predicate.  The latter demands noticeable
            # elbow-vs-shoulder travel and misses valid forearm-dominant slow
            # gestures.  Safety still comes from the adaptive deadzone and
            # scale veto at the actual entry branch.
            self.long_arm_quality_streak = min(4, self.long_arm_quality_streak + 1)
        long_arm_quality_confirmed = self.long_arm_quality_streak >= 2
        # v121: keep explicit recent high-quality witnesses.  A single HQ
        # frame followed by a very large step from near-neutral is not enough
        # to authenticate slow_hq_step; correlated/outlier noise can create
        # exactly that pattern.  The current frame is appended only after the
        # prior-witness count is computed, so one frame cannot certify itself.
        while self.recent_hq_history and now - self.recent_hq_history[0][0] > 0.40:
            self.recent_hq_history.popleft()
        prior_hq_sign = 1 if raw > 0.0 else (-1 if raw < 0.0 else 0)
        prior_hq_hits = sum(1 for _, sign in self.recent_hq_history if sign == prior_hq_sign) if prior_hq_sign else 0
        if (not scale_veto) and long_arm_high_quality:
            hq_sign = prior_hq_sign
            if hq_sign != 0:
                self.recent_hq_until = now + 0.35
                self.recent_hq_sign = hq_sign
                self.recent_hq_history.append((now, hq_sign))
        if now >= self.recent_hq_until:
            self.recent_hq_sign = 0

        # Preserve a nearly-qualified slow gesture across a very short tracking
        # dip.  This is not an entry permission: it only protects the 0.6 s
        # history from being erased.  Requiring already-confirmed long-arm
        # evidence plus proximity to the personal safety boundary avoids the
        # static cross-gap failure that unconditional history retention caused.
        if (
            (not scale_veto)
            and long_arm_confirmed
            and abs(raw) >= 0.65 * enter_deadzone
        ):
            sign_now = 1 if raw > 0.0 else (-1 if raw < 0.0 else 0)
            if sign_now != 0:
                self.slow_candidate_until = now + 0.13
                self.slow_candidate_sign = sign_now

        dropout_first_good_gate = False
        short_reseed = False
        if self.tracking_lost_at is not None:
            # v72: a very narrow first-good-frame continuation for a gesture
            # that began immediately before a one/few-frame confidence loss.
            # This only applies when the controller was still INACTIVE before
            # the gap; ACTIVE continuity continues to use short_reseed.
            pre_raw = self.preloss_entry_raw
            pre_dz = self.preloss_entry_deadzone
            dropout_first_good_gate = bool(
                (not self.preloss_active)
                and pre_raw is not None
                and pre_dz is not None
                and (not scale_veto)
                and (not kinematic_reject)
                and _score(wrist) >= 0.58
                and pre_raw * raw > 0.0
                and abs(pre_raw) >= 0.55 * pre_dz
                and abs(raw) >= 1.25 * enter_deadzone
                and abs(raw) - abs(pre_raw) >= 0.080
                and long_arm_disp_norm >= 0.060
                and long_arm_forearm_norm >= 0.040
            )
            loss_duration = now - self.tracking_lost_at
            if loss_duration >= self._LONG_LOSS_SECONDS:
                self.await_neutral_after_loss = True
            self.reacquire_good_frames += 1
            self.reacquire_rel_samples.append(corrected_rel)
            if self.reacquire_good_frames < self._REACQUIRE_GOOD_FRAMES:
                self.filtered = 0.0
                self.active = False
                self.filter_last_at = now
                return self._snapshot(reference_y=reference_y, travel=travel, raw=raw, scale_mode=scale_mode)

            # Two good frames must agree before a short-loss continuity reseed.
            reacq_spread = max(self.reacquire_rel_samples) - min(self.reacquire_rel_samples) if self.reacquire_rel_samples else 0.0
            consistent = reacq_spread <= max(4.0 * self.noise_sigma, 0.11 * travel)
            same_side = self.preloss_corrected_rel is not None and (corrected_rel - self.anchor_rel_y) * (self.preloss_corrected_rel - self.anchor_rel_y) > 0.0
            still_active = abs(raw) >= release_deadzone * 1.05
            high_conf = _score(wrist) >= max(self._MIN_SCORE, 0.58)
            if loss_duration < self._LONG_LOSS_SECONDS and self.preloss_active and same_side and still_active and high_conf:
                # During a short confidence loss the hand may genuinely keep
                # moving.  A recovered frame already passes current score,
                # same-side and still-active checks (and the kinematic filter),
                # so one good frame is sufficient for conservative continuity.
                # Requiring its recovered position to match the old
                # position closely turns a harmless 1-2 frame dropout into a
                # long dead period.  Same-side continuation is safe enough to
                # resume conservatively; disagreement only lowers the reseed
                # fraction rather than blocking recovery entirely.
                magnitude = min(abs(self.preloss_filtered), max(0.0, (abs(raw) - release_deadzone) / max(1e-6, 1.0 - release_deadzone)))
                fraction = self._SHORT_RESEED_FRACTION if consistent else 0.34
                self.filtered = math.copysign(fraction * magnitude, raw)
                short_reseed = abs(self.filtered) > 0.0

            self.tracking_lost_at = None
            self.reacquire_good_frames = 0
            self.reacquire_rel_samples.clear()

        if self.await_neutral_after_loss:
            band = max(0.14, enter_deadzone * 1.08)
            if abs(raw) <= band:
                self.neutral_rearm_samples.append(corrected_rel)
            else:
                self.neutral_rearm_samples.clear()
            self.filtered = 0.0
            self.active = False
            self.filter_last_at = now
            if len(self.neutral_rearm_samples) >= 3:
                observed = float(statistics.median(self.neutral_rearm_samples))
                shift = _clamp(observed - self.anchor_rel_y, -0.07 * travel, 0.07 * travel)
                self.anchor_rel_y += shift
                self.await_neutral_after_loss = False
                self.neutral_rearm_samples.clear()
                self.last_rel_y = corrected_rel
                self.trend_samples.clear()
            return self._snapshot(reference_y=reference_y, travel=travel, raw=raw, adaptive_deadzone=enter_deadzone, scale_mode=scale_mode)

        abs_raw = abs(raw)
        trend_intent = self._trend_intent(raw, base_deadzone, travel, now, corrected_rel)
        elbow = pose_map.get("right_elbow")
        elbow_reliable = bool(_score(elbow) >= self._KINEMATIC_MIN_SCORE and _finite_y(elbow))
        raw_sign = 1 if raw > 0.0 else (-1 if raw < 0.0 else 0)
        prev_raw = self.prev_entry_raw
        prior_near_boundary = bool(prev_raw is not None and abs(prev_raw) >= 0.68 * enter_deadzone)
        slow_hq_prior_witness_ok = bool(prior_near_boundary or prior_hq_hits >= 2)
        slow_hq_step_reentry = bool(
            (not self.active)
            and bridge_entry_ready
            and (not scale_veto)
            and (not kinematic_reject)
            and raw_sign != 0
            and self.recent_hq_sign == raw_sign
            and now < self.recent_hq_until
            and slow_hq_prior_witness_ok
            and prev_raw is not None
            and prev_raw * raw > 0.0
            and abs(prev_raw) >= 0.55 * enter_deadzone
            and abs(raw) >= 1.25 * enter_deadzone
            and abs_raw - abs(prev_raw) >= 0.10
        )

        proof_streak_qualified = bool(
            motion_proof
            and (
                max(self.current_wrist_body_step_norm, self.current_elbow_body_step_norm) >= 0.013
                or abs_raw >= enter_deadzone
            )
        )
        if kinematic_reject:
            self.motion_proof_streak = 0
        elif proof_streak_qualified:
            self.motion_proof_streak = min(4, self.motion_proof_streak + 1)
        elif motion_proof:
            # A proof made only from tiny near-neutral correlated motion may
            # refresh short-lived intent memory, but it cannot pre-load the
            # consecutive-proof counter used by a later large wrist excursion.
            self.motion_proof_streak = 0
        else:
            self.motion_proof_streak = 0

        if motion_proof and not kinematic_reject:
            self.motion_intent_until = now + 0.11
            self.motion_intent_sign = 1 if raw > 0.0 else (-1 if raw < 0.0 else 0)

        prev_ratio = 0.0 if prev_raw is None else abs(prev_raw) / max(1e-6, enter_deadzone)
        current_ratio = abs_raw / max(1e-6, enter_deadzone)
        high_noise_acquisition = enter_deadzone >= max(base_deadzone * 1.18, base_deadzone + 0.015)
        ambiguous_acquisition_trigger = bool(
            (not self.active)
            and high_noise_acquisition
            and motion_proof
            and self.motion_proof_streak >= 2
            and 1.45 <= current_ratio <= 1.65
            and self.current_wrist_body_step_norm < 0.035
            and self.current_elbow_body_step_norm < 0.018
            and prev_ratio < 1.35
        )
        if ambiguous_acquisition_trigger:
            # Two moderate correlated proof frames can be statistically
            # indistinguishable from the beginning of a gesture in a noisy
            # subject.  Do not let those frames pre-authorize grace/position
            # fallbacks.  A later unmistakably strong multi-joint move can
            # still override this short quarantine immediately.
            self.ambiguous_acquisition_until = now + 0.20
            self.ambiguous_acquisition_sign = raw_sign
            self.motion_intent_until = 0.0
            self.motion_intent_sign = 0
            self.motion_proof_streak = 0
            self.position_evidence_frames = 0
            self.position_evidence_started_at = None
            self.position_evidence_start_abs_raw = 0.0
            self.gap_candidate_until = 0.0
            self.gap_candidate_sign = 0
            self.gap_candidate_abs_raw = 0.0
            self.gap_candidate_bad_frames = 0
        if now >= self.ambiguous_acquisition_until:
            self.ambiguous_acquisition_until = 0.0
            self.ambiguous_acquisition_sign = 0
        strong_quarantine_override = bool(
            motion_proof
            and current_ratio >= 2.10
            and self.current_wrist_body_step_norm >= 0.080
            and self.current_elbow_body_step_norm >= 0.040
        )
        acquisition_quarantine = bool(
            (not self.active)
            and now < self.ambiguous_acquisition_until
            and self.ambiguous_acquisition_sign != 0
            and raw * self.ambiguous_acquisition_sign > 0.0
            and (not strong_quarantine_override)
        )

        if now >= self.motion_intent_until:
            self.motion_intent_sign = 0
        motion_grace = now < self.motion_intent_until and self.motion_intent_sign != 0 and raw * self.motion_intent_sign > 0.0

        position_sign = 1 if raw > 0.0 else (-1 if raw < 0.0 else 0)
        position_far = (
            (not scale_veto)
            and (not kinematic_reject)
            and position_sign != 0
            and abs(raw) >= 1.25 * enter_deadzone
        )
        if position_far:
            if self.position_evidence_sign != position_sign:
                self.position_evidence_sign = position_sign
                self.position_evidence_started_at = now
                self.position_evidence_frames = 1
                self.position_evidence_start_abs_raw = abs_raw
            else:
                self.position_evidence_frames = min(20, self.position_evidence_frames + 1)
                if self.position_evidence_started_at is None:
                    self.position_evidence_started_at = now
                    self.position_evidence_start_abs_raw = abs_raw
        else:
            self.position_evidence_started_at = None
            self.position_evidence_sign = 0
            self.position_evidence_frames = 0
            self.position_evidence_start_abs_raw = 0.0
        position_evidence_s = 0.0 if self.position_evidence_started_at is None else max(0.0, now - self.position_evidence_started_at)
        position_progress = max(0.0, abs_raw - self.position_evidence_start_abs_raw) if self.position_evidence_frames else 0.0

        # v64 candidate bridge.  First far observation only records a candidate.
        # A later same-side far observation may use it if the intervening gap is
        # very short and the new displacement is decisively farther out.
        gap_candidate_reentry = False
        if now >= self.gap_candidate_until:
            self.gap_candidate_until = 0.0
            self.gap_candidate_sign = 0
            self.gap_candidate_abs_raw = 0.0
            self.gap_candidate_bad_frames = 0
        if position_far:
            if (
                self.gap_candidate_sign == position_sign
                and now < self.gap_candidate_until
                and 1 <= self.gap_candidate_bad_frames <= 2
                and (
                    abs_raw >= self.gap_candidate_abs_raw + 0.050
                    or abs_raw >= 1.62 * enter_deadzone
                )
            ):
                gap_candidate_reentry = True
            # Refresh candidate only from a physically observed far frame.
            self.gap_candidate_until = now + 0.090
            self.gap_candidate_sign = position_sign
            self.gap_candidate_abs_raw = max(self.gap_candidate_abs_raw if self.gap_candidate_sign == position_sign else 0.0, abs_raw)
            self.gap_candidate_bad_frames = 0
        elif now < self.gap_candidate_until:
            self.gap_candidate_bad_frames += 1
            if self.gap_candidate_bad_frames > 2:
                self.gap_candidate_until = 0.0
                self.gap_candidate_sign = 0
                self.gap_candidate_abs_raw = 0.0

        sustained_position = bool(self.position_evidence_frames >= 3 and position_evidence_s >= 0.055)
        # Two accelerated forms remain stricter than the generic sustained path:
        # (1) at low FPS, two far frames can enter if independent long-arm
        # evidence is already confirmed and at least 40 ms have elapsed;
        # (2) normal-speed motion can enter after two far frames when magnitude
        # is still progressing outward by >=0.06 raw within >=20 ms.  Adversarial
        # static/deformation scans showed no such windows, while these recover
        # several normal/zoom-action tails.
        position_longarm_accel = bool(
            self.position_evidence_frames >= 2
            and position_evidence_s >= 0.040
            and bridge_entry_ready
            and long_arm_confirmed
        )
        position_progress_accel = bool(
            self.position_evidence_frames >= 2
            and position_evidence_s >= 0.020
            and position_progress >= 0.060
        )

        if short_reseed and abs_raw > release_deadzone:
            self.active = True
            self.intent_confirm_frames = 0
        elif self.active:
            # Entry evidence is only meaningful while inactive.
            self.long_arm_evidence_s = 0.0
            self.long_arm_evidence_sign = 0
            if abs_raw <= release_deadzone:
                same_verified_side = (
                    now < self.verified_active_until
                    and self.verified_active_sign != 0
                    and raw * self.verified_active_sign > 0.0
                )
                shallow_dip = abs_raw >= 0.72 * release_deadzone
                if same_verified_side and shallow_dip:
                    # Keep the internal ACTIVE state through a brief noisy dip.
                    # The output target below is still zero while raw is inside
                    # release_deadzone, so this does not create visible drift; it
                    # only avoids forcing a proven slow gesture to re-authenticate.
                    if self.release_grace_started_at is None:
                        self.release_grace_started_at = now
                    if now - self.release_grace_started_at > self._VERIFIED_ACTIVE_RELEASE_GRACE_SECONDS:
                        self.active = False
                        self.intent_confirm_frames = 0
                        self.release_grace_started_at = None
                else:
                    self.active = False
                    self.intent_confirm_frames = 0
                    self.release_grace_started_at = None
            else:
                self.release_grace_started_at = None
        else:
            strong_enter = max(enter_deadzone * self._STRONG_ENTER_MULT, enter_deadzone + 0.035)
            decisive_enter = max(enter_deadzone * 2.0, enter_deadzone + 0.115)
            proof_floor = max(base_deadzone * 1.08, release_deadzone * 0.82)
            proof_immediate = max(strong_enter, base_deadzone * 1.75)
            if kinematic_reject:
                self.intent_confirm_frames = 0
            elif acquisition_quarantine:
                # Ambiguous two-frame acquisition is observationally too close
                # to correlated pose drift.  Hold output at zero for this short
                # interval unless strong_quarantine_override has already made
                # acquisition_quarantine false.
                self.intent_confirm_frames = 0
            elif dropout_first_good_gate:
                # The gap erased one observation, not the physical gesture.
                # Current and pre-gap geometry independently agree on a strong
                # same-direction outward move, and the long window also shows
                # wrist-vs-body plus wrist-vs-elbow change.
                self.active = True
                self.intent_confirm_frames = 0
                self.verified_active_until = now + self._VERIFIED_ACTIVE_RELEASE_GRACE_SECONDS
                self.verified_active_sign = 1 if raw > 0.0 else -1
            elif motion_grace and abs_raw >= strong_enter:
                # A verified motion frame may survive one quarantined landmark
                # jump.  If the next accepted frame continues on the same side
                # with strong displacement, keep that intent instead of making
                # the controller relearn from zero.
                self.active = True
                self.intent_confirm_frames = 0
                self.verified_active_until = now + self._VERIFIED_ACTIVE_RELEASE_GRACE_SECONDS
                self.verified_active_sign = 1 if raw > 0.0 else -1
            elif motion_proof and abs_raw >= proof_floor and (self.motion_proof_streak >= 2 or abs_raw >= proof_immediate):
                # A single correlated pose excursion can satisfy the old
                # one-frame wrist+elbow proof.  Moderate displacement therefore
                # needs two consecutive independent proofs; clearly large real
                # motion may still enter immediately.
                self.active = True
                self.intent_confirm_frames = 0
                self.verified_active_until = now + self._VERIFIED_ACTIVE_RELEASE_GRACE_SECONDS
                self.verified_active_sign = 1 if raw > 0.0 else -1
            elif (
                bridge_entry_ready
                and (not scale_veto)
                and (not slow_body_deformation_veto)
                and long_arm_quality_confirmed
                and long_arm_disp_norm >= 0.150
                and hq_direction_stable
                and abs_raw >= enter_deadzone * 1.03
            ):
                # High-quality slow-arm geometry already contains long-window
                # trend, speed and wrist-vs-elbow internal-motion checks.  Once
                # it also clears the user's own adaptive safety deadzone and
                # has accumulated at least 15% of authored travel over the long
                # window, do not wait for the older long_arm predicate to certify
                # elbow/shoulder motion.  This is deliberately *not* a sub-
                # deadzone bypass.
                self.active = True
                self.intent_confirm_frames = 0
                self.verified_active_until = now + self._VERIFIED_ACTIVE_RELEASE_GRACE_SECONDS
                self.verified_active_sign = 1 if raw > 0.0 else -1
                self.long_arm_evidence_s = 0.0
                self.long_arm_evidence_sign = 0
                self.long_arm_quality_streak = 0
            elif bridge_entry_ready and (not slow_body_deformation_veto) and long_arm_confirmed and long_arm_quality_confirmed and abs_raw >= base_deadzone * 0.78:
                # Only sustained, internally coherent arm motion may bypass the
                # adaptive deadzone.  Two high-quality windows are required.
                self.active = True
                self.intent_confirm_frames = 0
                self.long_arm_evidence_s = 0.0
                self.long_arm_evidence_sign = 0
                self.long_arm_quality_streak = 0
                self.verified_active_until = now + self._VERIFIED_ACTIVE_RELEASE_GRACE_SECONDS
                self.verified_active_sign = 1 if raw > 0.0 else -1
            elif (
                bridge_entry_ready
                and (not scale_veto)
                and (not kinematic_reject)
                and long_arm_intent
                and (not long_arm_confirmed)
                and abs_raw >= 1.40 * enter_deadzone
                and long_arm_r2 >= 0.40
                and long_arm_disp_norm >= 0.100
                and long_arm_speed_norm >= 0.25
                and long_arm_forearm_norm >= 0.080
            ):
                # Strong-internal-motion pre-confirm gate.  This targets cases
                # where the long-arm boolean has already flipped, but the
                # sequential timer is one frame short.  Requiring substantial
                # wrist-vs-elbow change distinguishes the target from the
                # body-dominant v71 path and from correlated whole-body sway.
                self.active = True
                self.intent_confirm_frames = 0
                self.verified_active_until = now + self._VERIFIED_ACTIVE_RELEASE_GRACE_SECONDS
                self.verified_active_sign = 1 if raw > 0.0 else -1
            elif (
                bridge_entry_ready
                and (not scale_veto)
                and (not kinematic_reject)
                and (not long_arm_intent)
                and abs_raw >= 1.45 * enter_deadzone
                and long_arm_r2 >= 0.50
                and long_arm_disp_norm >= 0.140
                and long_arm_speed_norm >= 0.35
                and long_arm_forearm_norm >= 0.040
                and long_arm_disp_norm >= 2.20 * max(1e-6, long_arm_forearm_norm)
            ):
                # Pre-long-arm normal-speed gate.  At high FPS the arm geometry
                # can already be strongly coherent one frame before the legacy
                # long_arm boolean flips.  Require large displacement, strong
                # temporal fit, sufficient speed, and body-dominant arm motion;
                # adversarial non-control scans showed zero matches.
                self.active = True
                self.intent_confirm_frames = 0
                self.verified_active_until = now + self._VERIFIED_ACTIVE_RELEASE_GRACE_SECONDS
                self.verified_active_sign = 1 if raw > 0.0 else -1
            elif (
                bridge_entry_ready
                and (not scale_veto)
                and (not kinematic_reject)
                and long_arm_confirmed
                and (not slow_body_deformation_veto)
                and abs_raw >= 1.08 * enter_deadzone
                and long_arm_r2 >= 0.82
                and long_arm_disp_norm >= 0.070
                and long_arm_forearm_norm <= 0.015
            ):
                # Whole-arm slow-motion narrow gate.  Some users elevate the
                # wrist and elbow together, so wrist-vs-elbow internal change
                # remains tiny even though elbow-vs-shoulder motion already
                # confirmed a coherent arm lift.  This branch is only for that
                # geometry and stays above the personal safety deadzone.
                self.active = True
                self.intent_confirm_frames = 0
                self.verified_active_until = now + self._VERIFIED_ACTIVE_RELEASE_GRACE_SECONDS
                self.verified_active_sign = 1 if raw > 0.0 else -1
            elif (
                False  # v114: remove non-generalizing early_normal bypass
                and bridge_entry_ready
                and (not scale_veto)
                and (not kinematic_reject)
                and long_arm_intent
                and (not long_arm_confirmed)
                and abs_raw >= 1.18 * enter_deadzone
                and long_arm_disp_norm >= 0.120
                and long_arm_forearm_norm >= 0.050
                and long_arm_speed_norm >= 0.30
                and long_arm_r2 >= 0.30
                and long_arm_disp_norm >= 1.70 * max(1e-6, long_arm_forearm_norm)
            ):
                # Early normal-speed gate for the "all geometry is already
                # convincing, sequential long-arm timer is one frame short"
                # tail.  The wrist-vs-body displacement must dominate internal
                # wrist-vs-elbow change, rejecting the difficult correlated
                # body-sway window that otherwise matches the scalar thresholds.
                self.active = True
                self.intent_confirm_frames = 0
                self.verified_active_until = now + self._VERIFIED_ACTIVE_RELEASE_GRACE_SECONDS
                self.verified_active_sign = 1 if raw > 0.0 else -1
            elif (
                bridge_entry_ready
                and (not scale_veto)
                and long_arm_confirmed
                and (not slow_body_deformation_veto)
                and abs_raw >= enter_deadzone
                and long_arm_disp_norm >= 0.140
                and long_arm_forearm_norm >= 0.075
            ):
                # High-noise slow-arm narrow gate.  The ordinary long-arm path
                # intentionally requires 1.35x the personal safety deadzone,
                # which delays some genuine slow gestures.  Here we allow entry
                # as soon as the deadzone is genuinely crossed only when the
                # long window shows both substantial wrist-vs-body progress and
                # internal wrist-vs-elbow geometry change.  Adversarial safety
                # scans over the difficult set produced zero matching windows.
                self.active = True
                self.intent_confirm_frames = 0
                self.long_arm_evidence_s = 0.0
                self.long_arm_evidence_sign = 0
                self.verified_active_until = now + self._VERIFIED_ACTIVE_RELEASE_GRACE_SECONDS
                self.verified_active_sign = 1 if raw > 0.0 else -1
            elif bridge_entry_ready and (not slow_body_deformation_veto) and long_arm_confirmed and abs_raw >= enter_deadzone * 1.35:
                # Ordinary long-arm evidence must clear the personal safety
                # deadzone by a strong margin; near-boundary long-arm evidence is now left to the sustained-position or high-quality paths because isolated body deformation could still create 1.15x–1.33x false excursions. A grazing crossing was the
                # remaining static-tail path in v41/v42.
                self.active = True
                self.intent_confirm_frames = 0
                self.long_arm_evidence_s = 0.0
                self.long_arm_evidence_sign = 0
                self.verified_active_until = now + self._VERIFIED_ACTIVE_RELEASE_GRACE_SECONDS
                self.verified_active_sign = 1 if raw > 0.0 else -1
            elif (not scale_veto) and (not elbow_reliable) and trend_intent and abs_raw >= base_deadzone * 0.82:
                # Short single-signal trend is only a compatibility fallback
                # when the elbow itself is unavailable.  With a full skeleton
                # it no longer has authority to open the control gate.
                self.active = True
                self.intent_confirm_frames = 0
            elif slow_hq_step_reentry and (not slow_body_deformation_veto):
                # Narrow 20-FPS slow-motion rescue.  It needs a recent
                # high-quality multi-joint slow-arm witness, a same-side prior
                # frame already near the personal safety boundary, and a large
                # outward step into clearly-far territory.  No global deadzone
                # is reduced.
                self.active = True
                self.intent_confirm_frames = 0
                self.verified_active_until = now + self._VERIFIED_ACTIVE_RELEASE_GRACE_SECONDS
                self.verified_active_sign = raw_sign
            elif gap_candidate_reentry and (not scale_veto) and (not kinematic_reject):
                # The first far observation was only a candidate.  Re-entry is
                # granted after the signal reappears on the same side and is
                # materially farther out; this repairs "one collapsed pose
                # frame erased the whole motion" without lowering deadzones.
                self.active = True
                self.intent_confirm_frames = 0
                self.verified_active_until = now + self._VERIFIED_ACTIVE_RELEASE_GRACE_SECONDS
                self.verified_active_sign = position_sign
                self.gap_candidate_until = 0.0
                self.gap_candidate_sign = 0
                self.gap_candidate_abs_raw = 0.0
                self.gap_candidate_bad_frames = 0
            elif position_longarm_accel or position_progress_accel:
                # A strictly qualified acceleration path: never crosses tracking
                # gaps, never runs during scale veto/kinematic quarantine (those
                # are prerequisites of position_far), and needs either separate
                # long-arm confirmation or strong outward progression.
                self.active = True
                self.intent_confirm_frames = 0
                self.verified_active_until = now + self._VERIFIED_ACTIVE_RELEASE_GRACE_SECONDS
                self.verified_active_sign = position_sign
            elif sustained_position:
                # Fallback for genuine slow/forearm-dominant motion whose elbow
                # witness is weak: position alone may certify intent only after
                # staying well beyond the personal safety deadzone, on the same
                # side, for a minimum real-time duration.  Camera-scale veto and
                # the kinematic outlier quarantine both suppress this path.
                self.active = True
                self.intent_confirm_frames = 0
                self.verified_active_until = now + self._VERIFIED_ACTIVE_RELEASE_GRACE_SECONDS
                self.verified_active_sign = position_sign
            elif (not scale_veto) and abs_raw >= decisive_enter:
                # Position alone is not allowed to open the near-threshold
                # gate, but a *decisive* displacement persisting for two frames
                # is retained as a last-resort path.  This rescues forearm-only
                # control and very slow motion when elbow velocity evidence is
                # weak, while staying far above the correlated 0.10-0.18 raw
                # excursions that caused v19's static tail.
                self.intent_confirm_frames += 1
                if self.intent_confirm_frames >= 2:
                    self.active = True
                    self.intent_confirm_frames = 0
            elif (not scale_veto) and (not elbow_reliable) and abs_raw >= strong_enter:
                self.intent_confirm_frames += 1
                if self.intent_confirm_frames >= self._ENTER_CONFIRM_FRAMES:
                    self.active = True
                    self.intent_confirm_frames = 0
            else:
                self.intent_confirm_frames = 0

        if self.active and (long_arm_intent or long_arm_high_quality or motion_proof):
            sign_now = 1 if raw > 0.0 else (-1 if raw < 0.0 else 0)
            if sign_now != 0:
                self.verified_active_until = max(self.verified_active_until, now + self._VERIFIED_ACTIVE_RELEASE_GRACE_SECONDS)
                self.verified_active_sign = sign_now

        if self.active:
            t = _clamp((abs_raw - release_deadzone) / max(1e-6, 1.0 - release_deadzone), 0.0, 1.0)
            target = math.copysign(t ** 1.10, raw)
        else:
            target = 0.0

        if self.last_rel_y is not None and not self.active and abs_raw <= enter_deadzone * 0.72:
            delta = abs(corrected_rel - self.last_rel_y)
            if delta <= max(0.0045, 4.5 * self.noise_sigma):
                observed = _clamp(delta / 0.95, 0.0005, 0.012)
                self.noise_sigma = _clamp(0.987 * self.noise_sigma + 0.013 * observed, 0.0007, 0.012)

        dt = max(1.0 / 120.0, min(0.10, now - (self.filter_last_at or now)))
        self.filter_last_at = now
        moving_outward = abs(target) > abs(self.filtered) + 0.01
        tau = self._FILTER_TAU_ATTACK if moving_outward else self._FILTER_TAU_RELEASE
        alpha = 1.0 - math.exp(-dt / tau)
        candidate = self.filtered + alpha * (target - self.filtered)
        max_step = self._MAX_STEP_PER_SECOND * dt
        self.filtered += _clamp(candidate - self.filtered, -max_step, max_step)
        if target == 0.0 and abs(self.filtered) < 0.028:
            self.filtered = 0.0
        self.filtered = _clamp(self.filtered, -1.0, 1.0)
        self.last_rel_y = corrected_rel
        self.prev_entry_raw = raw
        self.last_enter_deadzone = enter_deadzone

        return self._snapshot(
            self.filtered,
            reference_y=reference_y,
            raw=raw,
            adaptive_deadzone=enter_deadzone,
            release_deadzone=release_deadzone,
            travel=travel,
            trend_intent=trend_intent,
            scale_mode=scale_mode,
            effective_scale_ratio=effective_scale_ratio,
            short_reseed=short_reseed,
            kinematic_reject=kinematic_reject,
            motion_proof=motion_proof,
            proof_streak_qualified=proof_streak_qualified,
            ambiguous_acquisition_trigger=ambiguous_acquisition_trigger,
            acquisition_quarantine=acquisition_quarantine,
            strong_quarantine_override=strong_quarantine_override,
            motion_grace=motion_grace,
            long_arm_intent=long_arm_intent,
            long_arm_confirmed=long_arm_confirmed,
            hq_direction_stable=hq_direction_stable,
            same_side_fraction_hq=same_side_fraction_hq,
            same_side_duration_hq=same_side_duration_hq,
            long_arm_high_quality=long_arm_high_quality,
            long_arm_r2=long_arm_r2,
            long_arm_disp_norm=long_arm_disp_norm,
            long_arm_speed_norm=long_arm_speed_norm,
            long_arm_forearm_norm=long_arm_forearm_norm,
            long_arm_quality_confirmed=long_arm_quality_confirmed,
            scale_motion_pressure=scale_motion_pressure,
            scale_pressure_strength=scale_pressure_strength,
            confirmed_scale_motion=confirmed_scale_motion,
            scale_context_far=scale_context_far,
            scale_veto=scale_veto,
            fused_reference=fused_reference,
            gap_candidate_reentry=gap_candidate_reentry,
            slow_hq_step_reentry=slow_hq_step_reentry,
            slow_hq_prior_witness_ok=slow_hq_prior_witness_ok,
            prior_hq_hits=prior_hq_hits,
            bridge_entry_ready=bridge_entry_ready,
            dropout_first_good_gate=dropout_first_good_gate,
        )
