"""实测参考必须保留完整比例与资料来源，不能把画外腿夹回画面。"""
import json
import math

import pytest

from motioncontrol.body_reference import (
    complete_pose, extract_recording_reference, make_reference,
    reach_sectors, validate_reference, visible_pose,
)


def standing(dx=0):
    pose = {"nose": {"x": .5 + dx, "y": .10, "score": .95}}
    for side, x in (("left", .4), ("right", .6)):
        for part, y in (("shoulder", .20), ("elbow", .35), ("wrist", .50),
                        ("hip", .50), ("knee", .70), ("ankle", .90)):
            pose[f"{side}_{part}"] = {"x": x + dx, "y": y, "score": .9}
    return pose


def reference():
    return make_reference([standing()], 640, 480, {"kind": "measurement"})


def save_frames(path, poses, width=640, height=480):
    lines = [{"schema": "motioncontrol.pose_recording.v1"}]
    lines.extend({"pose": pose, "width": width, "height": height, "t": i / 30}
                 for i, pose in enumerate(poses))
    path.write_text("\n".join(json.dumps(frame) for frame in lines), encoding="utf-8")


def test_no_data_and_incomplete_frames_do_not_create_reference(tmp_path):
    assert visible_pose(None) == {}
    assert not complete_pose({})
    assert make_reference([], 640, 480, {}) is None
    assert make_reference([{"nose": standing()["nose"]}], 640, 480, {}) is None
    assert extract_recording_reference(tmp_path / "absent.jsonl") is None
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    assert extract_recording_reference(path) is None
    assert reach_sectors(None) == []


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, True, -0.01, 1.01])
def test_visible_points_and_saved_reference_reject_invalid_coordinates(bad):
    pose = standing()
    pose["left_ankle"]["y"] = bad
    assert "left_ankle" not in visible_pose(pose)
    assert not complete_pose(pose)
    raw = reference()
    raw["pose"] = pose
    assert validate_reference(raw) is None


@pytest.mark.parametrize("dimensions", [(0, 480), (640, -1), (math.inf, 480), (True, 480)])
def test_bad_dimensions_are_rejected(dimensions):
    assert make_reference([standing()], *dimensions, {}) is None
    raw = reference()
    raw["width"], raw["height"] = dimensions
    assert validate_reference(raw) is None


def test_visibility_is_validated_and_observed_points_are_marked_current():
    pose = standing()
    pose["nose"] = {"x": .5, "y": .1, "visibility": .9}
    pose["extra_low"] = {"x": .2, "y": .3, "score": .419}
    pose["extra_bad"] = {"x": .2, "y": .3, "score": math.nan}
    visible = visible_pose(pose)
    assert complete_pose(pose)
    assert "extra_low" not in visible and "extra_bad" not in visible
    assert all(point["source"] == "current" for point in visible.values())


def test_median_reference_does_not_select_an_extreme_single_frame():
    poses = [standing(dx) for dx in (-.001, 0, .001, .002, .20)]
    raw = make_reference(poses, 640, 480, {"kind": "measurement", "frames": 5})
    assert raw["pose"]["nose"]["x"] == pytest.approx(.501)
    assert raw["source"]["frames"] == 5
    assert raw["captured_at"] > 0
    assert all(point["source"] == "recorded" for point in raw["pose"].values())
    assert validate_reference(raw) == raw


def test_recording_selects_stable_contiguous_window_and_keeps_source(tmp_path):
    path = tmp_path / "arbitrary_capture.jsonl"
    moving = [standing(.02 if i % 2 else -.02) for i in range(20)]
    bent = standing()
    bent["left_knee"]["x"] = .6
    stable = [standing(.001 if i == 0 else 0) for i in range(15)]
    save_frames(path, moving + [bent] + stable, 1280, 720)
    before = path.read_bytes()
    raw = extract_recording_reference(path)
    assert raw["width"] == 1280 and raw["height"] == 720
    assert raw["source"] == {"kind": "recording", "path": str(path.resolve()),
                             "t_start": 21 / 30, "t_end": 35 / 30, "frames": 15}
    assert raw["pose"]["left_ankle"]["x"] == pytest.approx(.4)
    assert path.read_bytes() == before


