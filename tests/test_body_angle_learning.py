"""合成角度验证个人在线校正；不打开相机、骨骼录制或真实游戏输出。"""
import copy
import json
import math
import threading
import time

import pytest

from motioncontrol import body_angle_learning as module
from motioncontrol.body_angle_learning import BodyAngleLearning, TRAIN_SAMPLES


ANGLES = (-12., -8., -4., 0., 4., 8., 12.)


def learner(tmp_path, *, persist=False):
    value = BodyAngleLearning(tmp_path, persist=persist)
    value.set_context("相机一-稳定站姿一")
    return value


def feed(value, start=0, count=110, *, gain=1.25, offset=.8):
    for index in range(start, start + count):
        angle = ANGLES[index % len(ANGLES)]
        value.update(angle, angle * gain + offset, index * .3)


def test_candidate_must_wait_for_future_independent_validation(tmp_path):
    value = learner(tmp_path)
    feed(value, count=TRAIN_SAMPLES)
    assert value.status()["state"] == "validating"
    assert value.status()["training_samples"] == TRAIN_SAMPLES
    assert value.status()["validation_samples"] == 0
    assert value.correct(8.) == 8.
    feed(value, TRAIN_SAMPLES, 3)
    assert value.status()["validation_samples"] == 0, "训练窗后留一秒间隔，不能把相邻帧冒充留验"
    feed(value, TRAIN_SAMPLES + 3, 33)
    assert value.status()["active"]
    assert value.correct(8.) == pytest.approx(10.8)
    metrics = value.status()["validation"]
    assert metrics["validation_samples"] >= 32
    assert metrics["corrected_rmse_deg"] < .01
    assert metrics["raw_rmse_deg"] > 2


def test_unseen_data_must_actually_improve_over_raw_angles(tmp_path):
    value = learner(tmp_path)
    feed(value, count=TRAIN_SAMPLES)
    feed(value, TRAIN_SAMPLES, 45, gain=1., offset=0.)
    assert not value.status()["active"]
    assert value.correct(10.) == 10.
    assert "未优于" in value.status()["reason"] or value.status()["state"] == "validating"


def test_neutral_or_few_extreme_samples_cannot_create_a_mapping(tmp_path):
    value = learner(tmp_path)
    for index in range(200):
        value.update(0., .8, index * .3)
    assert value.status()["samples"] <= module.MAX_PER_BIN
    for index, angle in enumerate((-20., -10., 10., 20.), 200):
        value.update(angle, angle * 1.25 + .8, index * .3)
    assert not value.status()["active"]
    assert value.status()["training_samples"] == 0


@pytest.mark.parametrize("kwargs", [
    {"model_deg": None}, {"model_deg": math.nan}, {"depth_deg": math.inf},
    {"model_deg": 36.}, {"valid": False}, {"quality": .79}, {"sync_gap_s": .081},
    {"sync_gap_s": math.inf}, {"sample_age_s": .251}, {"sample_age_s": -1.},
    {"depth_deg": -10.}, {"now": math.nan},
])
def test_invalid_unreliable_or_stale_pairs_are_rejected_without_throwing(tmp_path, kwargs):
    value = learner(tmp_path)
    arguments = {"model_deg": 10., "depth_deg": 12., "now": 1.}
    arguments.update(kwargs)
    assert not value.update(**arguments)
    assert value.status()["samples"] == 0
    assert value.correct(5.) == 5.


def test_sampling_rate_duplicate_time_and_large_jumps_are_rejected(tmp_path):
    value = learner(tmp_path)
    assert value.update(0., 0., 1.)
    assert not value.update(1., 1., 1.1)
    assert not value.update(2., 2., 1.)
    assert not value.update(35., 35., 1.25)
    assert value.status()["samples"] == 1
    assert value.update(4., 5., 1.5)
    assert value.status()["samples"] == 2


