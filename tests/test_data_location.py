"""真实移动目录、陈旧路径和身份冲突；禁止接触当前用户的位置记录。"""
import shutil

import pytest

from motioncontrol import data_location, user_paths
from motioncontrol.game_launch import elevated_command


@pytest.fixture
def locations(monkeypatch):
    saved = {}
    monkeypatch.setattr(data_location, "_write_location", lambda value: saved.update(value))
    monkeypatch.setattr(data_location, "_read_location", lambda: dict(saved))
    return saved


def test_windows_folder_migration_finds_same_data_even_with_stale_environment(tmp_path, locations, monkeypatch):
    old = tmp_path / "原 LocalAppData" / "MotionControl"
    new_local = tmp_path / "迁移后 LocalAppData"
    old.mkdir(parents=True)
    payload = b'{"selected_id":"personal-game","original":true}'
    (old / "game_profile_selection.json").write_bytes(payload)
    data_location.remember_user_data(old)
    new_local.mkdir()
    shutil.move(str(old), str(new_local / "MotionControl"))
    monkeypatch.setenv("LOCALAPPDATA", str(old.parent))
    monkeypatch.setattr(data_location, "_known_local_appdata", lambda: new_local)
    located = data_location.locate_user_data()
    assert located == (new_local / "MotionControl").resolve()
    assert (located / "game_profile_selection.json").read_bytes() == payload
    assert not old.exists()


def test_missing_original_refuses_unrelated_default_configuration(tmp_path, locations):
    original = tmp_path / "original"
    data_location.remember_user_data(original)
    shutil.rmtree(original)
    unrelated = tmp_path / "another"
    unrelated.mkdir()
    (unrelated / "game_profile_selection.json").write_text('{}')
    with pytest.raises(FileNotFoundError, match="没有载入默认配置"):
        data_location.find_user_data([unrelated], locations)
    assert not original.exists()


def test_manual_relocation_is_remembered_once_and_all_configs_follow(tmp_path, locations, monkeypatch):
    moved = tmp_path / "D 盘新位置" / "MotionControl"
    moved.mkdir(parents=True)
    (moved / "general_settings.json").write_text('{}')
    (moved / "depth_comparison_runtime.json").write_text('{"models":[]}')
    data_location.remember_user_data(moved, require_existing=True)
    monkeypatch.delenv("MOTIONCONTROL_USER_DIR")
    monkeypatch.setattr(user_paths, "_located_root", None)
    assert user_paths.user_data_root() == moved.resolve()
    for key in user_paths.USER_DATA_FILES:
        assert user_paths.user_path(key).parent == moved.resolve()
    # 工作器的登记文件也从同一个根目录取，不再保留旧盘符。
    assert (user_paths.user_data_root() / "depth_comparison_runtime.json").is_file()


def test_empty_wrong_folder_cannot_be_registered_as_old_data(tmp_path, locations):
    folder = tmp_path / "empty"
    folder.mkdir()
    with pytest.raises(ValueError, match="没有 MotionControl"):
        data_location.remember_user_data(folder, require_existing=True)
    assert not locations and not (folder / data_location.MARKER).exists()


def test_bad_identity_record_is_preserved_instead_of_replaced(tmp_path, locations):
    marker = tmp_path / data_location.MARKER
    marker.write_bytes(b'{"partial')
    with pytest.raises(ValueError, match="身份记录损坏"):
        data_location.remember_user_data(tmp_path)
    assert marker.read_bytes() == b'{"partial' and not locations


def test_explicit_directory_wins_without_changing_saved_location(tmp_path, locations, monkeypatch):
    saved = data_location.remember_user_data(tmp_path / "saved")
    monkeypatch.setenv("MOTIONCONTROL_USER_DIR", str(tmp_path / "explicit"))
    assert user_paths.user_data_root() == tmp_path / "explicit"
    assert locations["path"] == str(saved)


def test_administrator_handoff_includes_same_moved_directory(tmp_path, monkeypatch):
    root = tmp_path / "移动的数据目录"
    monkeypatch.setenv("MOTIONCONTROL_USER_DIR", str(root))
    _, arguments, _ = elevated_command(tmp_path)
    assert "--user-data-dir" in arguments and str(root.resolve()) in arguments


def test_camera_backend_cache_follows_the_same_directory(isolated_user_data):
    from motioncontrol.control_kernel import NativeCameraService
    camera = object.__new__(NativeCameraService)
    assert camera._backend_cache_path() == isolated_user_data / "camera_backend.json"
