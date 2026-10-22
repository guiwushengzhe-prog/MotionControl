"""在既有第二段上检验胸腹内部区域特征，保存深度以便后续重放不再运行大模型。"""
import argparse
import json
from pathlib import Path
import sys

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from motioncontrol.depth_comparison_worker import Adapter
from motioncontrol.depth_input import person_rect
from motioncontrol.monocular_lean_features import torso_features
from tools.depth_evaluation.prepare_recording import read_rows

parser = argparse.ArgumentParser()
parser.add_argument("--recording", type=Path, required=True)
args = parser.parse_args()
directory = args.recording / "analysis"
samples = read_rows(directory / "lean-samples.jsonl")
runtime = json.loads((Path.home() / "AppData/Local/MotionControl/depth_comparison_runtime.json").read_text(encoding="utf-8-sig"))
adapter = Adapter("vda_metric_s", runtime["models"]["vda_metric_s"], 392)
rect = person_rect(samples[0]["pose"], 640, 360)
x, y, w, h = rect
rows, maps = [], []
for sample in samples:
    frame = cv2.flip(cv2.imread(sample["file"])[1::3, 1::3].copy(), 1)
    depth = adapter.infer(frame[y:y+h, x:x+w], ("person", rect))
    pose = {n: {**p, "x": (p["x"] * 640 - x) / w, "y": (p["y"] * 360 - y) / h} for n, p in sample["pose"].items()}
    result = torso_features(depth, pose, source_height=360)
    rows.append({"t": sample["t"], "frame_index": sample["frame_index"], "pose": pose,
                 "reference_deg": sample["sdk_pitch_camera_deg"], "torso": result})
    maps.append(depth.astype(np.float16))
with (directory / "lean-region-features.jsonl").open("w", encoding="utf-8") as stream:
    for row in rows:
        stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
np.savez_compressed(directory / "lean-region-depths.npz", depth=np.stack(maps), rect=rect)
valid = [r for r in rows if r["torso"] is not None]
print(json.dumps({"frames": len(rows), "valid": len(valid),
    "geometric_correlation": float(np.corrcoef([r["torso"]["geometric_deg"] for r in valid], [r["reference_deg"] for r in valid])[0, 1])}), flush=True)