def test_missing_depth_and_rejected_candidates_preserve_verified_correction(tmp_path):
    value = learner(tmp_path)
    feed(value)
    old = value.snapshot()["active"]
    for i in range(110, 140):
        assert not value.update(8., None, i * .3, valid=False, sync_gap_s=math.inf)
    assert value.correct(8.) == pytest.approx(10.8)
    feed(value, 140, 300)
    assert value.snapshot()["active"] == old
    assert value.correct(8.) == pytest.approx(10.8)


def test_reliable_independent_evidence_retracts_a_now_worse_old_mapping(tmp_path):
    value = learner(tmp_path)
    feed(value)
    assert value.status()["active"]
    feed(value, 110, 300, gain=1., offset=0.)
    assert not value.status()["active"]
    assert value.correct(8.) == 8.


def test_old_pending_validation_cannot_be_completed_after_a_long_gap(tmp_path):
    value = learner(tmp_path)
    feed(value, count=TRAIN_SAMPLES)
    feed(value, TRAIN_SAMPLES, 34)
    assert value.status()["validation_samples"] == 31
    assert not value.status()["active"]
    assert value.update(0., .8, 600.)
    assert not value.status()["active"]
    assert value.status()["validation_samples"] == 0
    assert value.correct(8.) == 8.


def test_small_training_outlier_group_cannot_change_the_personal_mapping(tmp_path):
    value = learner(tmp_path)
    # 每个角度段有足够多数可靠对照，三个异常对不能决定增益。
    for index in range(140):
        angle = ANGLES[index % len(ANGLES)]
        offset = 5. if index in (1, 8, 15) else .8
        value.update(angle, 1.25 * angle + offset, index * .3)
    assert value.status()["active"]
    assert value.status()["gain"] == pytest.approx(1.25, abs=.02)
    assert value.correct(8.) == pytest.approx(10.8, abs=.15)


def test_context_change_retracts_old_mapping_and_support_limits_extrapolation(tmp_path):
    value = learner(tmp_path)
    feed(value)
    assert value.correct(30.) - 30. == pytest.approx(value.correct(12.) - 12.)
    assert value.set_context("相机一-新站姿")
    assert value.correct(8.) == 8.
    assert value.status()["samples"] == 0
    assert not value.status()["active"]
    assert not value.set_context("相机一-新站姿")


def test_persistence_is_atomic_limited_and_flushable_without_storing_images(tmp_path, monkeypatch):
    writes = []
    original = module.atomic_bytes

    def write(path, content):
        writes.append(bytes(content))
        return original(path, content)

    monkeypatch.setattr(module, "atomic_bytes", write)
    value = learner(tmp_path, persist=True)
    feed(value, count=100)
    assert not writes
    feed(value, 100, 55)
    value._save_thread.join(2.)
    assert not value._save_thread.is_alive()
    assert len(writes) == 1, "常驻学习不能每帧落盘"
    feed(value, 155, 1)
    assert value.flush()
    assert len(writes) == 2
    assert not value.flush()
    raw = json.loads(value.path.read_text(encoding="utf-8"))
    assert set(raw) == {"version", "context", "saved_at", "samples", "accepted_samples", "active"}
    assert all(isinstance(pair, list) and len(pair) == 3 for pair in raw["samples"])
    restored = BodyAngleLearning(tmp_path)
    assert not restored.set_context("相机一-稳定站姿一")
    assert restored.correct(8.) == pytest.approx(value.correct(8.))
    assert restored.status()["active"]


