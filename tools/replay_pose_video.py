from __future__ import annotations

"""Replay an MP4 through the production MediaPipe Full pose/control rules.

The replay sink is deliberately a no-op: this regression tool records the
kernel decisions but never sends keyboard, mouse, or gamepad output.
"""

import argparse
import csv
import json
import math
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from control_kernel import ControlKernel  # noqa: E402
from head_control import CENTER_MIN_SAMPLES, HEAD11_NAMES, HORIZONTAL_ALGORITHMS  # noqa: E402


ANNOTATION_LABELS = (
    "CENTER",
    "TURN_LEFT",
    "TURN_RIGHT",
    "HOLD",
    "RETURNING",
    "NON_YAW",
    "UNCERTAIN",
)
ANNOTATION_REQUIRED_COLUMNS = ("frame", "time_s", "intent_label", "confidence", "note")
EXPECTED_OUTPUT_SIGN = {
    "CENTER": 0,
    "TURN_LEFT": -1,
    "TURN_RIGHT": 1,
    "HOLD": 0,
    "RETURNING": 0,
    "NON_YAW": 0,
    "UNCERTAIN": None,
}
EXPECTED_STATES = {
    "CENTER": frozenset(("CENTER", "IDLE")),
    "TURN_LEFT": frozenset(("TURN_LEFT",)),
    "TURN_RIGHT": frozenset(("TURN_RIGHT",)),
    "HOLD": frozenset(("HOLD", "STABLE_OFFSET")),
    "RETURNING": frozenset(("RETURNING",)),
    # Pure pitch/roll is scored only as zero horizontal output.  Its internal
    # horizontal state depends on the preceding temporal context.
    "NON_YAW": None,
    "UNCERTAIN": None,
}
INCH_TO_METER = 0.0254


class _NoopOutput:
    enabled = False

    def apply(self, *_args, **_kwargs):
        return {"executed": False}

    def set_buttons(self, *_args, **_kwargs):
        return {"executed": False}

    def set_holds(self, *_args, **_kwargs):
        return {"executed": False}

    def set_action_holds(self, *_args, **_kwargs):
        return {"executed": False}

    def execute_action(self, *_args, **_kwargs):
        return {"executed": False}

    def clear_source(self, *_args, **_kwargs):
        return None


def _finite(value, default=0.0):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _load_annotations(path: Path, fps: float, frame_count: int) -> dict[int, dict]:
    """Load the strict frame-level human/vision-model intent labels."""
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = tuple(reader.fieldnames or ())
        missing = [name for name in ANNOTATION_REQUIRED_COLUMNS if name not in columns]
        if missing:
            raise ValueError(
                "annotation columns missing: " + ", ".join(missing)
                + "; regenerate labels from the v2 template"
            )
        rows: dict[int, dict] = {}
        for line_number, raw in enumerate(reader, 2):
            try:
                frame = int(str(raw["frame"]).strip())
                time_s = float(str(raw["time_s"]).strip())
                confidence = float(str(raw["confidence"]).strip())
            except (TypeError, ValueError) as exc:
                raise ValueError(f"annotation line {line_number}: invalid frame/time/confidence") from exc
            label = str(raw["intent_label"]).strip().upper()
            if label not in ANNOTATION_LABELS:
                raise ValueError(
                    f"annotation line {line_number}: intent_label must be one of "
                    + ", ".join(ANNOTATION_LABELS)
                )
            if frame < 1 or frame > frame_count:
                raise ValueError(f"annotation line {line_number}: frame {frame} outside 1..{frame_count}")
            if frame in rows:
                raise ValueError(f"annotation line {line_number}: duplicate frame {frame}")
            expected_time = (frame - 1) / max(fps, 1e-6)
            if abs(time_s - expected_time) > max(0.002, 0.25 / max(fps, 1e-6)):
                raise ValueError(
                    f"annotation line {line_number}: time_s {time_s:.6f} does not match frame {frame} "
                    f"({expected_time:.6f})"
                )
            if not 0.0 <= confidence <= 1.0:
                raise ValueError(f"annotation line {line_number}: confidence must be within 0..1")
            rows[frame] = {
                "intent_label": label,
                "confidence": confidence,
                "note": str(raw.get("note") or "").strip(),
            }
    missing_frames = [frame for frame in range(1, frame_count + 1) if frame not in rows]
    if missing_frames:
        preview = ", ".join(str(value) for value in missing_frames[:12])
        suffix = "..." if len(missing_frames) > 12 else ""
        raise ValueError(f"annotation is not frame-complete; missing: {preview}{suffix}")
    return rows


