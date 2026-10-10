"""身体与手部重叠执行，仍只提交同一帧，并绑定原相机寿命。"""
import copy
import threading
from types import SimpleNamespace

import pytest

from motioncontrol.control_kernel import CameraUnavailable, MP_NAMES
from motioncontrol.recognition_models import (
    LazyGestureRecognizer, ScheduledGestureRecognizer, hand_crops_match,
)
from test_control_coordination import FakeCapture, FakeDetector, _camera, _wait_until, _shorten_worker_joins


def hand_pose():
    return {"left_elbow": {"x": .4, "y": .5, "score": 1},
            "left_wrist": {"x": .4, "y": .7, "score": 1}}


@pytest.mark.parametrize("change", ["moved", "lost", "low_score", "wrong_hand"])
def test_previous_crop_requires_current_body_confirmation(change):
    previous, current = hand_pose(), hand_pose()
    hands = {"left": [{"x": .4, "y": .7}]}
    assert hand_crops_match(previous, current, ["left"], 640, 480, hands)
    if change == "moved":
        current["left_wrist"]["x"] = .8
    elif change == "lost":
        current = None
    elif change == "low_score":
        current["left_wrist"]["score"] = .1
    else:
        hands["left"][0]["x"] = .9
    assert not hand_crops_match(previous, current, ["left"], 640, 480, hands)


def test_hand_thread_is_lazy_and_closes_model_on_its_own_thread(monkeypatch):
    calls = []
    monkeypatch.setattr(LazyGestureRecognizer, "recognize",
                        lambda *_args: calls.append(("recognize", threading.get_ident())) or {})
    monkeypatch.setattr(LazyGestureRecognizer, "close",
                        lambda *_args: calls.append(("close", threading.get_ident())))
    monkeypatch.setattr(LazyGestureRecognizer, "change_threads",
                        lambda *_args: calls.append(("threads", threading.get_ident())) or True)
    scheduled = ScheduledGestureRecognizer(None)
    assert scheduled.begin(None, None, [], None) is None
    assert scheduled.executor is None
    scheduled.begin(None, None, ["left"], None).result()
    assert scheduled.change_threads(1)
    scheduled.close()
    assert [item[0] for item in calls] == ["recognize", "threads", "close"]
    assert len({item[1] for item in calls}) == 1 and calls[0][1] != threading.get_ident()
    assert scheduled.executor is None


def test_thread_allocation_without_hand_demand_does_not_load_model():
    scheduled = ScheduledGestureRecognizer(None)
    assert scheduled.change_threads(2)
    assert scheduled.executor is None and scheduled.gesture.detector is None
    assert scheduled.gesture.cpu_threads is None and scheduled.gesture.requested_cpu_threads == 2


@pytest.mark.parametrize("case,model_sides", [("native", []), ("one_uncertain", ["right"]),
                                            ("depth_off", ["left", "right"]),
                                            ("other_person", ["left", "right"])])
