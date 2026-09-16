"""Recording real skeleton data without disturbing the control loop.

The recorder runs inside the loop that drives the gamepad, so the two things
worth protecting are that it never writes to disk while recording, and that it
cannot run forever if nobody stops it.
"""

from __future__ import annotations

import json

import pytest

from pose_recorder import MAX_DURATION_S, PoseRecorder


def frame(x=0.5, y=0.5, score=0.9):
    return {
        "left_wrist": {"x": x, "y": y, "z": 0.0, "score": score},
        "right_wrist": {"x": 1 - x, "y": y, "z": 0.0, "visibility": score},
    }


@pytest.fixture
def recorder(tmp_path):
    return PoseRecorder(tmp_path / "recordings")


def test_starts_idle(recorder):
    assert recorder.status()["state"] == "idle"


def test_countdown_runs_before_anything_is_captured(recorder):
    recorder.start(delay_s=3.0, duration_s=15.0, now=100.0)
    assert recorder.status(now=100.0)["state"] == "waiting"

    recorder.capture(frame(), now=102.0)   # still counting down
    assert recorder.status(now=102.0)["frames"] == 0

    recorder.capture(frame(), now=103.5)   # past the countdown
    assert recorder.status(now=103.5)["state"] == "recording"
    assert recorder.status(now=103.5)["frames"] == 1


def test_nothing_is_written_until_the_recording_ends(recorder):
    recorder.start(delay_s=0.0, duration_s=15.0, now=0.0)
    for i in range(10):
        recorder.capture(frame(), now=i * 0.1)
    assert not list(recorder.directory.glob("*.jsonl")), "disk I/O belongs after the run, not during"


def test_file_is_written_when_the_duration_elapses(recorder):
    recorder.start(delay_s=0.0, duration_s=1.0, now=0.0)
    for i in range(5):
        recorder.capture(frame(x=0.1 * i), now=i * 0.2)
    recorder.capture(frame(), now=1.5)     # past the end: finishes and saves

    status = recorder.status()
    assert status["state"] == "done"
    files = list(recorder.directory.glob("*.jsonl"))
    assert len(files) == 1
    assert status["file"] == str(files[0])


def test_saved_file_is_a_header_line_then_one_line_per_frame(recorder):
    recorder.start(delay_s=0.0, duration_s=1.0, now=0.0)
    for i in range(4):
        recorder.capture(frame(), now=i * 0.2, width=640, height=480, source="phone")
    recorder.capture(frame(), now=2.0)

    lines = list(recorder.last_path.read_text(encoding="utf-8").splitlines())
    header = json.loads(lines[0])
    assert header["schema"] == "motioncontrol.pose_recording.v1"
    assert header["frames"] == 4
    assert header["source"] == "phone"
    assert len(lines) == 5

    first = json.loads(lines[1])
    assert first["width"] == 640
    assert "left_wrist" in first["pose"]
    assert set(first["pose"]["left_wrist"]) == {"x", "y", "z", "score"}


def test_visibility_is_normalised_to_score(recorder):
    """Phone frames carry visibility, local ones carry score; files use one name."""
    recorder.start(delay_s=0.0, duration_s=0.5, now=0.0)
    recorder.capture(frame(score=0.77), now=0.1)
    recorder.capture(frame(), now=1.0)
    saved = json.loads(recorder.last_path.read_text(encoding="utf-8").splitlines()[1])
    assert saved["pose"]["right_wrist"]["score"] == pytest.approx(0.77)


def test_extra_fields_are_recorded_alongside_the_pose(recorder):
    """The fist reading is what makes a recording useful for tuning."""
    recorder.start(delay_s=0.0, duration_s=0.5, now=0.0)
    recorder.capture(frame(), now=0.1, extra={"hand_spread": 0.24, "fist": True})
    recorder.capture(frame(), now=1.0)
    saved = json.loads(recorder.last_path.read_text(encoding="utf-8").splitlines()[1])
    assert saved["extra"]["hand_spread"] == 0.24
    assert saved["extra"]["fist"] is True


def test_frames_are_copied_not_referenced(recorder):
    """The kernel reuses its pose dicts between frames."""
    recorder.start(delay_s=0.0, duration_s=1.0, now=0.0)
    live = frame(x=0.2)
    recorder.capture(live, now=0.1)
    live["left_wrist"]["x"] = 0.9        # the kernel mutates in place
    recorder.capture(frame(), now=2.0)

    saved = json.loads(recorder.last_path.read_text(encoding="utf-8").splitlines()[1])
    assert saved["pose"]["left_wrist"]["x"] == pytest.approx(0.2)


def test_cancel_discards_without_writing(recorder):
    recorder.start(delay_s=0.0, duration_s=15.0, now=0.0)
    recorder.capture(frame(), now=0.1)
    status = recorder.cancel()
    assert status["state"] == "cancelled"
    assert not list(recorder.directory.glob("*.jsonl"))


def test_a_second_start_is_refused_while_running(recorder):
    recorder.start(delay_s=0.0, duration_s=15.0, now=0.0)
    with pytest.raises(ValueError, match="已经在录制中"):
        recorder.start(now=0.1)


def test_absurd_durations_are_refused():
    recorder = PoseRecorder("unused")
    with pytest.raises(ValueError, match="录制时长"):
        recorder.start(duration_s=MAX_DURATION_S + 1, now=0.0)
    with pytest.raises(ValueError, match="倒计时"):
        recorder.start(delay_s=-1, now=0.0)


def test_frame_cap_stops_a_recorder_nobody_stopped(recorder, monkeypatch):
    """Without a cap a stuck recorder would grow until the process died."""
    monkeypatch.setattr("pose_recorder.MAX_FRAMES", 5)
    recorder.start(delay_s=0.0, duration_s=MAX_DURATION_S, now=0.0)
    for i in range(20):
        recorder.capture(frame(), now=i * 0.01)
    assert recorder.status()["state"] == "done"
    assert recorder.last_frames == 5


def test_capture_is_inert_when_idle(recorder):
    recorder.capture(frame(), now=1.0)
    assert recorder.status()["state"] == "idle"
    assert not list(recorder.directory.glob("*.jsonl"))
