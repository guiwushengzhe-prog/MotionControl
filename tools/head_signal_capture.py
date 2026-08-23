#!/usr/bin/env python3
"""Real-person A/B signal capture for head-control-v4.3-reference-video-tuned.

The tool talks to an already-running MotionControl service.  It does not load
MediaPipe, open a second camera, or synthesize pose frames, so it works with
both the computer-camera and phone-pose source.

Examples:
    python tools/head_signal_capture.py --algorithm pnp
    python tools/head_signal_capture.py --algorithm ratio
"""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path
from urllib import request

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SEGMENTS = (
    ("front", "正视前方"),
    ("up", "抬头"),
    ("down", "低头"),
    ("left", "左转"),
    ("right", "右转"),
    ("body_forward", "头保持正视，身体稍向前靠"),
    ("body_side", "头保持正视，身体稍向一侧移动"),
)
SIGNAL_FIELDS = (
    "raw_yaw", "raw_pitch", "raw_roll",
    "filtered_yaw", "filtered_pitch",
    "normalized_x", "normalized_y",
    "output_x", "output_y", "confidence",
)


def _get(base: str, route: str) -> dict:
    with request.urlopen(base.rstrip("/") + route, timeout=3.0) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(base: str, route: str, body: dict) -> dict:
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        base.rstrip("/") + route,
        data=raw,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with request.urlopen(req, timeout=3.0) as response:
        return json.loads(response.read().decode("utf-8"))


def _head(snapshot: dict) -> dict:
    return ((snapshot.get("kernel") or {}).get("head") or {})


def _median(values: list[float]) -> float:
    values = sorted(v for v in values if math.isfinite(v))
    if not values:
        return math.nan
    m = len(values) // 2
    return values[m] if len(values) % 2 else (values[m - 1] + values[m]) * 0.5


def _stats(values: list[float]) -> dict:
    values = [v for v in values if math.isfinite(v)]
    if not values:
        return {"sample_count": 0, "median": None, "min": None, "max": None}
    return {
        "sample_count": len(values),
        "median": round(_median(values), 6),
        "min": round(min(values), 6),
        "max": round(max(values), 6),
    }


def _float(value) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return math.nan
    return value if math.isfinite(value) else math.nan


def _capture(base: str, label: str, duration: float) -> dict:
    input(f"\n>>> {label}，准备好后按 Enter 开始 {duration:.1f}s 采集...")
    samples = {key: [] for key in SIGNAL_FIELDS}
    validity = {"valid": 0, "invalid": 0}
    algorithms: dict[str, int] = {}
    start = time.monotonic()
    while time.monotonic() - start < duration:
        snapshot = _get(base, "/api/kernel/status")
        head = _head(snapshot)
        algorithms[str(head.get("algorithm") or "unknown")] = algorithms.get(str(head.get("algorithm") or "unknown"), 0) + 1
        validity["valid" if head.get("estimate_valid") else "invalid"] += 1
        for key in SIGNAL_FIELDS:
            samples[key].append(_float(head.get(key)))
        time.sleep(0.03)
    result = {key: _stats(values) for key, values in samples.items()}
    result["estimate_validity"] = validity
    result["algorithm_distribution"] = algorithms
    return result


def _capture_center(base: str, timeout: float = 6.0) -> dict:
    _post(base, "/api/head/calibration/start", {})
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        last = _head(_get(base, "/api/kernel/status"))
        if not last.get("calibrating") and last.get("calibrated"):
            return last
        time.sleep(0.1)
    raise RuntimeError(f"中心记录未在 {timeout:.1f}s 内完成：{last.get('estimate_error') or last.get('notice') or 'unknown'}")


def main() -> int:
    parser = argparse.ArgumentParser(description="head-control-v4.3-reference-video-tuned 真人 A/B 信号采集")
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    parser.add_argument("--algorithm", choices=("pnp", "ratio"), default="pnp")
    parser.add_argument("--duration", type=float, default=3.0)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    snapshot = _get(args.base, "/api/kernel/status")
    if snapshot.get("version") != "0.9.5":
        raise SystemExit(f"服务版本不是 0.9.5：{snapshot.get('version')!r}")
    if not (snapshot.get("kernel") or {}).get("active_body_source"):
        raise SystemExit("当前没有人体姿态源；先启动电脑摄像头或连接手机姿态")

    _post(args.base, "/api/head/config", {"algorithm": args.algorithm, "enabled": True})
    print(f"算法：{args.algorithm}")
    print("先记录自然正视中心；期间不会主动移动鼠标。")
    input("自然正视后按 Enter 开始中心记录...")
    center = _capture_center(args.base)
    print(f"中心完成：yaw={center.get('center_yaw')} pitch={center.get('center_pitch')} 稳定区X/Y={center.get('effective_deadzone_x')}/{center.get('effective_deadzone_y')}")

    segments = {}
    for key, label in SEGMENTS:
        segments[key] = _capture(args.base, label, args.duration)

    if args.output:
        out = Path(args.output)
    else:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out = PROJECT_ROOT / "output" / f"head-control-v4.3-reference-video-tuned-{args.algorithm}-real-signal-{stamp}.json"
    payload = {
        "tool": "head_signal_capture.py",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "service_version": snapshot.get("version"),
        "signal_version": "head-control-v4.3-reference-video-tuned",
        "algorithm": args.algorithm,
        "segment_duration_s": args.duration,
        "center": {
            key: center.get(key)
            for key in (
                "center_yaw", "center_pitch", "noise_yaw", "noise_pitch",
                "effective_deadzone_x", "effective_deadzone_y",
            )
        },
        "segments": segments,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完成：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