def test_camera_fuses_native_points_and_only_runs_missing_hand_recognition(monkeypatch, case, model_sides):
    from test_kinect_camera import body
    native = body()
    for side in ("left", "right"):
        native["points"][side + "_wrist"] = {**native["points"][side + "_shoulder"], "color_y": .5}
    native["hand_states"] = {"left": {"state": "closed", "score": 1},
                             "right": {"state": "open", "score": 1}}
    raw_pose = {name: {"x": point["color_x"] + .01, "y": point["color_y"], "score": 1}
                for name, point in native["points"].items()}
    raw_pose["left_wrist"]["x"] += .04
    capture, detector = FakeCapture(one_frame=True), FakeDetector()
    camera = _camera(monkeypatch, capture, detector)
    request = {"enabled": False, "hands": []}
    camera.kernel.hand_mouse_controller = SimpleNamespace(tracking_request=lambda: copy.deepcopy(request))
    monkeypatch.setattr(detector, "detect_for_video", lambda *_args: SimpleNamespace(
        pose_landmarks=[[SimpleNamespace(x=raw_pose.get(name, {}).get("x", .5),
                        y=raw_pose.get(name, {}).get("y", .5), z=-.1, visibility=1)
                         for name in MP_NAMES]], pose_world_landmarks=[]))
    calls = []
    monkeypatch.setattr(LazyGestureRecognizer, "recognize",
                        lambda _self, _rgb, _pose, sides, _mp: calls.append(list(sides)) or {})
    try:
        camera.start()
        _wait_until(lambda: camera.frames == 1)
        assert calls == []
        request.update(enabled=True, hands=["left", "right"])
        if case == "one_uncertain":
            native["hand_states"]["right"]["score"] = 0
        elif case == "other_person":
            for point in native["points"].values():
                point["color_x"] += .3
        with camera._condition:
            camera._latest_frame = SimpleNamespace(shape=(480, 640, 3))
            camera._latest_depth = {"depth_valid": case != "depth_off", "bodies": [native]}
            camera._latest_sequence += 1
            camera._latest_capture_at += .033
            camera._condition.notify_all()
        _wait_until(lambda: camera.frames == 2)
        _, pose, extra = camera.kernel.frames[-1]
        if case in {"native", "one_uncertain"}:
            assert extra["depth_body"]["tracking_id"] == "person-one"
            assert pose["left_wrist"]["x"] == native["points"]["left_wrist"]["color_x"]
            assert pose["left_wrist"]["position_source"] == "kinect"
        else:
            assert extra["depth_body"] is None
            assert pose["left_wrist"]["x"] == raw_pose["left_wrist"]["x"]
            assert "position_source" not in pose["left_wrist"]
        assert calls == ([model_sides] if model_sides else [])
        if case == "native":
            assert camera.hand_model_state == "native"
    finally:
        capture.finish.set()
        camera.stop_and_wait(timeout_s=2)


def test_native_body_threads_use_current_body_before_hand_cropping(monkeypatch):
    capture, detector = FakeCapture(one_frame=True), FakeDetector()
    camera = _camera(monkeypatch, capture, detector)
    camera.pose_cpu_threads = 6
    camera.kernel.hand_mouse_controller = SimpleNamespace(tracking_request=lambda: {"enabled": True, "hands": ["left"]})
    entered, finish = threading.Event(), threading.Event()
    pose_calls, hand_calls = [], []
    def detect(image, _timestamp):
        pose_calls.append(image["data"])
        if len(pose_calls) == 2:
            entered.set()
            assert finish.wait(2)
        pose = hand_pose()
        pose["left_wrist"]["x"] = .8 if len(pose_calls) == 2 else .4
        points = [SimpleNamespace(x=pose.get(name, {}).get("x", .5), y=pose.get(name, {}).get("y", .5), z=0, visibility=1) for name in MP_NAMES]
        return SimpleNamespace(pose_landmarks=[points], pose_world_landmarks=[])
    def recognize(_self, rgb, pose, _sides, _mp):
        hand_calls.append((rgb, pose))
        return {}
    monkeypatch.setattr(detector, "detect_for_video", detect)
    monkeypatch.setattr(LazyGestureRecognizer, "recognize", recognize)
    try:
        camera.start()
        _wait_until(lambda: camera.frames == 1)
        second_frame = SimpleNamespace(shape=(480, 640, 3))
        with camera._condition:
            camera._latest_frame = second_frame
            camera._latest_sequence += 1
            camera._latest_capture_at += .033
            camera._condition.notify_all()
        assert entered.wait(1)
        assert len(hand_calls) == 1, "内部多线程启用后，手部不应提前用上一帧身体点裁切"
        finish.set()
        _wait_until(lambda: camera.frames == 2)
        assert len(hand_calls) == 2 and hand_calls[1][0] is second_frame
        assert hand_calls[1][1]["left_wrist"]["x"] == .8
        assert camera.performance()["hand_scheduling"] == "serial_same_frame"
    finally:
        finish.set()
        capture.finish.set()
        camera.stop_and_wait(timeout_s=2)


