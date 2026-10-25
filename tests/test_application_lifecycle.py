"""退出失败隔离、按键优先释放和退出后的迟到请求；不连接真实设备。"""
import threading
from types import SimpleNamespace

import pytest

from test_configuration_coordination import application
from test_profile_api_v2 import request_for
from motioncontrol.control_kernel import LocalControlRuntime


@pytest.mark.parametrize("failed", [None, "camera", "recording"])
def test_runtime_always_closes_kernel_even_when_camera_or_recording_close_fails(failed):
    events = []

    def stop(name):
        events.append(name)
        if name == failed:
            raise OSError("模拟设备关闭失败")

    runtime = object.__new__(LocalControlRuntime)
    runtime.camera = SimpleNamespace(stop_and_wait=lambda: stop("camera"),
                                     depth_comparison=SimpleNamespace(lean_recording=SimpleNamespace(stop=lambda: stop("recording"))))
    runtime.kernel = SimpleNamespace(close=lambda: stop("kernel"))
    if failed:
        with pytest.raises(OSError, match="模拟设备关闭失败"):
            runtime.close()
    else:
        runtime.close()
    assert events == ["camera", "recording", "kernel"]


@pytest.mark.parametrize("failed", ["VOICE", "STUDIO", "RUNTIME", "FITNESS", "CLOUD_METADATA"])
def test_one_close_failure_does_not_skip_camera_output_or_ports(application, monkeypatch, failed):
    app, events = application, []

    def action(name):
        def run(*args, **kwargs):
            events.append(name)
            if name == failed:
                raise OSError("模拟收尾失败")
        return run

    with monkeypatch.context() as patch:
        patch.setattr(app, "_begin_shutdown", lambda: app.SHUTTING_DOWN.set())
        for name in ("SYSTEM_COMMANDS", "VOICE", "STUDIO", "INPUT_BRIDGE", "RUNTIME", "FITNESS_SYNC",
                     "FITNESS", "HOTKEYS", "LEARNER", "CLOUD_METADATA", "DISCOVERY"):
            patch.setattr(app, name, SimpleNamespace(close=action(name)))
        patch.setattr(app, "OUTPUT", SimpleNamespace(emergency_stop=action("release"), close=action("output")))
        device = SimpleNamespace(shutdown=action("device-stop"), server_close=action("device-close"))
        admin = SimpleNamespace(server_close=action("admin-close"))
        thread = SimpleNamespace(join=action("join"))
        errors = app._close_application(device, admin, thread, threading.Event(), thread)
        assert len(errors) == 1
        assert events[0] == "release" and events[-1] == "admin-close"
        assert all(name in events for name in ("VOICE", "RUNTIME", "output", "device-close", "admin-close"))


def test_exit_releases_keys_before_other_cleanup_and_invalidates_delayed_start(application, monkeypatch):
    app, events = application, []
    previous = app.INPUT_STOP_GENERATION
    with monkeypatch.context() as patch:
        patch.setattr(app.OUTPUT, "emergency_stop", lambda: events.append("release"))
        patch.setattr(app.SYSTEM_COMMANDS, "invalidate", lambda: events.append("cancel-commands"))
        patch.setattr(app.RUNTIME.camera, "cancel_start", lambda: events.append("cancel-camera"))
        patch.setattr(app.INPUT_BRIDGE, "set_body_enabled", lambda value: events.append("phone-off"))
        patch.setattr(app.POSE_TIMER, "cancel", lambda: events.append("cancel-pose"))
        assert app._begin_shutdown()
        assert not app._begin_shutdown()
        assert events == ["release", "cancel-commands", "cancel-camera", "phone-off", "cancel-pose"]
        assert app.INPUT_STOP_GENERATION == previous + 1
        with pytest.raises(ValueError, match="正在退出"):
            app._set_output_enabled(True)
        with pytest.raises(app.CameraUnavailable, match="正在退出"):
            app._set_body_input("computer", True)
        assert not app._command_allowed({})


def test_exit_wins_when_command_has_already_passed_its_first_check(application, monkeypatch):
    app, enabled = application, []
    with monkeypatch.context() as patch:
        patch.setattr(app, "_command_allowed", lambda action: True)
        patch.setattr(app.OUTPUT, "set_config", lambda **kwargs: enabled.append(kwargs))
        app.SHUTTING_DOWN.set()
        assert app.execute_system_target("OUTPUT.START")["executed"] is False
        assert not enabled


def test_exit_during_request_read_cannot_reenable_game_output(application, monkeypatch):
    app, replies, enabled = application, [], []

    def body():
        app.SHUTTING_DOWN.set()
        return {"enabled": True}

    request = request_for(app, "/api/output/config", replies)
    request._body = body
    with monkeypatch.context() as patch:
        patch.setattr(app.OUTPUT, "set_config", lambda **kwargs: enabled.append(kwargs))
        request.do_POST()
        assert replies[-1][0] >= 400
        assert not enabled


@pytest.mark.parametrize("local,origin,status", [(False, None, 403), (True, "http://other.test", 403),
                                                (True, "http://127.0.0.1:18866", 200)])
def test_exit_route_accepts_only_local_page_and_repeated_request_is_harmless(application, local, origin, status):
    app, replies, stopped, calls = application, [], threading.Event(), []

    def shutdown():
        calls.append("shutdown")
        stopped.set()

    request = request_for(app, "/api/shutdown", replies)
    request.server = SimpleNamespace(server_port=18866, shutdown=shutdown)
    request.headers = {"Origin": origin} if origin else {}
    request._is_loopback = lambda: local
    request._body = lambda: {}
    request.do_POST()
    assert replies[-1][0] == status
    if status == 200:
        assert stopped.wait(1)
        request.do_POST()
        assert calls == ["shutdown"] and replies[-1][1]["shutting_down"]
        mutation = request_for(app, "/api/output/config", replies)
        mutation._body = lambda: {"enabled": True}
        mutation.do_POST()
        assert replies[-1][0] == 503
    else:
        assert not app.SHUTTING_DOWN.is_set()
