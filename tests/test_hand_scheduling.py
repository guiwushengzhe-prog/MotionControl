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
    scheduled = ScheduledGestureRecognizer(None)
    assert scheduled.begin(None, None, [], None) is None
    assert scheduled.executor is None
    scheduled.begin(None, None, ["left"], None).result()
    scheduled.close()
    assert [item[0] for item in calls] == ["recognize", "close"]
    assert calls[0][1] == calls[1][1] != threading.get_ident()
    assert scheduled.executor is None


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
