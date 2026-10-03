"""Exercise the real request handler with isolated service state."""
import pytest

from motioncontrol_shared.profile_schema import flatten_bindings
from test_configuration_coordination import application


def request_for(app, route, replies):
    request = object.__new__(app.AdminHandler)
    request._is_loopback = lambda: True
    request.path = route
    request._send_json = lambda data, status=200: replies.append((status, data))
    return request


def test_custom_pose_rule_update_reaches_store_and_kernel(application):
    from motioncontrol.custom_poses import CustomPoseStore
    from test_pose_template import T_POSE, pose

    app, replies = application, []
    app.CUSTOM_POSES.capture(T_POSE, "两个姿势")
    raised = pose(left_arm=170, right_arm=170)
    app.CUSTOM_POSES.append_frame("custom1", raised)
    request = request_for(app, "/api/pose/custom/update", replies)
    request._body = lambda: {"id": "custom1", "match_mode": "any", "dwell_frames": 1}
    request.do_POST()
    assert replies[-1][0] == 200
    assert replies[-1][1]["pose"]["match_mode"] == "any"
    assert len(replies[-1][1]["poses"]) == 1
    assert CustomPoseStore(app.CUSTOM_POSES.path).status()[0]["match_mode"] == "any"
    app.KERNEL.handle_pose_map("test", raised, width=640, height=480)
    assert "custom1" in app.KERNEL.pose_active

    request._body = lambda: {"id": "custom1", "match_mode": "all"}
    request.do_POST()
    assert replies[-1][0] == 400
    assert app.CUSTOM_POSES.status()[0]["match_mode"] == "any"


def test_target_game_conflict_and_legacy_request_compatibility(application):
    app, replies = application, []
    before = app.KERNEL.control_bindings.copy()
    request = request_for(app, "/api/game-profiles/overrides", replies)
    body = {"profile_id": "demo", "overrides": {}}
    request._body = lambda: body
    request.do_POST()
    assert replies[-1][0] == 409
    assert app.KERNEL.control_bindings == before
    assert app.CONFIG_REVISION == 0
    assert app.PROFILES.effective_profile()["id"] == "generic-xbox"
    body.pop("profile_id")
    request.do_POST()
    assert replies[-1][0] == 200
    assert app.CONFIG_REVISION == 1
    bindings = app.PROFILES.effective_profile()["bindings"]
    assert app.KERNEL.control_bindings == flatten_bindings(bindings)
    assert app.VOICE._profile_bindings == bindings


@pytest.mark.parametrize("failed_input", ["body", "voice"])
def test_body_and_voice_start_independently(application, monkeypatch, failed_input):
    app, attempted, replies = application, [], []

    def start(name):
        attempted.append(name)
        if failed_input == name:
            raise RuntimeError(name + " unavailable")
        return {"running": True}

    monkeypatch.setattr(app.VOICE, "start_local_microphone", lambda device=None: start("voice"))
    monkeypatch.setattr(app.RUNTIME, "set_source", lambda *args, **kwargs: start("body"))
    request = request_for(app, "/api/input/source", replies)
    request._body = lambda: {"source": "computer", "enabled": True}
    request.do_POST()
    assert set(attempted) == {"body", "voice"}
    assert replies[-1][0] == (400 if failed_input == "body" else 200)
