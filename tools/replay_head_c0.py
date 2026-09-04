"""Deterministic, output-free v160/v187/C0 replay on frozen Pose33 data.

This harness never opens a camera or an OS input backend.  Calibration samples
come only from the already-frozen timestamp windows.  Controller motion code is
executed unchanged; the harness temporarily prevents its live timer from ending
calibration early, then calls the controller's existing finalizer once at the
end of the fixed window.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import math
import os
import sys
from pathlib import Path
from typing import Any


LANDMARK_NAMES = (
    "nose", "left_eye_inner", "left_eye", "left_eye_outer",
    "right_eye_inner", "right_eye", "right_eye_outer", "left_ear",
    "right_ear", "mouth_left", "mouth_right", "left_shoulder",
    "right_shoulder", "left_elbow", "right_elbow", "left_wrist",
    "right_wrist", "left_pinky", "right_pinky", "left_index",
    "right_index", "left_thumb", "right_thumb", "left_hip", "right_hip",
    "left_knee", "right_knee", "left_ankle", "right_ankle", "left_heel",
    "right_heel", "left_foot_index", "right_foot_index",
)
EPS = 1e-12


def finite_attr(obj: Any, name: str) -> float | None:
    value = getattr(obj, name, math.nan)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def load_module(tag: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(f"head_c0_{tag}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def pose_map(points: Any) -> dict[str, dict] | None:
    if not isinstance(points, list) or len(points) != 33:
        return None
    result: dict[str, dict] = {}
    for index, point in enumerate(points):
        if not isinstance(point, dict):
            return None
        name = str(point.get("name") or LANDMARK_NAMES[index])
        result[name] = {
            key: point[key]
            for key in ("x", "y", "z", "score", "visibility", "presence")
            if key in point
        }
    return result


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            row = json.loads(line)
            if int(row["frame_index"]) != len(rows):
                raise ValueError(f"{path}:{line_number}: non-contiguous frame_index")
            timestamp = row.get("source_pts_time")
            if timestamp is None:
                timestamp = float(row["timestamp_ms"]) / 1000.0
            row["_t"] = float(timestamp)
            rows.append(row)
    if not rows or any(b["_t"] <= a["_t"] for a, b in zip(rows, rows[1:])):
        raise ValueError(f"{path}: timestamps are not strictly increasing")
    return rows


def in_ranges(value: float, ranges: list[list[float]]) -> bool:
    return any(float(start) <= value < float(end) for start, end in ranges)


def replay_one(
    module: Any,
    variant: str,
    recording: str,
    rows: list[dict],
    width: int,
    height: int,
    calibration_ranges: list[list[float]],
    calibration_end_inclusive: bool,
) -> tuple[list[dict], dict]:
    if len(calibration_ranges) != 1:
        raise ValueError(f"{recording}: exactly one calibration range is required")
    cal_start, cal_end = map(float, calibration_ranges[0])
    controller = module.HeadController(None)
    controller.config["enabled"] = True
    controller.config["algorithm"] = "pnp"
    controller.config["invert_x"] = False
    if variant == "product_gesture_v153":
        controller.config["horizontal_algorithm"] = "gesture_v153"
    elif variant == "product_gesture_v188":
        controller.config["horizontal_algorithm"] = "gesture_v188"
    elif variant == "product_frozen22":
        controller.config["horizontal_algorithm"] = "frozen22"
    elif variant == "product_frozen22_v188":
        controller.config["horizontal_algorithm"] = "frozen22_v188"

    original_min_s = module.CENTER_MIN_COLLECTION_S
    original_target = module.CENTER_TARGET_SAMPLES
    calibration_started = False
    calibration_finished = False
    calibration_samples = 0
    output: list[dict] = []

    try:
        # Keep the exact fixed window.  This affects calibration lifecycle only;
        # all estimator, center finalization, and motion code remain the module's.
        module.CENTER_MIN_COLLECTION_S = math.inf
        module.CENTER_TARGET_SAMPLES = sys.maxsize
        for row in rows:
            now = float(row["_t"])
            if not calibration_started and now >= cal_start:
                controller.start_center(now=cal_start - float(module.CENTER_PREPARE_S), kind="offline-fixed-window")
                controller.center_deadline = cal_end + 1.0
                calibration_started = True
            past_calibration = now > cal_end if calibration_end_inclusive else now >= cal_end
            if calibration_started and not calibration_finished and past_calibration:
                calibration_samples = len(controller.center_yaw_samples)
                module.CENTER_MIN_COLLECTION_S = original_min_s
                module.CENTER_TARGET_SAMPLES = original_target
                controller._finish_center(True)
                calibration_finished = True

            image_pose = pose_map(row.get("image_landmarks")) if row.get("detected") else None
            world_pose = pose_map(row.get("world_landmarks")) if row.get("world_landmarks") else None
            row_width = int(row.get("width") or width)
            row_height = int(row.get("height") or height)
            x, _ = controller.update(image_pose, row_width, row_height, now=now, world_pose=world_pose)
            x_intent = getattr(controller, "_x_intent", None)
            if x_intent is None:
                x_intent = getattr(controller, "_x_intent_v153", None)
            output.append({
                "recording": recording,
                "variant": variant,
                "frame_index": int(row["frame_index"]),
                "timestamp_s": now,
                "detected": bool(row.get("detected")),
                "calibrated": bool(controller.calibrated),
                "output_x": float(x),
                "state": str(getattr(x_intent, "state", "")),
                "committed": bool(getattr(x_intent, "committed", False)),
                "gate_source": str(getattr(controller, "frozen22_gate_source", "BASE")),
                "gate_scale": float(getattr(controller, "frozen22_gate_scale", 1.0)),
                "frozen22_calibration_valid": bool(getattr(controller, "frozen22_calibration_valid", False)),
                "personal_pnp_active": bool(getattr(controller, "personal_pnp_active", False)),
                "real_shape_active": bool(getattr(controller, "real_shape_active", False)),
                # Diagnostic-only signals.  They do not participate in replay
                # output, but let candidate work explain *why* a frozen gate
                # opened on pitch, return, or wrong-direction frames.
                "norm_x": finite_attr(controller, "norm_x"),
                "raw_yaw": finite_attr(controller.raw, "yaw"),
                "raw_pitch": finite_attr(controller.raw, "pitch"),
                "center_yaw": finite_attr(controller, "center_yaw"),
                "center_pitch": finite_attr(controller, "center_pitch"),
                "filtered_yaw": finite_attr(controller, "filtered_yaw"),
                "filtered_pitch": finite_attr(controller, "filtered_pitch"),
                "frozen22_yaw_median": finite_attr(controller, "frozen22_yaw_median"),
                "world_rigid_yaw": finite_attr(controller, "current_world_rigid_yaw"),
                "world_rigid_sigma": finite_attr(controller, "noise_world_rigid_yaw"),
                "frozen22_cal_sigma": finite_attr(controller, "frozen22_cal_sigma_deg"),
                "gate_fast_delta": finite_attr(controller, "frozen22_gate_fast_delta_deg"),
                "gate_slow_delta": finite_attr(controller, "frozen22_gate_slow_delta_deg"),
                "gate_slow_world_delta": finite_attr(controller, "frozen22_gate_slow_world_delta_deg"),
                "gate_pitch_delta": finite_attr(controller, "frozen22_gate_pitch_delta_deg"),
                "gate_fast_threshold": finite_attr(controller, "frozen22_gate_fast_threshold_deg"),
                "gate_slow_threshold": finite_attr(controller, "frozen22_gate_slow_threshold_deg"),
                "intent_velocity": finite_attr(x_intent, "velocity"),
                "intent_acceleration": finite_attr(x_intent, "acceleration"),
                "intent_direction": int(getattr(x_intent, "_active_direction", 0)),
                "return_latched": bool(getattr(x_intent, "return_latched", False)),
                "return_from_direction": int(getattr(x_intent, "_return_from_direction", 0)),
                "turn_mode": str(getattr(x_intent, "_turn_mode", "")),
            })
        if calibration_started and not calibration_finished:
            calibration_samples = len(controller.center_yaw_samples)
            module.CENTER_MIN_COLLECTION_S = original_min_s
            module.CENTER_TARGET_SAMPLES = original_target
            controller._finish_center(True)
            calibration_finished = True
    finally:
        module.CENTER_MIN_COLLECTION_S = original_min_s
        module.CENTER_TARGET_SAMPLES = original_target
        close_recorder = getattr(controller, "close_stage_a_recorder", None)
        if callable(close_recorder):
            close_recorder()

    calibration = {
        "window_s": [cal_start, cal_end],
        "end_inclusive": calibration_end_inclusive,
        "fixed_window_offline_finalization": True,
        "sample_count": calibration_samples,
        "required_statistical_samples": int(module.CENTER_MIN_SAMPLES),
        "live_min_collection_s": float(original_min_s),
        "window_duration_s": cal_end - cal_start,
        "calibrated": bool(controller.calibrated),
        "center_quality": str(controller.center_quality),
        "frozen22_calibration_valid": bool(getattr(controller, "frozen22_calibration_valid", False)),
        "personal_pnp_active": bool(getattr(controller, "personal_pnp_active", False)),
        "noise_yaw": float(controller.noise_yaw),
        "noise_pitch": float(controller.noise_pitch),
    }
    return output, calibration


def sample_duration(rows: list[dict], index: int, end: float, default_dt: float) -> float:
    here = float(rows[index]["timestamp_s"])
    next_time = float(rows[index + 1]["timestamp_s"]) if index + 1 < len(rows) else here + default_dt
    return max(0.0, min(next_time, end) - here)


def score_window(frames: list[dict], window: dict, fps: float) -> dict:
    start = float(window["start_s"])
    end = float(window["end_s"])
    end_inclusive = bool(window.get("end_inclusive", False))
    selected = [
        row for row in frames
        if start <= float(row["timestamp_s"]) and (
            float(row["timestamp_s"]) <= end if end_inclusive else float(row["timestamp_s"]) < end
        )
    ]
    expected = window.get("expected_mouse_x_sign")
    expected_sign = int(expected) if expected is not None else None
    abs_integral = 0.0
    signed_integral = 0.0
    correct_integral = 0.0
    wrong_integral = 0.0
    reverse_residual = 0.0
    nonzero = correct = wrong = 0
    first_correct: float | None = None
    last_correct: float | None = None
    default_dt = 1.0 / fps
    integration_end = end + default_dt if end_inclusive else end
    for index, row in enumerate(selected):
        value = float(row["output_x"])
        dt = sample_duration(selected, index, integration_end, default_dt)
        abs_integral += abs(value) * dt
        signed_integral += value * dt
        if abs(value) > EPS:
            nonzero += 1
        if expected_sign in (-1, 1):
            projected = value * expected_sign
            if projected > EPS:
                correct += 1
                correct_integral += projected * dt
                if first_correct is None:
                    first_correct = float(row["timestamp_s"])
                last_correct = float(row["timestamp_s"])
            elif projected < -EPS:
                wrong += 1
                wrong_integral += -projected * dt
            if window["kind"] == "return" and projected > EPS:
                reverse_residual += projected * dt

    discontinuity_rate: float | None = None
    if first_correct is not None:
        active_span = [row for row in selected if first_correct <= float(row["timestamp_s"]) <= (last_correct or first_correct)]
        if active_span:
            discontinuity_rate = sum(abs(float(row["output_x"])) <= EPS for row in active_span) / len(active_span)
    return {
        "id": window["id"],
        "kind": window["kind"],
        "scorable": bool(window.get("scorable")),
        "start_s": start,
        "end_s": end,
        "end_inclusive": end_inclusive,
        "expected_mouse_x_sign": expected_sign,
        "frames": len(selected),
        "detected_frames": sum(bool(row["detected"]) for row in selected),
        "nonzero_frames": nonzero,
        "nonzero_activity_rate": nonzero / len(selected) if selected else None,
        "absolute_output_integral": abs_integral,
        "signed_output_integral": signed_integral,
        "correct_sign_frames": correct,
        "wrong_sign_frames": wrong,
        "correct_sign_integral": correct_integral,
        "wrong_sign_integral": wrong_integral,
        "direction_correct_rate_nonzero": correct / nonzero if nonzero else None,
        "first_effective_response_delay_s": first_correct - start if first_correct is not None else None,
        "in_action_discontinuity_rate": discontinuity_rate,
        "returning_reverse_residual_integral": reverse_residual,
    }


def real_recordings(windows: dict, data_dir: Path, manifest: dict) -> list[dict]:
    probes = {item["source"]["name"]: item["probe"] for item in manifest["videos"]}
    records = []
    for video in windows["videos"]:
        stem = Path(video["file"]).stem
        path = data_dir / f"{stem}.pose.full_tasks.frames.jsonl"
        cal = [[item["start_s"], item["end_s"]] for item in video["windows"] if item["kind"] == "calibration"]
        records.append({
            "name": stem,
            "group": "new_real",
            "path": path,
            "source_sha256": video["sha256"],
            "fps": float(video["fps"]),
            "width": int(probes[video["file"]]["width"]),
            "height": int(probes[video["file"]]["height"]),
            "calibration": cal,
            "calibration_end_inclusive": False,
            "windows": [item for item in video["windows"] if bool(item.get("scorable"))],
        })
    return records


def historical_recordings(windows: dict, data_dir: Path) -> list[dict]:
    records = []
    for tag, item in windows["recordings"].items():
        replay_windows = []
        for index, (start, end) in enumerate(item["neutral"], 1):
            replay_windows.append({"id": f"{tag}_neutral_{index:02d}", "kind": "neutral_static", "start_s": start, "end_s": end, "end_inclusive": True, "scorable": True})
        for index, (start, end) in enumerate(item["motion"], 1):
            replay_windows.append({"id": f"{tag}_motion_{index:02d}", "kind": "historical_motion", "start_s": start, "end_s": end, "end_inclusive": True, "scorable": True})
        records.append({
            "name": tag,
            "group": "historical_ab",
            "path": data_dir / f"{tag}.pose.frames.jsonl",
            "source_sha256": sha256(data_dir / f"{tag}.mp4"),
            "fps": 30.0,
            "width": 720,
            "height": 1280,
            "calibration": item["calibration"],
            "calibration_end_inclusive": True,
            "windows": replay_windows,
        })
    return records


def aggregate_window_metrics(window_rows: list[dict], group: str, variant: str, kind: str) -> dict:
    selected = [row for row in window_rows if row["group"] == group and row["variant"] == variant and row["kind"] == kind]
    frames = sum(int(row["frames"]) for row in selected)
    nonzero = sum(int(row["nonzero_frames"]) for row in selected)
    return {
        "group": group,
        "variant": variant,
        "kind": kind,
        "window_count": len(selected),
        "frames": frames,
        "nonzero_activity_rate": nonzero / frames if frames else None,
        "absolute_output_integral": sum(float(row["absolute_output_integral"]) for row in selected),
        "correct_sign_frames": sum(int(row["correct_sign_frames"]) for row in selected),
        "wrong_sign_frames": sum(int(row["wrong_sign_frames"]) for row in selected),
        "wrong_sign_integral": sum(float(row["wrong_sign_integral"]) for row in selected),
        "returning_reverse_residual_integral": sum(float(row["returning_reverse_residual_integral"]) for row in selected),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--real-data", type=Path, required=True)
    parser.add_argument("--historical-root", type=Path, required=True)
    parser.add_argument("--v160", type=Path, required=True)
    parser.add_argument("--c0", type=Path, help="External evaluated C0 copy; default is created under --output")
    parser.add_argument("--candidate", type=Path, help="Optional versioned development candidate")
    parser.add_argument("--product-gesture", type=Path, help="Optional production head_control.py replayed with gesture_v153 selected")
    parser.add_argument("--product-gesture-v188", type=Path, help="Optional production head_control.py replayed with gesture_v188 selected")
    parser.add_argument("--product", type=Path, help="Optional production head_control.py replayed with frozen22 selected")
    parser.add_argument("--product-v188", type=Path, help="Optional production head_control.py replayed with frozen22_v188 selected")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    worktree = args.worktree.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    os.environ.pop("MOTIONCONTROL_HEAD_STAGEA_LOG", None)
    os.environ.pop("MOTIONCONTROL_HEAD_STAGEA_LABEL_FILE", None)

    v187_path = (worktree / "frozen" / "MotionControl_head_control_v187_frozen22_uncertainty_gate.py").resolve()
    c0_path = (args.c0.resolve() if args.c0 else args.output.resolve() / "MotionControl_head_control_C0_evaluated_no_fallback.py")
    original = v187_path.read_bytes()
    source = b"FROZEN22_GATE_COMMITTED_FALLBACK_SCALE = 0.03"
    target = b"FROZEN22_GATE_COMMITTED_FALLBACK_SCALE = 0.00"
    if original.count(source) != 1:
        raise RuntimeError("v187 does not contain exactly one fallback-scale literal")
    expected_c0 = original.replace(source, target)
    if c0_path.exists() and c0_path.read_bytes() != expected_c0:
        raise RuntimeError(f"existing C0 path is not the exact one-line candidate: {c0_path}")
    if not c0_path.exists():
        c0_path.write_bytes(expected_c0)

    paths = {
        "v160": args.v160.resolve(),
        "v187": v187_path,
        "C0": c0_path,
    }
    if args.candidate:
        paths["v188"] = args.candidate.resolve()
    if args.product_gesture:
        paths["product_gesture_v153"] = args.product_gesture.resolve()
    if args.product_gesture_v188:
        paths["product_gesture_v188"] = args.product_gesture_v188.resolve()
    if args.product:
        paths["product_frozen22"] = args.product.resolve()
    if args.product_v188:
        paths["product_frozen22_v188"] = args.product_v188.resolve()
    modules = {name: load_module(name.lower(), path) for name, path in paths.items()}
    if modules["v187"].FROZEN22_GATE_COMMITTED_FALLBACK_SCALE != 0.03:
        raise RuntimeError("v187 fallback scale is not 0.03")
    if modules["C0"].FROZEN22_GATE_COMMITTED_FALLBACK_SCALE != 0.00:
        raise RuntimeError("C0 fallback scale is not 0.00")

    rgb_windows_path = worktree / "evaluation" / "evaluation_windows_v1.json"
    historical_windows_path = args.historical_root / "windows" / "windows.json"
    manifest_path = args.real_data / "manifest.json"
    rgb_windows = json.loads(rgb_windows_path.read_text(encoding="utf-8"))
    historical_windows = json.loads(historical_windows_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = real_recordings(rgb_windows, args.real_data, manifest)
    records += historical_recordings(historical_windows, args.historical_root / "data")

    frame_rows: list[dict] = []
    window_rows: list[dict] = []
    calibrations: list[dict] = []
    for record in records:
        source_rows = load_jsonl(record["path"])
        if sha256(record["path"]) == "":
            raise AssertionError("unreachable")
        for variant, module in modules.items():
            replayed, calibration = replay_one(
                module, variant, record["name"], source_rows,
                record["width"], record["height"], record["calibration"],
                record["calibration_end_inclusive"],
            )
            frame_rows.extend(replayed)
            calibrations.append({"recording": record["name"], "group": record["group"], "variant": variant, **calibration})
            for window in record["windows"]:
                metric = score_window(replayed, window, record["fps"])
                window_rows.append({"recording": record["name"], "group": record["group"], "variant": variant, **metric})
            print(f"replayed {record['name']} {variant}: {len(replayed)} frames; calibration={calibration['calibrated']} samples={calibration['sample_count']}")

    fieldnames = list(frame_rows[0])
    with (args.output / "frames.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader(); writer.writerows(frame_rows)
    metric_fields = list(window_rows[0])
    with (args.output / "window_metrics.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=metric_fields, lineterminator="\n")
        writer.writeheader(); writer.writerows(window_rows)

    aggregates = [
        aggregate_window_metrics(window_rows, group, variant, kind)
        for group in ("new_real", "historical_ab")
        for variant in modules
        for kind in sorted({row["kind"] for row in window_rows if row["group"] == group})
    ]
    payload = {
        "schema": "motioncontrol.head_c0.replay.v1",
        "boundary": "offline replay only; no camera, OS input, phone, or product acceptance",
        "calibration_method": "exact frozen timestamp-window samples; live auto-finish suppressed during the window; original controller finalizer called once at window end",
        "inputs": {
            "controllers": {name: {"path": str(path), "sha256": sha256(path), "bytes": path.stat().st_size} for name, path in paths.items()},
            "rgb_windows": {"path": str(rgb_windows_path), "sha256": sha256(rgb_windows_path)},
            "historical_windows": {"path": str(historical_windows_path), "sha256": sha256(historical_windows_path)},
            "real_manifest": {"path": str(manifest_path), "sha256": sha256(manifest_path)},
        },
        "calibrations": calibrations,
        "window_metrics": window_rows,
        "aggregates": aggregates,
        "artifacts": {"frames_csv": "frames.csv", "window_metrics_csv": "window_metrics.csv"},
    }
    summary_path = args.output / "replay_summary.json"
    summary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for path in (args.output / "frames.csv", args.output / "window_metrics.csv", summary_path):
        print(f"{path.name} {sha256(path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
