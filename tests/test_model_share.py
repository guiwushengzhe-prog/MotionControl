"""Handing a model directory to the phone.

The phone stopped carrying its own 41.5 MB copy of the Chinese speech model and
fetches it from the paired PC instead. Two properties make that safe to do over
a LAN-facing socket: only files the server itself found are servable, and every
file arrives with a digest so a truncated download is caught rather than loaded.
"""

from __future__ import annotations

import hashlib

import pytest

from model_share import ModelShare


@pytest.fixture
def model(tmp_path):
    root = tmp_path / "vosk-model-small-cn-0.22"
    (root / "am").mkdir(parents=True)
    (root / "conf").mkdir()
    (root / "am" / "final.mdl").write_bytes(b"acoustic" * 1000)
    (root / "conf" / "model.conf").write_text("--min-active=200\n", encoding="utf-8")
    (root / "README").write_text("small cn 0.22\n", encoding="utf-8")
    return ModelShare("vosk-model-small-cn-0.22", root)


def test_the_manifest_lists_every_file_with_its_digest(model):
    manifest = model.manifest()
    assert manifest["available"] is True
    assert manifest["name"] == "vosk-model-small-cn-0.22"
    paths = [item["path"] for item in manifest["files"]]
    assert paths == ["README", "am/final.mdl", "conf/model.conf"]
    entry = next(item for item in manifest["files"] if item["path"] == "am/final.mdl")
    assert entry["size"] == 8000
    assert entry["sha256"] == hashlib.sha256(b"acoustic" * 1000).hexdigest()
    assert manifest["total_bytes"] == sum(item["size"] for item in manifest["files"])


def test_paths_use_forward_slashes(model):
    """手机拿到什么就按什么拼路径，分隔符不能跟着电脑的操作系统走。"""
    assert all("\\" not in item["path"] for item in model.manifest()["files"])


def test_a_missing_model_is_reported_rather_than_raised(tmp_path):
    """这台电脑没配语音时，手机该看到"没有"，而不是一个 500。"""
    absent = ModelShare("vosk-model-small-cn-0.22", tmp_path / "nowhere")
    manifest = absent.manifest()
    assert manifest["available"] is False
    assert manifest["files"] == []
    assert absent.resolve("conf/model.conf") is None


def test_no_model_configured_at_all(tmp_path):
    unset = ModelShare("vosk-model-small-cn-0.22", None)
    assert unset.manifest()["available"] is False
    assert unset.resolve("README") is None


# --- what may be served ---------------------------------------------------


def test_a_listed_file_resolves(model):
    path = model.resolve("conf/model.conf")
    assert path is not None and path.read_text(encoding="utf-8").startswith("--min-active")


def test_backslashes_resolve_too(model):
    """安卓那边拼出来的路径可能带反斜杠，同一个文件不该因此拿不到。"""
    assert model.resolve("conf\\model.conf") == model.resolve("conf/model.conf")


@pytest.mark.parametrize("wanted", [
    "../../../etc/passwd",
    "../secrets.txt",
    "conf/../../outside",
    "/etc/passwd",
    "am/final.mdl/../../../..",
    "",
])
def test_anything_not_in_the_manifest_does_not_exist(model, wanted):
    """这道防线是"在不在清单里"，不是清洗字符串。

    清单是这个进程自己走目录走出来的，所以目录外的东西根本没有机会匹配上——
    不需要另外再写一套针对 .. 的判断，也就不会漏掉哪种写法。
    """
    assert model.resolve(wanted) is None


def test_a_file_outside_the_root_is_not_served(model, tmp_path):
    outside = tmp_path / "secrets.txt"
    outside.write_text("never", encoding="utf-8")
    assert model.resolve("secrets.txt") is None
    assert model.resolve(str(outside)) is None


# --- caching --------------------------------------------------------------


def test_editing_a_file_changes_its_digest(model):
    before = next(item for item in model.manifest()["files"] if item["path"] == "README")
    target = model.root / "README"
    target.write_text("small cn 0.22 edited\n", encoding="utf-8")
    after = next(item for item in model.manifest()["files"] if item["path"] == "README")
    assert after["sha256"] != before["sha256"]
    assert after["size"] != before["size"]


def test_digests_are_not_recomputed_for_unchanged_files(model, monkeypatch):
    """一次清单请求要哈希 65 MB，手机每连一次就重算一遍太浪费。"""
    model.manifest()
    opened = []
    original = type(model.root).open

    def counting_open(self, *args, **kwargs):
        opened.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(model.root), "open", counting_open)
    model.manifest()
    assert opened == []


# --- telling one model from another ---------------------------------------
#
# 换一个更大更好的模型时，手机必须发现"我手上这份不是电脑现在给的那份"。只问
# "有没有模型"会让它抱着几个月前下的那份不放。


def test_the_manifest_identifies_the_exact_set_of_files(model):
    assert len(model.manifest()["digest"]) == 64


def test_editing_any_file_changes_the_manifest_digest(model):
    before = model.manifest()["digest"]
    (model.root / "conf" / "model.conf").write_text("--min-active=400", encoding="utf-8")
    assert model.manifest()["digest"] != before


def test_adding_a_file_changes_the_manifest_digest(model):
    before = model.manifest()["digest"]
    (model.root / "graph").mkdir()
    (model.root / "graph" / "HCLr.fst").write_bytes(b"graph")
    assert model.manifest()["digest"] != before


def test_removing_a_file_changes_the_manifest_digest(model):
    before = model.manifest()["digest"]
    (model.root / "README").unlink()
    assert model.manifest()["digest"] != before


def test_an_unchanged_model_keeps_its_digest(model):
    assert model.manifest()["digest"] == model.manifest()["digest"]


def test_a_missing_model_has_no_digest(tmp_path):
    assert ModelShare("gone", tmp_path / "nowhere").manifest()["digest"] == ""
