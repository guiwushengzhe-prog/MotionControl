"""触发窗口、重叠合并、持续动作和后台保存的行为检查。"""

import json
import threading
from pathlib import Path

import pytest

from motioncontrol.control_kernel import ControlKernel
from motioncontrol.trigger_recorder import SCHEMA, TriggerRecorder
from test_march_foot_separation import lifted
from test_minimal_controls import KernelOutput, _standing_pose, _zone_feeder


def pose(x=.5):
    return {"left_wrist": {"x": x, "y": .5, "visibility": .9}}


def documents(recorder):
    recorder.close()
    return [[json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            for path in sorted(recorder.directory.glob("*.jsonl"))]


def configured(tmp_path):
    recorder = TriggerRecorder(tmp_path / "clips")
    recorder.configure({"enabled": True, "triggers": ["motion.march", "zone.leftHand", "pose.custom"]})
    return recorder


def test_disabled_and_unselected_triggers_write_nothing_and_idle_memory_is_bounded(tmp_path):
    recorder = configured(tmp_path)
    for index in range(2000):
        recorder.capture(pose(), index / 60, {"motion.calf_back"})
    assert len(recorder._buffer) <= 61
    assert not recorder.directory.exists()
    recorder.configure({"enabled": False})
    for index in range(2000):
        recorder.capture(pose(), 50 + index / 60, {"motion.march"})
    assert not recorder._buffer
    assert not documents(recorder)


def test_only_trigger_window_is_saved_with_complete_hold_and_capture_clock(tmp_path):
    recorder = configured(tmp_path)
    for index in range(101):
        now = index / 10
        recorder.capture(pose(), now, {"motion.march"} if 3 <= now <= 6 else set(),
                         sample_at=now-.04, source="phone", width=640, height=480,
                         snapshot_factory=lambda: {"march_algorithm": "responsive"})
    clips = documents(recorder)
    assert len(clips) == 1
    header, *frames, end = clips[0]
    assert header["schema"] == SCHEMA
    assert header["first_trigger_t"] == 1.
    assert header["snapshot"]["march_algorithm"] == "responsive"
    assert end["type"] == "end" and end["frames"] == len(frames)
    assert frames[0]["t"] == 0. and frames[-1]["t"] == 5.1
    assert frames[0]["sample_t"] == -.04
    assert all(frame["source"] == "phone" for frame in frames)
    assert len([frame for frame in frames if frame["selected_active"]]) == 31
    assert recorder.saved_clips == 1


def test_overlapping_pre_and_post_windows_merge_but_later_event_is_separate(tmp_path):
    recorder = configured(tmp_path)
    for index in range(121):
        now = index / 10
        active = ({"zone.leftHand"} if 3 <= now <= 3.2
                  else {"pose.custom"} if 5. <= now <= 5.2
                  else {"motion.march"} if now == 9. else set())
        recorder.capture(pose(), now, active)
    clips = documents(recorder)
    assert len(clips) == 2
    first = clips[0][1:-1]
    assert first[-1]["t"] == 4.3
    assert {key for frame in first for key in frame["selected_active"]} == {"zone.leftHand", "pose.custom"}
    assert len({frame["t"] for frame in first}) == len(first)
    assert clips[1][0]["first_trigger_t"] == 1.


def test_one_frame_event_and_multiple_selected_triggers_are_kept(tmp_path):
    recorder = configured(tmp_path)
    for index in range(70):
        now = index / 10
        recorder.capture(pose(), now, {"motion.march", "pose.custom"} if now == 3. else set())
    clip = documents(recorder)[0]
    fired = [frame for frame in clip[1:-1] if frame["selected_active"]]
    assert len(fired) == 1
    assert fired[0]["selected_active"] == ["motion.march", "pose.custom"]


def test_snapshot_and_frames_are_copied_and_written_off_the_capture_thread(tmp_path, monkeypatch):
    recorder = configured(tmp_path)
    current_thread = threading.get_ident()
    write_threads = []
    original = Path.open

    def checked_open(path, mode="r", *args, **kwargs):
        if mode == "x":
            write_threads.append(threading.get_ident())
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", checked_open)
    points, extra = pose(.4), {"fist": False}
    recorder.capture(points, 1., set(), extra=extra)
    points["left_wrist"]["x"] = .8
    extra["fist"] = True
    recorder.capture(points, 1.5, {"zone.leftHand"}, extra=extra)
    clip = documents(recorder)[0]
    assert clip[1]["pose"]["left_wrist"]["x"] == .4
    assert clip[1]["extra"]["fist"] is False
    assert write_threads and current_thread not in write_threads


def test_close_or_disable_saves_the_partial_selected_clip(tmp_path):
    recorder = configured(tmp_path)
    recorder.capture(pose(), 1., {"motion.march"})
    recorder.configure({"enabled": False})
    clip = documents(recorder)[0]
    assert clip[-1]["reason"] == "关闭录制"
    assert clip[-1]["frames"] == 1


def test_missing_pose_stream_ends_the_clip_without_needing_another_frame(tmp_path):
    recorder = configured(tmp_path)
    recorder.capture(pose(), 1., {"motion.march"})
    recorder.end_active(1.5)
    recorder.tick(3.6)
    assert recorder.status()["state"] == "waiting"
    assert len(documents(recorder)) == 1


def test_save_error_is_reported(tmp_path):
    recorder = configured(tmp_path)
    recorder.directory.write_text("这里是文件", encoding="utf-8")
    recorder.capture(pose(), 1., {"motion.march"})
    recorder.close()
    assert recorder.status()["state"] == "error"
    assert "保存失败" in recorder.status()["error"]
    assert recorder.saved_clips == 0


@pytest.mark.parametrize("bad", [
    {"triggers": "motion.march"}, {"triggers": ["voice.jump"]}, {"pre_s": float("nan")}, {"post_s": -1},
])
def test_invalid_settings_do_not_replace_the_existing_configuration(tmp_path, bad):
    recorder = TriggerRecorder(tmp_path / "clips")
    before = recorder.status()["config"]
    with pytest.raises(ValueError):
        recorder.configure(bad)
    assert recorder.status()["config"] == before


def test_real_kernel_stepping_saves_selected_frames_and_restores_settings(monkeypatch):
    kernel = ControlKernel(KernelOutput())
    kernel.configure_march_algorithm("responsive")
    kernel.configure_trigger_recording({"triggers": ["motion.march"], "enabled": True})
    try:
        feed = _zone_feeder(kernel, monkeypatch)
        feed(_standing_pose(), 40)
        feed(lifted("left"), 4)
        feed(_standing_pose(), 2)
        feed(lifted("right"), 3)
        assert kernel.trigger_recorder.status()["state"] == "recording"
        feed(_standing_pose(), 100)
        assert kernel.trigger_recorder.status()["state"] == "waiting"
    finally:
        kernel.close()
    clips = documents(kernel.trigger_recorder)
    assert len(clips) == 1
    assert any("motion.march" in frame["selected_active"] for frame in clips[0][1:-1])
    restored = ControlKernel(KernelOutput())
    try:
        assert restored.trigger_recorder.config["enabled"]
        assert restored.trigger_recorder.config["triggers"] == ["motion.march"]
        assert "pose.hands_cross" in {item["key"] for item in restored.trigger_recording_choices()}
    finally:
        restored.close()
