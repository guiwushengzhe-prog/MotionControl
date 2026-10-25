"""配置丢失、半写入与外部更新的恢复；只使用隔离用户目录。"""
import copy
import json
from pathlib import Path

import pytest

from motioncontrol import config_transaction
from motioncontrol.game_profiles import GameProfileStore, ProfileSelectionChanged
from motioncontrol_shared.profile_schema import flatten_bindings
from test_configuration_coordination import application
from test_profile_api_v2 import request_for

ROOT = Path(__file__).resolve().parents[1]
BINDING = {"zone.leftHand": {"action": {"type": "keyboard", "target": "K", "behavior": "hold"}}}


@pytest.mark.parametrize("damage", ["missing", "broken"])
def test_custom_identity_and_bindings_survive_missing_or_broken_mirror(isolated_user_data, damage):
    store = GameProfileStore(ROOT)
    game = store.add_custom_game("个人游戏")
    store.select(game["id"])
    store.set_overrides(BINDING)
    if damage == "missing":
        store.custom_games_path.unlink()
    else:
        store.custom_games_path.write_text("{半写入", encoding="utf-8")
    restored = GameProfileStore(ROOT)
    assert restored.list_games("个人")["games"][0]["id"] == game["id"]
    assert restored.effective_profile()["overrides"] == BINDING


@pytest.mark.parametrize("damage", ["missing", "broken"])
def test_last_good_copy_restores_selection_including_custom_games(isolated_user_data, damage):
    store = GameProfileStore(ROOT)
    game = store.add_custom_game("原来的游戏")
    store.select(game["id"])
    store.set_overrides(BINDING)
    expected = json.loads(store.selection_path.read_text(encoding="utf-8"))
    if damage == "missing":
        store.selection_path.unlink()
    else:
        store.selection_path.write_bytes(b'{"partial')
    restored = GameProfileStore(ROOT)
    assert json.loads(restored.selection_path.read_text(encoding="utf-8")) == expected
    assert restored.effective_profile()["overrides"] == BINDING
    assert restored.persistence_status()["recovery_count"] == 1
    if damage == "broken":
        preserved = list(isolated_user_data.glob("game_profile_selection.json.invalid-*"))
        assert len(preserved) == 1 and preserved[0].read_bytes() == b'{"partial'


def test_stale_writer_cannot_overwrite_external_changes(isolated_user_data):
    first = GameProfileStore(ROOT)
    game = first.add_custom_game("两个窗口")
    stale = GameProfileStore(ROOT)
    first.select(game["id"])
    first.set_overrides(BINDING)
    saved = first.selection_path.read_bytes()
    with pytest.raises(ProfileSelectionChanged, match="阻止旧状态覆盖"):
        stale.select("generic-xbox")
    assert first.selection_path.read_bytes() == saved
    assert stale.refresh_from_disk()
    assert stale.effective_profile()["overrides"] == BINDING


def test_failed_custom_save_rolls_back_catalog_selection_backup_and_memory(isolated_user_data, monkeypatch):
    store = GameProfileStore(ROOT)
    game = store.add_custom_game("不会丢的")
    paths = (store.selection_path, store.custom_games_path, store.backup_path)
    before = {p: p.read_bytes() for p in paths}
    memory = copy.deepcopy((store._selection, store._custom))
    original = config_transaction.atomic_bytes

    def fail(path, raw):
        if path == store.custom_games_path and json.loads(raw)["games"][0]["name"] == "改名":
            raise OSError("模拟第二个文件写入失败")
        original(path, raw)

    monkeypatch.setattr(config_transaction, "atomic_bytes", fail)
    with pytest.raises(OSError):
        store.rename_custom_game(game["id"], "改名")
    assert {p: p.read_bytes() for p in paths} == before
    assert (store._selection, store._custom) == memory
    assert not store.journal_path.exists()
    assert GameProfileStore(ROOT).list_games("不会丢的")["count"] == 1


def test_repair_reloads_both_consumers_and_pauses_output(application):
    app = application
    revision = app.CONFIG_REVISION
    other = GameProfileStore(ROOT)
    game = other.add_custom_game("恢复出来的")
    other.select(game["id"])
    other.set_overrides(BINDING)
    app.OUTPUT.enabled = True
    app._reconcile_profile_configuration(force=True)
    profile = app.PROFILES.effective_profile()
    assert not app.PROFILES.last_error, app.PROFILES.last_error
    assert profile["selected_id"] == game["id"]
    assert profile["overrides"] == BINDING
    assert app.KERNEL.control_bindings == flatten_bindings(profile["bindings"])
    assert app.VOICE._profile_bindings == profile["bindings"]
    assert not app.OUTPUT.enabled
    assert app.CONFIG_REVISION == revision + 1


