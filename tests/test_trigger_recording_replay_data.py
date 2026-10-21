"""触发片段保留同帧原始来源和配置，可离线恢复；不启动设备或系统输出。"""

import copy
import json
import math

import pytest

from motioncontrol.control_kernel import ControlKernel
from motioncontrol.intent_library import BASE_T, Segment, make_replay_kernel, replay_segment
from test_control_kernel import FakeOutput, pose_map
from test_hfhpe_head_control import PitchEstimator, frame as head_frame


def strict_json(line):
    def reject(value):
        raise AssertionError("外层JSON不应包含非有限浮点数：" + value)
    return json.loads(line, parse_constant=reject)


def test_multitrigger_clip_keeps_current_sources_profile_and_restores_head(tmp_path, monkeypatch):
    output = FakeOutput()
    output.enabled = False
    kernel = ControlKernel(output)
    controller = kernel.head_controller
    controller.estimator = PitchEstimator()
    controller.configure(horizontal_algorithm="hfhpe_simple", deadzone=.03)
    controller._hfhpe_calibration = {"version": 1, "center_yaw_deg": 7., "noise_yaw_deg": .16, "sample_count": 34}
    kernel.configure_sensitivity_experiments({"head_hold": True})
    profile_bytes = kernel._head_profile_path().read_bytes()
    original_profile = controller.profile_document
    monkeypatch.setattr(controller, "profile_document", lambda: {**original_profile(), "unobserved_reference": math.nan})
    kernel.trigger_recorder.directory = tmp_path / "clips"
    kernel.trigger_recorder.configure({"enabled": True, "triggers": ["motion.jump", "pose.hands_cross"], "pre_s": 0., "post_s": 0.})
    clock = [100.]
    monkeypatch.setattr("motioncontrol.control_kernel.time.monotonic", lambda: clock[0])

    def select_both(pose, now):
        kernel.motion_active.add("jump")
        kernel.pose_active = {"hands_cross"}

    monkeypatch.setattr(kernel, "_update_cross_poses_locked", select_both)
    raw = pose_map()
    raw["nose"]["x"] = .31
    fused = copy.deepcopy(raw)
    fused["nose"]["x"] = .73
    world = {name: {"x": point["x"], "y": point["y"], "z": 0., "score": .95}
             for name, point in raw.items()}
    depth = {"tracking_id": 25, "points": {"unobserved": {"x": math.nan, "y": 1., "z": 2.}}}
    try:
        for yaw in (7., 13., 13.):
            clock[0] += 1 / 30
            kernel.handle_pose_map("computer_camera", fused, width=640, height=360, model_pose=raw,
                                   world_pose=world, depth_body=depth, head_pose=head_frame(yaw),
                                   sample_at=clock[0] - .01, return_status=False)
        kernel.trigger_recorder.close()
        path = next(kernel.trigger_recorder.directory.rglob("*.jsonl"))
        header, *frames, end = [strict_json(line) for line in path.read_text(encoding="utf-8").splitlines()]
        assert header["snapshot"]["head_replay"]
        profile = json.loads(header["snapshot"]["head_profile_json"])
        assert math.isnan(profile["unobserved_reference"])
        assert header["snapshot"]["sensitivity_experiments"]["head_hold"]
        assert "body_lean_neutral" in header["snapshot"]
        assert end["frames"] == len(frames) == 3
        assert all(row["selected_active"] == ["motion.jump", "pose.hands_cross"] for row in frames)
        assert [row["extra"]["head"]["hfhpe_yaw_deg"] for row in frames] == [7., 13., 13.]
        assert frames[1]["extra"]["head_pose"]["yaw_deg"] == 13.
        assert frames[1]["extra"]["head"]["hfhpe_hold"]
        assert frames[1]["extra"]["model_pose"]["nose"]["x"] == .31
        assert frames[1]["pose"]["nose"]["x"] == .73
        assert frames[1]["extra"]["depth_body"]["points"]["unobserved"]["x"] is None
        assert not frames[1]["extra"]["output_enabled"]
        replay = make_replay_kernel(header["snapshot"])
        seen = []
        try:
            replay_segment(replay, Segment("trigger", "action", "saved", {}, frames),
                           lambda c, row: seen.append({"head": dict(c.head), "model": copy.deepcopy(c.latest_model_pose),
                                                      "depth": copy.deepcopy(c.latest_depth_body), "sample": c.pose_sample_at}))
            assert replay.head_controller.profile_path is None
            assert replay.head_controller._hfhpe_calibration["center_yaw_deg"] == 7.
            assert replay.head_controller.hfhpe_hold
            assert replay.width == 640 and replay.height == 360
            assert [row["head"]["hfhpe_yaw_deg"] for row in seen] == [7., 13., 13.]
            assert seen[1]["model"]["nose"]["x"] == .31
            assert seen[1]["depth"]["tracking_id"] == 25
            assert seen[1]["sample"] == pytest.approx(BASE_T + frames[1]["sample_t"])
            assert kernel._head_profile_path().read_bytes() == profile_bytes
        finally:
            replay.close()
    finally:
        kernel.close()


@pytest.mark.parametrize("profile", [None, "{broken", '{"signal_version":"unsupported","params":{}}'])
def test_legacy_or_invalid_head_snapshot_stays_disabled_and_does_not_invent_model_input(profile):
    live = ControlKernel(FakeOutput(), persist=False)
    try:
        snapshot = live.replay_snapshot()
    finally:
        live.close()
    if profile is not None:
        snapshot.update(head_replay=True, head_profile_json=profile)
    replay = make_replay_kernel(snapshot)
    try:
        assert replay.head_controller.profile_path is None
        assert not replay.head_controller.config["enabled"]
        frames = [{"t": 0., "w": 640, "h": 480,
                   "pose": {name: [point["x"], point["y"], point["score"]] for name, point in pose_map().items()}}]
        replay_segment(replay, Segment("old", "idle", "legacy", {}, frames))
        assert replay.latest_model_pose is None
        assert replay.latest_depth_body is None
        assert replay.latest_head_pose is None
    finally:
        replay.close()
