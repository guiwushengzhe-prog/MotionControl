"""Sample the repository's existing Hand Landmarker model (test artifact only).

This is intentionally a sparse evidence check, not a second production
pipeline: one image-mode inference per approximately one second of each clip.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("videos", nargs=2, type=Path)
    args = parser.parse_args()

    import cv2
    import mediapipe as mp
    from mediapipe.tasks.python import BaseOptions
    from mediapipe.tasks.python import vision

    args.output.mkdir(parents=True, exist_ok=True)
    all_summary = {}
    all_rows = []
    options = vision.HandLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(args.model.resolve())),
        running_mode=vision.RunningMode.IMAGE,
        num_hands=2,
        min_hand_detection_confidence=0.40,
        min_hand_presence_confidence=0.40,
        min_tracking_confidence=0.40,
    )
    for video in args.videos:
        cap = cv2.VideoCapture(str(video.resolve()))
        if not cap.isOpened():
            raise RuntimeError(f"cannot open {video}")
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
        sample_step = max(1, round(fps))
        index = 0
        rows = []
        no_hand_run = 0
        no_hand_runs = []
        with vision.HandLandmarker.create_from_options(options) as detector:
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                if index % sample_step:
                    index += 1
                    continue
                image = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                result = detector.detect(image)
                labels = []
                detections = []
                for hand_index, landmarks in enumerate(result.hand_landmarks):
                    handedness = result.handedness[hand_index][0].category_name if hand_index < len(result.handedness) else "unknown"
                    labels.append(handedness or "unknown")
                    # Keep a compact but real 21-point record for auditability.
                    detections.append({
                        "label": handedness or "unknown",
                        "landmarks": [
                            {"x": float(point.x), "y": float(point.y), "z": float(point.z)}
                            for point in landmarks
                        ],
                    })
                has_hand = bool(detections)
                if has_hand:
                    if no_hand_run:
                        no_hand_runs.append(no_hand_run)
                    no_hand_run = 0
                else:
                    no_hand_run += 1
                rows.append({"t": round(index / max(fps, 1e-6), 6), "hand_count": len(detections), "labels": labels, "hands": detections})
                index += 1
        cap.release()
        if no_hand_run:
            no_hand_runs.append(no_hand_run)
        summary = {
            "video": str(video.resolve()),
            "model": str(args.model.resolve()),
            "fps": fps,
            "sample_step_frames": sample_step,
            "sample_count": len(rows),
            "samples_with_any_hand": sum(bool(row["hand_count"]) for row in rows),
            "samples_with_two_hands": sum(row["hand_count"] >= 2 for row in rows),
            "any_hand_rate": sum(bool(row["hand_count"]) for row in rows) / max(1, len(rows)),
            "left_label_rate": sum("Left" in row["labels"] for row in rows) / max(1, len(rows)),
            "right_label_rate": sum("Right" in row["labels"] for row in rows) / max(1, len(rows)),
            "max_no_hand_run_samples": max(no_hand_runs, default=0),
            "note": "Sparse image-mode sampling only; handedness may be mirror-dependent. Not a continuous hand-control result.",
        }
        all_summary[video.name] = summary
        all_rows.extend({"video": video.name, "t": row["t"], "hand_count": row["hand_count"], "labels": ",".join(row["labels"])} for row in rows)
        (args.output / f"{video.stem}.hand_samples.json").write_text(json.dumps({"summary": summary, "samples": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output / "hand_sample_summary.json").write_text(json.dumps(all_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    with (args.output / "hand_sample_summary.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["video", "t", "hand_count", "labels"])
        writer.writeheader()
        writer.writerows(all_rows)
    print(json.dumps(all_summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
