"""Exercise the real request handler without importing hardware service globals."""
import ast
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlparse
import pytest

from motioncontrol.game_profiles import ProfileSelectionChanged
from test_game_profiles_v097 import make_store


def make_handler(namespace):
    """Exec the request handler out of server.py without importing the module.

    server.py pulls in the Windows output backend at import time, so the class
    is lifted out by AST instead.  Since the dual-plane split it comes in two
    parts: _BaseHandler holds the shared plumbing and AdminHandler the /api/*
    routes, so both have to be compiled, in order, for the base class to exist.
    """
    tree = ast.parse((Path(__file__).parents[1] / "server.py").read_text(encoding="utf-8"))
    wanted = ("_BaseHandler", "AdminHandler")
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name in wanted]
    assert [node.name for node in classes] == list(wanted), [node.name for node in classes]
    namespace.update(SimpleHTTPRequestHandler=SimpleHTTPRequestHandler, urlparse=urlparse,
                     unquote=unquote, parse_qs=parse_qs, Path=Path)
    exec(compile(ast.Module(body=classes, type_ignores=[]), "server.py", "exec"), namespace)
    request = object.__new__(namespace["AdminHandler"])
    request._is_loopback = lambda: True
    return request


def test_target_game_conflict_and_legacy_request_compatibility(tmp_path):
    store = make_store(tmp_path)
    applied, replies, released = [], [], []
    namespace = {
        "PROFILES": store, "PROFILE_UPDATE_LOCK": RLock(), "ProfileSelectionChanged": ProfileSelectionChanged,
        "KERNEL": SimpleNamespace(configure_bindings=applied.append), "INPUT_BRIDGE": SimpleNamespace(),
        # configure_profile_bindings 是后加的：换游戏时语音那边也要跟着换一批口令。
        # 替身里没它的话，路由会招 AttributeError 然后返回 400——看起来像参数不对，
        # 其实是少了一个方法。
        "VOICE": SimpleNamespace(_lock=RLock(), source_id="phone", _release_locked=released.append,
                                 configure_profile_bindings=lambda bindings: None),
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