def test_slow_background_save_does_not_block_angles_or_lose_new_samples(tmp_path, monkeypatch):
    entered, release, controlled = threading.Event(), threading.Event(), threading.Event()
    original = module.atomic_bytes

    def slow(path, content):
        entered.set()
        assert release.wait(3.)
        return original(path, content)

    monkeypatch.setattr(module, "atomic_bytes", slow)
    value = learner(tmp_path, persist=True)
    worker = None
    try:
        feed(value, count=102)
        assert entered.wait(2.)

        def control():
            value.update(8., 10.8, 35.)
            value.correct(8.)
            value.status()
            controlled.set()

        worker = threading.Thread(target=control)
        worker.start()
        assert controlled.wait(.5), "磁盘刷盘期间不能持有控制所需的角度锁"
    finally:
        release.set()
        if worker is not None:
            worker.join(2.)
        if value._save_thread is not None:
            value._save_thread.join(2.)
    assert value.status()["pending_save"], "旧快照写完不能把期间的新样本标成已保存"
    assert value.flush()
    saved = json.loads(value.path.read_text(encoding="utf-8"))
    assert saved["accepted_samples"] == value.status()["accepted_samples"]


def test_save_failure_keeps_previous_file_and_effective_mapping(tmp_path, monkeypatch):
    value = learner(tmp_path, persist=True)
    feed(value)
    value.flush()
    previous = value.path.read_bytes()
    effective = value.correct(8.)

    def fail(path, content):
        raise OSError("模拟磁盘错误")

    monkeypatch.setattr(module, "atomic_bytes", fail)
    feed(value, 110, 8)
    assert not value.flush()
    assert value.path.read_bytes() == previous
    assert value.correct(8.) == effective
    assert value.status()["pending_save"]
    assert "保存失败" in value.status()["last_error"]


def test_expired_training_samples_are_removed_but_verified_mapping_remains(tmp_path):
    value = learner(tmp_path, persist=True)
    feed(value)
    value.flush()
    raw = json.loads(value.path.read_text(encoding="utf-8"))
    for sample in raw["samples"]:
        sample[2] = time.time() - module.SAMPLE_LIFETIME_S - 1
    value.path.write_text(json.dumps(raw), encoding="utf-8")
    restored = BodyAngleLearning(tmp_path)
    assert restored.status()["samples"] == 0
    assert restored.status()["active"]
    assert restored.correct(8.) == pytest.approx(10.8)


def test_snapshot_is_independent_and_invalid_snapshot_preserves_old_mapping(tmp_path):
    value = learner(tmp_path)
    feed(value)
    snapshot = value.snapshot()
    assert set(snapshot) == {"version", "context", "active"}
    assert "samples" not in snapshot
    restored = BodyAngleLearning(tmp_path, persist=False)
    assert restored.load_snapshot(snapshot)
    assert restored.correct(8.) == value.correct(8.)
    changed = copy.deepcopy(snapshot)
    changed["active"]["gain"] = 1.9
    changed["active"]["validation"] = {"raw_rmse_deg": 10., "corrected_rmse_deg": 0.}
    assert not restored.load_snapshot(changed), "不能只信保存的指标，需用留验角度重算"
    assert restored.correct(8.) == value.correct(8.)
    snapshot["active"]["gain"] = .5
    assert value.correct(8.) == pytest.approx(10.8)
    assert not restored.flush()
    assert not value.path.exists()


def test_corrupt_file_or_invalid_parameters_are_not_applied_and_do_not_get_rewritten(tmp_path):
    value = learner(tmp_path, persist=True)
    feed(value)
    value.flush()
    raw = json.loads(value.path.read_text(encoding="utf-8"))
    raw["active"]["gain"] = -1.
    value.path.write_text(json.dumps(raw), encoding="utf-8")
    previous = value.path.read_bytes()
    restored = BodyAngleLearning(tmp_path)
    assert not restored.status()["active"]
    assert restored.correct(8.) == 8.
    assert value.path.read_bytes() == previous
    assert "文件无效" in restored.status()["last_error"]


def test_persist_false_never_reads_existing_personal_parameters(tmp_path):
    value = learner(tmp_path, persist=True)
    feed(value)
    value.flush()
    restored = BodyAngleLearning(tmp_path, persist=False)
    assert not restored.status()["active"]
    assert restored.correct(8.) == 8.
    assert not restored.flush()
