from __future__ import annotations

import hashlib
import io
import tarfile

import pytest

from tools import download_hfhpe_models as downloader


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _archive(path, entries):
    with tarfile.open(path, "w:gz") as bundle:
        for name, data in entries:
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            bundle.addfile(entry, io.BytesIO(data))


def test_changed_download_preserves_old_model_and_removes_partial(tmp_path, monkeypatch):
    target = tmp_path / "model.onnx"
    target.write_bytes(b"previous")
    monkeypatch.setattr(downloader.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(b"tampered"))
    with pytest.raises(ValueError, match="校验值不一致"):
        downloader.download_verified("https://example.test/model", target, _sha(b"expected"), 8)
    assert target.read_bytes() == b"previous"
    assert not target.with_name(target.name + ".partial").exists()


def test_valid_existing_download_never_uses_network(tmp_path, monkeypatch):
    target = tmp_path / "model.onnx"
    target.write_bytes(b"valid")
    monkeypatch.setattr(downloader.urllib.request, "urlopen", lambda *a, **kw: pytest.fail("不应联网"))
    assert downloader.download_verified("unused", target, _sha(b"valid"), 5) == target


def test_extract_reads_only_exact_member_without_expanding_paths(tmp_path):
    bundle = tmp_path / "bundle.tar.gz"
    _archive(bundle, [("../escaped.onnx", b"wrong"), ("package/model.onnx", b"valid"),
                      ("package/training.pt", b"large training data")])
    target = tmp_path / "models" / "model.onnx"
    spec = {"member": "package/model.onnx", "sha256": _sha(b"valid"), "size": 5}
    downloader.extract_model(bundle, target, spec)
    assert target.read_bytes() == b"valid"
    assert not (tmp_path.parent / "escaped.onnx").exists()
    assert list(target.parent.iterdir()) == [target]


def test_archive_symlink_cannot_replace_model(tmp_path):
    bundle = tmp_path / "bundle.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        member = tarfile.TarInfo("model.onnx")
        member.type = tarfile.SYMTYPE
        member.linkname = "../other.onnx"
        archive.addfile(member)
    with pytest.raises(ValueError, match="普通文件"):
        downloader.extract_model(bundle, tmp_path / "model.onnx", {"member": "model.onnx", "size": 0})
    assert not (tmp_path / "model.onnx").exists()


def test_default_install_only_downloads_selected_pose_model(tmp_path, monkeypatch):
    payload = b"pose-model"
    spec = {"archive": "sample.tar.gz", "archive_sha256": "unused", "archive_size": 1,
            "member": "package/pose.onnx", "sha256": _sha(payload), "size": len(payload)}
    source = tmp_path / "source.tar.gz"
    _archive(source, [(spec["member"], payload)])
    calls = []

    def fetch(url, *args):
        calls.append(url)
        return source

    monkeypatch.setitem(downloader.MODEL_SPECS, "vitt128", spec)
    monkeypatch.setattr(downloader, "download_verified", fetch)
    output = tmp_path / "models"
    assert downloader.install_models(output) == [output / "pose.onnx"]
    assert len(calls) == 1 and calls[0].endswith("/sample.tar.gz")
    assert not (output / "deimv2_head.onnx").exists()
    downloader.install_models(output)
    assert len(calls) == 1
