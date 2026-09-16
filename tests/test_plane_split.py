"""The 8765/8766 split, asserted on the source rather than on a running server.

server.py cannot simply be imported here: it constructs the output backend,
control kernel and camera runtime at module scope.  So these read the AST, the
same trick test_profile_api_v2.py already uses.

What is being protected is a structural property.  Before the split, only
/api/shutdown was loopback-gated in do_GET, so anything on the LAN could read
the camera preview, the scene reference photo of the user's room, the live
kernel state and the current game config.  Binding the admin plane to
127.0.0.1 makes that unreachable no matter what any individual route forgets to
check -- provided the two handler classes stay independent, which is what the
inheritance test below is for.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

SERVER = pathlib.Path(__file__).resolve().parent.parent / "server.py"


@pytest.fixture(scope="module")
def tree():
    return ast.parse(SERVER.read_text(encoding="utf-8"))


def _class(tree, name):
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in server.py")


def _method(cls, name):
    for node in cls.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{cls.name}.{name} not found")


def test_both_planes_exist(tree):
    assert _class(tree, "_BaseHandler")
    assert _class(tree, "AdminHandler")
    assert _class(tree, "DeviceHandler")


def test_device_plane_does_not_inherit_the_admin_route_chain(tree):
    """The whole point of the split.

    If DeviceHandler subclassed AdminHandler it would inherit every /api/*
    route, and any route added to the admin plane later would silently become
    LAN-reachable.  Both must descend from _BaseHandler only.
    """
    bases = [b.id for b in _class(tree, "DeviceHandler").bases if isinstance(b, ast.Name)]
    assert bases == ["_BaseHandler"], bases
    admin_bases = [b.id for b in _class(tree, "AdminHandler").bases if isinstance(b, ast.Name)]
    assert admin_bases == ["_BaseHandler"], admin_bases


def test_device_plane_serves_only_the_whitelist(tree):
    """Exactly /ws/input plus the two read-only model routes, nothing else."""
    do_get = _method(_class(tree, "DeviceHandler"), "do_GET")
    literals = {
        node.value for node in ast.walk(do_get)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("/")
    }
    assert literals == {"/ws/input"}, literals
    # The model routes are reached through the shared helper, not re-listed.
    called = {
        node.func.attr for node in ast.walk(do_get)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "_try_model_route" in called
    assert "send_error" in called


def test_device_plane_rejects_writes(tree):
    """No POST/HEAD surface on the LAN plane at all."""
    device = _class(tree, "DeviceHandler")
    for verb in ("do_POST", "do_HEAD"):
        body = _method(device, verb).body
        calls = [n for n in ast.walk(ast.Module(body=body, type_ignores=[])) if isinstance(n, ast.Call)]
        assert calls, f"{verb} must do something"
        assert all(isinstance(c.func, ast.Attribute) and c.func.attr == "send_error" for c in calls), verb


def test_ws_input_is_not_on_the_admin_plane(tree):
    do_get = _method(_class(tree, "AdminHandler"), "do_GET")
    literals = {
        node.value for node in ast.walk(do_get)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "/ws/input" not in literals


def test_admin_plane_binds_loopback_only():
    """The listening address is the first boundary; assert it literally."""
    source = SERVER.read_text(encoding="utf-8")
    assert '_listen("127.0.0.1", args.admin_port, AdminHandler' in source
    assert "_listen(args.host, args.port, DeviceHandler" in source


def test_browser_opens_the_admin_plane():
    source = SERVER.read_text(encoding="utf-8")
    assert 'url = f"http://127.0.0.1:{args.admin_port}/"' in source


def test_admin_port_flag_exists():
    source = SERVER.read_text(encoding="utf-8")
    assert '"--admin-port"' in source
    assert "default=8766" in source
