"""检查录制中的肩髋前后深浅关系；先固定中立基准，再检验后续动作。"""
from __future__ import annotations

import argparse
import bisect
import json
from pathlib import Path
import sys

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from motioncontrol.depth_comparison_metrics import DepthMetrics
from motioncontrol.depth_comparison_worker import Adapter
from motioncontrol.depth_input import person_rect
from motioncontrol_shared.pose_points import MP_NAMES
from tools.depth_evaluation.prepare_recording import read_rows

TORSO = ("left_shoulder", "right_shoulder", "left_hip", "right_hip")
SDK_NAMES = ("ShoulderLeft", "ShoulderRight", "HipLeft", "HipRight")


def save_rows(path, rows):
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")


def prepare(recording, pose_model):
    import mediapipe as mp
    from mediapipe.tasks import python
    from mediapipe.tasks.python import vision

    directory = recording / "analysis"
    colors = read_rows(recording / "kinect/color_frames.jsonl")
    bodies = read_rows(recording / "kinect/bodies.jsonl")
    previous = {row["frame_index"]: row for row in read_rows(directory / "samples.jsonl")}
    ticks = [row["sensor_ticks"] for row in bodies]
    options = vision.PoseLandmarkerOptions(base_options=python.BaseOptions(model_asset_path=pose_model),
                                           running_mode=vision.RunningMode.VIDEO, num_poses=1)
    rows, projection, native_projection, person = [], [], [], None
    depth_index = read_rows(recording / "kinect/depth_frames.jsonl")
    depth_ticks = [row["sensor_ticks"] for row in depth_index]
    device_id = next(iter(previous.values()))["depth_body"]["device_id"]
    with vision.PoseLandmarker.create_from_options(options) as detector:
        for color in colors:
            t = (color["sensor_ticks"] - colors[0]["sensor_ticks"]) / 1e7
            frame = cv2.flip(cv2.imread(str(recording / "kinect" / color["file"]))[1::3, 1::3].copy(), 1)
            result = detector.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB,
                        data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)), round(t * 1000))
            pose = (previous[color["frame_index"]]["pose"] if color["frame_index"] in previous else
                    {name: {"x": point.x, "y": point.y, "score": point.visibility}
                     for name, point in zip(MP_NAMES, result.pose_landmarks[0])} if result.pose_landmarks else {})
            index = bisect.bisect_left(ticks, color["sensor_ticks"])
            choices = [i for i in (index - 1, index) if 0 <= i < len(bodies)]
            native = bodies[min(choices, key=lambda i: abs(ticks[i] - color["sensor_ticks"]))]
            if abs(native["sensor_ticks"] - color["sensor_ticks"]) > 500_000:
                native = {"bodies": []}
            tracked = [b for b in native["bodies"] if b["is_tracked"] and (person is None or b["tracking_id"] == person)]
            points, sdk_pitch = {}, None
            if tracked:
                body = tracked[0]
                person = body["tracking_id"]
                points = body["joints"]
                if all(points[name]["tracking_state_code"] == 2 for name in SDK_NAMES):
                    vectors = np.asarray([[points[n][axis + "_m"] for axis in ("x", "y", "z")] for n in SDK_NAMES])
                    up = vectors[:2].mean(axis=0) - vectors[2:].mean(axis=0)
                    sdk_pitch = float(np.degrees(np.arctan2(up[2], up[1])))
            rows.append({"frame_index": color["frame_index"], "t": t, "file": str(recording / "kinect" / color["file"]),
                         "pose": pose, "sdk_pitch_camera_deg": sdk_pitch})
            projection.append({**color, "pose": {n: {"x": 1 - p["x"], "y": p["y"], "score": p["score"]}
                                                 for n, p in pose.items() if n in TORSO}})
            index = bisect.bisect_left(depth_ticks, color["sensor_ticks"])
            choices = [i for i in (index - 1, index) if 0 <= i < len(depth_index)]
            raw_depth = depth_index[min(choices, key=lambda i: abs(depth_ticks[i] - color["sensor_ticks"]))]
            if abs(raw_depth["sensor_ticks"] - color["sensor_ticks"]) > 500_000:
                raise ValueError("彩色与原始深度不同步")
            native_projection.append({"frame_index": color["frame_index"], "device_id": device_id,
                "depth_offset": raw_depth["offset"], "minimum": raw_depth["reliable_min_mm"], "maximum": raw_depth["reliable_max_mm"],
                "points": {n: points[s] for n, s in zip(TORSO, SDK_NAMES) if s in points and points[s]["tracking_state_code"] == 2}})
    save_rows(directory / "lean-samples.jsonl", rows)
    save_rows(directory / "lean-projector-input.jsonl", projection)
    save_rows(directory / "lean-native-input.jsonl", native_projection)
    angles = [r["sdk_pitch_camera_deg"] for r in rows if r["sdk_pitch_camera_deg"] is not None]
    print(json.dumps({"frames": len(rows), "sdk_pitch_camera_range_deg": [min(angles), max(angles)],
                      "complete_torso_frames": sum(all(n in r["pose"] and r["pose"][n]["score"] >= .6 for n in TORSO) for r in rows)}), flush=True)


