from __future__ import annotations

"""Replay an MP4 through the production MediaPipe Full pose/control rules.

The replay sink is deliberately a no-op: this regression tool records the
kernel decisions but never sends keyboard, mouse, or gamepad output.
"""

import argparse
import json
import math
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from control_kernel import ControlKernel, MP_NAMES  # noqa: E402


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
    parser = argparse.ArgumentParser(description="Replay MP4 through MotionControl production pose/control rules")
    parser.add_argument("video", type=Path)
    parser.add_argument("--model", default=None)
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "video-replay.json")
    parser.add_argument("--vertical-source", choices=("head", "right_wrist"), default="head")
    parser.add_argument("--calibration-start", type=float, default=0.0)
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
    kernel.configure_head(vertical_look_source=args.vertical_source, enabled=True)

    # Use a monotonic base so the watchdog does not treat replay timestamps as
    # ancient. All intent logic still receives the original video time delta.
    base = time.monotonic() + 10.0
    center_started = False
    samples: list[dict] = []
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
                center_started = True

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
            pose_map = None
            if landmarks:
                pose_map = {
                    MP_NAMES[i]: {
                        "x": _finite(point.x),
                        "y": _finite(point.y),
                        "z": _finite(point.z),
                        "score": _finite(getattr(point, "visibility", getattr(point, "presence", 1.0)), 1.0),
                    }
                    for i, point in enumerate(landmarks[: len(MP_NAMES)])
                }
            else:
                invalid_frames += 1

            world_pose = None
            if world_landmarks:
                world_pose = {
                    MP_NAMES[i]: {
                        "x": _finite(point.x),
                        "y": _finite(point.y),
                        "z": _finite(point.z),
                        "score": _finite(getattr(point, "visibility", getattr(point, "presence", 1.0)), 1.0),
                    }
                    for i, point in enumerate(world_landmarks[: len(MP_NAMES)])
                }

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
                row["video_t"] = round(t_video, 3)
                samples.append(row)
    finally:
        cap.release()
        try:
            detector.close()
        except Exception:
            pass
        kernel.close()

    report = {
        "schema": "motioncontrol.video_replay.v1",
        "video": str(video),
        "model": str(model),
        "fps": fps,
        "frame_count": frame_count,
        "processed_samples": len(samples),
        "invalid_pose_frames": invalid_frames,
        "vertical_source": args.vertical_source,
        "calibration_start_s": args.calibration_start,
        "events": _compact_events(samples),
        "samples": samples,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"replay complete: {len(samples)} samples, invalid pose {invalid_frames}")
    print(f"report: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
