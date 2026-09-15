"""Exercise the real request handler without importing hardware service globals."""
import ast
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from urllib.parse import urlparse
import pytest

from game_profiles import ProfileSelectionChanged
from test_game_profiles_v097 import make_store


def make_handler(namespace):
    tree = ast.parse((Path(__file__).parents[1] / "server.py").read_text(encoding="utf-8"))
    handler = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Handler")
    namespace.update(SimpleHTTPRequestHandler=SimpleHTTPRequestHandler, urlparse=urlparse, Path=Path)
    exec(compile(ast.Module(body=[handler], type_ignores=[]), "server.py", "exec"), namespace)
    request = object.__new__(namespace["Handler"])
    request._is_loopback = lambda: True
    return request


def test_target_game_conflict_and_legacy_request_compatibility(tmp_path):
    store = make_store(tmp_path)
    applied, replies, released = [], [], []
    namespace = {
        "PROFILES": store, "PROFILE_UPDATE_LOCK": RLock(), "ProfileSelectionChanged": ProfileSelectionChanged,
        "KERNEL": SimpleNamespace(configure_bindings=applied.append), "INPUT_BRIDGE": SimpleNamespace(),
        "VOICE": SimpleNamespace(_lock=RLock(), source_id="phone", _release_locked=released.append),
    }
    request = make_handler(namespace)
    request.path = "/api/game-profiles/overrides"
    request._send_json = lambda data, status=200: replies.append((status, data))
    body = {"profile_id": "demo", "overrides": {}}
    request._body = lambda: body
    request.do_POST()
    assert replies[-1][0] == 409
    assert applied == []
    assert store.effective_profile()["id"] == "generic-xbox"
    body.pop("profile_id")
    request.do_POST()
    assert replies[-1][0] == 200
    assert len(applied) == 1
    assert released == ["phone"]


@pytest.mark.parametrize("failed_input", ["body", "voice"])
def test_body_and_voice_start_independently(failed_input):
    attempted, replies = [], []
    def start(name):
        attempted.append(name)
        if failed_input == name:
            raise RuntimeError(name + " unavailable")
        return {"running": True}
    namespace = {
        "INPUT_BRIDGE": SimpleNamespace(set_body_mode=lambda mode: None, clear_mobile_sources=lambda: None),
        "VOICE": SimpleNamespace(stop_local_microphone=lambda: None, disconnect=lambda: None,
                                 start_local_microphone=lambda: start("voice"), status=lambda: {}),
        "RUNTIME": SimpleNamespace(set_source=lambda *a, **kw: start("body"), status=lambda: {}),
    }
    request = make_handler(namespace)
    request.path = "/api/input/source"
    request._body = lambda: {"source": "computer", "enabled": True}
    request._send_json = lambda data, status=200: replies.append((status, data))
    request.do_POST()
    assert set(attempted) == {"body", "voice"}
    assert replies[-1][0] == (400 if failed_input == "body" else 200)