def _load_bu_ground_truth(path: Path, frame_count: int) -> dict[int, dict]:
    """Read BU tracker rows: frame, X, Y, depth, roll, yaw, pitch."""
    rows: dict[int, dict] = {}
    with path.open("r", encoding="ascii") as handle:
        for line_number, line in enumerate(handle, 1):
            parts = line.split()
            if not parts:
                continue
            if len(parts) != 7:
                raise ValueError(f"ground-truth line {line_number}: expected 7 fields, got {len(parts)}")
            try:
                frame = int(parts[0])
                x_in, y_in, depth_in, roll_deg, yaw_deg, pitch_deg = map(float, parts[1:])
            except ValueError as exc:
                raise ValueError(f"ground-truth line {line_number}: invalid number") from exc
            if frame in rows:
                raise ValueError(f"ground-truth line {line_number}: duplicate frame {frame}")
            rows[frame] = {
                "position_in": {"x": x_in, "y": y_in, "depth": depth_in},
                "position_m": {
                    "x": x_in * INCH_TO_METER,
                    "y": y_in * INCH_TO_METER,
                    "depth": depth_in * INCH_TO_METER,
                },
                "rotation_deg": {"roll": roll_deg, "yaw": yaw_deg, "pitch": pitch_deg},
            }
    expected = set(range(1, frame_count + 1))
    if set(rows) != expected:
        missing = sorted(expected - set(rows))
        extra = sorted(set(rows) - expected)
        raise ValueError(f"ground-truth frames do not match video; missing={missing[:8]}, extra={extra[:8]}")
    return rows


