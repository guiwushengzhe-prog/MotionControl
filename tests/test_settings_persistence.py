"""保存失败不丢最新设置、重试限次、有效副本恢复与接口真实反馈。"""
import json
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

from motioncontrol import config_transaction
from motioncontrol.control_kernel import ControlKernel
from motioncontrol.settings_persistence import SettingsPersistence
from motioncontrol.view_control import configure_view_control
from test_configuration_coordination import application
from test_general_settings_persist import Output
from test_profile_api_v2 import request_for


@pytest.mark.parametrize("damage", ["missing", "broken", "wrong-shape"])
def test_recent_good_settings_restore_after_damage(tmp_path, damage):
    path = tmp_path / "general_settings.json"
    saved = {"body_source": "phone", "unknown_key": {"keep": True}}
    store = SettingsPersistence(path)
    store.save(saved)
    original = path.read_bytes()
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b'{partial' if damage == "broken" else b'[]')
    restored = SettingsPersistence(path)
    assert restored.document == saved and path.read_bytes() == original
    assert restored.status()["state"] == "ready" and "恢复" in restored.status()["message"]
    assert len(list(tmp_path.glob("*.invalid-*"))) == int(damage != "missing")


def test_invalid_settings_without_backup_wait_for_explicit_repair(tmp_path):
    path = tmp_path / "general_settings.json"
    path.write_bytes(b'{original-invalid')
    store = SettingsPersistence(path)
    store.save({"hand_mouse": {"sensitivity": 42}})
    assert path.read_bytes() == b'{original-invalid'
    assert store.status()["state"] == "blocked"
    assert store.retry(force=True)["state"] == "blocked"
    state = store.retry(force=True, repair_payload={"hand_mouse": {"sensitivity": 42}})
    assert state["state"] == "ready"
    assert next(tmp_path.glob("*.invalid-*")).read_bytes() == b'{original-invalid'
    assert SettingsPersistence(path).document["hand_mouse"]["sensitivity"] == 42


def test_repair_preserves_the_only_remaining_broken_backup(tmp_path):
    path = tmp_path / "general_settings.json"
    backup = path.with_name(path.name + ".last-good")
    backup.write_bytes(b'{only-remaining-copy')
    store = SettingsPersistence(path)
    assert store.status()["state"] == "blocked"
    assert store.retry(force=True, repair_payload={"value": "new"})["state"] == "ready"
    assert next(tmp_path.glob("*.invalid-*")).read_bytes() == b'{only-remaining-copy'


def test_failures_keep_latest_snapshot_and_retry_is_bounded(tmp_path, monkeypatch):
    clock = [100.]
    store = SettingsPersistence(tmp_path / "general_settings.json", clock=lambda: clock[0])
    original = config_transaction.atomic_bytes
    calls = []

    def fail(path, content):
        calls.append(path)
        raise PermissionError("模拟文件占用")

    with monkeypatch.context() as patch:
        patch.setattr(config_transaction, "atomic_bytes", fail)
        for value in range(20):
            store.save({"value": value})
        assert len(calls) == 1 and store.pending == {"value": 19}
        assert store.status()["state"] == "pending"
        for _ in range(10):
            clock[0] += 100
            store.retry()
        assert len(calls) == len(store.RETRY_DELAYS)
        assert store.status()["state"] == "error" and not store.status()["automatic_retry"]
    assert store.retry(force=True)["state"] == "ready"
    assert SettingsPersistence(store.path).document == {"value": 19}
    assert config_transaction.atomic_bytes is original


def test_failed_backup_write_rolls_back_main_and_can_retry(tmp_path, monkeypatch):
    store = SettingsPersistence(tmp_path / "general_settings.json")
    store.save({"value": "original"})
    before = {p: p.read_bytes() for p in (store.path, store.backup_path)}
    original = config_transaction.atomic_bytes

    def fail_backup(path, content):
        if path == store.backup_path and b'new' in content:
            raise OSError("模拟第二份文件写入失败")
        original(path, content)

    with monkeypatch.context() as patch:
        patch.setattr(config_transaction, "atomic_bytes", fail_backup)
        store.save({"value": "new"})
    assert {p: p.read_bytes() for p in before} == before
    assert store.pending == {"value": "new"}
    assert store.retry(force=True)["state"] == "ready"
    assert SettingsPersistence(store.path).document == {"value": "new"}


