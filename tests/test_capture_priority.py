"""普通摄像头只用处理器解码；Kinect 原厂服务调高需管理员授权，且只在采集被拖慢时提示。"""
from types import SimpleNamespace

import pytest

from motioncontrol import kinect_camera
from motioncontrol.control_kernel import NativeCameraService


class FakeCv2:
    CAP_MSMF, CAP_DSHOW = 1400, 700
    CAP_PROP_HW_ACCELERATION, VIDEO_ACCELERATION_NONE = 50, 0

    def __init__(self):
        self.calls = []

    def VideoCapture(self, *args):
        self.calls.append(args)
        return SimpleNamespace(isOpened=lambda: False)


def test_msmf_is_opened_without_gpu_decoding_and_dshow_unchanged(monkeypatch):
    monkeypatch.delenv("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", raising=False)
    cv2 = FakeCv2()
    NativeCameraService._video_capture(cv2, 2, cv2.CAP_MSMF)
    NativeCameraService._video_capture(cv2, 2, cv2.CAP_DSHOW)
    assert cv2.calls == [(2, 1400, [50, 0]), (2, 700)]
    import os
    assert os.environ["OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS"] == "0"


def test_old_runtime_without_acceleration_property_still_opens():
    calls = []
    cv2 = SimpleNamespace(CAP_MSMF=1400, VideoCapture=lambda *args: calls.append(args))
    NativeCameraService._video_capture(cv2, 0, 1400)
    assert calls == [(0, 1400)]


@pytest.fixture
def helper(monkeypatch):
    monkeypatch.setattr(kinect_camera, "bridge_executable", lambda: "KinectCamera.exe")
    monkeypatch.setattr(kinect_camera, "service_boosted", lambda: True)
    elevated = []
    monkeypatch.setattr(kinect_camera, "_run_elevated", lambda exe, args, timeout: elevated.append((exe, args)) or 0)
    return elevated


def test_regular_user_is_asked_for_permission_before_boosting(monkeypatch, helper):
    monkeypatch.setattr("motioncontrol.game_launch.is_administrator", lambda: False)
    assert kinect_camera.boost_service() == {"boosted": True}
    assert helper == [("KinectCamera.exe", "--boost-service")]


def test_administrator_boosts_without_a_second_prompt(monkeypatch, helper):
    monkeypatch.setattr("motioncontrol.game_launch.is_administrator", lambda: True)
    runs = []
    monkeypatch.setattr(kinect_camera.owned_process, "run",
                        lambda args, **_kwargs: runs.append(args) or SimpleNamespace(returncode=0))
    assert kinect_camera.boost_service() == {"boosted": True}
    assert runs == [["KinectCamera.exe", "--boost-service"]] and helper == []


def test_refused_or_failed_boost_is_reported(monkeypatch, helper):
    monkeypatch.setattr("motioncontrol.game_launch.is_administrator", lambda: False)
    monkeypatch.setattr(kinect_camera, "_run_elevated", lambda *_args: 3)
    with pytest.raises(RuntimeError, match="权限"):
        kinect_camera.boost_service()
    def cancelled(*_args):
        raise PermissionError("已取消管理员授权，原厂服务优先级未改变")
    monkeypatch.setattr(kinect_camera, "_run_elevated", cancelled)
    with pytest.raises(PermissionError, match="已取消"):
        kinect_camera.boost_service()


@pytest.mark.parametrize("kind,backend,boosted,expected", [
    ("capture", "kinect", False, ["boost_kinect_service", "disable_depth"]),
    ("capture", "kinect", True, ["disable_depth"]),
    ("capture", "kinect", None, ["disable_depth"]),
    ("inference", "kinect", False, ["standard_model", "disable_depth"]),
    ("capture", "msmf", False, []),
])
def test_boost_is_offered_only_when_the_kinect_service_is_starved(monkeypatch, kind, backend, boosted, expected):
    monkeypatch.setattr(kinect_camera, "service_boosted", lambda: boosted)
    camera = SimpleNamespace(_shortage=SimpleNamespace(kind=kind), running=True, actual_model="heavy",
                             selected_backend=backend, depth_supported=backend == "kinect",
                             depth_enabled=backend == "kinect")
    assert NativeCameraService._resource_hint(camera) == {"kind": kind, "actions": expected}