@pytest.mark.parametrize("action", ["normal", "stop", "moved", "configuration", "expired", "rotated", "lost"])
def test_camera_parallel_hands_keep_frame_and_stop_ownership(monkeypatch, action):
    capture, detector = FakeCapture(one_frame=True), FakeDetector()
    camera = _camera(monkeypatch, capture, detector)
    body_entered, hand_entered, finish = threading.Event(), threading.Event(), threading.Event()
    pose_calls, hand_calls, closed = [], [], []
    configured = {"enabled": True, "hands": ["left"]}
    camera.kernel.hand_mouse_controller = SimpleNamespace(tracking_request=lambda: copy.deepcopy(configured))

    def detect(image, _timestamp):
        pose_calls.append(image["data"])
        if len(pose_calls) == 2:
            body_entered.set()
            assert finish.wait(2)
        if action == "lost" and len(pose_calls) == 2:
            return SimpleNamespace(pose_landmarks=[], pose_world_landmarks=[])
        pose = hand_pose()
        if action == "moved" and len(pose_calls) == 2:
            pose["left_wrist"]["x"] = .8
        points = [SimpleNamespace(x=pose.get(name, {}).get("x", .5),
                                  y=pose.get(name, {}).get("y", .5), z=0,
                                  visibility=1) for name in MP_NAMES]
        return SimpleNamespace(pose_landmarks=[points], pose_world_landmarks=[])

    def recognize(_self, rgb, pose, _sides, _mp):
        hand_calls.append((rgb, pose))
        if len(hand_calls) == 2:
            hand_entered.set()
            assert finish.wait(2)
        if not pose:
            return {}
        wrist = pose["left_wrist"]
        return {"left": [{"x": wrist["x"], "y": wrist["y"]}]}

    monkeypatch.setattr(detector, "detect_for_video", detect)
    monkeypatch.setattr(LazyGestureRecognizer, "recognize", recognize)
    monkeypatch.setattr(LazyGestureRecognizer, "close", lambda *_args: closed.append(True))
    try:
        camera.start()
        _wait_until(lambda: camera.frames == 1)
        second_frame = SimpleNamespace(shape=(480, 640, 3))
        with camera._condition:
            camera._latest_frame = second_frame
            camera._latest_sequence += 1
            camera._latest_capture_at += .5 if action == "expired" else .033
            if action == "rotated":
                camera._latest_rotation = "cw90"
            camera._condition.notify_all()
        # 两个调用都进入，且相互都未结束：直接验证重叠而非比较易波动的耗时。
        assert body_entered.wait(1)
        if action in {"expired", "rotated"}:
            assert not hand_entered.is_set(), "过期或转向后的裁切不能提前使用"
        else:
            assert hand_entered.wait(1)
            assert hand_calls[1][0] is pose_calls[1] is second_frame
        if action == "stop":
            _shorten_worker_joins(camera, monkeypatch)
            camera.stop()
            capture.finish.set()
            assert camera._session is not None and closed == []
            with pytest.raises(CameraUnavailable):
                camera.start()
        elif action == "configuration":
            configured["enabled"] = False
        finish.set()
        if action == "stop":
            _wait_until(lambda: camera._session is None)
            assert len(camera.kernel.frames) == 1
            assert len(closed) == detector.close_count == capture.release_count == 1
        else:
            _wait_until(lambda: camera.frames == 2)
            hands = camera.kernel.frames[-1][2]["hands"]
            if action in {"configuration", "lost"}:
                assert hands == {}
            else:
                expected_x = .8 if action == "moved" else .4
                assert hands["left"][0]["x"] == expected_x
                assert len(hand_calls) == (3 if action == "moved" else 2)
                if action == "moved":
                    assert hand_calls[2][0] is second_frame
            metrics = camera.performance()
            assert sum(metrics["inference_stages_ms"].values()) == pytest.approx(metrics["inference_avg_ms"], abs=.2)
    finally:
        finish.set()
        capture.finish.set()
        camera.stop_and_wait(timeout_s=2)
