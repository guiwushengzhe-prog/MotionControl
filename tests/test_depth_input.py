"""验证裁切的像素/参考坐标、固定窗口和录制镜像对齐，不使用摄像头。"""
import json
from types import SimpleNamespace

import numpy as np
import pytest

from motioncontrol.depth_comparison import DepthComparison
from motioncontrol.depth_input import crop_references, person_rect
from tools.depth_evaluation.prepare_recording import prepare, read_rows


def pose(offset=0):
    names = ["left_shoulder", "right_shoulder", "nose", "left_hip", "right_hip", "left_knee", "right_knee", "left_ankle", "right_ankle"]
    return {name: {"x": .3 + (i % 3) * .05 + offset, "y": .2 + (i // 3) * .2, "score": 1} for i, name in enumerate(names)}


def test_person_crop_preserves_surface_distance_and_pixel_location():
    rect = person_rect(pose(), 640, 360)
    assert rect and rect[2] == rect[3] and rect[2] < 640
    assert person_rect(None, 640, 360) is None
    references = [{"name": "shoulder", "x": .35, "y": .4, "z": 1.7},
                  {"name": "outside", "x": 0, "y": .4, "z": 9}]
    result = crop_references(references, rect, 640, 360)
    assert len(result) == 1 and result[0]["z"] == 1.7
    assert result[0]["x"] * rect[2] + rect[0] == pytest.approx(.35 * 640)
    assert result[0]["y"] * rect[3] + rect[1] == pytest.approx(.4 * 360)


def test_small_false_person_or_face_only_cannot_lock_the_body_window():
    small = {name: {**p, "x": .1 + p["x"] * .05, "y": .1 + p["y"] * .05} for name, p in pose().items()}
    assert person_rect(small, 640, 360) is None
    incomplete = pose()
    incomplete["left_hip"]["score"] = .1
    assert person_rect(incomplete, 640, 360) is None


def test_person_window_stays_fixed_through_motion_and_brief_native_tracking_loss(monkeypatch):
    comparison = DepthComparison(SimpleNamespace())
    comparison.config.update(enabled=True, input_scope="person")
    comparison._thread = SimpleNamespace(is_alive=lambda: True, join=lambda **_: None)
    frame = np.arange(360 * 640 * 3, dtype=np.int32).reshape(360, 640, 3)
    monkeypatch.setattr("motioncontrol.depth_comparison.time.monotonic", lambda: 10)
    def submit(points, body):
        comparison._last_submit = 0
        comparison.submit(frame, points, body, {}, "none", 1, "camera")
        return comparison._pending
    assert submit(None, None) is None
    assert comparison._state == "waiting_person"
    meta, copied = submit(pose(), {"tracking_id": 7})
    x, y, w, h = meta["frame_region"]
    assert np.array_equal(copied, frame[y:y+h, x:x+w])
    assert not np.shares_memory(copied, frame)
    for body in (None, {"tracking_id": 7}):
        following, _ = submit(pose(.2), body)
        assert following["signature"] == meta["signature"]
        assert following["frame_region"] == meta["frame_region"]
    next_person, _ = submit(pose(.2), {"tracking_id": 8})
    assert next_person["frame_region"] != meta["frame_region"]
    comparison.action("reframe")
    assert comparison._rect is None and comparison._pending is None


def test_prepared_recording_keeps_display_mirror_and_factory_projection_opposite(tmp_path):
    kinect = tmp_path / "kinect"
    kinect.mkdir()
    body = {"sensor_seconds": 1, "matched_to_model": True, "body_valid": True, "surface_age_ms": 0,
            "points": {"left_shoulder": {"score": 1, "surface": {"z": 1.5, "color_x": .2, "color_y": .4}}}}
    frame = {"sample_t": 0, "pose": {}, "extra": {"depth_body": body,
             "body_model_identity": json.dumps(["camera", "model", "backend", "none", True]), "model_pose": pose()}}
    recording = tmp_path / "pose.jsonl"
    recording.write_text(json.dumps({"recorded_at": "2026-10-22"}) + "\n" + json.dumps(frame), encoding="utf-8")
    (kinect / "color_frames.jsonl").write_text(json.dumps({"sensor_ticks": 10_000_000, "frame_index": 1, "file": "unused.jpg"}), encoding="utf-8")
    (kinect / "bodies.jsonl").write_text(json.dumps({"sensor_ticks": 10_000_000}), encoding="utf-8")
    prepare(tmp_path, recording)
    assert read_rows(tmp_path / "analysis/samples.jsonl")[0]["references"][0]["x"] == .8
    assert read_rows(tmp_path / "analysis/projector-input.jsonl")[0]["pose"]["native_left_shoulder"]["x"] == pytest.approx(.2)
