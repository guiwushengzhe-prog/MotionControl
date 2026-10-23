"""单目胸腹读取、持续姿态、独立学习与断帧释放；不访问设备或真实输出。"""
import copy
import json
import math
from types import SimpleNamespace

import numpy as np
import pytest

from motioncontrol.body_lean import BodyLeanController
from motioncontrol.depth_comparison import DepthComparison
from motioncontrol.monocular_lean import MonocularLean, fit_readout, kinect_pitch, validate_readout
from motioncontrol.monocular_lean_features import NAMES, torso_features
from test_body_lean import poses


def image_pose():
    return {n: {"x": .3 if "left" in n else .7, "y": .15 if "shoulder" in n else .85, "score": 1} for n in NAMES}


def feature(angle=0):
    return {"features": [angle / 100, -angle / 100, 0., 0., angle / 100, -angle / 100, 2.],
            "geometric_deg": angle, "sampled_regions": 30, "quality": 1}


def test_inner_regions_exclude_shoulder_background_and_keep_depth_polarity():
    pose = image_pose()
    depth = np.full((240, 320), 8., dtype=float)
    depth[:, 120:201] = 2.
    assert depth[36, 96] == 8, "轮廓上的原始肩点在背景"
    region = torso_features(depth, pose)
    assert region["sampled_regions"] == 30
    assert region["geometric_deg"] == pytest.approx(0, abs=1e-9)
    yy = np.linspace(0, 1, 240)[:, None]
    tilted = np.broadcast_to(2 + yy, depth.shape).copy()
    metric = torso_features(tilted, pose)
    inverse = torso_features(1 / tilted, pose, inverse=True)
    assert metric["geometric_deg"] < -7
    assert inverse["geometric_deg"] == pytest.approx(metric["geometric_deg"], abs=.01)
    mirrored = {n: {**p, "x": 1 - p["x"]} for n, p in pose.items()}
    assert torso_features(tilted[:, ::-1], mirrored)["geometric_deg"] == pytest.approx(metric["geometric_deg"])
    pose["right_hip"]["score"] = .1
    assert torso_features(tilted, pose) is None


def test_neutral_translation_does_not_create_tilt_and_invalid_depth_is_rejected():
    for distance in (1., 2., 4.):
        observed = torso_features(np.full((240, 320), distance), image_pose())
        assert observed["geometric_deg"] == pytest.approx(0., abs=1e-9)
    assert torso_features(np.zeros((240, 320)), image_pose()) is None
    assert torso_features(np.full((240, 320), np.nan), image_pose()) is None


def test_forward_hold_back_hold_neutral_and_stale_are_rgb_only():
    estimator, controller = MonocularLean(), BodyLeanController()
    estimator.set_context(["ordinary-rgb", "vda"])
    def feed(angle, at):
        state = estimator.update(feature(angle), at, reference=None)
        actions = controller.update(None, None, None, 640, 360, at,
            forward=True, back=True, fore_aft_source="monocular", monocular=state)
        return state, actions
    for t in np.arange(0, 3.1, .1):
        state, actions = feed(0, float(t))
        assert not actions
    for t in np.arange(3.1, 5.1, .1):
        state, actions = feed(-12, float(t))
    assert actions == {"lean_forward"}
    assert state["reference_used_for_prediction"] is False
    for t in np.arange(5.1, 5.6, .1):
        _, actions = feed(0, float(t))
    assert actions == set()
    for t in np.arange(5.6, 7.6, .1):
        _, actions = feed(12, float(t))
    assert actions == {"lean_back"}
    assert controller.update(None, None, None, 640, 360, 8., forward=True, back=True,
        fore_aft_source="monocular", monocular=state) == set()
    estimator.reset()
    assert not estimator.update(feature(12), 9.)["valid"]


def test_reference_cannot_change_prediction_when_learning_is_off():
    left, right = MonocularLean(), MonocularLean()
    for at in np.arange(0, 5, .1):
        angle = 0 if at < 3 else -10
        a = left.update(feature(angle), float(at), reference=None, learn=False)
        b = right.update(feature(angle), float(at), reference=42, learn=False)
        assert a.get("angle_deg") == b.get("angle_deg")
    assert not left.profile["samples"] and not right.profile["samples"]


