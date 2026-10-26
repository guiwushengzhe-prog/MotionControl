"""模型迁移不猜同名文件；只在显式检查时验摘要并试加载。"""
import copy
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from motioncontrol import config_transaction
from motioncontrol.model_resources import ACTIVE_MODEL, MODEL_FILES, SOURCE_FILES, ModelResources, file_hash
from test_configuration_coordination import application
from test_settings_persistence import local_api

ROOT = Path(__file__).resolve().parents[1]
MODELS = {ACTIVE_MODEL: {"name": "个人前后倾"}}


@pytest.fixture
def registered(tmp_path, isolated_user_data, monkeypatch):
    root = tmp_path / "原模型根目录"
    source = root / "models/personal/source"
    weights = root / "models/personal/upperbody"
    source.mkdir(parents=True)
    weights.mkdir()
    for name in SOURCE_FILES:
        (source / name).write_text("# original source", encoding="utf-8")
    for name in MODEL_FILES:
        (weights / name).write_bytes(b"original configuration")
    checkpoint = weights / "model.onnx"
    checkpoint.write_bytes(b"original weights")
    readout = source.parent / "readout.json"
    readout.write_bytes(b'{"type":"mocapnet_personal_readout"}')
    license_file = source.parent / "license.txt"
    license_file.write_text("original license")
    python = root / "runtime/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"original runtime")
    packages = root / "runtime/packages"
    packages.mkdir()
    document = {"python": str(python), "models": {ACTIVE_MODEL: {"source": str(source), "checkpoint": str(checkpoint),
        "sha256": file_hash(checkpoint), "readout": str(readout), "readout_sha256": file_hash(readout),
        "packages": [str(packages)], "license": str(license_file)}}}
    path = isolated_user_data / "depth_comparison_runtime.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    resource = ModelResources(path)
    monkeypatch.setattr(resource, "_probe", lambda *args: None)
    return SimpleNamespace(root=root, path=path, resource=resource, document=document)


@pytest.mark.parametrize("missing", ["python", "source", "checkpoint", "readout", "license", "packages", "model-config", "source-file"])
def test_missing_resources_are_reported_individually_without_loading(registered, monkeypatch, missing):
    item = registered
    spec = item.document["models"][ACTIVE_MODEL]
    paths = {"python": Path(item.document["python"]), "source": Path(spec["source"]),
        "checkpoint": Path(spec["checkpoint"]), "readout": Path(spec["readout"]), "license": Path(spec["license"]),
        "packages": Path(spec["packages"][0]), "model-config": Path(spec["checkpoint"]).parent / MODEL_FILES[0],
        "source-file": Path(spec["source"]) / SOURCE_FILES[0]}
    path = paths[missing]
    if path.is_dir():
        destination = path.with_name(path.name + "-missing")
        assert destination.resolve().is_relative_to(item.root.resolve())
        path.rename(destination)
    else:
        path.unlink()
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("状态刷新不能试加载"))
    monkeypatch.setattr("motioncontrol.model_resources.file_hash", lambda *a: pytest.fail("状态刷新不能重新验模型摘要"))
    state = item.resource.diagnose(MODELS)
    assert not state["models"][ACTIVE_MODEL]["available"]
    assert str(path) in {entry["path"] for entry in state["models"][ACTIVE_MODEL]["issues"]}


def test_registry_and_resource_status_are_cached_but_external_changes_are_observed(registered, monkeypatch):
    item = registered
    assert item.resource.diagnose(MODELS)["models"][ACTIVE_MODEL]["available"]
    original = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: pytest.fail("重复轮询不应重复读登记"))
    assert item.resource.diagnose(MODELS)["models"][ACTIVE_MODEL]["available"]
    monkeypatch.setattr(Path, "read_text", original)
    item.path.write_text('{broken', encoding="utf-8")
    assert "无法读取" in item.resource.diagnose(MODELS)["error"]


@pytest.mark.parametrize("previous_state", ["running", "error"])
def test_external_registration_change_retires_old_worker_without_frame_file_reads(registered, monkeypatch, previous_state):
    import numpy as np
    from motioncontrol.depth_comparison import DepthComparison
    published = []
    comparison = DepthComparison(SimpleNamespace(publish_monocular_lean=published.append))
    comparison.resources = registered.resource
    comparison.resources.snapshot()
    comparison.config["enabled"] = True
    comparison._worker_model = comparison._selected_model()
    comparison._worker_resource_revision = comparison.resources.revision
    comparison._thread = SimpleNamespace(is_alive=lambda: True, join=lambda **kwargs: None)
    comparison._state = previous_state
    generation = comparison._generation
    changed = copy.deepcopy(registered.document)
    changed["models"][ACTIVE_MODEL]["readout_sha256"] = "f" * 64
    registered.path.write_text(json.dumps(changed))
    comparison.status()  # 沿用已有状态刷新发现登记更新。
    monkeypatch.setattr(registered.resource, "read", lambda: pytest.fail("提交画面不能读文件"))
    monkeypatch.setattr(registered.resource, "_stamp", lambda: pytest.fail("提交画面不能查文件状态"))
    monkeypatch.setattr("motioncontrol.depth_comparison.threading.Thread",
        lambda **kwargs: SimpleNamespace(start=lambda: None))
    comparison.submit(np.zeros((10, 10, 3), dtype=np.uint8), None, None, None, "none", 1, "camera")
    assert comparison._generation == generation + 1
    assert published == [None]
    assert comparison._state == "loading"
    assert comparison._pending[0]["sample_at"] == 1


