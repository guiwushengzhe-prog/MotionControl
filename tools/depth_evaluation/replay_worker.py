"""用录制帧核验正式子进程协议、自动校准及新帧统计；不接触设备或用户偏好。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from motioncontrol.depth_comparison_worker import read_packet, write_packet
from motioncontrol.depth_input import crop_references, person_rect
from tools.depth_evaluation.evaluate_recording import error_summary
from tools.depth_evaluation.prepare_recording import read_rows


def replay(recording, model, scope):
    directory = Path(recording) / "analysis"
    samples = read_rows(directory / "samples.jsonl")
    runtime = Path.home() / "AppData/Local/MotionControl/depth_comparison_runtime.json"
    python = json.loads(runtime.read_text(encoding="utf-8-sig"))["python"]
    rect, future, results = None, [], []
    with (directory / f"{model}-{scope}-worker.log").open("w", encoding="utf-8") as log:
        with subprocess.Popen([python, "-X", "utf8", "-m", "motioncontrol.depth_comparison_worker", "--runtime", str(runtime),
                               "--model", model, "--size", "392", "--auto-scale"],
                              cwd=Path(__file__).resolve().parents[2], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=log, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0) as child:
            try:
                info, _ = read_packet(child.stdout)
                if info.get("kind") != "ready":
                    raise RuntimeError(info)
                for sample in samples:
                    frame = cv2.flip(cv2.imread(sample["file"])[1::3, 1::3].copy(), 1)
                    h, w = frame.shape[:2]
                    if rect is None:
                        rect = person_rect(sample["pose"], w, h) if scope == "person" else (0, 0, w, h)
                    if rect is None:
                        continue
                    x, y, rw, rh = rect
                    meta = {"sequence": sample["frame_index"], "width": rw, "height": rh, "sample_at": sample["t"],
                            "signature": ["recorded-source", rect],
                            "references": crop_references(sample["references"], rect, w, h)}
                    write_packet(child.stdin, meta, frame[y:y+rh, x:x+rw].copy().tobytes())
                    result, preview = read_packet(child.stdout)
                    if result.get("kind") != "frame" or not preview:
                        raise RuntimeError(result)
                    results.append(result)
                    if sample["t"] >= 6:
                        future.extend((p["z"], p["calibrated_m"]) for p in result["comparison"]["points"] if p["calibrated_m"] is not None)
                child.stdin.close()
                child.wait(timeout=15)
                if child.returncode:
                    raise RuntimeError(f"深度进程异常退出：{child.returncode}")
            finally:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=10)
    report = {"model": model, "scope": scope, "frames": len(results), "ready": info,
              "input_shape": results[-1]["input_shape"], "future_error": error_summary(future),
              "comparison": {k: v for k, v in results[-1]["comparison"].items() if k != "points"}}
    (directory / f"{model}-{scope}-worker.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--recording", required=True)
    parser.add_argument("--model", required=True, choices=("dav2_metric_s", "vda_metric_s", "zipdepth"))
    parser.add_argument("--scope", choices=("full", "person"), default="person")
    args = parser.parse_args()
    replay(args.recording, args.model, args.scope)
