"""Offline, no-output comparison of the legacy wrist point and hand anchors.

The inputs are the already captured Pose33 streams in
``test_results/head_hand_eval_20260828``.  This is a continuity/engineering
comparison, not a gesture-accuracy claim: the streams have no hand-zone
ground-truth labels and contain no continuous 21-point Hand Landmarker data.
"""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from motioncontrol.hand_anchor import HAND_ANCHOR_VERSION, HandAnchorTracker


ROOT = Path(__file__).resolve().parents[1]
INPUTS = {
    "bf5ad4437d893c1bbb493ff90ab5d368": ROOT / "test_results" / "head_hand_eval_20260828" / "final" / "bf5ad4437d893c1bbb493ff90ab5d368.pose_stream.json",
    "c7fbc865832d892ec4aaa362fda5800d": ROOT / "test_results" / "head_hand_eval_20260828" / "final" / "c7fbc865832d892ec4aaa362fda5800d.pose_stream.json",
}
OUTPUT_DIR = ROOT / "test_results" / "hand_anchor_eval_v1_20260828"


def finite(value) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def wrist_valid(pose: dict, side: str) -> bool:
    point = pose.get(f"{side}_wrist")
    return isinstance(point, dict) and finite(point.get("x")) is not None and finite(point.get("y")) is not None and finite(point.get("score", 0.0)) >= 0.42


def wrist_xy(pose: dict, side: str) -> tuple[float, float] | None:
    if not wrist_valid(pose, side):
        return None
    return float(pose[f"{side}_wrist"]["x"]), float(pose[f"{side}_wrist"]["y"])


def longest_missing(values: list[bool]) -> int:
    longest = current = 0
    for valid in values:
        if valid:
            current = 0
        else:
            current += 1
            longest = max(longest, current)
    return longest


def p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(len(ordered) * 0.95) - 1))]


def hysteresis_events(values: list[float], *, edge: float, side_sign: float, band: float = 0.015) -> tuple[int, int, list[int]]:
    state = False
    starts: list[int] = []
    durations: list[int] = []
    start = None
    for index, value in enumerate(values):
        if not math.isfinite(value):
            continue
        signed = side_sign * (value - edge)
        if not state and signed >= band:
            state = True
            start = index
            starts.append(index)
        elif state and signed <= -band:
            state = False
            if start is not None:
                durations.append(index - start)
                start = None
    if state and start is not None:
        durations.append(len(values) - start)
    return len(starts), sum(1 for duration in durations if duration <= 2), durations


def boundary_jitter(values: list[float], *, edge: float, side_sign: float, band: float = 0.025) -> dict:
    distances = []
    for previous, current in zip(values, values[1:]):
        if previous is None or current is None:
            continue
        if not math.isfinite(float(previous)) or not math.isfinite(float(current)):
            continue
        d0 = side_sign * (previous - edge)
        d1 = side_sign * (current - edge)
        if abs(d0) <= band or abs(d1) <= band:
            distances.append(abs(d1 - d0))
    return {"samples": len(distances), "median": statistics.median(distances) if distances else None, "p95": p95(distances), "max": max(distances) if distances else None}


def recapture_jumps(values: list[tuple[float, float] | None], valid: list[bool]) -> dict:
    jumps: list[float] = []
    previous = None
    missing = 0
    for point, is_valid in zip(values, valid):
        if not is_valid or point is None:
            missing += 1
            continue
        if previous is not None and missing:
            jumps.append(math.hypot(point[0] - previous[0], point[1] - previous[1]))
        previous = point
        missing = 0
    return {"samples": len(jumps), "median": statistics.median(jumps) if jumps else None, "p95": p95(jumps), "max": max(jumps) if jumps else None}


