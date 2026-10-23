"""个人研究路径的数据边界、三秒校准和未来学习；不访问设备或外部模型。"""
import copy
import json
from types import SimpleNamespace

import numpy as np
import pytest

from motioncontrol.depth_comparison import DEFAULTS, DepthComparison, validate_config
from motioncontrol.monocular_lean import MonocularLean, fit_readout, readout, validate_readout

TORSO = (0, 1, 2, 3, 4, 5, 16, 18, 20)


def feature(angle=0):
    return {"features": [angle] + [0.] * 41, "linear_coef": [1.] + [0.] * 41,
            "geometric_deg": angle, "personal_research": True, "sampled_regions": 0}


def test_research_model_is_only_a_lean_source_and_starts_disabled():
    assert validate_config(DEFAULTS, {"monocular_model": "mocapnet_personal"})["enabled"] is False
    with pytest.raises(ValueError):
        validate_config(DEFAULTS, {"model": "mocapnet_personal"})


def test_retired_comparison_cannot_resume_from_old_saved_settings():
    kernel = SimpleNamespace(general_setting=lambda key, default: {"enabled": True, "monocular_model": "zipdepth"})
    engine = DepthComparison(kernel)
    assert engine.config["enabled"] is False
    assert engine.config["monocular_model"] == "mocapnet_personal"
    assert engine.status()["active"] is False


def test_pose_only_submission_never_reads_copies_or_crops_pixels():
    class Frame:
        shape = (360, 640, 3)
        def __getitem__(self, key):
            raise AssertionError("轻量路径不应读取画面")
    kernel = SimpleNamespace(sensitivity_experiments={"fore_aft_source": "monocular"})
    engine = DepthComparison(kernel)
    engine.config["monocular_model"] = "mocapnet_personal"
    engine._thread = SimpleNamespace(is_alive=lambda: True)
    pose = {"left_shoulder": {"x": .4, "y": .2, "score": 1}}
    engine.submit(Frame(), pose, None, {}, "none", 10, "camera")
    meta, pixels = engine._pending
    assert pixels is None and meta["input_scope"] == "pose"
    assert meta["pose"] == pose and not meta["references"]
    assert meta["effective_fps"] == 15


def test_personal_linear_readout_uses_own_neutral_and_does_not_use_reference_for_prediction():
    left, right = MonocularLean(feature_dim=42), MonocularLean(feature_dim=42)
    for at in np.arange(0, 3.1, .1):
        a = left.update(feature(5), float(at), reference=-12, learn=False)
        b = right.update(feature(5), float(at), reference=22, learn=False)
    assert a["valid"] and b["valid"]
    assert a["angle_deg"] == b["angle_deg"] == 0
    for angle in (-12, 12):
        a = left.update(feature(angle+5), 3.2 + (angle+12)/100, reference=-30, learn=False)
        b = right.update(feature(angle+5), 3.2 + (angle+12)/100, reference=30, learn=False)
        assert a["angle_deg"] == b["angle_deg"] == angle
        assert a["readout"] == "personal_research" and a["feature_dim"] == 42
        assert a["reference_used_for_prediction"] is False
    assert not left._valid_sample({"features": [0.]*7, "reference": 0})


def test_personal_future_learning_retains_validation_and_isolates_feature_schema(tmp_path):
    engine = MonocularLean(tmp_path, persist=True, feature_dim=42, feature_indices=TORSO)
    engine.set_context(["mocapnet_personal", "one-camera"])
    at = 0.
    for _ in range(13):
        engine.update(feature(), at, reference=-10, learn=True)
        at += .25
    for i in range(240):
        angle = (0., -12., 12., -6., 6.)[(i//8) % 5]
        state = engine.update(feature(angle * .35), at, reference=angle-10, learn=True)
        at += .25
        if engine.profile["active"]:
            break
    assert state["readout"] == "learned" and state["learning"]["metrics"]["mae_deg"] < .5
    restored = MonocularLean(tmp_path, persist=True, feature_dim=42, feature_indices=TORSO)
    restored.set_context(["mocapnet_personal", "one-camera"])
    assert restored.profile["active"]
    other = MonocularLean(tmp_path, persist=True, feature_dim=7)
    other.set_context(["mocapnet_personal", "one-camera"])
    assert not other.profile["active"]


def test_relearned_torso_restores_amplitude_without_following_hand_or_head_motion():
    samples = []
    for angle in (-12., 0., 12.):
        for _ in range(24):
            points = feature(angle * .3)["features"]
            points[21], points[12] = angle, -angle
            samples.append({"features": points, "reference": angle})
    mapping = fit_readout(samples, TORSO)
    metrics = validate_readout(mapping, samples, 0, readout(mapping, feature()["features"]))
    assert metrics is not None
    for angle in (-12., 12.):
        points = feature(angle * .3)["features"]
        for unrelated in (-100., 100.):
            points[21], points[12] = unrelated, -unrelated
            assert readout(mapping, points) == pytest.approx(angle, abs=.3)
    assert all(mapping["coef"][i] == 0 for i in range(42) if i not in TORSO)


def test_direction_correct_but_compressed_readout_cannot_pass_neutral_heavy_validation():
    mapping = {"mean": [0.], "scale": [1.], "coef": [.4, 0.]}
    samples = [{"features": [angle], "reference": angle}
               for angle in ([-9.] * 8 + [0.] * 112 + [9.] * 8)]
    assert validate_readout(mapping, samples, 0) is None


def test_old_mapping_with_arm_weights_is_not_restored_but_training_samples_remain(tmp_path):
    engine = MonocularLean(tmp_path, persist=True, feature_dim=42)
    engine.set_context("camera")
    samples = [{"features": [angle] * 42, "reference": angle}
               for angle in (-12., 0., 12.) for _ in range(24)]
    mapping = fit_readout(samples)
    active = {"mapping": mapping, "center": 0., "origin": 0., "validation": samples}
    engine.path.write_text(json.dumps({"version": 1, "profiles": {
        engine.context: {"samples": samples, "active": active}}}), encoding="utf-8")
    restricted = MonocularLean(tmp_path, persist=True, feature_dim=42, feature_indices=TORSO)
    restricted.set_context("camera")
    assert restricted.profile["active"] is None
    assert len(restricted.profile["samples"]) == len(samples)


def test_new_neutral_rechecks_saved_mapping_without_copying_depth_into_prediction():
    engine = MonocularLean(feature_dim=42, feature_indices=TORSO)
    samples = [{"features": feature(angle)["features"], "reference": angle-10}
               for angle in (-12., 0., 12.) for _ in range(24)]
    mapping = fit_readout(samples, TORSO)
    engine.profile["active"] = {"mapping": mapping, "center": -10., "origin": -10.,
                                "validation": copy.deepcopy(samples)}
    for at in np.arange(0, 3.1, .1):
        state = engine.update(feature(5.), float(at), reference=-10., learn=False)
    assert engine.profile["active"] is None
    assert state["angle_deg"] == pytest.approx(0)
    assert state["reference_used_for_prediction"] is False
