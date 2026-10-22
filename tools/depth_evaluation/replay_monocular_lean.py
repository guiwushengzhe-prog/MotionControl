"""第二段实录的完整单目链回放；原厂角度只在输出评分时读取，不送入预测。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import queue
import sys

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from motioncontrol.body_lean import BodyLeanController
from motioncontrol.depth_comparison import DEFAULTS, DepthComparison, MODELS
from motioncontrol.monocular_lean import MonocularLean
from tools.depth_evaluation.prepare_recording import read_rows


class ReplayInput:
    _persist = False
    _monocular_neutral_revision = 1
    sensitivity_experiments = {"fore_aft_source": "monocular", "learn_monocular": False}

    def __init__(self):
        self.received = queue.Queue()

    def publish_monocular_lean(self, state, revision=None):
        self.received.put(state)


def score(rows):
    # 两路各自采用最初三秒的末1.1秒作中立，不寻找最佳时间偏移或调换方向。
    center = float(np.median([r["reference_deg"] for r in rows if 1.9 <= r["t"] <= 3.]))
    truth, confusion, compared = "neutral", {a: {b: 0 for b in ("forward", "neutral", "back", "unavailable")}
                                           for a in ("forward", "neutral", "back")}, []
    for row in rows:
        angle = row["reference_deg"] - center
        truth = ("forward" if angle <= -(5 if truth == "forward" else 7) else
                 "back" if angle >= (5 if truth == "back" else 7) else "neutral")
        row.update(reference_relative_deg=angle, reference_state=truth)
        if row["t"] >= 6:
            confusion[truth][row["control_state"]] += 1
            if row["angle_deg"] is not None:
                compared.append(row)
    error = [abs(r["angle_deg"] - r["reference_relative_deg"]) for r in compared]
    total = sum(sum(r.values()) for r in confusion.values())
    return {"evaluated_frames": total, "reference_center_deg": center, "confusion": confusion,
            "state_agreement": sum(confusion[k][k] for k in confusion) / total if total else None,
            "relative_angle_mae_deg": float(np.mean(error)) if error else None,
            "note": "第二段回放，非独立真人验收；未用参考训练或修正预测，后倾样本较少"}


def replay(recording, model, cached):
    directory = Path(recording).resolve() / "analysis"
    samples = read_rows(directory / ("lean-region-features.jsonl" if cached else "lean-samples.jsonl"))
    controller, rows = BodyLeanController(), []
    engine, manager, source = MonocularLean(), None, ReplayInput()
    engine.set_context(["recorded-rgb-only", model])
    if not cached:
        manager = DepthComparison(source)
        manager.config = {**DEFAULTS, "enabled": False, "model": model, "monocular_model": model,
                          "input_scope": "full", "max_fps": 30, "auto_scale": False}
    last = {}
    try:
        for sample in samples:
            at = float(sample["t"])
            if cached:
                state = engine.update(sample["torso"], at, reference=None, learn=False)
            else:
                frame = cv2.imread(sample["file"])
                if frame is None:
                    raise ValueError("录制彩色帧无法读取")
                frame = cv2.flip(frame[1::3, 1::3].copy(), 1)
                # 回放按录制时间顺序逐帧运行，不把离线耗时冒充实时延迟。
                manager._last_submit = -float("inf")
                manager.submit(frame, sample["pose"], None, {}, "none", at, ["recorded-rgb-only", 640, 360, "mirrored"])
                state = source.received.get(timeout=120)
                if not state or state.get("sample_at") != at:
                    raise RuntimeError((state or {}).get("reason", manager.status()["error"]))
                last = manager.status()["results"].get(model, {})
                if last.get("comparison", {}).get("reference_points"):
                    raise AssertionError("纯单目回放不应收到原厂深度")
            actions = controller.update(None, None, None, 640, 360, at,
                forward=True, back=True, fore_aft_source="monocular", monocular=state)
            rows.append({"t": at, "reference_deg": sample.get("reference_deg", sample.get("sdk_pitch_camera_deg")),
                "valid": state["valid"], "angle_deg": state.get("angle_deg"),
                "control_state": "unavailable" if not state["valid"] else
                    "forward" if "lean_forward" in actions else "back" if "lean_back" in actions else "neutral",
                "sampled_regions": state.get("sampled_regions"), "readout": state.get("readout"),
                "reference_used_for_prediction": state.get("reference_used_for_prediction", False)})
    finally:
        if manager:
            manager.close()
    report = {"model": model, "mode": "cached-features" if cached else "real-worker-rgb-only", "frames": len(rows),
        "valid_after_neutral": sum(r["valid"] for r in rows if r["t"] >= 3), "learned_readout_used": any(r["readout"] == "learned" for r in rows),
        "kinect_prediction_inputs": 0, "input_shape": last.get("input_shape"),
        "inference_avg_ms": last.get("inference_avg_ms"), "gpu_reserved_mb": last.get("gpu_reserved_mb"), **score(rows)}
    label = f"monocular-{model}-{'cached' if cached else 'worker'}"
    (directory / (label + "-rows.jsonl")).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    (directory / (label + ".json")).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--recording", required=True)
    parser.add_argument("--model", choices=MODELS, default="vda_metric_s")
    parser.add_argument("--cached", action="store_true", help="仅复用已生成的VDA胸腹特征，不运行大模型")
    args = parser.parse_args()
    if args.cached and args.model != "vda_metric_s":
        parser.error("现有胸腹缓存仅由VDA生成，不能当作其他模型")
    replay(args.recording, args.model, args.cached)
