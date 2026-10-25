"""各配置共用保存和恢复，严格事务失败不改变正在使用的库。"""
import copy
import json

import pytest

from motioncontrol import config_transaction
from motioncontrol.custom_poses import CustomPoseError, CustomPoseStore
from motioncontrol.head_control import HeadController
from motioncontrol.key_macros import MacroError, MacroStore
from test_custom_poses import T_POSE
from test_configuration_coordination import application
from test_settings_persistence import local_api


@pytest.mark.parametrize("kind", ["macro", "pose"])
@pytest.mark.parametrize("operation", ["create", "update", "remove"])
def test_library_write_failure_preserves_disk_and_memory(tmp_path, monkeypatch, kind, operation):
    if kind == "macro":
        store = MacroStore(tmp_path / "key_macros.json")
        entry = store.create("原宏")
        before = copy.deepcopy(store.macros)
        call = {"create": lambda: store.create("新宏"), "update": lambda: store.update(entry["id"], name="新名字"),
                "remove": lambda: store.remove(entry["id"])}[operation]
        expected = MacroError
    else:
        store = CustomPoseStore(tmp_path / "custom_poses.json")
        entry = store.capture(T_POSE, "原动作")
        before = copy.deepcopy(store.poses)
        call = {"create": lambda: store.capture(T_POSE, "新动作"), "update": lambda: store.update(entry["id"], name="新名字"),
                "remove": lambda: store.remove(entry["id"])}[operation]
        expected = CustomPoseError
    files = {path: path.read_bytes() for path in (store.path, store._persistence.backup_path)}
    original = config_transaction.atomic_bytes

    def fail(path, content):
        if path == store.path:
            raise PermissionError("模拟文件占用")
        original(path, content)

    with monkeypatch.context() as patch:
        patch.setattr(config_transaction, "atomic_bytes", fail)
        with pytest.raises(expected, match="原配置已保留"):
            call()
    assert (store.macros if kind == "macro" else store.poses) == before
    assert {path: path.read_bytes() for path in files} == files
    assert not store._persistence.journal_path.exists()


@pytest.mark.parametrize("operation", ["append", "remove", "invalid-update"])
def test_pose_progress_and_frames_are_unchanged_on_failure(tmp_path, monkeypatch, operation):
    store = CustomPoseStore(tmp_path / "custom_poses.json")
    entry = store.capture(T_POSE, "动作")
    store.append_frame(entry["id"], T_POSE)
    store._progress[entry["id"]] = {"step": 1, "seen": 3}
    poses, progress = copy.deepcopy(store.poses), copy.deepcopy(store._progress)
    with monkeypatch.context() as patch:
        patch.setattr("motioncontrol.custom_poses.replace_documents", lambda *args: (_ for _ in ()).throw(OSError("disk")))
        with pytest.raises(CustomPoseError):
            if operation == "append":
                store.append_frame(entry["id"], T_POSE)
            elif operation == "remove":
                store.remove_frame(entry["id"], 0)
            else:
                store.update(entry["id"], name="不应部分生效", threshold=0)
    assert store.poses == poses and store._progress == progress


@pytest.mark.parametrize("kind", ["head", "macro", "pose"])
def test_broken_file_recovers_latest_complete_document(tmp_path, kind):
    path = tmp_path / (kind + ".json")
    if kind == "head":
        store = HeadController(path)
        store.configure(sensitivity_x=73)
        factory = HeadController
    elif kind == "macro":
        store = MacroStore(path)
        store.create("保留的宏")
        factory = MacroStore
    else:
        store = CustomPoseStore(path)
        store.capture(T_POSE, "保留的动作")
        factory = CustomPoseStore
    original = path.read_bytes()
    path.write_bytes(b'{partial')
    restored = factory(path)
    assert path.read_bytes() == original
    assert list(tmp_path.glob("*.invalid-*"))
    if kind == "head":
        assert restored.config["sensitivity_x"] == 73
    elif kind == "macro":
        assert restored.macros == store.macros
    else:
        assert restored.poses == store.poses


def test_duplicate_pose_ids_cannot_replace_a_valid_backup(tmp_path):
    store = CustomPoseStore(tmp_path / "custom_poses.json")
    entry = store.capture(T_POSE, "原动作")
    store.path.write_text(json.dumps({"schema": "motioncontrol.custom_poses.v1", "poses": [entry, entry]}), encoding="utf-8")
    restored = CustomPoseStore(store.path)
    assert len(restored.poses) == 1 and restored.poses[0]["name"] == "原动作"
    assert list(tmp_path.glob("*.invalid-*"))


def test_head_only_failure_is_reported_and_retry_recovers_it(local_api, monkeypatch):
    app, request = local_api
    head = app.KERNEL.head_controller._profile_store
    original = config_transaction.atomic_bytes

    def fail_head(path, content):
        if path == head.path:
            raise PermissionError("模拟仅头部文件不可写")
        original(path, content)

    with monkeypatch.context() as patch:
        patch.setattr(config_transaction, "atomic_bytes", fail_head)
        status, response = request("/api/head/config", {"sensitivity_x": 73})
    assert status == 503 and response["applied"]
    assert any(part["name"] == "头部设置" and part["state"] == "pending" for part in response["settings_persistence"]["components"])
    assert response["settings_write"]["targets"]
    assert app.KERNEL.head_controller.config["sensitivity_x"] == 73
    status, response = request("/api/settings/retry", {})
    assert status == 200 and response["settings_persistence"]["state"] == "ready"
    assert HeadController(head.path).config["sensitivity_x"] == 73


def test_unrelated_library_error_does_not_report_a_successful_operation_as_failed(local_api):
    app, request = local_api
    app.CUSTOM_POSES._persistence.blocked = True
    app.CUSTOM_POSES._persistence.error = "模拟动作库损坏"
    status, response = request("/api/macros/create", {"name": "已保存"})
    assert status == 200 and response["ok"]
    assert response["settings_persistence"]["state"] == "blocked"
    assert response["settings_write"]["state"] == "ready"
    assert app.MACROS.get(response["macro"]["id"])
    status, response = request("/api/settings/retry", {"repair": True})
    assert status == 200 and app.CUSTOM_POSES.last_error == ""
