"""自动命名、旧记录完整迁移、画面单独清理及中断恢复。"""

import json
import shutil
from pathlib import Path

from motioncontrol.recording_storage import (
    organize_recordings, recording_file, recording_path, recordings_summary,
)


def old_pose(root, name="pose-old.jsonl"):
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text(json.dumps({"schema": "motioncontrol.pose_recording.v1",
                               "recorded_at": "2026-09-25T00:30:10+08:00", "source": "mobile_pose:camera-id",
                               "duration_s": 15, "frames": 1}) + '\n{"t":0,"pose":{}}\n', encoding="utf-8")
    return path


def old_stereo(root, name="stereo-old", state="done"):
    session = root / name
    (session / "kinect/color").mkdir(parents=True)
    (session / "manifest.json").write_text(json.dumps({"schema": "motioncontrol.stereo_recording.v1",
                                                      "recorded_at": "2026-10-07T17:43:33.603829+08:00",
                                                      "state": state, "files": {"pc": "pc.jsonl"}}, indent=2), encoding="utf-8")
    for name in ("pc.jsonl", "phone.jsonl", "stereo.jsonl", "kinect/bodies.jsonl"):
        (session / name).write_text('{"pose":{}}\n', encoding="utf-8")
    (session / "kinect/color_frames.jsonl").write_text('{"file":"color/one.jpg"}\n', encoding="utf-8")
    (session / "kinect/color/one.jpg").write_bytes(b"old-image")
    (session / "kinect/depth_frames.jsonl").write_text('{"offset":0,"byte_count":12}\n', encoding="utf-8")
    (session / "kinect/depth.bin").write_bytes(b"old-depth-mm")
    return session


def test_names_include_date_source_duration_and_safe_label_without_writing(tmp_path):
    result = recording_path(tmp_path, source="mobile_pose:camera-uuid", label="触发_左手区/举手?",
                            recorded_at="2026-10-08T12:34:56.123456+08:00")
    assert result.relative_to(tmp_path).parts == ("姿态点", "2026-10-08", "12-34-56-123456_手机_触发_左手区_举手.jsonl")
    assert not result.parent.exists()


def test_known_pose_bytes_preserved_collision_not_overwritten_and_unknown_untouched(tmp_path):
    old = old_pose(tmp_path)
    original = old.read_bytes()
    existing = recording_path(tmp_path, source="mobile", label="骨骼_15秒", recorded_at="2026-09-25T00:30:10+08:00")
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"another-recording")
    unknown = tmp_path / "unknown.jsonl"
    unknown.write_text('{"data":"user"}\n', encoding="utf-8")
    result = organize_recordings(tmp_path)
    assert not result["errors"] and len(result["moved"]) == 1
    moved = tmp_path / result["moved"][0]["to"]
    assert moved.read_bytes() == original and moved != existing
    assert existing.read_bytes() == b"another-recording"
    assert unknown.exists()
    assert not organize_recordings(tmp_path)["moved"]


def test_stereo_separated_with_all_original_bytes_and_relative_indexes_preserved(tmp_path):
    session = old_stereo(tmp_path)
    original = {str(p.relative_to(session)): p.read_bytes() for p in session.rglob("*") if p.is_file()}
    result = organize_recordings(tmp_path)
    assert not result["errors"]
    pose_session = tmp_path / result["moved"][0]["to"]
    for name, data in original.items():
        assert recording_file(pose_session, name).read_bytes() == data
    video_index = recording_file(pose_session, "kinect/color_frames.jsonl")
    video_name = json.loads(video_index.read_text())["file"]
    assert (video_index.parent / video_name).read_bytes() == b"old-image"
    summary = recordings_summary(tmp_path)
    assert summary["count"] == summary["pose"]["count"] == 1
    assert summary["video"]["count"] == summary["depth"]["count"] == 1
    assert summary["bytes"] == sum(p.stat().st_size for p in tmp_path.rglob("*") if p.is_file())
    assert not organize_recordings(tmp_path)["moved"]
    shutil.rmtree(tmp_path / "视频")
    shutil.rmtree(tmp_path / "深度")
    assert recording_file(pose_session, "pc.jsonl").read_bytes() == original["pc.jsonl"]
    assert recordings_summary(tmp_path)["pose"]["count"] == 1
    assert recordings_summary(tmp_path)["video"]["bytes"] == 0


def test_active_session_is_not_moved(tmp_path):
    session = old_stereo(tmp_path, state="recording")
    assert not organize_recordings(tmp_path)["moved"]
    assert (session / "kinect/depth.bin").exists()


def test_resume_split_if_interrupted_and_never_replace_existing_media(tmp_path, monkeypatch):
    old_stereo(tmp_path)
    original = Path.rename

    def interrupted(path, target):
        if path.name == "depth.bin":
            raise OSError("测试模拟中断")
        return original(path, target)

    monkeypatch.setattr(Path, "rename", interrupted)
    result = organize_recordings(tmp_path)
    assert result["errors"]
    session = tmp_path / result["moved"][0]["to"]
    assert (session / "kinect/depth.bin").read_bytes() == b"old-depth-mm"
    monkeypatch.setattr(Path, "rename", original)
    assert not organize_recordings(tmp_path)["errors"]
    assert recording_file(session, "kinect/depth.bin").read_bytes() == b"old-depth-mm"


def test_existing_media_session_name_is_not_reused(tmp_path):
    old_stereo(tmp_path)
    taken = tmp_path / "视频" / "2026-10-07" / "17-43-33-603829_双目深度对照"
    taken.mkdir(parents=True)
    (taken / "keep.txt").write_text("用户已有的", encoding="utf-8")
    result = organize_recordings(tmp_path)
    assert not result["errors"] and result["moved"][0]["to"].endswith("_2")
    assert (taken / "keep.txt").read_text(encoding="utf-8") == "用户已有的"
