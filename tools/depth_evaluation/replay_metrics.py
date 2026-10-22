"""将录制的逐点真实预测送回产品统计器，验证实际校准门限与固定尺度。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from motioncontrol.depth_comparison_metrics import DepthMetrics
from tools.depth_evaluation.evaluate_recording import error_summary
from tools.depth_evaluation.prepare_recording import read_rows


def replay(path):
    rows = read_rows(path)
    relative = Path(path).name.startswith("zipdepth")
    metrics = DepthMetrics(relative=relative)
    future, raw_future, mapping_at_finish = [], [], None
    for row in rows:
        samples = [({"name": p["name"], "x": p["x"], "y": p["y"], "z": p["reference_m"]}, p["raw"])
                   for p in row["points"]]
        if samples and not metrics.calibration_attempted:
            metrics.start_calibration(now=row["t"])
        result = metrics.evaluate_samples(samples, now=row["t"])
        if not result["calibrating"] and metrics.mapping is not None:
            if mapping_at_finish is None:
                mapping_at_finish = dict(metrics.mapping)
            assert metrics.mapping == mapping_at_finish, "未来帧不应改变尺度"
        if row["t"] >= 6:
            if not relative:
                raw_future.extend((p["reference_m"], p["raw"]) for p in row["points"])
            future.extend((p["z"], p["calibrated_m"]) for p in result["points"] if p["calibrated_m"] is not None)
    report = {"prediction_file": str(path), "frames": len(rows), "calibration_points": len(metrics.calibration),
              "mapping": metrics.mapping, "calibration_message": result["calibration_message"],
              "validation_start_s": 6, "possible_future_points": sum(len(r["points"]) for r in rows if r["t"] >= 6),
              "raw_future_error": error_summary(raw_future), "calibrated_future_error": error_summary(future),
              "product_statistics": {k: v for k, v in result.items() if k != "points"}}
    if relative and metrics.calibration:
        x, z = np.asarray(metrics.calibration).T
        report["calibration_inverse_correlation"] = float(np.corrcoef(x, 1 / z)[0, 1])
    destination = Path(path).with_name(Path(path).name.replace("-points.jsonl", "-product-metrics.json"))
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, allow_nan=False), flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("prediction_files", nargs="+")
    for path in parser.parse_args().prediction_files:
        replay(path)