def test_training_frozen_candidate_and_future_validation_persist_without_depth(tmp_path):
    estimator = MonocularLean(tmp_path, persist=True)
    estimator.set_context(["camera", "vda", "one-window"])
    at = 0.
    for _ in range(13):
        estimator.update(feature(), at, reference=-10, learn=True)
        at += .25
    frozen = None
    for i in range(240):
        angle = (0., -12., 12., -6., 6.)[(i // 8) % 5]
        state = estimator.update(feature(angle), at, reference=angle - 10, learn=True)
        if estimator.pending:
            if frozen is None:
                frozen = copy.deepcopy(estimator.pending["mapping"])
            else:
                assert estimator.pending["mapping"] == frozen
        at += .25
        if estimator.profile["active"]:
            break
    assert state["learning"]["active"] and state["readout"] == "learned"
    assert estimator.profile["active"]["metrics"]["mae_deg"] < .5
    restored = MonocularLean(tmp_path, persist=True)
    restored.set_context(["camera", "vda", "one-window"])
    assert restored.profile["active"]
    for t in np.arange(0, 3.1, .1):
        restored.update(feature(), float(t), reference=None, learn=False)
    state = restored.update(feature(-12), 3.2, reference=None, learn=False)
    assert state["angle_deg"] < -10 and state["reference_used_for_prediction"] is False
    restored.set_context(["different-camera", "vda", "one-window"])
    assert restored.profile["active"] is None


def test_neutral_only_cannot_train_a_three_state_readout():
    estimator = MonocularLean()
    estimator.set_context("camera")
    for i in range(300):
        state = estimator.update(feature(), i * .25, reference=-10, learn=True)
    assert not state["learning"]["active"] and estimator.pending is None
    assert len(estimator.profile["samples"]) <= 48


def test_bad_future_labels_never_validate_and_bad_saved_mapping_does_not_load(tmp_path):
    training = [{"features": feature(a)["features"], "reference": a - 10} for a in (-12., 0., 12.) for _ in range(24)]
    mapping = fit_readout(training)
    wrong = [{**s, "reference": 30 - s["reference"]} for s in training]
    assert validate_readout(mapping, wrong, -10) is None
    assert validate_readout(mapping, training, -10, origin=10) is None, "验证必须包含实际中立读出偏差"
    path = tmp_path / "monocular_lean_learning.json"
    path.write_text(json.dumps({"version": 1, "profiles": []}), encoding="utf-8")
    estimator = MonocularLean(tmp_path, persist=True)
    estimator.set_context("camera")
    assert not estimator.profile["active"]


def test_kinect_is_optional_teacher_and_rejects_unmatched_or_old_body():
    body = poses(back=-12)[2]
    body.update(matched_to_model=True, body_valid=True, body_age_ms=10)
    assert kinect_pitch(body) == pytest.approx(-12.)
    for changes in ({"body_age_ms": None}, {"body_age_ms": 100}, {"matched_to_model": False}):
        assert kinect_pitch({**body, **changes}) is None


def test_monocular_source_reuses_same_frame_even_when_diagnostics_are_off(monkeypatch):
    kernel = SimpleNamespace(sensitivity_experiments={"fore_aft_source": "monocular"})
    comparison = DepthComparison(kernel)
    comparison._thread = SimpleNamespace(is_alive=lambda: True, join=lambda **_: None)
    pose = {**image_pose(), **{str(i): {"x": .5, "y": .4, "score": 1} for i in range(6)}}
    comparison.submit(np.zeros((240, 320, 3), np.uint8), pose, None, None, "none", 2, "source")
    meta, frame = comparison._pending
    assert meta["lean"] and not comparison.config["enabled"]
    assert meta["teacher"] is None and meta["references"] == []
    assert meta["input_scope"] == "pose" and frame is None
    assert comparison.status()["control_requested"] is True
    assert comparison.status()["active_model"] == "mocapnet_personal"
    assert comparison.config["model"] == "dav2_metric_s", "轻量单目选择独立于已退役的深度对比偏好"


def test_switching_between_comparison_and_monocular_restarts_only_the_owned_model(monkeypatch):
    kernel = SimpleNamespace(sensitivity_experiments={"fore_aft_source": "model"})
    comparison, started, joined = DepthComparison(kernel), [], []
    comparison.config.update(enabled=True, model="zipdepth")
    comparison._worker_model = "zipdepth"
    comparison._thread = SimpleNamespace(is_alive=lambda: True, join=lambda **_: joined.append(True))
    class Thread:
        def __init__(self, **kwargs):
            started.append(kwargs["args"][2]["model"])
        def start(self):
            pass
        def is_alive(self):
            return True
        def join(self, **kwargs):
            pass
    monkeypatch.setattr("motioncontrol.depth_comparison.threading.Thread", Thread)
    pose = {**image_pose(), **{str(i): {"x": .5, "y": .4, "score": 1} for i in range(6)}}
    frame = np.zeros((240, 320, 3), np.uint8)
    kernel.sensitivity_experiments["fore_aft_source"] = "monocular"
    comparison.submit(frame, pose, None, None, "none", 2., "source")
    assert joined == [True] and started == ["mocapnet_personal"]
    assert comparison._worker_model == "mocapnet_personal"
    kernel.sensitivity_experiments["fore_aft_source"] = "model"
    comparison.submit(frame, pose, None, None, "none", 3., "source")
    assert started == ["mocapnet_personal", "zipdepth"]
    assert comparison._worker_model == "zipdepth"
    comparison.close()