def evaluate_video(video_id: str, path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    frames = payload.get("frames") or []
    result = {"video": video_id, "source": str(path), "frame_count": len(frames), "sides": {}}
    for side in ("left", "right"):
        tracker = HandAnchorTracker(side)
        old_valid: list[bool] = []
        new_valid: list[bool] = []
        new_observed: list[bool] = []
        new_zone_ready: list[bool] = []
        old_values: list[tuple[float, float] | None] = []
        new_values: list[tuple[float, float] | None] = []
        confidences: list[float] = []
        sources: dict[str, int] = {}
        for frame in frames:
            pose = frame.get("pose") if isinstance(frame, dict) else None
            pose = pose if isinstance(pose, dict) else {}
            t = finite(frame.get("t", 0.0)) if isinstance(frame, dict) else 0.0
            t = 0.0 if t is None else t
            old_point = wrist_xy(pose, side)
            state = tracker.update(pose, now=t)
            point = state.get("point") if state.get("valid") else None
            old_valid.append(old_point is not None)
            new_valid.append(bool(state.get("valid")))
            new_observed.append(state.get("source") == "observed")
            new_zone_ready.append(bool(point and finite(point.get("score")) is not None and float(point["score"]) >= 0.42))
            old_values.append(old_point)
            new_values.append((float(point["x"]), float(point["y"])) if point else None)
            confidences.append(float(state.get("confidence", 0.0)))
            source = str(state.get("source", "missing"))
            sources[source] = sources.get(source, 0) + 1

        common = [a for a, b in zip(old_values, new_values) if a is not None and b is not None]
        all_x = [point[0] for point in common]
        edge = statistics.median(all_x) if all_x else 0.5
        side_sign = -1.0 if side == "left" else 1.0
        old_x = [point[0] if point else math.nan for point in old_values]
        new_x = [point[0] if point else math.nan for point in new_values]
        old_events, old_short, old_durations = hysteresis_events(old_x, edge=edge, side_sign=side_sign)
        new_events, new_short, new_durations = hysteresis_events(new_x, edge=edge, side_sign=side_sign)
        result["sides"][side] = {
            "legacy_wrist": {
                "valid_frames": sum(old_valid),
                "availability_rate": sum(old_valid) / max(1, len(frames)),
                "longest_missing_frames": longest_missing(old_valid),
                "recapture_jump": recapture_jumps(old_values, old_valid),
                "boundary_jitter": boundary_jitter(old_x, edge=edge, side_sign=side_sign),
                "boundary_events": old_events,
                "short_event_proxy": old_short,
                "event_durations_frames": old_durations,
            },
            "hand_anchor": {
                "valid_frames_including_fallback": sum(new_valid),
                "available_rate_including_fallback": sum(new_valid) / max(1, len(frames)),
                "observed_frames": sum(new_observed),
                "observed_rate": sum(new_observed) / max(1, len(frames)),
                "zone_ready_frames": sum(new_zone_ready),
                "zone_ready_rate": sum(new_zone_ready) / max(1, len(frames)),
                "longest_missing_frames": longest_missing(new_valid),
                "recapture_jump": recapture_jumps(new_values, new_valid),
                "boundary_jitter": boundary_jitter(new_x, edge=edge, side_sign=side_sign),
                "boundary_events": new_events,
                "short_event_proxy": new_short,
                "event_durations_frames": new_durations,
                "confidence_median": statistics.median(confidences) if confidences else 0.0,
                "source_counts": sources,
                "common_boundary_edge_x": edge,
            },
            "comparison": {
                "availability_delta_including_fallback": sum(new_valid) / max(1, len(frames)) - sum(old_valid) / max(1, len(frames)),
                "zone_ready_delta": sum(new_zone_ready) / max(1, len(frames)) - sum(old_valid) / max(1, len(frames)),
                "longest_missing_delta_frames": longest_missing(new_valid) - longest_missing(old_valid),
                "short_event_proxy_delta": new_short - old_short,
            },
        }
    return result


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    videos = [evaluate_video(video_id, path) for video_id, path in INPUTS.items()]
    aggregate = {"version": HAND_ANCHOR_VERSION, "inputs": videos, "notes": [
        "All results are offline and no-output; the pose streams were captured previously with MediaPipe Pose Full.",
        "Boundary events use a common median-x edge and 2-frame hysteresis as an engineering jitter/false-trigger proxy, not action labels.",
        "Hand anchor valid includes the finite 250 ms forearm/arm fallback; zone_ready additionally satisfies the legacy 0.42 score gate.",
        "Pose33 thumb/index/pinky points are support points. This does not evaluate continuous 21-point fist/pinch recognition.",
    ]}
    json_path = OUTPUT_DIR / "HAND_ANCHOR_AB.json"
    json_path.write_text(json.dumps(aggregate, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# Hand Anchor v1 Offline A/B",
        "",
        "范围：两段已有 MediaPipe Pose33 流、无输出。旧方案为单腕点；新方案为腕主导的 wrist+thumb+index+pinky 鲁棒 anchor，含 250ms 有限前臂降级。",
        "边界代理使用共同 median-x 边界和 2 帧迟滞，只反映连续性/抖动，不是动作准确率。",
        "",
        "| 视频 | 侧别 | 旧腕有效率 | 新 anchor 可用率(含降级) | 新直接观测率 | 新 zone-ready率 | 旧最长缺失 | 新最长缺失 | 旧/新短事件代理 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for video in videos:
        for side, item in video["sides"].items():
            old, new = item["legacy_wrist"], item["hand_anchor"]
            lines.append(
                f"| {video['video']} | {side} | {old['availability_rate']:.2%} | {new['available_rate_including_fallback']:.2%} | {new['observed_rate']:.2%} | {new['zone_ready_rate']:.2%} | {old['longest_missing_frames']} | {new['longest_missing_frames']} | {old['short_event_proxy']} / {new['short_event_proxy']} |"
            )
    lines += [
        "",
        "详细 JSON：`HAND_ANCHOR_AB.json`。本轮没有启动摄像头、游戏或输出；仍需真人确认真实区域边界和动作语义。",
    ]
    (OUTPUT_DIR / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(json_path), "videos": len(videos), "frames": [v["frame_count"] for v in videos]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
