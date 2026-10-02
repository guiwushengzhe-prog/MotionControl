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