def test_drifted_consumers_repair_without_file_changes(application):
    app = application
    app.KERNEL.control_bindings = {}
    app.VOICE._profile_bindings = {}
    app.OUTPUT.enabled = True
    app._reconcile_profile_configuration(force=True)
    expected = app.PROFILES.effective_profile()["bindings"]
    assert app.KERNEL.control_bindings == flatten_bindings(expected)
    assert app.VOICE._profile_bindings == expected
    assert not app.OUTPUT.enabled
    assert "统一恢复" in app.PROFILES.recovery_message


def test_old_editor_revision_cannot_erase_latest_bindings(application):
    app, replies = application, []
    revision = app.PROFILES.revision()
    other = GameProfileStore(ROOT)
    other.set_overrides(BINDING)
    request = request_for(app, "/api/game-profiles/overrides", replies)
    request._body = lambda: {"expected_revision": revision, "overrides": {}}
    request.do_POST()
    assert replies[-1][0] == 409
    assert app.PROFILES.effective_profile()["overrides"] == BINDING
    assert app.PROFILES.effective_profile()["configuration_revision"] == app.PROFILES.revision()


@pytest.mark.parametrize("loopback", [True, False])
def test_health_reports_real_instance_and_configuration_only_locally(application, loopback):
    app, replies = application, []
    request = request_for(app, "/api/health", replies)
    request._is_loopback = lambda: loopback
    request.do_GET()
    status, data = replies[-1]
    assert status == (200 if loopback else 403)
    if loopback:
        assert Path(data["program_root"]) == ROOT.resolve()
        assert Path(data["user_data_root"]) == app.PROFILES.selection_path.parent.resolve()
        assert data["configuration"]["state"] == "ready"


def test_custom_selection_directory_keeps_mirror_and_backups_together(tmp_path):
    store = GameProfileStore(ROOT, tmp_path / "moved" / "selection.json")
    game = store.add_custom_game("移动过的")
    store.select(game["id"])
    assert store.custom_games_path.parent == store.selection_path.parent == store.backup_path.parent
    assert GameProfileStore(ROOT, store.selection_path).effective_profile()["selected_id"] == game["id"]


def test_valid_json_with_invalid_binding_cannot_replace_known_good_copy(isolated_user_data):
    store = GameProfileStore(ROOT)
    store.set_overrides(BINDING)
    saved = store.backup_path.read_bytes()
    broken = json.loads(saved)
    broken["overrides_by_profile"]["generic-xbox"]["zone.leftHand"]["action"]["type"] = "unknown"
    store.selection_path.write_text(json.dumps(broken), encoding="utf-8")
    restored = GameProfileStore(ROOT)
    assert restored.effective_profile()["overrides"] == BINDING
    assert restored.backup_path.read_bytes() == saved


def test_repeated_locked_recovery_does_not_create_unlimited_damage_copies(isolated_user_data, monkeypatch):
    import motioncontrol.game_profiles as module
    store = GameProfileStore(ROOT)
    store.set_overrides(BINDING)
    store.selection_path.write_bytes(b'{"broken')
    original = module.atomic_bytes

    def locked(path, raw):
        if path == store.selection_path:
            raise PermissionError("文件暂时被占用")
        original(path, raw)

    monkeypatch.setattr(module, "atomic_bytes", locked)
    for _ in range(3):
        assert not store.refresh_from_disk()
    assert len(list(isolated_user_data.glob("*.invalid-*"))) == 1
    assert store.effective_profile()["overrides"] == BINDING
    assert store.last_error
    monkeypatch.setattr(module, "atomic_bytes", original)
    store.refresh_from_disk()
    assert not store.last_error and store.selection_path.read_bytes() == store.backup_path.read_bytes()


def test_missing_library_keeps_requested_selection_and_custom_bindings(isolated_user_data, monkeypatch):
    store = GameProfileStore(ROOT)
    original_get = store.get_profile
    game = next(g["id"] for g in store.list_games()["games"] if g["id"] != "generic-xbox")
    store.select(game)
    store.set_overrides(BINDING)
    saved = store.selection_path.read_bytes()

    def unavailable(ident):
        if ident == game:
            raise OSError("游戏库暂时不可读")
        return original_get(ident)

    monkeypatch.setattr(store, "get_profile", unavailable)
    assert store.effective_profile()["unavailable"]
    assert store.selection_path.read_bytes() == saved
    monkeypatch.setattr(store, "get_profile", original_get)
    assert store.effective_profile()["selected_id"] == game
    assert store.effective_profile()["overrides"] == BINDING
