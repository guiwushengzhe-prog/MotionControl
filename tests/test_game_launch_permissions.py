"""Per-game administrator preference and the restart handoff."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from motioncontrol.game_launch import elevated_command
from motioncontrol.game_profiles import GameProfileStore
from motioncontrol_shared.canonical import canonicalize
from test_configuration_coordination import application


ROOT = Path(__file__).resolve().parents[1]


def test_only_the_chosen_game_remembers_administrator(isolated_user_data):
    path = isolated_user_data / "game_profile_selection.json"
    store = GameProfileStore(ROOT, selection_path=path)
    other = next(game["id"] for game in store.list_games()["games"] if game["id"] != "generic-xbox")
    store.set_admin("generic-xbox", True)
    assert store.requires_admin("generic-xbox")
    assert not store.requires_admin(other)
    assert GameProfileStore(ROOT, selection_path=path).requires_admin("generic-xbox")
    store.set_admin("generic-xbox", False)
    assert not GameProfileStore(ROOT, selection_path=path).requires_admin("generic-xbox")
    assert json.loads(path.read_text(encoding="utf-8"))["launch_mode_by_profile"]["generic-xbox"] == "normal"


def test_relaunch_uses_same_installation_and_one_wait_argument():
    executable, parameters, directory = elevated_command(
        ROOT, ["--no-browser", "--wait-for-pid", "12"], wait_for_pid=34, no_browser=True)
    assert executable
    assert directory == str(ROOT)
    assert str(ROOT / "server.py") in parameters
    assert parameters.count("--wait-for-pid") == 1
    assert "34" in parameters and "12" not in parameters


def test_admin_restart_persists_then_stops_old_server(isolated_user_data, monkeypatch):
    import server

    store = GameProfileStore(ROOT, selection_path=isolated_user_data / "game_profile_selection.json")
    monkeypatch.setattr(server, "PROFILES", store)
    monkeypatch.setattr(server, "is_administrator", lambda: False)
    requested = []
    monkeypatch.setattr(server, "launch_as_administrator", lambda *args, **kwargs: requested.append((args, kwargs)))
    http = ThreadingHTTPServer(("127.0.0.1", 0), server.AdminHandler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{http.server_address[1]}/api/game-profiles/launch-mode",
            data=json.dumps({"admin": True}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.load(response)
        assert data["restarting"] is True
        assert data["launch"]["requires_admin"] is True
        assert requested and requested[0][1]["wait_for_pid"] > 0
        assert store.requires_admin(data["launch"]["game_id"])
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=3)


def test_declined_elevation_keeps_old_preference_and_server(isolated_user_data, monkeypatch):
    import server

    store = GameProfileStore(ROOT, selection_path=isolated_user_data / "game_profile_selection.json")
    monkeypatch.setattr(server, "PROFILES", store)
    monkeypatch.setattr(server, "is_administrator", lambda: False)
    monkeypatch.setattr(server, "launch_as_administrator", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("cancelled")))
    http = ThreadingHTTPServer(("127.0.0.1", 0), server.AdminHandler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{http.server_address[1]}"
        request = urllib.request.Request(
            url + "/api/game-profiles/launch-mode", data=b'{"admin":true}',
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(request, timeout=10)
            assert False, "cancelled elevation should fail"
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
        assert not store.requires_admin(store.effective_profile()["selected_id"])
        with urllib.request.urlopen(url + "/api/game-profiles/selected", timeout=10) as response:
            assert json.load(response)["launch"]["is_admin"] is False
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=3)


def test_cloud_documents_carry_the_games_launch_mode(application, isolated_user_data, monkeypatch):
    server = application
    from threading import RLock

    profile_id = "generic-xbox"
    selection = canonicalize("profile_selection", {
        "schema": "motioncontrol.profile_selection.v2", "selected_id": profile_id,
        "overrides_by_profile": {}, "launch_mode_by_profile": {profile_id: "admin"},
    }).data
    assert selection["launch_mode_by_profile"][profile_id] == "admin"
    bundle = canonicalize("game_bundle", {
        "schema": "motioncontrol.game_bundle.v1", "game_id": profile_id,
        "overrides": {}, "motions": [], "launch_mode": "admin",
    }).data
    assert bundle["launch_mode"] == "admin"

    store = GameProfileStore(ROOT, selection_path=isolated_user_data / "game_profile_selection.json")
    monkeypatch.setattr(server, "PROFILES", store)
    server._install_profile_selection(selection, profile_id)
    assert store.requires_admin(profile_id)


def test_shared_custom_game_keeps_identity_and_admin_on_another_pc(application, isolated_user_data, monkeypatch):
    server = application
    from threading import RLock

    first = GameProfileStore(ROOT, selection_path=isolated_user_data / "first.json")
    game = first.add_custom_game("和平精英")
    first.select(game["id"])
    first.set_admin(game["id"], True)
    uploaded = canonicalize("profile_selection", json.loads(
        first.selection_path.read_text(encoding="utf-8"))).data
    assert uploaded["custom_games"][0]["id"] == game["id"]

    second_dir = isolated_user_data / "second"
    second_dir.mkdir()
    monkeypatch.setenv("MOTIONCONTROL_USER_DIR", str(second_dir))
    second = GameProfileStore(ROOT)
    monkeypatch.setattr(server, "PROFILES", second)
    monkeypatch.setattr(server, "MOTION_CONFIG_FILE", second_dir / "motion_mappings.json")
    server._install_profile_selection(uploaded, game["id"])
    assert second.effective_profile()["name"] == "和平精英"
    assert second.requires_admin(game["id"])

    bundle = canonicalize("game_bundle", {
        "schema": "motioncontrol.game_bundle.v1", "game_id": game["id"],
        "custom_game": game, "overrides": {}, "motions": [], "launch_mode": "admin",
    }).data
    third_dir = isolated_user_data / "third"
    third_dir.mkdir()
    monkeypatch.setenv("MOTIONCONTROL_USER_DIR", str(third_dir))
    third = GameProfileStore(ROOT)
    monkeypatch.setattr(server, "PROFILES", third)
    monkeypatch.setattr(server, "MOTION_CONFIG", [])
    monkeypatch.setattr(server, "MOTION_CONFIG_FILE", third_dir / "motion_mappings.json")
    monkeypatch.setattr(server, "save_motion_config", lambda value: value)
    server._install_game_bundle(bundle)
    assert third.effective_profile()["name"] == "和平精英"
    assert third.requires_admin(game["id"])
