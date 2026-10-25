"""完整配置恢复的真实边界：自建身份、跨机隔离、中断回退和启动先后。"""
import io
import base64
import json
from pathlib import Path
import zipfile
import threading

import pytest

from motioncontrol import configuration_backup as backup, config_transaction
from motioncontrol.custom_poses import CustomPoseStore
from motioncontrol.game_profiles import GameProfileStore
from motioncontrol.head_control import HeadController
from motioncontrol.key_macros import MacroStore
from motioncontrol.startup_identity import startup_snapshot
from test_custom_poses import T_POSE
from test_configuration_coordination import application
from test_settings_persistence import local_api

ROOT = Path(__file__).resolve().parents[1]


def write(root, name, document):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def original(tmp_path):
    root = tmp_path / "原配置"
    root.mkdir()
    write(root, ".motioncontrol-data.json", {"schema": 1, "id": "original-directory"})
    store = GameProfileStore(ROOT, selection_path=root / "game_profile_selection.json")
    game = store.add_custom_game("和平精英")
    store.select(game["id"])
    store.set_overrides({"zone.leftHand": {"action": {"type": "keyboard", "target": "K"}}})
    store.add_mode("载具")
    store.set_admin(game["id"], True)
    MacroStore(root / "key_macros.json").create("连击")
    CustomPoseStore(root / "custom_poses.json").capture(T_POSE, "双手平举")
    head = HeadController(root / "head_profile.json")
    head.configure(sensitivity_x=73)
    write(root, "general_settings.json", {"output_mode": "gamepad", "body_lean_neutral": {"source": "旧机位"}, "camera_index": 5})
    write(root, "body_angle_learning.json", {"version": 1, "context": "original", "samples": []})
    write(root, "monocular_lean_learning.json", {"version": 1, "profiles": {}})
    write(root, "depth_comparison_runtime.json", {"python": "original-python", "models": {}})
    write(root, "studio.json", {"mirror": False})
    write(root, "fitness.json", {"schema": 1, "sessions": [], "cloud": {"cursor": "private-cursor"}})
    write(root, "intent_recordings/index.json", {"schema": "motioncontrol.intent_index.v1", "items": {
        "leftHand": {"file": "intent-example.jsonl", "step": 0}}})
    (root / "intent_recordings/intent-example.jsonl").write_text(
        '{"schema":"motioncontrol.intent_recording.v1"}\n{"t":0,"pose":{}}\n', encoding="utf-8")
    return root


def stage(root, package, mode="full"):
    preview = backup.preview_configuration(root, package)
    return backup.stage_configuration(root, preview["token"], mode)


def test_backup_and_restore_preserve_every_game_identity_binding_mode_and_library(original, tmp_path):
    package = backup.export_configuration(original, version="检查版")
    manifest, files = backup.read_package(package)
    target = tmp_path / "恢复目录"
    target.mkdir()
    write(target, ".motioncontrol-data.json", {"schema": 1, "id": "target-directory"})
    (target / "pairing_key.txt").write_text("target-key")
    write(target, "cloud_account.json", {"token": "target-secret"})
    write(target, "custom_poses.json", {"schema": "motioncontrol.custom_poses.v1", "poses": []})
    before = (target / "custom_poses.json").read_bytes()
    staged = stage(target, package)
    assert Path(staged["rollback"]).is_file()
    assert (target / "custom_poses.json").read_bytes() == before
    assert backup.apply_pending_configuration(target)
    assert not backup.apply_pending_configuration(target)
    restored = GameProfileStore(ROOT, selection_path=target / "game_profile_selection.json")
    expected = GameProfileStore(ROOT, selection_path=original / "game_profile_selection.json")
    assert restored.configuration_document() == expected.configuration_document()
    assert restored.list_games("和平")["games"][0]["name"] == "和平精英"
    assert restored.modes() == expected.modes()
    assert len(MacroStore(target / "key_macros.json").macros) == 1
    assert len(CustomPoseStore(target / "custom_poses.json").poses) == 1
    assert HeadController(target / "head_profile.json").config["sensitivity_x"] == 73
    assert (target / "intent_recordings/intent-example.jsonl").read_bytes() == files["intent_recordings/intent-example.jsonl"]
    assert json.loads((target / ".motioncontrol-data.json").read_bytes())["id"] == "target-directory"
    assert (target / "pairing_key.txt").read_text() == "target-key"
    assert json.loads((target / "cloud_account.json").read_bytes())["token"] == "target-secret"
    for name in backup.WITH_BACKUP:
        if (target / name).exists():
            assert (target / (name + ".last-good")).read_bytes() == (target / name).read_bytes()
    assert manifest["version"] == "检查版"


