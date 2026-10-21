"""实验踏步两路来源和基准隔离；不访问设备。"""
import copy

import pytest

from motioncontrol.experimental_march import ExperimentMarchLifts


def pose(left=0., right=0.):
    return {f"{side}_{part}": {"x": .4 if side == "left" else .6, "y": y, "score": 1.}
            for side, lift in (("left", left), ("right", right))
            for part, y in (("shoulder", .3), ("hip", .6), ("ankle", .9 - lift * .3))}


def body(left=0., right=0., identity="person"):
    return {"tracking_id": identity, "floor": [0, 1, 0, 0],
            "points": {f"{side}_{part}": {"x": -.1 if side == "left" else .1, "y": y, "z": 2., "score": 1.}
                       for side, lift in (("left", left), ("right", right))
                       for part, y in (("shoulder", 1.5), ("hip", 1.), ("ankle", .1 + lift * .5))}}


def calibrate(adapter, image=None, depth=None, source="model"):
    for i in range(12):
        adapter.update(pose() if image is None else image, body() if depth is None else depth,
                       640, 480, 1 + i / 30, source=source)
    assert adapter.status()["ready"]


@pytest.mark.parametrize("source", ["model", "depth"])
def test_each_source_measures_its_own_leg_direction_and_never_falls_back(source):
    adapter = ExperimentMarchLifts()
    calibrate(adapter, source=source)
    image, depth = pose(left=.1), body(right=.12)
    result = adapter.update(image, depth, 640, 480, 1.4, source=source)
    assert result["left"] == pytest.approx(.1 if source == "model" else -.12)
    snapshot = adapter.status()
    assert snapshot["sources"]["model"]["lifts"]["left"] == pytest.approx(.1)
    assert snapshot["sources"]["depth"]["lifts"]["right"] == pytest.approx(.12)
    image, depth = (None, depth) if source == "model" else (image, None)
    assert adapter.update(image, depth, 640, 480, 1.44, source=source) is None
    assert not adapter.status()["ready"]
    assert adapter.status()["sources"]["depth" if source == "model" else "model"]["ready"]


def test_sources_keep_separate_neutral_offsets_and_switch_silently():
    adapter = ExperimentMarchLifts()
    calibrate(adapter, image=pose(left=.02), depth=body(right=.03))
    assert adapter.update(pose(left=.12), body(left=.07), 640, 480, 1.4)["left"] == pytest.approx(.1)
    assert adapter.update(pose(left=.12), body(left=.07), 640, 480, 1.44, source="depth") is None
    assert adapter.update(pose(left=.12), body(left=.07), 640, 480, 1.48, source="depth")["left"] == pytest.approx(.1)
    assert adapter.update(pose(left=.12), body(left=.07), 640, 480, 1.52, source="model") is None


@pytest.mark.parametrize("failure", ["fused", "out_of_frame", "gap"])
def test_model_rejects_fused_or_invisible_feet_and_rebuilds_after_gap(failure):
    adapter = ExperimentMarchLifts()
    calibrate(adapter)
    image, now = pose(left=.1), 1.4
    if failure == "fused":
        image["left_ankle"]["position_source"] = "kinect"
    elif failure == "out_of_frame":
        image["left_ankle"]["y"] = 1.3
    else:
        now = 2.
    assert adapter.update(image, body(), 640, 480, now) is None
    assert not adapter.status()["ready"]


def test_depth_person_change_and_missing_frames_never_lease_old_lifts():
    adapter = ExperimentMarchLifts()
    calibrate(adapter, source="depth")
    assert adapter.update(pose(), body(left=.1), 640, 480, 1.4, source="depth")["left"] > 0
    assert adapter.update(pose(left=.1), None, 640, 480, 1.44, source="depth") is None
    assert adapter.update(pose(), body(identity="another"), 640, 480, 1.48, source="depth") is None
    assert "站稳" in adapter.status()["message"]


def test_moving_or_raised_feet_do_not_become_first_neutral_baseline():
    adapter = ExperimentMarchLifts()
    for i in range(20):
        assert adapter.update(pose(left=.1), body(left=.1), 640, 480, 1 + i / 30) is None
    assert not any(state["ready"] for state in adapter.status()["sources"].values())
    adapter.reset()
    assert not adapter.status()["ready"]
