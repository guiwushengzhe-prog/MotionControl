"""个人研究路径的数据边界、三秒校准和未来学习；不访问设备或外部模型。"""
from types import SimpleNamespace

import numpy as np
import pytest

from motioncontrol.depth_comparison import DEFAULTS, DepthComparison, validate_config
from motioncontrol.monocular_lean import MonocularLean


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
    engine = MonocularLean(tmp_path, persist=True, feature_dim=42)
    engine.set_context(["mocapnet_personal", "one-camera"])
    at = 0.
    for _ in range(13):
        engine.update(feature(), at, reference=-10, learn=True)
        at += .25
    for i in range(240):
        angle = (0., -12., 12., -6., 6.)[(i//8) % 5]
        state = engine.update(feature(angle), at, reference=angle-10, learn=True)
        at += .25
        if engine.profile["active"]:
            break
    assert state["readout"] == "learned" and state["learning"]["metrics"]["mae_deg"] < .5
    restored = MonocularLean(tmp_path, persist=True, feature_dim=42)
    restored.set_context(["mocapnet_personal", "one-camera"])
    assert restored.profile["active"]
    other = MonocularLean(tmp_path, persist=True, feature_dim=7)
    other.set_context(["mocapnet_personal", "one-camera"])
    assert not other.profile["active"]