def test_export_excludes_credentials_recordings_models_and_source_data_identity_file(original):
    for name in ("cloud_account.json", "paired_devices.json", "pairing_key.txt", "camera_backend.json", "discovery_instance.txt"):
        (original / name).write_text("秘密")
    (original / "recordings").mkdir()
    (original / "recordings/private.mp4").write_bytes(b"private video")
    (original / "weights.pth").write_bytes(b"private weights")
    _, files = backup.read_package(backup.export_configuration(original))
    assert "depth_comparison_runtime.json" in files and "monocular_lean_learning.json" in files
    assert not set(files) & {"cloud_account.json", "pairing_key.txt", ".motioncontrol-data.json", "camera_backend.json"}
    assert not any(name.startswith("recordings/") or name.endswith(".pth") for name in files)


def test_portable_restore_keeps_target_camera_calibration_and_model_registration(original, tmp_path):
    target = tmp_path / "另一台电脑"
    target.mkdir()
    write(target, "general_settings.json", {"camera_index": 9, "body_lean_neutral": {"target": True}, "output_mode": "mouse"})
    head = json.loads((original / "head_profile.json").read_bytes())
    head["center"] = {"keep_target": True}
    write(target, "head_profile.json", head)
    write(target, "depth_comparison_runtime.json", {"python": "target-python", "models": {}})
    write(target, "monocular_lean_learning.json", {"version": 1, "profiles": {"target": {}}})
    stage(target, backup.export_configuration(original), "portable")
    backup.apply_pending_configuration(target)
    general = json.loads((target / "general_settings.json").read_bytes())
    assert general["camera_index"] == 9 and general["body_lean_neutral"] == {"target": True}
    assert general["output_mode"] == "gamepad"
    assert json.loads((target / "head_profile.json").read_bytes())["center"] == {"keep_target": True}
    assert json.loads((target / "depth_comparison_runtime.json").read_bytes())["python"] == "target-python"
    assert json.loads((target / "monocular_lean_learning.json").read_bytes())["profiles"] == {"target": {}}
    assert json.loads((target / "fitness.json").read_bytes())["cloud"]["cursor"] == ""


@pytest.mark.parametrize("bad_name", ["../outside.json", "intent_recordings/../outside.json", "intent_recordings\\intent-x.jsonl", "cloud_account.json", "C:/outside.json"])
def test_unknown_and_outside_paths_are_rejected_before_staging(original, bad_name):
    raw = backup.export_configuration(original)
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as src, zipfile.ZipFile(out, "w") as dest:
        for item in src.infolist():
            dest.writestr(item.filename, src.read(item))
        dest.writestr(bad_name, b"{}")
    with pytest.raises(ValueError):
        backup.preview_configuration(original, out.getvalue())
    assert not (original / backup.PREVIEW).exists()


def test_digest_damage_and_missing_learning_data_are_rejected(original):
    raw = backup.export_configuration(original)
    manifest, files = backup.read_package(raw)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as dest:
        dest.writestr(backup.MANIFEST, json.dumps(manifest))
        for name, value in files.items():
            dest.writestr(name, b"{}" if name == "general_settings.json" else value)
    with pytest.raises(ValueError, match="不完整"):
        backup.read_package(out.getvalue())
    files.pop("intent_recordings/intent-example.jsonl")
    raw = backup._pack(files, source_id="", version="")
    with pytest.raises(ValueError, match="缺少索引"):
        backup.read_package(raw)


def test_full_restore_write_failure_rolls_back_files_backups_and_pending_marker(original, monkeypatch):
    raw = backup.export_configuration(original)
    write(original, "general_settings.json", {"newer": "keep on failure"})
    stage(original, raw)
    tracked = [path for path in original.rglob("*") if path.is_file() and path.parent.name != "backups"]
    before = {path: path.read_bytes() for path in tracked}
    original_write = config_transaction.atomic_bytes
    failed = False

    def fail_later(path, content):
        nonlocal failed
        if path.name == "head_profile.json.last-good" and not failed:
            failed = True
            raise PermissionError("模拟恢复中途被占用")
        original_write(path, content)

    with monkeypatch.context() as patch:
        patch.setattr(config_transaction, "atomic_bytes", fail_later)
        with pytest.raises(PermissionError):
            backup.apply_pending_configuration(original)
    assert failed and {path: path.read_bytes() for path in tracked} == before
    assert backup.apply_pending_configuration(original)
    assert json.loads((original / "general_settings.json").read_bytes())["output_mode"] == "gamepad"


def test_restore_invalidates_old_journals_and_keeps_pre_restore_raw_backup(original):
    raw = backup.export_configuration(original)
    path = original / "general_settings.json"
    path.write_bytes(b'{broken-original')
    (original / "general_settings.json.last-good").write_bytes(b'{only-copy')
    staged = stage(original, raw)
    for name in (".head-profile.journal", ".view-control.journal", ".voice-config-transaction.json"):
        (original / name).write_bytes(b"old unfinished contents")
    backup.apply_pending_configuration(original)
    assert not (original / ".voice-config-transaction.json").exists()
    with zipfile.ZipFile(staged["rollback"]) as archive:
        assert archive.read("general_settings.json") == b'{broken-original'
        assert archive.read("general_settings.json.last-good") == b'{only-copy'


