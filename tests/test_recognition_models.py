"""模型自动选择与传送不改变手机默认值，也不依赖显卡名称。"""
import hashlib

import pytest

from motioncontrol.recognition_models import AutoPoseChoice, LazyGestureRecognizer, RecognitionModels
from motioncontrol.control_kernel import NativeCameraService
from test_configuration_coordination import application
from test_profile_api_v2 import request_for


@pytest.mark.parametrize("duration,fps,expected", [(20, 30, "heavy"), (42, 30, "full"), (42, 15, "heavy")])
def test_auto_choice_uses_frame_budget_and_actual_human_frames(duration, fps, expected):
    choice = AutoPoseChoice()
    for _ in range(40):
        assert choice.observe(1, fps, visible=False) is None
    for _ in range(40):
        assert choice.observe(1, fps, visible=True, hands_ready=False) is None
    results = [choice.observe(duration, fps, visible=True) for _ in range(27)]
    assert results[-1] == expected
    assert choice.measured_ms == duration


def test_sharing_full_does_not_accidentally_return_selected_heavy(tmp_path):
    models = tmp_path / "models" / "mediapipe"
    models.mkdir(parents=True)
    full = models / "pose_landmarker_full.task"
    full.write_bytes(b"full")
    heavy = models / "pose_landmarker_heavy.task"
    heavy.write_bytes(b"heavy")
    catalog = RecognitionModels(models.parent)
    assert catalog.paths["full"] == full.resolve()
    assert catalog.metadata("heavy")["sha256"] == hashlib.sha256(b"heavy").hexdigest()
    assert catalog.metadata("gesture")["available"] is False
    assert catalog.metadata("heavy")["url"] == "/api/model/mp-heavy"


def test_hand_model_is_not_created_without_hand_features(monkeypatch):
    detector = LazyGestureRecognizer(None)
    detector.configure(False)
    assert detector.detector is None and detector.state == "idle"
    detector.configure(True)
    assert detector.state == "error"
    detector.configure(False)
    assert detector.state == "idle" and detector.error is None


def test_model_setting_survives_restart_without_changing_source_or_controls(application, monkeypatch):
    app = application
    camera = app.RUNTIME.camera
    camera.model_paths = {"full": "full", "heavy": "heavy"}
    events = []
    camera.running = True
    monkeypatch.setattr(camera, "stop_and_wait", lambda: events.append("release"))
    monkeypatch.setattr(camera, "start", lambda: events.append("start"))
    controls = (dict(app.KERNEL.head_controller.config), dict(app.KERNEL.hand_mouse_controller.config))
    source = app.RUNTIME.body_mode
    replies = []
    request = request_for(app, "/api/recognition/models", replies)
    request._body = lambda: {"preference": "heavy"}
    request.do_POST()
    assert replies[-1][0] == 200
    assert events == ["release", "start"]
    assert app.KERNEL.general_setting("pose_model") == "heavy"
    assert NativeCameraService(app.KERNEL).model_preference == "heavy"
    assert app.RUNTIME.body_mode == source
    assert controls == (app.KERNEL.head_controller.config, app.KERNEL.hand_mouse_controller.config)
    camera.running = False


def test_model_send_is_explicit_and_does_not_select_phone_heavy(application, tmp_path, monkeypatch):
    app = application
    model = tmp_path / "pose_landmarker_heavy.task"
    model.write_bytes(b"heavy")
    app.RECOGNITION_MODELS = RecognitionModels(tmp_path)
    sent = []
    monkeypatch.setattr(app.INPUT_BRIDGE, "status", lambda: {"mobile_pose_connected": True})
    monkeypatch.setattr(app.INPUT_BRIDGE, "broadcast_control_config", lambda config: sent.append(config))
    replies = []
    request = request_for(app, "/api/recognition/models/send", replies)
    request._body = lambda: {}
    request.do_POST()
    assert replies[-1][0] == 200
    offer = sent[-1]["pose_model_offer"]
    assert offer["id"] == "mp-heavy" and isinstance(offer["request_id"], str)
    assert "selected" not in offer
    assert offer["sha256"] == hashlib.sha256(b"heavy").hexdigest()