def test_recording_requires_ten_consecutive_candidates_and_accepts_ten(tmp_path):
    path = tmp_path / "short.jsonl"
    partial = standing()
    del partial["left_ankle"]
    save_frames(path, [standing()] * 9 + [partial] + [standing()] * 9)
    assert extract_recording_reference(path) is None
    save_frames(path, [standing()] * 10)
    assert extract_recording_reference(path)["source"]["frames"] == 10


def test_recording_rejects_raised_foot_and_bent_knees(tmp_path):
    path = tmp_path / "not_standing.jsonl"
    raised, bent = standing(), standing()
    raised["left_ankle"]["y"] -= .04
    bent["left_knee"]["x"] += .2
    save_frames(path, [raised] * 15 + [bent] * 15)
    assert extract_recording_reference(path) is None


def test_recording_knee_angle_is_measured_in_pixels(tmp_path):
    path = tmp_path / "portrait.jsonl"
    pose = standing()
    pose["left_knee"]["x"] += .1
    save_frames(path, [pose] * 10, 100, 1000)
    assert extract_recording_reference(path) is not None, "细长画面上的10像素侧移仍接近直腿"
    save_frames(path, [pose] * 10, 1000, 100)
    assert extract_recording_reference(path) is None, "宽画面相同归一化侧移实际是100像素弯腿"


def test_reach_sectors_need_samples_and_valid_complete_bone_chains():
    raw = reference()
    assert reach_sectors(raw) == []
    raw["reaches"] = {"leftHand": [{"x": .1, "y": .2}] * 4,
                      "rightFoot": [{"x": math.inf, "y": .8}] * 5}
    assert reach_sectors(raw) == []
    raw["reaches"]["leftHand"].append({"x": .1, "y": .2})
    assert len(reach_sectors(raw)) == 1
    raw["pose"]["left_elbow"] = dict(raw["pose"]["left_shoulder"])
    assert reach_sectors(raw) == []


def test_reach_sectors_are_symmetric_limited_and_use_pixel_bone_lengths():
    raw = reference()
    raw["reaches"] = {"leftHand": [{"x": -.2, "y": -4}] * 5,
                      "rightHand": [{"x": 1.2, "y": -4}] * 5,
                      "leftFoot": [{"x": -.2, "y": -4}] * 5,
                      "rightFoot": [{"x": 1.2, "y": -4}] * 5}
    sectors = {(item["side"], item["kind"]): item for item in reach_sectors(raw)}
    assert len(sectors) == 4
    for kind, maximum in (("hand", 160), ("foot", 85)):
        left, right = sectors["left", kind], sectors["right", kind]
        assert left["end_angle"] == maximum
        assert right["end_angle"] == maximum
        assert 0 < left["end_angle"] - left["start_angle"] < 180
        assert left["rx"] * raw["width"] == pytest.approx(left["ry"] * raw["height"])
        assert left["rx"] == right["rx"] and left["ry"] == right["ry"]
        assert len(left["points"]) < 30
        for a, b in zip(left["points"], right["points"]):
            assert a["x"] + b["x"] == pytest.approx(1)
            assert a["y"] == pytest.approx(b["y"])
            assert a["x"] <= left["cx"] + 1e-9
            assert b["x"] >= right["cx"] - 1e-9
    assert sectors["left", "hand"]["ry"] == pytest.approx(.30)
    assert sectors["left", "foot"]["ry"] == pytest.approx(.40)


def test_reach_sectors_follow_mirrored_shoulders_and_reject_bad_samples():
    raw = reference()
    for point in raw["pose"].values():
        point["x"] = 1 - point["x"]
    raw["reaches"] = {"leftHand": [{"x": .95, "y": .2}] * 5
                      + [{"x": 6, "y": .2}, {"x": True, "y": .1}, {"x": .8, "y": math.nan}]}
    sector, = reach_sectors(raw)
    assert sector["side"] == "left"
    assert sector["end_angle"] == pytest.approx(100)
    assert all(point["x"] >= sector["cx"] - 1e-9 for point in sector["points"])
