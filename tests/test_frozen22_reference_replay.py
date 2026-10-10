"""Replay the audited R3 frozen22 recordings against the product helpers.

常规测试使用从独立冻结参考程序计算并随仓库保存的真实帧和预期值。
设置 MOTIONCONTROL_FROZEN22_ROOT 后额外回放完整归档；显式指定的归档缺失应失败。
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import sys
from pathlib import Path

import pytest

from motioncontrol import head_control as product

FIXTURE = Path(__file__).parent / "fixtures" / "frozen22_reference.json"


def _check_saved_reference(tag):
    stream = json.loads(FIXTURE.read_text(encoding="utf-8"))["streams"][tag]
    center, sigma, signature = (stream[key] for key in
                                ("calibration_center", "calibration_sigma", "signature"))
    assert len(stream["cases"]) >= 16
    for case in stream["cases"]:
        feature, scale = product._head11_local_feature(case["pose"], case["width"], case["height"])
        assert feature == pytest.approx(case["feature"], abs=1e-12, rel=0)
        assert scale == pytest.approx(case["scale"], abs=1e-12, rel=0)
        yaw = product._personal22_matched_yaw(feature, center, sigma, signature)
        assert yaw == pytest.approx(case["yaw"], abs=1e-12, rel=0)


def _load_reference(path: Path):
    spec = importlib.util.spec_from_file_location("frozen22_reference", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load reference module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _pose_map(landmarks: object) -> dict[str, dict] | None:
    if not isinstance(landmarks, list) or len(landmarks) != 33:
        return None
    names = (
        "nose", "left_eye_inner", "left_eye", "left_eye_outer",
        "right_eye_inner", "right_eye", "right_eye_outer",
        "left_ear", "right_ear", "mouth_left", "mouth_right",
        "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
        "left_wrist", "right_wrist", "left_pinky", "right_pinky",
        "left_index", "right_index", "left_thumb", "right_thumb",
        "left_hip", "right_hip", "left_knee", "right_knee",
        "left_ankle", "right_ankle", "left_heel", "right_heel",
        "left_foot_index", "right_foot_index",
    )
    result: dict[str, dict] = {}
    for name, point in zip(names, landmarks):
        if isinstance(point, dict):
            result[name] = dict(point)
    return result


@pytest.mark.parametrize("tag", ["A", "B"])
def test_frozen22_matches_audited_reference_replay(tag: str):
    _check_saved_reference(tag)
    root_raw = os.environ.get("MOTIONCONTROL_FROZEN22_ROOT", "").strip()
    if not root_raw:
        return
    root = Path(root_raw)
    reference_path = root / "frozen22" / "MotionControl_head_control_v186_stageA_frozen22_evidence.py"
    source_path = root / "data" / f"{tag}.pose.frames.jsonl"
    baseline_path = root / "frozen22" / f"baseline_{tag}.json"
    if not reference_path.is_file() or not source_path.is_file() or not baseline_path.is_file():
        pytest.fail(f"指定的冻结参考归档不完整：{root}")
    reference = _load_reference(reference_path)
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    center = tuple(float(value) for value in baseline["calibration_center"])
    sigma = tuple(float(value) for value in baseline["calibration_sigma"])
    signature = tuple(float(value) for value in baseline["signature"])

    max_feature_error = 0.0
    max_yaw_error = 0.0
    feature_frames = 0
    with source_path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            pose = _pose_map(row.get("image_landmarks"))
            width = int(row.get("width") or 0)
            height = int(row.get("height") or 0)
            ours = product._head11_local_feature(pose, width, height)
            theirs = reference._head11_local_feature(pose, width, height)
            assert (ours is None) == (theirs is None)
            if ours is None or theirs is None:
                continue
            feature_frames += 1
            ours_feature, ours_scale = ours
            ref_feature, ref_scale = theirs
            max_feature_error = max(
                max_feature_error,
                abs(float(ours_scale) - float(ref_scale)),
                *(abs(float(a) - float(b)) for a, b in zip(ours_feature, ref_feature)),
            )
            ours_yaw = product._personal22_matched_yaw(
                ours_feature, center, sigma, signature
            )
            ref_yaw = reference._personal22_matched_yaw(
                ref_feature, center, sigma, signature
            )
            if math.isfinite(ours_yaw) or math.isfinite(ref_yaw):
                assert math.isfinite(ours_yaw) and math.isfinite(ref_yaw)
                max_yaw_error = max(max_yaw_error, abs(ours_yaw - ref_yaw))

    assert feature_frames > 0
    assert max_feature_error <= 1e-12
    assert max_yaw_error <= 1e-12
    print(
        f"{tag}: frames={feature_frames} max_feature_error={max_feature_error:.3e} "
        f"max_yaw_error={max_yaw_error:.3e}"
    )
