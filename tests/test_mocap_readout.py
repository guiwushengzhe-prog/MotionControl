"""躯干角度换算的输入边界、幅度与中立站姿；不读取个人模型和录像。"""
import numpy as np
import pytest

from motioncontrol.mocap_readout import TorsoReadout
from motioncontrol.monocular_lean import MonocularLean


TORSO = [0, 1, 2, 3, 4, 5, 16, 18, 20]


def head():
    keys = [f"unused{i}" for i in range(42)]
    for i, joint, axis in zip(TORSO, [j for j in ("abdomen", "chest", "hip") for _ in range(3)], "xyz" * 3):
        keys[i] = f"{joint}_{axis}rotation"
    angles = np.linspace(-.15, .15, 40)
    points = np.zeros((len(angles), 9))
    points[:, 0] = angles
    return {"keys": keys, "method": "local_torso_v1", "local": {
        "feature_indices": TORSO, "features": points.tolist(),
        "targets": (120 * angles + 100 * angles ** 2).tolist(),
        "scale": [.05] * 9, "neighbors": 8, "ridge": .01}}


def torso(model, position):
    points = [position] + [0.] * 41
    return {"features": points, "geometric_deg": model.infer(points), "sampled_regions": 0,
            "personal_research": True, "readout_kind": "local_torso_v1"}


def test_local_angle_preserves_amplitude_and_ignores_head_arm_and_position():
    model = TorsoReadout(head())
    for position in (-.1, .1):
        values = [position] + [0.] * 41
        expected = 120 * position + 100 * position ** 2
        assert model.infer(values) == pytest.approx(expected, abs=.15)
        for i in (12, 15, 17, 19, 21, 24, 39):
            values[i] = 500.
        assert model.infer(values) == pytest.approx(expected, abs=.15)


def test_basic_linear_mapping_also_rejects_non_torso_weights():
    value = head()
    value.pop("local")
    value.pop("method")
    value["weights"] = [1.] + [0.] * 41
    assert TorsoReadout(value).infer([12.] + [0.] * 41) == 12
    value["weights"][21] = 1.
    with pytest.raises(ValueError, match="仅使用躯干"):
        TorsoReadout(value)


@pytest.mark.parametrize("change", [
    lambda m: m["features"][0].pop(),
    lambda m: m["targets"].__setitem__(0, float("nan")),
    lambda m: m["scale"].__setitem__(0, 0),
    lambda m: m.__setitem__("feature_indices", list(range(9))),
])
def test_local_mapping_rejects_wrong_shape_nonfinite_or_changed_features(change):
    value = head()
    change(value["local"])
    with pytest.raises(ValueError):
        TorsoReadout(value)


def test_local_angle_uses_current_neutral_without_reference_and_recalibrates():
    model = TorsoReadout(head())
    left, right = (MonocularLean(feature_dim=42, feature_indices=TORSO) for _ in range(2))
    for at in np.arange(0, 3.1, .1):
        a = left.update(torso(model, .02), float(at), reference=-12)
        b = right.update(torso(model, .02), float(at), reference=22)
    assert a["angle_deg"] == b["angle_deg"] == 0
    for position, at in ((-.1, 3.2), (.1, 3.3)):
        a = left.update(torso(model, position), at, reference=-30)
        b = right.update(torso(model, position), at, reference=30)
        assert a["angle_deg"] == b["angle_deg"]
        assert a["angle_deg"] == pytest.approx(model.infer(torso(model, position)["features"])
                                               - model.infer(torso(model, .02)["features"]))
        assert a["reference_used_for_prediction"] is False
    reset = left.update(torso(model, .04), 4., revision=1)
    assert reset["calibrating"] and "angle_deg" not in reset
    for at in np.arange(4.1, 7.2, .1):
        state = left.update(torso(model, .04), float(at), revision=1)
    assert state["valid"] and state["angle_deg"] == pytest.approx(0)


@pytest.mark.parametrize("base_gain", [1., .35])
def test_online_learning_compares_against_local_baseline_and_records_it(base_gain):
    engine = MonocularLean(feature_dim=42, feature_indices=TORSO)
    for at in np.arange(0, 3.1, .25):
        engine.update({**torso(TorsoReadout(head()), 0.), "geometric_deg": -10.}, float(at),
                      reference=-10, learn=True)
    at = 3.25
    for i in range(240):
        angle = (0., -12., 12., -6., 6.)[(i // 8) % 5]
        frame = {"features": [angle * .35] + [0.] * 41, "geometric_deg": angle * base_gain - 10,
                 "sampled_regions": 0, "personal_research": True, "readout_kind": "local_torso_v1"}
        state = engine.update(frame, at, reference=angle - 10, learn=True)
        at += .25
    assert all("base_deg" in s for s in engine.profile["samples"])
    assert bool(engine.profile["active"]) == (base_gain < 1)
    assert state["angle_deg"] == pytest.approx(angle, abs=.3)
