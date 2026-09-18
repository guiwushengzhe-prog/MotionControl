import json
from unittest.mock import patch

import pytest

from motioncontrol.game_profiles import GameProfileStore, ProfileSelectionChanged
from test_game_profiles_v097 import make_store


def mapping(key):
    return {"zone.rightHandLower": {"action": {"type": "keyboard", "target": key}}}


def test_each_game_retains_edits_after_switch_restart_and_reset(tmp_path):
    store = make_store(tmp_path)
    store.set_overrides(mapping("Q"), "generic-xbox")
    store.select("demo")
    store.set_overrides(mapping("E"), "demo")
    store = GameProfileStore(tmp_path)
    assert store.effective_profile()["overrides"] == mapping("E")
    assert store.select("generic-xbox")["overrides"] == mapping("Q")
    store.set_overrides({}, "generic-xbox")
    assert store.select("demo")["overrides"] == mapping("E")
    assert store.select("generic-xbox")["overrides"] == {}


def test_legacy_migration_keeps_exact_backup_and_aliases(tmp_path):
    store = make_store(tmp_path)
    original = json.dumps({"schema": "motioncontrol.profile_selection.v1", "selected_id": "demo",
                           "overrides": mapping("Z")}).encode()
    store.selection_path.write_bytes(original)
    migrated = GameProfileStore(tmp_path)
    assert migrated.effective_profile()["overrides"] == mapping("Z")
    assert migrated.selection_path.with_name(migrated.selection_path.name + ".v1.bak").read_bytes() == original
    migrated.select("generic-xbox")
    assert GameProfileStore(tmp_path).select("demo")["overrides"] == mapping("Z")


def test_stale_editor_is_rejected_without_changing_any_game(tmp_path):
    store = make_store(tmp_path)
    store.set_overrides(mapping("Q"))  # Existing clients remain compatible.
    store.select("demo")
    before = store.selection_path.read_bytes()
    with pytest.raises(ProfileSelectionChanged):
        store.set_overrides(mapping("E"), "generic-xbox")
    assert store.selection_path.read_bytes() == before
    assert store.effective_profile()["overrides"] == {}


def test_failed_save_preserves_memory_and_disk(tmp_path):
    store = make_store(tmp_path)
    store.set_overrides(mapping("Q"))
    before = store.selection_path.read_bytes()
    with patch("motioncontrol.game_profiles.os.replace", side_effect=PermissionError("文件被占用")):
        with pytest.raises(PermissionError):
            store.set_overrides(mapping("E"))
        with pytest.raises(PermissionError):
            store.select("demo")
    assert store.selection_path.read_bytes() == before
    assert store.effective_profile()["selected_id"] == "generic-xbox"
    assert store.effective_profile()["overrides"] == mapping("Q")


def test_unavailable_game_does_not_erase_its_saved_mapping(tmp_path):
    store = make_store(tmp_path)
    store.select("demo")
    store.set_overrides(mapping("R"))
    path = store.profile_dir / "demo.json"
    original = path.read_bytes()
    path.unlink()
    assert store.effective_profile()["selected_id"] == "generic-xbox"
    path.write_bytes(original)
    assert store.select("demo")["overrides"] == mapping("R")


def test_invalid_legacy_mapping_is_not_rewritten(tmp_path):
    store = make_store(tmp_path)
    original = b'{"schema":"motioncontrol.profile_selection.v1","overrides":[]}'
    store.selection_path.write_bytes(original)
    with pytest.raises(ValueError, match="原文件已保留"):
        GameProfileStore(tmp_path)
    assert store.selection_path.read_bytes() == original
