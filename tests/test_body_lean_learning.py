"""个人前后倾校正只接纳同帧可靠监督，原始骨骼和读数保持独立。"""
import copy

import pytest

from motioncontrol.body_lean import BodyLeanController
from test_body_lean import poses


class Learner:
    def __init__(self):
        self.contexts, self.updates, self.corrected, self.flushes = [], [], [], 0

    def set_context(self, value):
        self.contexts.append(value)

    def update(self, model, depth, now, **quality):
        self.updates.append((model, depth, now, quality))

    def correct(self, value):
        self.corrected.append(value)
        return value * 2

    def status(self):
        return {"active": True}

    def flush(self):
        self.flushes += 1


def feed(controller, start, *, model=0., depth=0., count=40, body_age_ms=10.,
         sample_age_s=.02, matched=True, missing_depth=False, learning=True):
    for index in range(count):
        now = start + index / 30
        image, world, _ = poses(back=model)
        native = poses(back=depth)[2]
        native.update(sample_at=now, body_age_ms=body_age_ms,
                      matched_to_model=matched, body_valid=True)
        result = controller.update(image, world, None if missing_depth else native, 1000, 1000, now,
                                   back=True, learn_from_depth=learning, sample_age_s=sample_age_s,
                                   model_identity="test:camera:model:rotation")
    return result


def test_learning_off_has_no_sample_or_correction_work_and_never_rewrites_source_angles():
    learner = Learner()
    c = BodyLeanController(learning=learner)
    feed(c, 10., learning=False)
    feed(c, 11.4, model=4., depth=8., count=10, learning=False)
    assert learner.updates == learner.corrected == []
    assert not c.status(11.7)["learning"]["enabled"]


def test_reliable_pair_trains_raw_angles_while_only_model_control_uses_corrected_angle():
    learner = Learner()
    c = BodyLeanController(learning=learner)
    feed(c, 10.)
    assert feed(c, 11.4, model=4., depth=8., count=10) == {"lean_back"}
    state = c.status(11.7)
    model, depth = state["sources"]["model"], state["sources"]["depth"]
    assert abs(model["back_raw_deg"] - 4.) < .01
    assert model["back_uncorrected_deg"] < 4.1 < model["back_deg"]
    assert abs(depth["back_raw_deg"] - 8.) < .01
    last = [row for row in learner.updates if row[3]["valid"]][-1]
    assert abs(last[0] - 4.) < .01 and abs(last[1] - 8.) < .01
    assert last[3]["quality"] == .95
    # 缺深度只停止新增监督，已验证的个人校正继续服务普通模型。
    assert feed(c, 11.74, model=4., count=10, missing_depth=True) == {"lean_back"}
    assert not learner.updates[-1][3]["valid"]
    assert feed(c, 12.08, model=4., count=10, learning=False) == set()
    assert learner.flushes == 1


def test_old_unsynced_unmatched_and_stale_pairs_are_not_supervision_and_reset_changes_context():
    learner = Learner()
    c = BodyLeanController(learning=learner)
    feed(c, 10.)
    feed(c, 11.4, model=4., depth=8., count=10, body_age_ms=200.)
    assert not any(row[3]["valid"] for row in learner.updates[-10:])
    feed(c, 11.74, model=4., depth=8., count=10, matched=False)
    assert not any(row[3]["valid"] for row in learner.updates[-10:])
    feed(c, 12.08, model=4., depth=8., count=10, sample_age_s=.4)
    assert learner.updates[-1][3]["sample_age_s"] == .4
    assert not learner.updates[-1][3]["valid"]
    previous = learner.contexts[-1]
    c.start_neutral(12.42)
    feed(c, 12.42, model=3., depth=6.)
    assert learner.contexts[-1] != previous


