#!/usr/bin/env python3
"""Head-control-v2 real-person signal capture tool.

Collects raw head-tracking signals across five guided segments and writes a
diagnostic JSON file.  This is a *diagnostic* tool, not part of the runtime
control loop.  It helps verify that yaw/pitch signals are continuous and that
the face-pair lock does not jump.

Usage (real camera only):
    python tools/head_signal_capture.py
    python tools/head_signal_capture.py --duration 3 --output output/my_signals.json

Segments (guided by on-screen prompts):
    1. front  - look straight ahead
    2. up     - tilt head up
    3. down   - tilt head down
    4. left   - turn head left
    5. right  - turn head right

Output JSON contains per-segment median/min/max/sample_count for:
    raw_yaw, raw_pitch_face, raw_pitch_z, raw_pitch_fused,
    normalized_yaw, normalized_pitch, output_x, output_y, face_pair
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path

# Ensure project root is on sys.path so control_kernel can be imported.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from control_kernel import ControlKernel  # noqa: E402


DEFAULT_MODEL_PATH = Path(
    os.environ.get(
        "MOTIONCONTROL_FULL_MODEL",
        r"I:\MotionControl-Pose-Models\models\mediapipe\pose_landmarker_full.task",
    )
)


SEGMENTS = [
    ("front", "正视前方", 3.0),
    ("up", "抬头", 3.0),
    ("down", "低头", 3.0),
    ("left", "左转", 3.0),
    ("right", "右转", 3.0),
]

SIGNAL_FIELDS = (
    "raw_yaw", "raw_pitch_face", "raw_pitch_z", "raw_pitch_fused",
    "normalized_yaw", "normalized_pitch", "output_x", "output_y",
)


class _CaptureOutput:
    """Minimal output backend that records head stick values."""

    def __init__(self):
        self.axes = (0.0, 0.0)
        self.enabled = True

    def set_buttons(self, buttons, **kwargs):
        pass

    def set_holds(self, holds, **kwargs):
        pass

    def apply(self, x, y):
        self.axes = (x, y)

    def set_sensor_state(self, source, buttons, **kwargs):
        pass

    def clear_source(self, source):
        pass


def _median(values):
    if not values:
        return math.nan
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def _stats(values):
    finite = [v for v in values if math.isfinite(v)]
    if not finite:
        return {"median": None, "min": None, "max": None, "sample_count": 0}
    return {
        "median": round(_median(finite), 6),
        "min": round(min(finite), 6),
        "max": round(max(finite), 6),
        "sample_count": len(finite),
    }


def _collect_segment(kernel, label_cn, duration):
    """Collect raw signals for one segment. Camera feeds frames in background."""
    print(f"\n>>> {label_cn}  ({duration:.0f}s)  — 准备好后按 Enter 开始...")
    input()
    print(f"    采集中... {label_cn}")
    samples = {field: [] for field in SIGNAL_FIELDS}
    pair_counts = {}
    start = time.monotonic()
    last_print = 0.0
    while time.monotonic() - start < duration:
        h = kernel.status()["head"]
        for field in SIGNAL_FIELDS:
            key = {
                "normalized_yaw": "normalized_x",
                "normalized_pitch": "normalized_y",
            }.get(field, field)
            val = h.get(key, math.nan)
            try:
                value = float(val)
            except (TypeError, ValueError):
                value = math.nan
            samples[field].append(value if math.isfinite(value) else math.nan)
        pair = h.get("face_pair", "none")
        pair_counts[pair] = pair_counts.get(pair, 0) + 1
        now = time.monotonic()
        if now - last_print > 1.0:
            elapsed = now - start
            print(f"    {elapsed:.1f}s / {duration:.0f}s  pair={pair}")
            last_print = now
        time.sleep(0.03)
    result = {field: _stats(samples[field]) for field in SIGNAL_FIELDS}
    result["face_pair_distribution"] = pair_counts
    result["face_pair"] = (
        max(pair_counts, key=pair_counts.get) if pair_counts else ""
    )
    return result


def _build_camera(kernel, model_path: Path):
    """Create the native camera service using the formal Full task model."""
    try:
        from control_kernel import NativeCameraService
        cam = NativeCameraService(kernel, model_path=model_path)
        cam.start()
        # Wait briefly for first frame
        for _ in range(30):
            if getattr(cam, "latest_frame", None) is not None:
                break
            time.sleep(0.1)
        return cam
    except Exception as exc:
        print(f"[WARN] Camera unavailable: {exc}")
        print("       Signal capture requires a working camera + MediaPipe.")
        return None


def main():
    parser = argparse.ArgumentParser(description="Head-control-v2 real signal capture")
    parser.add_argument("--duration", type=float, default=3.0, help="Seconds per segment")
    parser.add_argument("--output", type=str, default=None, help="Output JSON path")
    parser.add_argument("--model", type=str, default=str(DEFAULT_MODEL_PATH), help="Formal MediaPipe Full task path")
    args = parser.parse_args()

    output = _CaptureOutput()
    kernel = ControlKernel(output)
    model_path = Path(args.model).expanduser().resolve()
    if not model_path.is_file():
        print(f"[ERROR] MediaPipe Full task not found: {model_path}")
        kernel.close()
        sys.exit(1)
    camera = _build_camera(kernel, model_path)

    if camera is None:
        print("\n[ERROR] Cannot capture without a camera. Exiting.")
        kernel.close()
        sys.exit(1)

    print("=" * 60)
    print("Head-control-v2 真人信号采集")
    print("=" * 60)
    print(f"每段时长: {args.duration:.0f}s")
    print(f"模型: {model_path}")
    print(f"信号字段: {', '.join(SIGNAL_FIELDS)}; face_pair")
    print("=" * 60)

    results = {}
    for seg_id, label_cn, default_dur in SEGMENTS:
        dur = args.duration
        results[seg_id] = _collect_segment(kernel, label_cn, dur)

    if camera:
        camera.stop()
    kernel.close()

    # Build output filename
    if args.output:
        out_path = Path(args.output)
    else:
        ts = time.strftime("%Y%m%d-%H%M%S")
        out_path = PROJECT_ROOT / "output" / f"head-control-v2-real-signal-{ts}.json"

    model_sha256 = hashlib.sha256(model_path.read_bytes()).hexdigest().upper()
    payload = {
        "tool": "head_signal_capture.py",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "signal_version": "head-control-v2",
        "model_path": str(model_path),
        "model_sha256": model_sha256,
        "segment_duration_s": args.duration,
        "segments": results,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{'=' * 60}")
    print(f"采集完成！结果已保存到:")
    print(f"  {out_path}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
