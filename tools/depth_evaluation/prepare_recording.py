"""按同一Kinect的传感器时钟对齐已录彩色帧、原始深度和现有模型骨骼。"""
from __future__ import annotations

import argparse
import bisect
import json
from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from motioncontrol.depth_comparison_metrics import surface_references


def read_rows(path):
    with Path(path).open(encoding="utf-8-sig") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def prepare(recording, pose_recording):
    recording = Path(recording).resolve()
    header, *poses = read_rows(pose_recording)
    color = read_rows(recording / "kinect/color_frames.jsonl")
    body_frames = read_rows(recording / "kinect/bodies.jsonl")
    clock_offsets = [p["extra"]["depth_body"]["sensor_seconds"] - p["sample_t"] for p in poses
                     if (p.get("extra", {}).get("depth_body") or {}).get("sensor_seconds") is not None]
    if not clock_offsets:
        raise ValueError("录制没有同一Kinect的传感器时钟，不能可靠对齐")
    origin = float(np.median(clock_offsets))
    times = [(p.get("extra", {}).get("depth_body") or {}).get("sensor_seconds", p["sample_t"] + origin) for p in poses]
    body_times = [p["sensor_ticks"] / 1e7 for p in body_frames]
    output = recording / "analysis"
    output.mkdir(exist_ok=True)
    samples, projector_input = [], []
    for image in color:
        at = image["sensor_ticks"] / 1e7
        index = bisect.bisect_left(times, at)
        choices = [i for i in (index - 1, index) if 0 <= i < len(times)]
        index = min(choices, key=lambda i: abs(times[i] - at))
        if abs(times[index] - at) > .035:
            continue
        frame = poses[index]
        extra = frame.get("extra", {})
        identity = json.loads(extra["body_model_identity"])
        rotation, mirrored = identity[3:5]
        if rotation != "none" or not mirrored:
            raise ValueError("本次精确重放要求未旋转、在采集后左右翻转的Kinect画面")
        body = extra.get("depth_body") or {}
        refs = surface_references(body, {**body, "image_mirrored": mirrored}, rotation)
        pose = extra.get("model_pose") or frame["pose"]
        native = extra.get("body_lean", {}).get("sources", {}).get("depth", {})
        model = extra.get("body_lean", {}).get("sources", {}).get("model", {})
        body_index = bisect.bisect_left(body_times, at)
        choices = [i for i in (body_index - 1, body_index) if 0 <= i < len(body_times)]
        body_index = min(choices, key=lambda i: abs(body_times[i] - at))
        samples.append({"frame_index": image["frame_index"], "file": str(recording / "kinect" / image["file"]),
                        "sensor_seconds": at, "t": at - color[0]["sensor_ticks"] / 1e7,
                        "pose_delta_ms": round((times[index] - at) * 1000, 3), "pose": pose,
                        "references": refs, "depth_body": body,
                        "sdk_body": body_frames[body_index] if abs(body_times[body_index] - at) <= .05 else None,
                        "depth_pitch_deg": native.get("back_uncorrected_deg"),
                        "model_pitch_deg": model.get("back_uncorrected_deg")})
        projected = {name: {"x": 1 - p["x"], "y": p["y"], "score": p.get("score", 0)}
                     for name, p in pose.items() if isinstance(p, dict) and name in
                     {"nose", "left_shoulder", "right_shoulder", "left_hip", "right_hip"}}
        projected.update({"native_" + r["name"]: {"x": 1 - r["x"], "y": r["y"], "score": 1} for r in refs})
        projector_input.append({**image, "pose": projected})
    if not samples:
        raise ValueError("没有对齐成功的彩色帧")
    for name, rows in (("samples.jsonl", samples), ("projector-input.jsonl", projector_input)):
        with (output / name).open("w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
    distances = [r["z"] for sample in samples for r in sample["references"]]
    angles = [s["depth_pitch_deg"] for s in samples if s["depth_pitch_deg"] is not None]
    report = {"source": str(recording), "pose_recording": str(pose_recording),
              "recorded_at": header["recorded_at"], "color_frames": len(color), "aligned_frames": len(samples),
              "frames_with_reference": sum(bool(s["references"]) for s in samples),
              "reference_points": len(distances), "reference_range_m": [min(distances), max(distances)] if distances else None,
              "depth_pitch_range_deg": [min(angles), max(angles)] if angles else None,
              "clock_offset_spread_ms": round(float(np.ptp(clock_offsets)) * 1000, 3),
              "max_pose_delta_ms": max(abs(s["pose_delta_ms"]) for s in samples)}
    (output / "alignment.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--recording", required=True)
    parser.add_argument("--pose-recording", required=True)
    args = parser.parse_args()
    prepare(args.recording, args.pose_recording)