def test_recording_and_replay_keep_mid_clip_verified_mapping_without_learning_again(monkeypatch):
    from motioncontrol.control_kernel import ControlKernel
    from motioncontrol.intent_library import Segment, make_replay_kernel, replay_segment
    from test_body_angle_learning import feed as train_angles
    from test_control_kernel import FakeOutput, pose_map

    kernel = ControlKernel(FakeOutput())
    replay = None
    try:
        kernel.configure_sensitivity_experiments({"learn_from_depth": True, "lean_back": True})
        kernel.latest_body_model_identity = "test:camera:model:rotation"
        feed(kernel._body_lean, 10.)
        before = kernel.replay_snapshot()
        train_angles(kernel._body_lean.learning, start=100, count=110)
        learned = kernel._body_lean.learning.snapshot()
        assert learned["active"] is not None
        kernel.configure_trigger_recording({"enabled": True, "triggers": ["motion.lean_back"]})
        captured = []
        monkeypatch.setattr(kernel.trigger_recorder, "capture", lambda *args, **kwargs: captured.append(kwargs))
        kernel._record_trigger_frame_locked(pose_map(), 34., poses()[1])
        recorded = captured[0]["extra"]
        assert recorded["body_angle_learning"] == learned
        assert recorded["body_lean_neutral"] == before["body_lean_neutral"]
        assert recorded["body_model_identity"] == kernel.latest_body_model_identity
        replay = make_replay_kernel(before)
        assert not replay._body_lean.learning.status()["active"]
        monkeypatch.setattr(replay._body_lean.learning, "update", lambda *args, **kwargs: pytest.fail("回放不得重新学习"))
        frames = []
        for index, mapping in enumerate((before["body_angle_learning"], learned)):
            frames.append({"t": index / 30, "sample_t": index / 30, "width": 640, "height": 480,
                           "pose": pose_map(), "world_pose": poses(back=8.)[1],
                           "extra": {"model_pose": pose_map(), "body_angle_learning": mapping,
                                     "body_lean_neutral": copy.deepcopy(before["body_lean_neutral"]),
                                     "sensitivity_experiments": copy.deepcopy(before["sensitivity_experiments"])}})
        active = []
        replay_segment(replay, Segment("trigger", "action", "recorded", {}, frames),
                       lambda current, row: active.append(current._body_lean.learning.status()["active"]))
        assert active == [False, True]
        assert replay._body_lean.learning.correct(8.) == pytest.approx(10.8)
    finally:
        if replay is not None:
            replay.close()
        kernel.close()


def test_unknown_or_changed_camera_and_model_do_not_apply_previous_mapping():
    from motioncontrol.body_angle_learning import BodyAngleLearning
    from test_body_angle_learning import feed as train_angles

    learning = BodyAngleLearning(persist=False)
    c = BodyLeanController(learning=learning)
    feed(c, 10.)
    train_angles(learning, start=100, count=110)
    assert c.status(11.3)["learning"]["active"]
    previous = learning.snapshot()["context"]
    image, world, depth = poses(back=8.)
    assert c.update(image, world, depth, 1000, 1000, 65., back=True, learn_from_depth=True) == set()
    assert not c.status(65.)["learning"]["active"]
    assert "标识" in c.status(65.)["learning"]["reason"]
    c.update(image, world, depth, 1000, 1000, 65.04, back=True,
             learn_from_depth=True, model_identity="test:another-camera:another-model")
    assert learning.snapshot()["context"] != previous
    assert not learning.status()["active"]


def test_startup_defers_context_check_until_actual_camera_identity_without_erasing_saved_mapping(tmp_path):
    from motioncontrol.body_angle_learning import BodyAngleLearning
    from test_body_angle_learning import feed as train_angles

    learner = BodyAngleLearning(tmp_path)
    controller = BodyLeanController(learning=learner)
    feed(controller, 10.)
    train_angles(learner, start=100, count=110)
    controller.flush_learning()
    saved = learner.snapshot()
    assert saved["active"] is not None
    loaded = BodyAngleLearning(tmp_path)
    restarted = BodyLeanController(controller.neutral_snapshot(), learning=loaded)
    assert loaded.snapshot() == saved, "先建立控制器、后取得当前摄像头标识时不能清掉保存的校正"
    assert not restarted.status(20.)["learning"]["active"]
    restarted.update(*poses(), 1000, 1000, 20., back=True, learn_from_depth=True,
                     model_identity="test:camera:model:rotation")
    assert restarted.status(20.)["learning"]["active"]
    assert loaded.snapshot()["active"] == saved["active"]
    restarted.flush_learning()