def _write_annotation_template(path: Path, fps: float, frame_count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ANNOTATION_REQUIRED_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for frame in range(1, frame_count + 1):
            writer.writerow({
                "frame": frame,
                "time_s": f"{(frame - 1) / max(fps, 1e-6):.3f}",
                "intent_label": "",
                "confidence": "",
                "note": "",
            })


def _landmark_payload(point) -> dict | None:
    xyz = tuple(_finite(getattr(point, axis, math.nan), None) for axis in ("x", "y", "z"))
    if any(value is None for value in xyz):
        return None
    return {
        "x": xyz[0],
        "y": xyz[1],
        "z": xyz[2],
        "visibility": _finite(getattr(point, "visibility", 1.0), 1.0),
        "presence": _finite(getattr(point, "presence", 1.0), 1.0),
    }


def _head11_input(landmarks, world_landmarks) -> tuple[dict | None, dict | None, list[dict]]:
    if not landmarks or len(landmarks) < len(HEAD11_NAMES):
        return None, None, []
    image_pose: dict[str, dict] = {}
    world_pose: dict[str, dict] | None = {} if world_landmarks and len(world_landmarks) >= len(HEAD11_NAMES) else None
    points: list[dict] = []
    for index, name in enumerate(HEAD11_NAMES):
        image = _landmark_payload(landmarks[index])
        if image is None:
            return None, None, []
        image_pose[name] = {**image, "score": min(image["visibility"], image["presence"])}
        world = None
        if world_pose is not None:
            world = _landmark_payload(world_landmarks[index])
            if world is None:
                return None, None, []
            world_pose[name] = {**world, "score": min(world["visibility"], world["presence"])}
        points.append({"name": name, "image": image, "world": world})
    return image_pose, world_pose, points


def _sign(value, epsilon=0.01) -> int:
    number = _finite(value, 0.0)
    return 1 if number >= epsilon else -1 if number <= -epsilon else 0


def _score_annotation(samples: list[dict]) -> dict | None:
    annotated = [row for row in samples if row.get("annotation")]
    if not annotated:
        return None
    sign_rows = []
    state_rows = []
    for row in annotated:
        label = row["annotation"]["intent_label"]
        expected_sign = EXPECTED_OUTPUT_SIGN[label]
        predicted_sign = _sign(row.get("x"))
        row["expected_output_x_sign"] = expected_sign
        row["predicted_output_x_sign"] = predicted_sign
        row["output_sign_correct"] = expected_sign is None or predicted_sign == expected_sign
        states = EXPECTED_STATES[label]
        row["state_correct"] = None if states is None else row.get("yaw_state") in states
        if expected_sign is not None:
            sign_rows.append(row)
        if states is not None:
            state_rows.append(row)
    return {
        "annotated_frames": len(annotated),
        "output_sign_scored_frames": len(sign_rows),
        "output_sign_accuracy": (
            sum(bool(row["output_sign_correct"]) for row in sign_rows) / len(sign_rows)
            if sign_rows else None
        ),
        "state_scored_frames": len(state_rows),
        "state_accuracy": (
            sum(bool(row["state_correct"]) for row in state_rows) / len(state_rows)
            if state_rows else None
        ),
        "uncertain_frames": sum(row["annotation"]["intent_label"] == "UNCERTAIN" for row in annotated),
    }


def _resolve_model(explicit: str | None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get("MOTIONCONTROL_MODEL_ROOT", "").strip().strip('"')
    if env:
        root = Path(env)
        candidates.extend([
            root / "mediapipe" / "pose_landmarker_full_compatible_075.task",
            root / "mediapipe" / "pose_landmarker_full.task",
        ])
    candidates.extend([
        ROOT / "models" / "mediapipe" / "pose_landmarker_full_compatible_075.task",
        ROOT / "models" / "mediapipe" / "pose_landmarker_full.task",
        Path(r"I:\MotionControl-Pose-Models\models\mediapipe\pose_landmarker_full_compatible_075.task"),
        Path(r"I:\MotionControl-Pose-Models\models\mediapipe\pose_landmarker_full.task"),
    ])
    for path in candidates:
        try:
            if path.is_file():
                return path.resolve()
        except OSError:
            pass
    raise SystemExit("MediaPipe Full task not found; pass --model <pose_landmarker_full*.task>")


def _event_snapshot(kernel: ControlKernel, t: float) -> dict:
    status = kernel.status_locked(t)
    head = status.get("head") or {}
    return {
        "t": round(float(t), 3),
        "gate": bool(status.get("vertical_gate_active")),
        "motions": list(status.get("motions") or []),
        "poses": list(status.get("poses_active") or []),
        "pose_confidence": status.get("pose_confidence") or {},
        "yaw": head.get("raw_yaw"),
        "pitch": head.get("raw_pitch"),
        "yaw_state": head.get("yaw_intent_state"),
        "pitch_state": head.get("pitch_intent_state"),
        "yaw_velocity": head.get("yaw_velocity_norm_s", head.get("yaw_velocity")),
        "pitch_velocity": head.get("pitch_velocity_norm_s", head.get("pitch_velocity")),
        "x": head.get("output_x"),
        "y": head.get("output_y"),
        "vertical_source": head.get("vertical_source"),
        "vertical_temp_center_pitch": head.get("vertical_temp_center_pitch"),
        "head_calibrating": bool(head.get("calibrating")),
        "head_calibrated": bool(head.get("calibrated")),
        "head_center_phase": head.get("center_phase"),
        "head_center_valid_s": head.get("center_valid_s"),
        "head_center_samples": head.get("center_samples"),
        "world_pose_available": bool(status.get("world_pose_available")),
        "personal_pnp_active": bool(head.get("personal_pnp_active")),
        "personal_pnp_template_quality": head.get("personal_pnp_template_quality"),
        "personal_pnp_rejection_reason": head.get("personal_pnp_rejection_reason"),
    }


def _compact_events(samples: list[dict]) -> list[dict]:
    out: list[dict] = []
    previous = None
    for row in samples:
        signature = (
            row["gate"], tuple(row["motions"]), tuple(row["poses"]),
            row["yaw_state"], row["pitch_state"], row["vertical_source"],
        )
        moving = abs(_finite(row.get("x"))) >= 0.01 or abs(_finite(row.get("y"))) >= 0.01
        if previous != signature or moving:
            out.append(row)
        previous = signature
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replay a video through MotionControl using exactly 11 head landmarks"
    )
    parser.add_argument("video", type=Path)
    parser.add_argument("--model", default=None)
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "video-replay.json")
    parser.add_argument("--head-data-output", type=Path, default=None)
    parser.add_argument("--annotation", type=Path, default=None)
    parser.add_argument("--annotation-template", type=Path, default=None)
    parser.add_argument("--ground-truth", type=Path, default=None)
    parser.add_argument("--horizontal-algorithm", choices=HORIZONTAL_ALGORITHMS, default="gesture_v188")
    parser.add_argument("--vertical-source", choices=("head", "right_wrist"), default="head")
    parser.add_argument("--calibration-start", type=float, default=0.0)
    parser.add_argument(
        "--calibration-end",
        type=float,
        default=0.0,
        help="offline fixed calibration end; 0 keeps the production live timer",
    )
    parser.add_argument("--max-seconds", type=float, default=0.0, help="0 = whole video")
    parser.add_argument("--every", type=int, default=1, help="run every Nth frame (default 1)")
    args = parser.parse_args()

    video = args.video.resolve()
    if not video.is_file():
        raise SystemExit(f"video not found: {video}")
    model = _resolve_model(args.model)

    try:
        import cv2
        import mediapipe as mp
        from mediapipe.tasks import python
        from mediapipe.tasks.python import vision
    except Exception as exc:
        raise SystemExit(f"opencv/mediapipe unavailable: {exc}")

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise SystemExit(f"cannot open video: {video}")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    if fps <= 0 or frame_count <= 0:
        cap.release()
        raise SystemExit("video metadata is invalid")
    if args.annotation_template:
        _write_annotation_template(args.annotation_template.resolve(), fps, frame_count)
    try:
        annotations = (
            _load_annotations(args.annotation.resolve(), fps, frame_count)
            if args.annotation else {}
        )
        ground_truth = (
            _load_bu_ground_truth(args.ground_truth.resolve(), frame_count)
            if args.ground_truth else {}
        )
    except (OSError, ValueError) as exc:
        cap.release()
        raise SystemExit(str(exc)) from exc
    fixed_calibration = args.calibration_end > args.calibration_start
    if args.calibration_end < 0 or args.calibration_start < 0:
        cap.release()
        raise SystemExit("calibration times must be non-negative")

    options = vision.PoseLandmarkerOptions(
        base_options=python.BaseOptions(model_asset_path=str(model)),
        running_mode=vision.RunningMode.VIDEO,
        num_poses=1,
        min_pose_detection_confidence=0.35,
        min_pose_presence_confidence=0.35,
        min_tracking_confidence=0.35,
    )
    detector = vision.PoseLandmarker.create_from_options(options)
    output = _NoopOutput()
    kernel = ControlKernel(output)
    kernel.configure_head(
        horizontal_algorithm=args.horizontal_algorithm,
        vertical_look_source=args.vertical_source,
        enabled=True,
    )

    # Use a monotonic base so the watchdog does not treat replay timestamps as
    # ancient. All intent logic still receives the original video time delta.
    base = time.monotonic() + 10.0
    center_started = False
    center_finished = False
    samples: list[dict] = []
    head_rows: list[dict] = []
    invalid_frames = 0
    frame_index = 0
    every = max(1, int(args.every))
    last_timestamp_ms = -1

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            t_video = frame_index / max(1e-6, fps)
            frame_index += 1
            if args.max_seconds > 0 and t_video > args.max_seconds:
                break
            if (frame_index - 1) % every:
                continue
            now = base + t_video
            if not center_started and t_video >= max(0.0, args.calibration_start):
                kernel.head_controller.start_center(now, kind="video_replay")
                if fixed_calibration:
                    # Offline datasets do not need the live one-second voice/UI
                    # preparation delay.  The fixed window itself is explicit.
                    kernel.head_controller.center_prepare_until = now
                center_started = True
            if (
                fixed_calibration
                and center_started
                and not center_finished
                and t_video >= args.calibration_end
            ):
                sample_count = len(kernel.head_controller.center_yaw_samples)
                if sample_count < CENTER_MIN_SAMPLES:
                    raise RuntimeError(
                        f"fixed calibration has only {sample_count}/{CENTER_MIN_SAMPLES} valid samples"
                    )
                kernel.head_controller._finish_center(True)
                center_finished = True

            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            timestamp_ms = max(int(round(t_video * 1000.0)), last_timestamp_ms + 1)
            last_timestamp_ms = timestamp_ms
            result = detector.detect_for_video(image, timestamp_ms)
            landmarks = result.pose_landmarks[0] if result.pose_landmarks else None
            world_landmarks = (
                result.pose_world_landmarks[0]
                if getattr(result, "pose_world_landmarks", None)
                else None
            )
            pose_map, world_pose, head_points = _head11_input(landmarks, world_landmarks)
            input_valid = pose_map is not None and world_pose is not None and len(head_points) == 11
            if not input_valid:
                invalid_frames += 1
                pose_map = None
                world_pose = None

            with kernel._lock:
                kernel.width = max(1, width)
                kernel.height = max(1, height)
                kernel.latest_pose = pose_map
                kernel.latest_world_pose = world_pose
                if world_pose is None:
                    kernel._process_pose_locked(pose_map, now)
                else:
                    kernel._process_pose_locked(pose_map, now, world_pose)
                row = _event_snapshot(kernel, now)
                row["frame"] = frame_index
                row["video_t"] = round(t_video, 3)
                row["head11_input_valid"] = input_valid
                row["annotation"] = annotations.get(frame_index)
                row["ground_truth"] = ground_truth.get(frame_index)
                samples.append(row)
                head_rows.append({
                    "schema": "motioncontrol.head11_frame.v1",
                    "frame": frame_index,
                    "time_s": round(t_video, 6),
                    "input_valid": input_valid,
                    "image_coordinate_space": "normalized_image_xyz; z_is_relative_depth_not_meters",
                    "world_coordinate_space": "mediapipe_world_xyz_meters",
                    "points": head_points,
                    "annotation": annotations.get(frame_index),
                    "bu_ground_truth": ground_truth.get(frame_index),
                })
    finally:
        cap.release()
        try:
            detector.close()
        except Exception:
            pass
        kernel.close()

    evaluation = _score_annotation(samples)
    head_data_output = (
        args.head_data_output.resolve()
        if args.head_data_output
        else args.output.resolve().with_name(args.output.stem + ".head11.jsonl")
    )
    head_data_output.parent.mkdir(parents=True, exist_ok=True)
    with head_data_output.open("w", encoding="utf-8", newline="\n") as handle:
        for row in head_rows:
            handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")

    report = {
        "schema": "motioncontrol.video_replay.v2",
        "boundary": "offline replay only; no camera, phone, OS input, game, or release acceptance",
        "video": str(video),
        "model": str(model),
        "fps": fps,
        "frame_count": frame_count,
        "processed_samples": len(samples),
        "invalid_pose_frames": invalid_frames,
        "head_input": {
            "landmarks": list(HEAD11_NAMES),
            "image_fields": ["x", "y", "z"],
            "image_z_unit": "normalized_relative_depth_not_meters",
            "world_fields": ["x", "y", "z"],
            "world_unit": "meter",
            "requires_image_and_world": True,
            "data_jsonl": str(head_data_output),
        },
        "horizontal_algorithm": args.horizontal_algorithm,
        "vertical_source": args.vertical_source,
        "calibration_start_s": args.calibration_start,
        "calibration_end_s": args.calibration_end if fixed_calibration else None,
        "calibrated": bool(kernel.head_controller.calibrated),
        "annotation": {
            "path": str(args.annotation.resolve()) if args.annotation else None,
            "allowed_labels": list(ANNOTATION_LABELS),
            "evaluation": evaluation,
        },
        "ground_truth": {
            "path": str(args.ground_truth.resolve()) if args.ground_truth else None,
            "columns": ["frame", "x_in", "y_in", "depth_in", "roll_deg", "yaw_deg", "pitch_deg"],
        },
        "events": _compact_events(samples),
        "samples": samples,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"replay complete: {len(samples)} samples, invalid pose {invalid_frames}")
    print(f"report: {args.output.resolve()}")
    print(f"head11 data: {head_data_output}")
    if args.annotation_template:
        print(f"annotation template: {args.annotation_template.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
