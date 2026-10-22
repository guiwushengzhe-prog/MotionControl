"""三个真实模型重放同一录制；原始误差、固定尺度留验与人体裁切分别计算。"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
from pathlib import Path
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from motioncontrol.depth_comparison_worker import Adapter
from motioncontrol.depth_comparison_metrics import DepthMetrics
from motioncontrol.depth_input import person_rect
from tools.depth_evaluation.prepare_recording import read_rows


def error_summary(pairs):
    if not pairs:
        return {"samples": 0}
    true, predicted = np.asarray(pairs, dtype=float).T
    error = predicted - true
    return {"samples": len(error), "mae_cm": round(float(np.abs(error).mean()) * 100, 2),
            "p95_cm": round(float(np.percentile(np.abs(error), 95)) * 100, 2),
            "bias_cm": round(float(error.mean()) * 100, 2)}


def calibration_models(rows):
    # 前三秒只拟合，六秒以后才验证，中间留三秒间隔，避免邻帧泄漏。
    train = [p for row in rows if row["t"] <= 3 for p in row["points"]]
    test = [p for row in rows if row["t"] >= 6 for p in row["points"]]
    report = {"train_points": len(train), "test_points": len(test), "validation_start_s": 6}
    if len(train) < 32 or not test:
        return report
    raw, true = np.asarray([[p["raw"], p["reference_m"]] for p in train]).T
    for kind in ("scale", "affine", "inverse_affine"):
        x, y = (raw, 1 / true) if kind == "inverse_affine" else (raw, true)
        keep = np.ones(len(x), bool)
        for _ in range(3):
            if kind == "scale":
                a, b = float(np.median(y[keep] / x[keep])), 0.
            else:
                a, b = np.linalg.lstsq(np.column_stack((x[keep], np.ones(keep.sum()))), y[keep], rcond=None)[0]
            residual = y - (a * x + b)
            mad = np.median(np.abs(residual - np.median(residual)))
            keep = np.abs(residual - np.median(residual)) <= max(.03, 3 * 1.4826 * mad)
        pairs = []
        for p in test:
            predicted = a * p["raw"] + b
            if kind == "inverse_affine":
                predicted = 1 / predicted if predicted > 0 else -1
            if .1 < predicted < 20:
                pairs.append((p["reference_m"], predicted))
        report[kind] = {"gain": float(a), "offset": float(b), **error_summary(pairs)}
    return report


def evaluate(recording, model, scope="full", size=392, fp32=False):
    directory = Path(recording) / "analysis"
    samples = read_rows(directory / "samples.jsonl")
    registry = json.loads((Path.home() / "AppData/Local/MotionControl/depth_comparison_runtime.json").read_text(encoding="utf-8-sig"))
    adapter = Adapter(model, registry["models"][model], size)
    if fp32:
        # 原官方32位路径用于排查半精度是否制造米级偏差。
        def infer(frame, _):
            with adapter.torch.inference_mode():
                tensor, (h, w) = adapter.model.image2tensor(frame, size)
                adapter.shape = list(tensor.shape[-2:])
                output = adapter.model(tensor)
                return adapter.torch.nn.functional.interpolate(output[:, None], (h, w), mode="bilinear", align_corners=True)[0, 0].cpu().numpy()
        adapter.infer = infer
    durations, rows, pairs = [], [], []
    rect = None
    for i, sample in enumerate(samples):
        raw = cv2.imread(sample["file"])
        frame = cv2.flip(raw[1::3, 1::3].copy(), 1)
        h, w = frame.shape[:2]
        if scope == "person" and rect is None:
            rect = person_rect(sample["pose"], w, h)
        x, y, rw, rh = rect if scope == "person" and rect else (0, 0, w, h)
        started = time.perf_counter()
        depth = adapter.infer(frame[y:y+rh, x:x+rw], (scope, rw, rh))
        if adapter.device == "cuda":
            adapter.torch.cuda.synchronize()
        duration = (time.perf_counter() - started) * 1000
        if i >= 2:
            durations.append(duration)
        points = []
        for point in sample["references"]:
            px, py = (point["x"] * w - x) / rw, (point["y"] * h - y) / rh
            if not 0 <= px <= 1 or not 0 <= py <= 1:
                continue
            value = DepthMetrics.sample(depth, px, py)
            if value is not None:
                points.append({"name": point["name"], "x": point["x"], "y": point["y"], "raw": value, "reference_m": point["z"]})
                if model != "zipdepth":
                    pairs.append((point["z"], value))
        rows.append({"frame_index": sample["frame_index"], "t": sample["t"], "points": points,
                     "inference_ms": round(duration, 2), "depth_pitch_deg": sample["depth_pitch_deg"]})
        if i % 40 == 0:
            print(json.dumps({"model": model, "scope": scope, "size": size, "processed": i, "total": len(samples)}, ensure_ascii=False), flush=True)
    report = {"model": model, "scope": scope, "size": size, "fp32": fp32, "frames": len(rows),
              "samples_sha256": hashlib.sha256((directory / "samples.jsonl").read_bytes()).hexdigest(),
              "input_shape": adapter.shape, "fixed_rect": rect, "raw_error": error_summary(pairs),
              "avg_ms": round(float(np.mean(durations)), 2), "p95_ms": round(float(np.percentile(durations, 95)), 2),
              "gpu_reserved_mb": round(adapter.torch.cuda.memory_reserved() / 2**20, 1) if adapter.device == "cuda" else None,
              "frozen_calibration": calibration_models(rows)}
    name = f"{model}-{scope}-{size}" + ("-fp32" if fp32 else "")
    (directory / (name + ".json")).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with (directory / (name + "-points.jsonl")).open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    del adapter
    gc.collect()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--recording", required=True)
    parser.add_argument("--model", choices=("dav2_metric_s", "vda_metric_s", "zipdepth"), required=True)
    parser.add_argument("--scope", choices=("full", "person"), default="full")
    parser.add_argument("--size", type=int, default=392)
    parser.add_argument("--fp32", action="store_true")
    args = parser.parse_args()
    evaluate(args.recording, args.model, args.scope, args.size, args.fp32)
