"""普通摄像头只用处理器解码；Kinect 原厂服务调高需管理员授权，且只在采集被拖慢时提示。"""
from types import SimpleNamespace

import pytest

from motioncontrol import kinect_camera
from motioncontrol.control_kernel import NativeCameraService


class FakeCv2:
    CAP_MSMF, CAP_DSHOW = 1400, 700
    CAP_PROP_HW_ACCELERATION, VIDEO_ACCELERATION_NONE, VIDEO_ACCELERATION_ANY = 50, 0, 1

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
    # 用户选了显卡加速才交给运行库自行选择硬件解码。
    NativeCameraService._video_capture(cv2, 2, cv2.CAP_MSMF, gpu=True)
    assert cv2.calls[-1] == (2, 1400, [50, 1])
    assert os.environ["OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS"] == "1"
    NativeCameraService._video_capture(cv2, 2, cv2.CAP_DSHOW, gpu=True)
    assert cv2.calls[-1] == (2, 700)


@pytest.mark.parametrize("available,expected", [(True, True), (False, False)])
def test_gpu_decoding_is_offered_only_when_the_runtime_has_media_foundation(available, expected):
    cv2 = SimpleNamespace(CAP_MSMF=1400, videoio_registry=SimpleNamespace(hasBackend=lambda api: available and api == 1400))
    assert NativeCameraService.gpu_decode_supported(cv2) is expected
    assert NativeCameraService.gpu_decode_supported(SimpleNamespace()) is False


def _decoder_camera(monkeypatch, supported):
    remembered = {}
    camera = SimpleNamespace(_lock=__import__("threading").Lock(), decoder_preference="cpu",
                             _remember=lambda key, value: remembered.update({key: value}),
                             backend_config=lambda: {"decoder": camera.decoder_preference},
                             gpu_decode_supported=lambda cv2=None: supported)
    return camera, remembered


def test_decoder_choice_is_saved_and_unsupported_gpu_is_refused(monkeypatch):
    from motioncontrol.control_kernel import CameraUnavailable
    camera, remembered = _decoder_camera(monkeypatch, supported=True)
    assert NativeCameraService.configure_decoder(camera, "gpu") == {"decoder": "gpu"}
    assert remembered == {"camera_decoder": "gpu"}
    camera, remembered = _decoder_camera(monkeypatch, supported=False)
    with pytest.raises(CameraUnavailable, match="不含显卡解码"):
        NativeCameraService.configure_decoder(camera, "gpu")
    with pytest.raises(CameraUnavailable):
        NativeCameraService.configure_decoder(camera, "fast")
    assert camera.decoder_preference == "cpu" and remembered == {}


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
    assert kinect_camera.boost_service() == {"boosted": True, "gpu_priority": "realtime"}
    assert helper == [("KinectCamera.exe", "--boost-service")]


def test_realtime_refused_falls_back_to_high_and_failures_are_reported(monkeypatch, helper):
    monkeypatch.setattr("motioncontrol.game_launch.is_administrator", lambda: False)
    monkeypatch.setattr(kinect_camera, "_run_elevated", lambda *_args: 10)
    assert kinect_camera.boost_service()["gpu_priority"] == "high"
    monkeypatch.setattr(kinect_camera, "_run_elevated", lambda *_args: 4)
    with pytest.raises(RuntimeError, match="显卡调度"):
        kinect_camera.boost_service()
    # 恢复默认只认成功退出码。
    monkeypatch.setattr(kinect_camera, "_run_elevated", lambda *_args: 10)
    with pytest.raises(RuntimeError):
        kinect_camera.restore_service()


def test_administrator_boosts_without_a_second_prompt(monkeypatch, helper):
    monkeypatch.setattr("motioncontrol.game_launch.is_administrator", lambda: True)
    runs = []
    monkeypatch.setattr(kinect_camera.owned_process, "run",
                        lambda args, **_kwargs: runs.append(args) or SimpleNamespace(returncode=0))
    assert kinect_camera.boost_service() == {"boosted": True, "gpu_priority": "realtime"}
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


def _boost_camera(game="forza", games=("forza",), backend="kinect", running=True, always=False):
    import threading
    remembered = {}
    camera = SimpleNamespace(running=running, selected_backend=backend, boost_games=set(games), boost_always=always,
                             backend_config=lambda: {"kinect_boost_always": camera.boost_always},
                             current_game=lambda: game, kinect_boost_state=None, _boost_declined=set(),
                             _boost_lock=threading.Lock(),
                             _remember=lambda key, value: remembered.update({key: value}))
    camera._current_game = lambda: NativeCameraService._current_game(camera)
    return camera, remembered


