"""User data lives outside the program folder, and gets there exactly once.

Two failures this prevents:

* Upgrading by unzipping a new folder used to leave every mapping, motion,
  voice binding and scene behind in the old one.
* 请先看.txt told people the folder could be copied to another computer. After
  a scene had been captured that was actively harmful -- scene_layout.json is a
  homography onto one frame from one camera in one position, and
  scene_reference.jpg is the photo it refers to, so on another machine the six
  zones land somewhere silently wrong rather than failing.

The migration copies, never moves and never overwrites. That is what makes it
safe to run on every start, and it leaves an older build able to find its own
configuration if the user rolls back.
"""

from __future__ import annotations

import json

import pytest

from motioncontrol_shared.user_layout import (
    LEGACY_PROGRAM_FILES,
    PROGRAM_CONFIG_FILES,
    USER_DATA_FILES,
    migration_plan,
    user_data_path,
)
import user_paths


@pytest.fixture
def program(tmp_path):
    root = tmp_path / "MotionControl-PC-2.0" / "app"
    (root / "config").mkdir(parents=True)
    return root


def write_legacy(program, name, payload):
    path = program / "config" / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# --- layout is a pure function -------------------------------------------


def test_user_data_path_is_pure():
    assert user_data_path("/base", "voice_mappings") == __import__("pathlib").Path("/base/voice_mappings.json")


def test_unknown_key_is_rejected():
    with pytest.raises(KeyError, match="unknown user data file"):
        user_data_path("/base", "not_a_thing")


def test_migration_plan_touches_no_disk(tmp_path):
    plan = migration_plan(tmp_path / "user", tmp_path / "program")
    assert plan, "there is something to migrate"
    assert all(len(entry) == 3 for entry in plan)
    # Nothing was created just by asking what the plan is.
    assert not (tmp_path / "user").exists()


def test_every_config_file_is_classified():
    """A new file under config/ must be deliberately sorted, not defaulted.

    Whitelisting what syncs is only half of it: something has to notice when a
    new config file appears at all, or it silently inherits whichever default
    the code happens to apply.
    """
    import pathlib

    config_dir = pathlib.Path(__file__).resolve().parent.parent / "config"
    user_names = set(USER_DATA_FILES.values())
    unclassified = []
    for path in config_dir.iterdir():
        if path.is_dir() or path.name.endswith((".example", ".bak")) or ".example." in path.name:
            continue
        if path.name in PROGRAM_CONFIG_FILES or path.name in user_names:
            continue
        unclassified.append(path.name)
    assert not unclassified, (
        "classify these in motioncontrol_shared/user_layout.py as either user "
        f"data or program config: {unclassified}"
    )


# --- migration behaviour --------------------------------------------------


def test_legacy_files_are_copied_into_the_user_directory(program, isolated_user_data):
    write_legacy(program, "voice_mappings.json", {"mappings": [{"phrase": "地图"}]})
    write_legacy(program, "motion_mappings.json", [{"id": "march"}])

    migrated = user_paths.migrate_legacy_user_data(program)

    assert set(migrated) == {"voice_mappings", "motion_mappings"}
    assert json.loads(user_paths.user_path("voice_mappings").read_text(encoding="utf-8"))["mappings"]
    assert user_paths.user_path("motion_mappings").is_file()


def test_originals_are_left_in_place(program, isolated_user_data):
    """A rollback to an older build must still find its configuration."""
    source = write_legacy(program, "voice_mappings.json", {"mappings": []})
    user_paths.migrate_legacy_user_data(program)
    assert source.is_file()


def test_existing_user_data_is_never_overwritten(program, isolated_user_data):
    write_legacy(program, "voice_mappings.json", {"mappings": [{"phrase": "旧"}]})
    destination = user_paths.user_path("voice_mappings")
    destination.write_text(json.dumps({"mappings": [{"phrase": "新"}]}, ensure_ascii=False),
                           encoding="utf-8")

    migrated = user_paths.migrate_legacy_user_data(program)

    assert "voice_mappings" not in migrated
    assert json.loads(destination.read_text(encoding="utf-8"))["mappings"][0]["phrase"] == "新"


def test_migration_is_idempotent(program, isolated_user_data):
    write_legacy(program, "voice_mappings.json", {"mappings": []})
    first = user_paths.migrate_legacy_user_data(program)
    second = user_paths.migrate_legacy_user_data(program)
    assert first == ["voice_mappings"]
    assert second == [], "running on every start must not keep re-copying"


def test_missing_legacy_files_are_not_an_error(program, isolated_user_data):
    assert user_paths.migrate_legacy_user_data(program) == []


def test_scene_data_migrates_but_is_not_syncable(program, isolated_user_data):
    """It moves out of the program folder, and stays out of the cloud bundle."""
    write_legacy(program, "scene_layout.json", {"version": 1})
    user_paths.migrate_legacy_user_data(program)
    assert user_paths.user_path("scene_layout").is_file()

    from motioncontrol_shared.sync_allowlist import CLOUD_SYNC_ALLOWLIST

    assert "scene_layout" not in CLOUD_SYNC_ALLOWLIST
    assert "scene_reference" not in CLOUD_SYNC_ALLOWLIST


# --- isolation ------------------------------------------------------------


def test_override_env_redirects_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("MOTIONCONTROL_USER_DIR", str(tmp_path / "elsewhere"))
    assert user_paths.user_data_root() == tmp_path / "elsewhere"
    assert user_paths.user_path("head_profile").parent == tmp_path / "elsewhere"


def test_no_user_file_resolves_into_the_program_folder(isolated_user_data):
    for key in USER_DATA_FILES:
        assert "config" not in user_paths.user_path(key).parts, key


def test_every_user_file_is_classified_for_sync():
    """The allowlist and the layout must not drift apart.

    A file added to the layout but left out of CLASSIFICATION would get no
    deliberate decision about whether it travels; this makes that a failure
    rather than an omission.
    """
    from motioncontrol_shared.sync_allowlist import CLASSIFICATION, CLOUD_SYNC_ALLOWLIST

    assert set(CLASSIFICATION) == set(USER_DATA_FILES), (
        "sync_allowlist.CLASSIFICATION and user_layout.USER_DATA_FILES disagree: "
        f"{set(CLASSIFICATION) ^ set(USER_DATA_FILES)}"
    )
    assert CLOUD_SYNC_ALLOWLIST <= set(USER_DATA_FILES)


def test_only_user_configuration_may_sync():
    from motioncontrol_shared.sync_allowlist import (
        CLASSIFICATION, CLOUD_SYNC_ALLOWLIST, USER_CONFIGURATION, classify, may_sync,
    )

    for key, category in CLASSIFICATION.items():
        if category != USER_CONFIGURATION:
            assert not may_sync(key), f"{key} is {category} and must never sync"
    assert all(classify(key) == USER_CONFIGURATION for key in CLOUD_SYNC_ALLOWLIST)


def test_unclassified_key_raises_with_guidance():
    from motioncontrol_shared.sync_allowlist import classify

    with pytest.raises(KeyError, match="not classified"):
        classify("something_new")