def evaluate(recording, ident):
    directory = recording / "analysis"
    samples = read_rows(directory / "lean-samples.jsonl")
    reference = {r["frame_index"]: r for r in read_rows(directory / "lean-native-torso.jsonl")}
    runtime = json.loads((Path.home() / "AppData/Local/MotionControl/depth_comparison_runtime.json").read_text(encoding="utf-8-sig"))
    adapter = Adapter(ident, runtime["models"][ident], 392)
    for scope in ("full", "person"):
        rect = person_rect(samples[0]["pose"], 640, 360) if scope == "person" else (0, 0, 640, 360)
        if rect is None:
            raise ValueError("未能取得可靠人体范围")
        x, y, w, h = rect
        rows = []
        for sample in samples:
            frame = cv2.flip(cv2.imread(sample["file"])[1::3, 1::3].copy(), 1)
            depth = adapter.infer(frame[y:y+h, x:x+w], (scope, rect))
            native = reference[sample["frame_index"]]
            points = {}
            for name in TORSO:
                surface = native["points"].get(name)
                if not surface:
                    continue
                p = {"x": 1 - surface["color_x"], "y": surface["color_y"]}
                px, py = (p["x"] * 640 - x) / w, (p["y"] * 360 - y) / h
                if not 0 <= px <= 1 or not 0 <= py <= 1:
                    continue
                value = DepthMetrics.sample(depth, px, py)
                if value is not None:
                    points[name] = {"raw": value, "reference_m": surface["z"], "x": p["x"], "y": p["y"]}
            rows.append({"frame_index": sample["frame_index"], "t": sample["t"], "points": points,
                         "sdk_pitch_camera_deg": sample["sdk_pitch_camera_deg"]})
        destination = directory / f"lean-{ident}-{scope}-points.jsonl"
        save_rows(destination, rows)
        print(json.dumps({"model": ident, "scope": scope, "frames": len(rows),
                          "complete_torso_frames": sum(len(r["points"]) == 4 for r in rows)}), flush=True)


def correlation(x, y):
    return float(np.corrcoef(x, y)[0, 1]) if len(x) > 2 and np.std(x) > 1e-10 and np.std(y) > 1e-10 else None


def report(recording):
    directory = recording / "analysis"
    report, traces = {}, {}
    for ident in ("dav2_metric_s", "vda_metric_s", "zipdepth"):
        for scope in ("full", "person"):
            rows = read_rows(directory / f"lean-{ident}-{scope}-points.jsonl")
            values = []
            # 正比例系数不改变方向；相对逆深度只用负号检查近大远小，不强行换算米数。
            for row in rows:
                p = row["points"]
                if len(p) != 4 or row["sdk_pitch_camera_deg"] is None:
                    continue
                raw = np.asarray([p[n]["raw"] for n in TORSO])
                native = np.asarray([p[n]["reference_m"] for n in TORSO])
                raw_gap = float(raw[:2].mean() - raw[2:].mean()) * (-1 if ident == "zipdepth" else 1)
                ref_gap = float(native[:2].mean() - native[2:].mean())
                values.append([row["t"], raw_gap, ref_gap, row["sdk_pitch_camera_deg"]])
            a = np.asarray(values)
            baseline = a[a[:, 0] <= 3]
            assert len(baseline) >= 10
            a[:, 1:] -= np.median(baseline[:, 1:], axis=0)
            future = a[a[:, 0] >= 6]
            true_active = np.abs(future[:, 2]) >= .02
            products = future[:, 1] * future[:, 2]
            sdk_active = np.abs(future[:, 3]) >= 3
            sdk_strong = np.abs(future[:, 3]) >= 7
            summary = {"frames": len(a), "future_frames": len(future), "baseline_end_s": 3, "validation_start_s": 6,
                       "reference_point_source": "原厂可靠肩髋表面坐标；模型只复用人体范围，未据此训练或更换方向",
                       "scope_note": "仅检验深度分支的方向信号，非完整单目动作识别准确率；不优化延迟或拟合未来帧",
                       "surface_depth_difference_correlation": correlation(future[:, 1], future[:, 2]),
                       "sdk_pitch_correlation": correlation(future[:, 1], future[:, 3]),
                       "surface_active_frames": int(true_active.sum()),
                       "surface_direction_agreement_pct": float(np.mean(products[true_active] > 0) * 100) if true_active.any() else None,
                       "sdk_active_frames": int(sdk_active.sum()),
                       "sdk_direction_agreement_pct": float(np.mean((future[:, 1] * future[:, 3])[sdk_active] > 0) * 100) if sdk_active.any() else None,
                       "sdk_strong_frames": int(sdk_strong.sum()),
                       "sdk_strong_direction_agreement_pct": float(np.mean((future[:, 1] * future[:, 3])[sdk_strong] > 0) * 100) if sdk_strong.any() else None,
                       "sdk_pitch_range_deg": [float(a[:, 3].min()), float(a[:, 3].max())]}
            for name, sign in (("forward", -1), ("back", 1)):
                active = future[:, 3] * sign >= 3
                summary[name] = {"frames": int(active.sum()), "direction_agreement_pct": float(np.mean(future[active, 1] * sign > 0) * 100) if active.any() else None}
            report[f"{ident}-{scope}"] = summary
            traces[f"{ident}-{scope}"] = a.tolist()
    (directory / "lean-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / "lean-traces.json").write_text(json.dumps(traces), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("prepare", "evaluate", "report"))
    parser.add_argument("--recording", type=Path, required=True)
    parser.add_argument("--pose-model")
    parser.add_argument("--model", choices=("dav2_metric_s", "vda_metric_s", "zipdepth"))
    args = parser.parse_args()
    if args.phase == "prepare":
        prepare(args.recording, args.pose_model)
    elif args.phase == "evaluate":
        evaluate(args.recording, args.model)
    else:
        report(args.recording)