def test_valid_current_configuration_can_restore_even_with_a_damaged_old_backup(original):
    (original / "general_settings.json.last-good").write_bytes(b'{old-damaged-copy')
    raw = backup.export_configuration(original, validate=False)
    _, files = backup.read_package(raw)
    assert files["general_settings.json.last-good"] == b'{old-damaged-copy'
    stage(original, raw)
    assert backup.apply_pending_configuration(original)
    assert (original / "general_settings.json.last-good").read_bytes() == (original / "general_settings.json").read_bytes()


def test_stale_preview_cannot_restore_a_different_package(original):
    raw = backup.export_configuration(original)
    first = backup.preview_configuration(original, raw)
    write(original, "general_settings.json", {"changed": True})
    backup.preview_configuration(original, backup.export_configuration(original))
    with pytest.raises(ValueError, match="过期"):
        backup.stage_configuration(original, first["token"], "full")
    assert not (original / backup.PENDING).exists()


def test_startup_projects_pending_game_before_load_without_changing_original(original, monkeypatch):
    raw = backup.export_configuration(original)
    store = GameProfileStore(ROOT, selection_path=original / "game_profile_selection.json")
    expected_id = store.selected_id()
    store.select("generic-xbox")
    stage(original, raw)
    before = (original / "game_profile_selection.json").read_bytes()
    monkeypatch.setenv("MOTIONCONTROL_USER_DIR", str(original))
    identity = startup_snapshot(ROOT)
    assert identity["restore_pending"] and identity["selected_id"] == expected_id
    assert (original / "game_profile_selection.json").read_bytes() == before
    backup.apply_pending_configuration(original)
    loaded = startup_snapshot(ROOT)
    assert loaded["configuration_revision"] == identity["configuration_revision"]


def test_interrupted_restore_projects_the_original_pending_target_then_recovers(original, monkeypatch):
    raw = backup.export_configuration(original)
    stage(original, raw)
    entries = [{"path": name, "content": base64.b64encode((original / name).read_bytes()).decode()}
               for name in (backup.PENDING, backup.PACKAGE)]
    write(original, backup.JOURNAL, {"schema": 1, "files": entries})
    (original / backup.PENDING).unlink()
    (original / backup.PACKAGE).unlink()
    monkeypatch.setenv("MOTIONCONTROL_USER_DIR", str(original))
    projected = startup_snapshot(ROOT)
    assert projected["restore_pending"]
    assert not (original / backup.PENDING).exists()
    assert backup.apply_pending_configuration(original)
    assert startup_snapshot(ROOT)["configuration_revision"] == projected["configuration_revision"]


def test_real_export_api_handles_new_install_without_selection_file(local_api):
    app, _ = local_api
    raw = app._backup_configuration()
    _, files = backup.read_package(raw)
    assert json.loads(files["game_profile_selection.json"])["selected_id"] == app.PROFILES.selected_id()


def test_backup_compression_does_not_hold_the_recognition_lock(application, monkeypatch):
    original = backup.export_configuration
    acquired = threading.Event()

    def export(*args, **kwargs):
        def recognition():
            with application.KERNEL._lock:
                acquired.set()
        worker = threading.Thread(target=recognition, daemon=True)
        worker.start()
        assert acquired.wait(1), "压缩备份期间仍应允许身体识别继续"
        worker.join(timeout=1)
        return original(*args, **kwargs)

    monkeypatch.setattr(backup, "export_configuration", export)
    backup.read_package(application._backup_configuration())


def test_restore_requires_paused_control_and_no_recording(local_api):
    app, request = local_api
    app.OUTPUT.enabled = True
    status, data = request("/api/data-backup/restore", {"token": "test", "mode": "full"})
    assert status == 400 and "暂停" in data["error"] and not app.SHUTTING_DOWN.is_set()
    app.OUTPUT.enabled = False
    app.KERNEL.pose_recorder.state = "recording"
    status, data = request("/api/data-backup/restore", {"token": "test", "mode": "full"})
    assert status == 400 and "录制" in data["error"] and not app.SHUTTING_DOWN.is_set()


def test_real_restore_request_stages_then_quits_without_live_file_replacement(local_api):
    app, request = local_api
    package = app._backup_configuration()
    root = app.user_data_root()
    preview = backup.preview_configuration(root, package)
    app.PROFILES.add_custom_game("稍后恢复前的游戏")
    before = app.PROFILES.selection_path.read_bytes()
    status, data = request("/api/data-backup/restore", {"token": preview["token"], "mode": "full"})
    assert status == 200 and data["pending"] and data["shutting_down"]
    assert app.SHUTTING_DOWN.is_set() and Path(data["rollback"]).is_file()
    assert app.PROFILES.selection_path.read_bytes() == before
    assert (root / backup.PENDING).is_file()


def test_startup_applies_restore_before_config_constructors():
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    restored = source.index("configuration_backup.apply_pending_configuration(user_data_root())")
    assert restored < source.index("FIREWALL =") < source.index("KERNEL =")
    launcher = (ROOT / "START.ps1").read_text(encoding="utf-8-sig")
    assert "$expected.restore_pending -eq $true" in launcher