def test_a_starved_game_is_remembered_once(monkeypatch):
    camera, remembered = _boost_camera(game="rdr2", games=())
    NativeCameraService._note_starved_game(camera)
    NativeCameraService._note_starved_game(camera)
    assert camera.boost_games == {"rdr2"} and remembered == {"kinect_boost_games": ["rdr2"]}


@pytest.fixture
def service(monkeypatch):
    state = {"boosted": False, "calls": 0, "result": None}
    monkeypatch.setattr(kinect_camera, "service_pid", lambda: 4242)
    monkeypatch.setattr(kinect_camera, "service_boosted", lambda: state["boosted"])
    def boost():
        state["calls"] += 1
        if isinstance(state["result"], Exception):
            raise state["result"]
        state["boosted"] = True
        return {"boosted": True}
    monkeypatch.setattr(kinect_camera, "boost_service", boost)
    def restore():
        state["restores"] = state.get("restores", 0) + 1
        if isinstance(state.get("restore_result"), Exception):
            raise state["restore_result"]
        state["boosted"] = False
        return {"boosted": False}
    monkeypatch.setattr(kinect_camera, "restore_service", restore)
    return state


def test_remembered_game_asks_for_permission_at_start(service):
    camera, _ = _boost_camera()
    NativeCameraService.request_boost_for_game(camera)
    assert service["calls"] == 1 and camera.kinect_boost_state is None
    NativeCameraService.request_boost_for_game(camera)
    assert service["calls"] == 1, "已经调高就不再申请"


@pytest.mark.parametrize("kwargs", [dict(game="other"), dict(backend="dshow"), dict(running=False)])
def test_other_games_cameras_or_stopped_recognition_do_not_ask(service, kwargs):
    camera, _ = _boost_camera(**kwargs)
    NativeCameraService.request_boost_for_game(camera)
    assert service["calls"] == 0


def test_declined_permission_is_not_asked_again_for_the_same_service(service):
    service["result"] = PermissionError("已取消管理员授权")
    camera, _ = _boost_camera()
    NativeCameraService.request_boost_for_game(camera)
    NativeCameraService.request_boost_for_game(camera)
    assert service["calls"] == 1 and camera.kinect_boost_state == "declined"


def test_restore_runs_the_helper_in_restore_mode(monkeypatch, helper):
    monkeypatch.setattr("motioncontrol.game_launch.is_administrator", lambda: False)
    monkeypatch.setattr(kinect_camera, "service_boosted", lambda: False)
    assert kinect_camera.restore_service() == {"boosted": False, "gpu_priority": None}
    assert helper == [("KinectCamera.exe", "--restore-service")]


def test_switch_on_asks_any_game_at_start(service):
    camera, _ = _boost_camera(game="new-game", games=(), always=True)
    NativeCameraService.request_boost_for_game(camera)
    assert service["calls"] == 1


def test_switch_on_boosts_now_and_is_remembered(service):
    camera, remembered = _boost_camera(games=())
    assert NativeCameraService.configure_boost(camera, True) == {"kinect_boost_always": True}
    assert service["calls"] == 1 and remembered == {"kinect_boost_always": True}


def test_declined_switch_stays_off(service):
    service["result"] = PermissionError("已取消管理员授权")
    camera, remembered = _boost_camera(games=())
    with pytest.raises(PermissionError):
        NativeCameraService.configure_boost(camera, True)
    assert camera.boost_always is False and remembered == {}
    assert not camera._boost_lock.locked()


def test_switch_off_restores_and_a_declined_restore_is_reported(service):
    service["boosted"] = True
    camera, remembered = _boost_camera(always=True)
    assert NativeCameraService.configure_boost(camera, False) == {"kinect_boost_always": False}
    assert service["restores"] == 1 and remembered == {"kinect_boost_always": False}
    service.update(boosted=True, restore_result=PermissionError("已取消"))
    camera, _ = _boost_camera(always=True)
    assert NativeCameraService.configure_boost(camera, False) == {"kinect_boost_always": False, "restore_declined": True}
    assert camera.boost_always is False


def test_switch_without_kinect_only_saves_the_choice(service, monkeypatch):
    monkeypatch.setattr(kinect_camera, "service_boosted", lambda: None)
    camera, remembered = _boost_camera(games=())
    NativeCameraService.configure_boost(camera, True)
    assert service["calls"] == 0 and remembered == {"kinect_boost_always": True}


def test_switch_waits_for_a_pending_confirmation(service):
    from motioncontrol.control_kernel import CameraUnavailable
    camera, _ = _boost_camera()
    camera._boost_lock.acquire()
    with pytest.raises(CameraUnavailable, match="正在等待管理员确认"):
        NativeCameraService.configure_boost(camera, True)
    assert service["calls"] == 0