def test_model_preparation_uses_shared_registry_backup_and_clears_old_pins(registered, tmp_path, monkeypatch):
    import runpy
    import sys
    item = registered
    candidate, expected = item.resource.prepare(str(item.root), ROOT)
    item.resource.commit(candidate, expected)
    source = tmp_path / "official"
    shutil.copytree(Path(item.document["models"][ACTIVE_MODEL]["source"]), source / "src/python/mnet4")
    (source / "license.txt").write_text("original license")
    head = tmp_path / "trained.json"
    head.write_text(json.dumps({"model": "mocapnet", "keys": list(range(42)), "weights": [1] * 42}))
    target = tmp_path / "new personal model"
    monkeypatch.setattr(sys, "argv", ["prepare_mocap_personal.py", "--source", str(source), "--model-dir",
        str(Path(item.document["models"][ACTIVE_MODEL]["checkpoint"]).parent), "--readout", str(head),
        "--destination", str(target), "--registry", str(item.path)])
    runpy.run_path(str(ROOT / "tools/prepare_mocap_personal.py"), run_name="__main__")
    saved = json.loads(item.path.read_bytes())
    assert "relocation" not in saved
    assert saved["models"][ACTIVE_MODEL]["readout"] == str(target / "personal-readout.json")
    assert item.path.read_bytes() == item.path.with_name(item.path.name + ".last-good").read_bytes()


def test_relocation_verifies_the_same_model_and_saves_a_valid_backup(registered, tmp_path):
    item = registered
    pinned, revision = item.resource.prepare(str(item.root), ROOT)
    item.resource.commit(pinned, revision)
    target = tmp_path / "迁移后的模型"
    shutil.copytree(item.root, target)
    original_offline = tmp_path / "原位置已离线"
    assert original_offline.resolve().is_relative_to(tmp_path.resolve())
    item.root.rename(original_offline)
    candidate, revision = item.resource.prepare(str(target), ROOT)
    assert Path(candidate["python"]).is_relative_to(target)
    assert Path(candidate["models"][ACTIVE_MODEL]["readout"]).is_relative_to(target)
    item.resource.commit(candidate, revision)
    assert item.resource.diagnose(MODELS, force=True)["models"][ACTIVE_MODEL]["available"]
    assert item.path.read_bytes() == item.path.with_name(item.path.name + ".last-good").read_bytes()
    assert candidate["relocation"]["sha256_by_path"]


@pytest.mark.parametrize("changed", ["checkpoint", "readout", "source"])
def test_same_named_changed_files_are_rejected_without_changing_registry(registered, tmp_path, changed):
    item = registered
    pinned, revision = item.resource.prepare(str(item.root), ROOT)
    item.resource.commit(pinned, revision)
    before = item.path.read_bytes()
    target = tmp_path / "同名错误副本"
    shutil.copytree(item.root, target)
    spec = item.document["models"][ACTIVE_MODEL]
    path = Path(spec[changed])
    if changed == "source":
        path /= SOURCE_FILES[0]
    (target / path.relative_to(item.root)).write_bytes(b"different content")
    with pytest.raises(ValueError, match="不相符"):
        item.resource.prepare(str(target), ROOT)
    assert item.path.read_bytes() == before


def test_readout_changes_intentionally_refresh_the_old_fingerprints(registered):
    # 用户通过正式读出工具更新登记后，不能把旧的摘要继续用于新版本。
    item = registered
    candidate, revision = item.resource.prepare(str(item.root), ROOT)
    item.resource.commit(candidate, revision)
    new_version = json.loads(item.path.read_bytes())
    readout = Path(new_version["models"][ACTIVE_MODEL]["readout"])
    readout.write_bytes(b'{"type":"mocapnet_personal_readout","updated":true}')
    new_version["models"][ACTIVE_MODEL]["readout_sha256"] = file_hash(readout)
    item.path.write_text(json.dumps(new_version))
    checked, revision = item.resource.prepare(str(item.root), ROOT)
    item.resource.commit(checked, revision)
    assert checked["relocation"]["readout_sha256"] == file_hash(readout)


