"""换电脑故障的确定性复现，不打开真实摄像头或控制输出。"""
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from motioncontrol.control_kernel import NativeCameraService, LocalControlRuntime, CameraUnavailable
from test_configuration_coordination import application
from test_profile_api_v2 import request_for


class Kernel:
    def general_setting(self, name, default=None):
        return default

    def remember_general_setting(self, *args):
        pass

    def clear_body(self):
        pass

    def status(self):
        return {'head': {}}


def camera_service(monkeypatch):
    monkeypatch.setattr(NativeCameraService, '_camera_catalog', staticmethod(lambda **kwargs: {}))
    return NativeCameraService(Kernel())


@pytest.mark.parametrize('choice', ['full', 'heavy'])
def test_model_and_heavy_fallback_use_bytes_from_chinese_path(tmp_path, monkeypatch, choice):
    camera = camera_service(monkeypatch)
    model = tmp_path / '中文识别模型' / 'pose.task'
    model.parent.mkdir()
    model.write_bytes(b'official-model-bytes')
    camera.model_paths = {choice: model}
    captured = []
    base = lambda **kwargs: SimpleNamespace(**kwargs)
    vision = SimpleNamespace(PoseLandmarkerOptions=base, RunningMode=SimpleNamespace(VIDEO='video'), PoseLandmarker=object())
    python = SimpleNamespace(BaseOptions=base, vision=vision)
    monkeypatch.setitem(sys.modules, 'mediapipe', SimpleNamespace(tasks=SimpleNamespace(python=python)))
    monkeypatch.setitem(sys.modules, 'mediapipe.tasks', SimpleNamespace(python=python))
    monkeypatch.setitem(sys.modules, 'mediapipe.tasks.python', python)
    monkeypatch.setattr('motioncontrol.pose_model_optimization.pose_model_without_mask', lambda path: b'optimized')
    def create(kind, options, **kwargs):
        captured.append(options.base_options)
        if choice == 'heavy' and len(captured) == 1:
            raise ValueError('运行库要求原始模型')
        return object(), 1, None
    monkeypatch.setattr('motioncontrol.cpu_inference.create_cpu_task', create)
    camera._create_detector(choice)
    assert all(not hasattr(options, 'model_asset_path') for options in captured)
    assert captured[-1].model_asset_buffer == model.read_bytes()


def test_slow_camera_keeps_runtime_status_readable_and_stop_deadline_bounded(monkeypatch):
    camera = camera_service(monkeypatch)
    runtime = LocalControlRuntime(camera.kernel, camera)
    entered, release, read = threading.Event(), threading.Event(), threading.Event()
    errors = []
    def slow(cv2):
        entered.set()
        assert release.wait(3)
        raise CameraUnavailable('替代设备停止')
    monkeypatch.setattr(camera, '_select_capture', slow)
    def start():
        try:
            runtime.set_source('computer')
        except CameraUnavailable as error:
            errors.append(str(error))
    worker = threading.Thread(target=start, daemon=True)
    worker.start()
    try:
        assert entered.wait(2)
        reader = threading.Thread(target=lambda: (runtime.status(), read.set()), daemon=True)
        reader.start()
        assert read.wait(.5), '状态读取不能等待设备初始化'
        begin = time.monotonic()
        with pytest.raises(CameraUnavailable):
            camera.stop_and_wait(.03)
        assert time.monotonic() - begin < .5
        assert camera.status()['lifecycle'] == 'stopping'
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and errors
    assert camera.status()['error_code'] == 'camera_cancelled'
    assert camera.status()['lifecycle'] == 'stopped'


@pytest.mark.parametrize('origin,site', [('https://untrusted.invalid', None), (None, 'cross-site')])
def test_foreign_page_cannot_change_output(application, monkeypatch, origin, site):
    app, replies, calls = application, [], []
    request = request_for(app, '/api/output/config', replies)
    request.headers = {key: value for key, value in [('Origin', origin), ('Sec-Fetch-Site', site)] if value}
    request._body = lambda: {'enabled': True}
    monkeypatch.setattr(app.OUTPUT, 'set_config', lambda **kwargs: calls.append(kwargs))
    request.do_POST()
    assert replies[-1][0] == 403 and not calls


def test_reading_shutdown_route_never_exits(application, monkeypatch):
    app, replies, calls = application, [], []
    request = request_for(app, '/api/shutdown', replies)
    monkeypatch.setattr(app, '_begin_shutdown', lambda: calls.append(True))
    request.do_GET()
    assert replies[-1][0] == 405 and not calls


def test_foreign_host_cannot_read_pairing_code(application):
    replies = []
    request = request_for(application, '/api/phone-connect', replies)
    request.headers = {'Host': 'untrusted.invalid:8766'}
    request.do_GET()
    assert replies[-1][0] == 403