def test_write_receipt_belongs_only_to_request_thread(tmp_path):
    store = SettingsPersistence(tmp_path / "general_settings.json")
    store.begin_request()
    assert store.receipt() is None
    store.save({"value": 1})
    assert store.receipt()["state"] == "ready"
    other = []
    thread = threading.Thread(target=lambda: other.append(store.receipt()))
    thread.start(); thread.join()
    assert other == [None]
    store.begin_request()
    assert store.receipt() is None


def test_view_transaction_replaces_pending_snapshot_and_recovers_after_restart(isolated_user_data, monkeypatch):
    kernel = ControlKernel(Output())
    try:
        with monkeypatch.context() as patch:
            patch.setattr(config_transaction, "atomic_bytes", lambda *a: (_ for _ in ()).throw(OSError("disk unavailable")))
            kernel.configure_hand_mouse({"sensitivity": 42})
        assert kernel.settings_persistence_status()["state"] == "pending"
        configure_view_control(kernel, "left", "head")
        assert kernel.settings_persistence_status()["state"] == "ready"
        kernel.retry_settings_save(force=True)
        kernel._general_settings_path().write_bytes(b'{damaged')
        restored = ControlKernel(Output())
        try:
            assert restored.hand_mouse_controller.config["sensitivity"] == 42
            assert restored.hand_mouse_controller.config["horizontal_hand"] == "left"
            assert restored.vertical_look["enabled"]
        finally:
            restored.close()
    finally:
        kernel.close()


@pytest.fixture
def local_api(application):
    server = ThreadingHTTPServer(("127.0.0.1", 0), application.AdminHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json", "Origin": base})
        try:
            response = opener.open(req, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.code, json.load(response)

    yield application, request
    server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_real_http_reports_applied_but_unsaved_and_manual_retry_saves_latest(local_api, monkeypatch):
    app, request = local_api
    with monkeypatch.context() as patch:
        patch.setattr(config_transaction, "atomic_bytes", lambda *a: (_ for _ in ()).throw(PermissionError("locked")))
        status, data = request("/api/hand-mouse/config", {"sensitivity": 42})
    assert status == 503 and data["applied"] and not data["ok"]
    receipt = data["settings_persistence"]
    assert receipt["state"] == "pending"
    assert app.KERNEL.hand_mouse_controller.config["sensitivity"] == 42
    status, data = request("/api/settings/retry", {})
    assert status == 200 and data["ok"]
    assert data["settings_persistence"]["saved_revision"] >= receipt["revision"]
    assert json.loads(app.KERNEL._general_settings_path().read_text(encoding="utf-8"))["hand_mouse"]["sensitivity"] == 42
    status, data = request("/api/kernel/status")
    assert status == 200 and data["settings_persistence"]["state"] == "ready"


def test_exit_does_not_discard_pending_settings(application, monkeypatch):
    app, replies = application, []
    with monkeypatch.context() as patch:
        patch.setattr(config_transaction, "atomic_bytes", lambda *a: (_ for _ in ()).throw(OSError("locked")))
        app.KERNEL.remember_general_setting("body_source", "phone")
    request = request_for(app, "/api/shutdown", replies)
    request.headers = {}
    request.server = SimpleNamespace(server_port=18866, shutdown=lambda: pytest.fail("不能退出"))
    request._body = lambda: {}
    request.do_POST()
    assert replies[-1][0] == 409 and not app.SHUTTING_DOWN.is_set()


def test_profile_and_layout_transaction_rolls_back_when_general_save_is_pending(application, monkeypatch):
    app = application
    app.KERNEL.remember_general_setting("test_marker", "original")
    app.PROFILES.set_overrides({})
    files = [app.KERNEL._general_settings_path(), app.KERNEL._settings_store.backup_path, app.PROFILES.selection_path]
    before = {path: path.read_bytes() for path in files}
    original = config_transaction.atomic_bytes

    def fail(path, content):
        if path == app.KERNEL._general_settings_path() and b'changed' in content:
            raise OSError("模拟布局保存失败")
        original(path, content)

    with monkeypatch.context() as patch:
        patch.setattr(config_transaction, "atomic_bytes", fail)
        with pytest.raises(OSError):
            with app._profile_configuration_transaction(zone_layout=True):
                app.PROFILES.set_overrides({"zone.leftHand": {"action": {"type": "keyboard", "target": "K"}}})
                app.KERNEL.remember_general_setting("test_marker", "changed")
    assert {path: path.read_bytes() for path in files} == before
    assert app.KERNEL.general_setting("test_marker") == "original"
    assert app.KERNEL.settings_persistence_status()["state"] == "ready"