@pytest.mark.parametrize("reply", [{"kind": "ready", "device": "cuda"}, {"kind": "error", "error": "缺少扩展库"}, []])
def test_probe_uses_the_real_worker_protocol_and_requires_cpu_ready(registered, monkeypatch, reply):
    from motioncontrol import owned_process
    from motioncontrol.depth_comparison_worker import write_packet
    import io
    packet = io.BytesIO()
    write_packet(packet, reply)
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        assert kwargs["stdin"] == subprocess.DEVNULL
        return SimpleNamespace(returncode=0, stdout=packet.getvalue(), stderr=b"")
    monkeypatch.setattr(owned_process, "run", run)
    with pytest.raises(ValueError, match="试加载"):
        ModelResources._probe(registered.resource, registered.document, ROOT)
    assert calls[0][3:5] == ["-m", "motioncontrol.mocap_lean_worker"]


def test_failed_position_save_rolls_back_primary_and_last_good(registered, monkeypatch):
    item = registered
    before = item.path.read_bytes()
    candidate, revision = item.resource.prepare(str(item.root), ROOT)
    original = config_transaction.atomic_bytes
    def fail_primary(path, content):
        if path == item.path:
            raise PermissionError("模拟被占用")
        original(path, content)
    monkeypatch.setattr(config_transaction, "atomic_bytes", fail_primary)
    with pytest.raises(ValueError, match="未能保存"):
        item.resource.commit(candidate, revision)
    assert item.path.read_bytes() == before
    assert item.path.with_name(item.path.name + ".last-good").read_bytes() == before


def test_external_registry_update_prevents_a_stale_probe_from_overwriting_it(registered):
    item = registered
    candidate, revision = item.resource.prepare(str(item.root), ROOT)
    changed = copy.deepcopy(item.document)
    changed["external_edit"] = "keep"
    item.path.write_text(json.dumps(changed))
    before = item.path.read_bytes()
    with pytest.raises(ValueError, match="其它操作"):
        item.resource.commit(candidate, revision)
    assert item.path.read_bytes() == before


def test_bad_registry_recovers_using_the_shared_last_good_document(registered):
    item = registered
    before = item.path.read_bytes()
    item.path.write_bytes(b'{partial')
    restored = ModelResources(item.path)
    assert restored.diagnose(MODELS)["models"][ACTIVE_MODEL]["available"]
    assert item.path.read_bytes() == before
    assert list(item.path.parent.glob("*.invalid-*"))


def test_a_removed_pinned_auxiliary_file_is_detected_without_rehashing(registered, monkeypatch):
    item = registered
    extra = Path(item.document["models"][ACTIVE_MODEL]["checkpoint"]).parent / "learned-transform.pca"
    extra.write_bytes(b"auxiliary parameters")
    candidate, revision = item.resource.prepare(str(item.root), ROOT)
    item.resource.commit(candidate, revision)
    extra.unlink()
    monkeypatch.setattr("motioncontrol.model_resources.file_hash", lambda *a: pytest.fail("状态刷新不能读完整模型"))
    state = item.resource.diagnose(MODELS, force=True)
    assert not state["models"][ACTIVE_MODEL]["available"]
    assert any(row["path"] == str(extra) for row in state["models"][ACTIVE_MODEL]["issues"])


def test_a_failed_backup_checkpoint_is_visible_without_disabling_a_valid_model(registered, monkeypatch):
    item = registered
    item.path.with_name(item.path.name + ".last-good").unlink()
    monkeypatch.setattr("motioncontrol.settings_persistence.atomic_bytes", lambda *a: (_ for _ in ()).throw(PermissionError("blocked")))
    resource = ModelResources(item.path)
    status = resource.diagnose(MODELS)
    assert status["models"][ACTIVE_MODEL]["available"]
    assert "恢复未完成" in status["warning"]


def test_system_folder_picker_cancel_changes_nothing(local_api, monkeypatch):
    app, request = local_api
    monkeypatch.setattr(app, "choose_dependency_folder", lambda: "")
    status, result = request("/api/model-resources/choose", {})
    assert status == 200 and result["folder"] == ""
    assert not app.RUNTIME.camera.depth_comparison._runtime.exists()


def test_active_control_is_never_interrupted_by_position_checks(local_api, monkeypatch):
    app, request = local_api
    app.OUTPUT.enabled = True
    monkeypatch.setattr(app.RUNTIME.camera.depth_comparison.resources, "prepare", lambda *a: pytest.fail("控制中不能试加载"))
    status, result = request("/api/model-resources/relocate", {"folder": "test"})
    assert status == 400 and "暂停" in result["error"] and app.OUTPUT.enabled
    app.OUTPUT.enabled = False


def test_late_probe_cannot_modify_registration_after_control_starts(local_api, monkeypatch):
    app, request = local_api
    resources = app.RUNTIME.camera.depth_comparison.resources
    def prepare(*args):
        app.OUTPUT.enabled = True
        return {}, "stale"
    monkeypatch.setattr(resources, "prepare", prepare)
    monkeypatch.setattr(resources, "commit", lambda *a: pytest.fail("迟到检查不能改登记"))
    status, result = request("/api/model-resources/relocate", {"folder": "test"})
    assert status == 400 and "未更改" in result["error"]
    app.OUTPUT.enabled = False
