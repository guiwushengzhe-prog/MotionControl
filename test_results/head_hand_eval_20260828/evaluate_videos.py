"""Offline, no-op evaluation of the supplied real videos.

This file intentionally lives under test_results.  It does not import the
server, does not touch user profiles/settings, and never calls an output
backend.  MediaPipe is run once per video; the resulting 33-point stream is
then fed to the two existing HeadController algorithms with the same frames
and calibration window.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from head_control import HeadController, HeadPoseEstimator  # noqa: E402
from control_kernel import MP_NAMES  # noqa: E402


FACE_NAMES = set(HeadPoseEstimator.required_points("pnp"))
ARM_POINTS = {
    "left": ("left_shoulder", "left_elbow", "left_wrist"),
    "right": ("right_shoulder", "right_elbow", "right_wrist"),
}


def finite(value: Any, default: float = math.nan) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def percentile(values: list[float], q: float) -> float | None:
    values = sorted(v for v in values if math.isfinite(v))
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    position = max(0.0, min(1.0, q)) * (len(values) - 1)
    lo, hi = int(position), math.ceil(position)
    if lo == hi:
        return values[lo]
    return values[lo] + (values[hi] - values[lo]) * (position - lo)


def pose_map(landmarks: list[Any] | None) -> dict[str, dict] | None:
    if not landmarks or len(landmarks) < len(MP_NAMES):
        return None
    result: dict[str, dict] = {}
    for name, point in zip(MP_NAMES, landmarks):
        result[name] = {
            "x": finite(getattr(point, "x", None), 0.0),
            "y": finite(getattr(point, "y", None), 0.0),
            "z": finite(getattr(point, "z", None), 0.0),
            "score": finite(getattr(point, "visibility", getattr(point, "presence", 1.0)), 1.0),
        }
    return result


def extract_video(video: Path, model: Path) -> tuple[dict, list[dict]]:
    import cv2
    import mediapipe as mp
    from mediapipe.tasks import python
    from mediapipe.tasks.python import vision

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {video}")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    options = vision.PoseLandmarkerOptions(
        base_options=python.BaseOptions(model_asset_path=str(model)),
        running_mode=vision.RunningMode.VIDEO,
        num_poses=1,
        min_pose_detection_confidence=0.35,
        min_pose_presence_confidence=0.35,
        min_tracking_confidence=0.35,
    )
    detector = vision.PoseLandmarker.create_from_options(options)
    frames: list[dict] = []
    invalid = 0
    index = 0
    last_ts = -1
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            t = index / max(fps, 1e-6)
            index += 1
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            timestamp = max(last_ts + 1, int(round(t * 1000.0)))
            last_ts = timestamp
            result = detector.detect_for_video(image, timestamp)
            landmarks = result.pose_landmarks[0] if result.pose_landmarks else None
            pose = pose_map(landmarks)
            if pose is None:
                invalid += 1
            frames.append({"index": index - 1, "t": round(t, 6), "pose": pose})
    finally:
        cap.release()
        detector.close()
    manifest = {
        "video": str(video.resolve()),
        "model": str(model.resolve()),
        "fps": fps,
        "width": width,
        "height": height,
        "declared_frame_count": frame_count,
        "decoded_frame_count": len(frames),
        "duration_s": len(frames) / max(fps, 1e-6),
        "invalid_pose_frames": invalid,
        "pose_valid_rate": (len(frames) - invalid) / max(1, len(frames)),
        "landmark_count": len(MP_NAMES),
        "hand_landmarks_present": False,
        "notes": "Pose Landmarker stream only; no 21-point hand model was used.",
    }
    return manifest, frames


def raw_estimates(frames: list[dict], algorithm: str) -> list[dict]:
    estimator = HeadPoseEstimator()
    values = []
    for row in frames:
        estimate = estimator.estimate(row["pose"], 720, 1280, algorithm)
        values.append({
            "t": row["t"],
            "valid": bool(estimate.valid),
            "yaw": estimate.yaw if math.isfinite(estimate.yaw) else None,
            "pitch": estimate.pitch if math.isfinite(estimate.pitch) else None,
            "confidence": estimate.confidence,
            "error": estimate.error,
        })
    return values


def landmark_coverage(frames: list[dict]) -> dict:
    """Coverage of actual pose points used by the tested estimators.

    Scores are the visibility/presence values emitted by MediaPipe Pose.  This
    deliberately reports only observed pose landmarks; no hand-21 inference is
    implied by the wrist/finger-named pose points.
    """
    names = sorted(FACE_NAMES | {point for points in ARM_POINTS.values() for point in points})
    total = max(1, len(frames))
    point_rates = {}
    for name in names:
        good = 0
        for frame in frames:
            point = (frame.get("pose") or {}).get(name)
            if isinstance(point, dict) and finite(point.get("score"), 0.0) >= 0.40:
                good += 1
        point_rates[name] = good / total
    pairs = {
        "outer_eye_pair": ("left_eye_outer", "right_eye_outer"),
        "mouth_pair": ("mouth_left", "mouth_right"),
        "ear_pair": ("left_ear", "right_ear"),
        "pnp_required_set": tuple(HeadPoseEstimator.required_points("pnp")),
        "ratio_required_set": tuple(HeadPoseEstimator.required_points("ratio")),
        "arm_left_set": ARM_POINTS["left"],
        "arm_right_set": ARM_POINTS["right"],
    }
    pair_rates = {}
    for label, pair in pairs.items():
        good = 0
        for frame in frames:
            pose = frame.get("pose") or {}
            if all(isinstance(pose.get(name), dict) and finite(pose[name].get("score"), 0.0) >= 0.40 for name in pair):
                good += 1
        pair_rates[label] = good / total
    return {"threshold": 0.40, "point_rates": point_rates, "set_rates": pair_rates}


def stable_window(frames: list[dict], window_s: float = 6.0) -> dict:
    """Find one shared low-motion window for both estimators.

    This is a selection aid, not an action label.  We prefer the earliest
    window with high face coverage and low normalized robust spread, then
    inspect/report the chosen interval rather than hiding the choice.
    """
    raw = {algorithm: raw_estimates(frames, algorithm) for algorithm in ("pnp", "ratio")}
    duration = frames[-1]["t"] if frames else 0.0
    candidates = []
    start = 0.0
    while start + window_s <= duration + 1e-6:
        selected = [i for i, row in enumerate(frames) if start <= row["t"] < start + window_s]
        scores = []
        coverage = []
        for algorithm in ("pnp", "ratio"):
            vals = raw[algorithm]
            yaw = [finite(vals[i]["yaw"]) for i in selected if vals[i]["valid"]]
            pitch = [finite(vals[i]["pitch"]) for i in selected if vals[i]["valid"]]
            coverage.append(len(yaw) / max(1, len(selected)))
            if len(yaw) < 5 or len(pitch) < 5:
                scores.append(99.0)
                continue
            yaw_sigma = statistics.pstdev(yaw)
            pitch_sigma = statistics.pstdev(pitch)
            scale_yaw = 18.0 if algorithm == "pnp" else 0.34
            scale_pitch = 12.0 if algorithm == "pnp" else 0.16
            scores.append(yaw_sigma / scale_yaw + pitch_sigma / scale_pitch)
        score = sum(scores)
        if min(coverage, default=0.0) >= 0.90:
            candidates.append({"start_s": round(start, 3), "duration_s": window_s, "score": score, "coverage": coverage})
        start += 0.5
    if not candidates:
        raise RuntimeError("no shared calibration window with >=90% face coverage")
    candidates.sort(key=lambda item: (item["score"], item["start_s"]))
    best_score = candidates[0]["score"]
    # Prefer an earlier, visually inspectable neutral window when its spread is
    # close to the absolute minimum.  This leaves more labelled/unlabelled
    # motion after calibration while keeping both algorithms on one window.
    near_best = [item for item in candidates if item["score"] <= best_score * 1.15]
    chosen = min(near_best or candidates, key=lambda item: item["start_s"])
    return {
        "chosen": chosen,
        "selection_policy": "earliest_window_within_15pct_of_min_spread",
        "minimum_score": best_score,
        "candidates": candidates[:12],
        "window_s": window_s,
    }


def run_controller(frames: list[dict], algorithm: str, calibration_start: float) -> tuple[dict, list[dict]]:
    controller = HeadController(None)
    controller.configure(algorithm=algorithm, enabled=True, invert_x=False, invert_y=False)
    base = 1000.0
    started = False
    rows = []
    for frame in frames:
        t = float(frame["t"])
        now = base + t
        if not started and t >= calibration_start:
            controller.start_center(now, kind="video_evaluation")
            started = True
        controller.update(frame["pose"], 720, 1280, now)
        status = controller.status(now)
        rows.append({
            "t": round(t, 6),
            "valid": bool(controller.raw.valid),
            "yaw": controller.raw.yaw if controller.raw.valid else None,
            "pitch": controller.raw.pitch if controller.raw.valid else None,
            "confidence": controller.raw.confidence if controller.raw.valid else 0.0,
            "signal_yaw": controller.signal_yaw if math.isfinite(controller.signal_yaw) else None,
            "signal_pitch": controller.signal_pitch if math.isfinite(controller.signal_pitch) else None,
            "norm_x": status.get("normalized_x"),
            "norm_y": status.get("normalized_y"),
            "output_x": status.get("output_x", 0.0),
            "output_y": status.get("output_y", 0.0),
            "yaw_state": status.get("yaw_intent_state"),
            "pitch_state": status.get("pitch_intent_state"),
            "calibrating": bool(status.get("calibrating")),
            "calibrated": bool(status.get("calibrated")),
            "center_yaw": status.get("center_yaw"),
            "center_pitch": status.get("center_pitch"),
            "noise_yaw": status.get("noise_yaw"),
            "noise_pitch": status.get("noise_pitch"),
            "effective_deadzone_x": status.get("effective_deadzone_x"),
            "effective_deadzone_y": status.get("effective_deadzone_y"),
        })
    final = controller.status(base + (frames[-1]["t"] if frames else 0.0))
    return final, rows


def runs(indices: list[int]) -> list[int]:
    lengths = []
    current = 0
    previous = None
    for index in indices:
        if previous is None or index == previous + 1:
            current += 1
        else:
            lengths.append(current)
            current = 1
        previous = index
    if current:
        lengths.append(current)
    return lengths


def controller_metrics(rows: list[dict], calibration_start: float) -> dict:
    valid = [row for row in rows if row["valid"]]
    calibrated_at = next((row["t"] for row in rows if row["calibrated"]), None)
    cal_rows = [row for row in rows if calibration_start <= row["t"] < calibration_start + 6.0 and row["yaw"] is not None]
    post = [row for row in rows if calibrated_at is not None and row["t"] >= calibrated_at]
    nonzero_x = [row for row in post if abs(finite(row["output_x"], 0.0)) >= 0.01]
    nonzero_y = [row for row in post if abs(finite(row["output_y"], 0.0)) >= 0.01]
    x_sign_agree = [row for row in post if abs(finite(row["norm_x"], 0.0)) >= 0.05 and abs(finite(row["output_x"], 0.0)) >= 0.01]
    y_sign_agree = [row for row in post if abs(finite(row["norm_y"], 0.0)) >= 0.05 and abs(finite(row["output_y"], 0.0)) >= 0.01]
    x_sign_ok = sum((finite(row["norm_x"], 0.0) * finite(row["output_x"], 0.0)) >= 0 for row in x_sign_agree)
    y_sign_ok = sum((finite(row["norm_y"], 0.0) * finite(row["output_y"], 0.0)) >= 0 for row in y_sign_agree)
    neutral = [abs(finite(row["output_x"], 0.0)) for row in post if abs(finite(row["norm_x"], 0.0)) < 0.05]
    # This is a controller-only frame lag proxy, not human/transport latency:
    # measure the first non-zero output after a filtered normalized signal
    # crosses the intent threshold.  No ground-truth action timestamps exist
    # in the supplied clips.
    lag_x = []
    lag_y = []
    for axis, lags in (("x", lag_x), ("y", lag_y)):
        norm_key, output_key = f"norm_{axis}", f"output_{axis}"
        for index, row in enumerate(post):
            previous = post[index - 1] if index else None
            crossed = abs(finite(row[norm_key], 0.0)) >= 0.05 and (
                previous is None or abs(finite(previous[norm_key], 0.0)) < 0.05
            )
            if not crossed:
                continue
            sign = 1 if finite(row[norm_key], 0.0) >= 0 else -1
            for following in post[index:index + 60]:
                out = finite(following[output_key], 0.0)
                if abs(out) >= 0.01 and (out * sign) > 0:
                    lags.append(max(0.0, (float(following["t"]) - float(row["t"])) * 1000.0))
                    break
    return_norm = []
    return_output = []
    reversal_overshoot = 0
    output_steps = []
    for index, row in enumerate(post):
        previous = post[index - 1] if index else None
        if previous is not None:
            dx = finite(row["output_x"], 0.0) - finite(previous["output_x"], 0.0)
            dy = finite(row["output_y"], 0.0) - finite(previous["output_y"], 0.0)
            output_steps.append(max(abs(dx), abs(dy)))
        for axis in ("x", "y"):
            norm_key, output_key = f"norm_{axis}", f"output_{axis}"
            prev_norm = finite(previous[norm_key], 0.0) if previous is not None else 0.0
            norm = finite(row[norm_key], 0.0)
            out = finite(row[output_key], 0.0)
            if abs(prev_norm) >= 0.20 and abs(norm) <= 0.05:
                return_norm.append(abs(norm))
                return_output.append(abs(out))
            if previous is not None and prev_norm * norm < 0 and abs(norm) >= 0.05 and abs(out) >= 0.01:
                if out * prev_norm > 0:
                    reversal_overshoot += 1
    return {
        "valid_estimate_rate": len(valid) / max(1, len(rows)),
        "valid_frames": len(valid),
        "calibrated_at_s": calibrated_at,
        "calibration_samples": len(cal_rows),
        "calibration_yaw_sigma": statistics.pstdev([finite(r["yaw"]) for r in cal_rows]) if len(cal_rows) > 1 else None,
        "calibration_pitch_sigma": statistics.pstdev([finite(r["pitch"]) for r in cal_rows]) if len(cal_rows) > 1 else None,
        "center_yaw": next((r["center_yaw"] for r in reversed(rows) if r["center_yaw"] is not None), None),
        "center_pitch": next((r["center_pitch"] for r in reversed(rows) if r["center_pitch"] is not None), None),
        "noise_yaw": next((r["noise_yaw"] for r in reversed(rows) if r["noise_yaw"] is not None), None),
        "noise_pitch": next((r["noise_pitch"] for r in reversed(rows) if r["noise_pitch"] is not None), None),
        "effective_deadzone_x": next((r["effective_deadzone_x"] for r in reversed(rows) if r["effective_deadzone_x"] is not None), None),
        "effective_deadzone_y": next((r["effective_deadzone_y"] for r in reversed(rows) if r["effective_deadzone_y"] is not None), None),
        "post_calibrated_frames": len(post),
        "nonzero_x_rate": len(nonzero_x) / max(1, len(post)),
        "nonzero_y_rate": len(nonzero_y) / max(1, len(post)),
        "max_abs_output_x": max((abs(finite(r["output_x"], 0.0)) for r in post), default=0.0),
        "max_abs_output_y": max((abs(finite(r["output_y"], 0.0)) for r in post), default=0.0),
        "neutral_output_x_p95": percentile(neutral, 0.95),
        "response_lag_proxy_x_p50_ms": percentile(lag_x, 0.50),
        "response_lag_proxy_x_p95_ms": percentile(lag_x, 0.95),
        "response_lag_proxy_y_p50_ms": percentile(lag_y, 0.50),
        "response_lag_proxy_y_p95_ms": percentile(lag_y, 0.95),
        "response_lag_proxy_sample_count_x": len(lag_x),
        "response_lag_proxy_sample_count_y": len(lag_y),
        "return_to_center_event_count": len(return_norm),
        "return_to_center_residual_norm_p95": percentile(return_norm, 0.95),
        "return_to_center_residual_output_p95": percentile(return_output, 0.95),
        "output_step_abs_p95": percentile(output_steps, 0.95),
        "output_step_abs_max": max(output_steps, default=0.0),
        "sign_reversal_overshoot_proxy_count": reversal_overshoot,
        "x_output_sign_consistency": x_sign_ok / max(1, len(x_sign_agree)),
        "y_output_sign_consistency": y_sign_ok / max(1, len(y_sign_agree)),
        "x_sign_samples": len(x_sign_agree),
        "y_sign_samples": len(y_sign_agree),
        "invalid_runs_max_frames": max(runs([i for i, row in enumerate(rows) if not row["valid"]]), default=0),
        "yaw_states": {state: sum(row["yaw_state"] == state for row in post) for state in ("IDLE", "TURN_LEFT", "TURN_RIGHT", "HOLD", "RETURNING")},
        "pitch_states": {state: sum(row["pitch_state"] == state for row in post) for state in ("IDLE", "TURN_LEFT", "TURN_RIGHT", "HOLD", "RETURNING")},
    }


def hand_metrics(frames: list[dict]) -> tuple[dict, list[dict]]:
    rows = []
    summary = {}
    for side, (shoulder_name, elbow_name, wrist_name) in ARM_POINTS.items():
        wrist_valid = []
        group_valid = []
        occluded = []
        angle_valid = []
        for frame in frames:
            pose = frame["pose"] or {}
            shoulder, elbow, wrist = (pose.get(name) for name in (shoulder_name, elbow_name, wrist_name))
            scores = [finite(point.get("score"), 0.0) if isinstance(point, dict) else 0.0 for point in (shoulder, elbow, wrist)]
            w_ok = scores[2] >= 0.40
            g_ok = sum(score >= 0.40 for score in scores) >= 2
            occl = not w_ok and scores[0] >= 0.40 and scores[1] >= 0.40
            if w_ok:
                wrist_valid.append(frame["index"])
            if g_ok:
                group_valid.append(frame["index"])
            if occl:
                occluded.append(frame["index"])
            angle = None
            if all(isinstance(point, dict) and score >= 0.40 for point, score in zip((shoulder, elbow, wrist), scores)):
                ax, ay = finite(shoulder["x"]), finite(shoulder["y"])
                bx, by = finite(elbow["x"]), finite(elbow["y"])
                cx, cy = finite(wrist["x"]), finite(wrist["y"])
                u = (ax - bx, ay - by)
                v = (cx - bx, cy - by)
                denom = math.hypot(*u) * math.hypot(*v)
                if denom > 1e-9:
                    angle = math.degrees(math.acos(max(-1.0, min(1.0, (u[0] * v[0] + u[1] * v[1]) / denom))))
                    angle_valid.append(frame["index"])
            rows.append({"t": frame["t"], "side": side, "wrist_valid": w_ok, "group_valid": g_ok, "wrist_occluded_with_arm": occl, "elbow_angle_deg": angle})
        summary[side] = {
            "wrist_valid_rate": len(wrist_valid) / max(1, len(frames)),
            "shoulder_elbow_group_valid_rate": len(group_valid) / max(1, len(frames)),
            "wrist_occluded_but_shoulder_elbow_valid_rate": len(occluded) / max(1, len(frames)),
            "wrist_invalid_max_run_frames": max(runs([i for i in range(len(frames)) if i not in wrist_valid]), default=0),
            "group_invalid_max_run_frames": max(runs([i for i in range(len(frames)) if i not in group_valid]), default=0),
            "arm_angle_valid_rate": len(angle_valid) / max(1, len(frames)),
            "note": "group_valid means >=2 of shoulder/elbow/wrist; angle requires all 3. It is a semantic arm-state proxy, not a hand/fist classifier.",
        }
    return summary, rows


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    keys = list(rows[0])
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("videos", nargs=2, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    all_manifest = []
    all_summary = {}
    all_head_rows = []
    all_hand_rows = []
    for video in args.videos:
        manifest, frames = extract_video(video.resolve(), args.model.resolve())
        stable = stable_window(frames, 6.0)
        cal_start = stable["chosen"]["start_s"]
        results = {}
        for algorithm in ("pnp", "ratio"):
            final, rows = run_controller(frames, algorithm, cal_start)
            results[algorithm] = {"final_status": final, "metrics": controller_metrics(rows, cal_start)}
            for row in rows:
                all_head_rows.append({"video": video.name, "algorithm": algorithm, **row})
        hand_summary, hand_rows = hand_metrics(frames)
        for row in hand_rows:
            all_hand_rows.append({"video": video.name, **row})
        manifest["calibration_window"] = stable
        manifest["calibration_start_s"] = cal_start
        manifest["landmark_coverage"] = landmark_coverage(frames)
        manifest["head_results"] = results
        manifest["hand_summary"] = hand_summary
        all_manifest.append(manifest)
        all_summary[video.name] = {
            "calibration_start_s": cal_start,
            "head": {algorithm: results[algorithm]["metrics"] for algorithm in results},
            "hand": hand_summary,
        }
        (args.output / f"{video.stem}.pose_stream.json").write_text(
            json.dumps({"manifest": manifest, "frames": frames}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    (args.output / "video_manifest.json").write_text(json.dumps(all_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output / "head_ab_summary.json").write_text(json.dumps(all_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(args.output / "head_ab_per_frame.csv", all_head_rows)
    write_csv(args.output / "hand_wrist_group_per_frame.csv", all_hand_rows)
    print(json.dumps({"videos": list(all_summary), "output": str(args.output.resolve())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
