"""A release identifies both source trees and refuses stale mobile assets."""

import json
import subprocess
from pathlib import Path

import pytest

from tools import stage_release
from tools.build_app_bundle import build


def git(root, *arguments):
    return subprocess.check_output(["git", "-C", str(root), *arguments], text=True).strip()


def initialize_repository(root):
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.invalid")


@pytest.fixture
def mobile_build(tmp_path):
    repository = tmp_path / "android"
    initialize_repository(repository)
    mobile = repository / "mobile"
    (mobile / "src").mkdir(parents=True)
    (mobile / "src" / "main.ts").write_text("export const original = true;")
    (mobile / "package.json").write_text('{"private":true}')
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "mobile source")
    dist = mobile / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text('<script src="assets/main.js"></script>')
    (dist / "assets" / "main.js").write_text("console.log('original')")
    manifest = {
        "schema": "motioncontrol.build_provenance.v1",
        "source": {"commit": git(repository, "rev-parse", "HEAD"), "dirty": False,
                   "files": [stage_release._file_record(mobile, mobile / "src" / "main.ts"),
                             stage_release._file_record(mobile, mobile / "package.json")]},
        "files": [stage_release._file_record(dist, dist / "assets" / "main.js"),
                  stage_release._file_record(dist, dist / "index.html")],
    }
    (dist / stage_release.BUILD_PROVENANCE_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    return dist, manifest


def test_phone_build_selection_is_explicit(mobile_build, monkeypatch):
    dist, _manifest = mobile_build
    monkeypatch.delenv("MOTIONCONTROL_PHONE_WEB_DIR", raising=False)
    with pytest.raises(ValueError, match="显式指定"):
        stage_release.resolve_phone_web(None)
    monkeypatch.setenv("MOTIONCONTROL_PHONE_WEB_DIR", str(dist))
    assert stage_release.resolve_phone_web(None) == dist
    assert stage_release.read_phone_web_provenance(dist)["source"]["commit"]


@pytest.mark.parametrize("change", ["asset", "source", "addition", "new-source", "commit"])
def test_stale_mobile_build_is_rejected(mobile_build, change):
    dist, _manifest = mobile_build
    mobile = dist.parent
    if change == "asset":
        (dist / "assets" / "main.js").write_text("changed build")
    elif change == "source":
        (mobile / "src" / "main.ts").write_text("changed source")
    elif change == "addition":
        (dist / "old.js").write_text("unlisted output")
    elif change == "new-source":
        (mobile / "src" / "new.ts").write_text("unbuilt source")
    else:
        git(mobile, "commit", "--allow-empty", "-qm", "different source revision")
    with pytest.raises(ValueError):
        stage_release.read_phone_web_provenance(dist)


def test_recorded_release_keeps_both_sources_through_app_packaging(tmp_path, mobile_build, monkeypatch):
    dist, mobile_manifest = mobile_build
    repository = tmp_path / "pc"
    initialize_repository(repository)
    source = repository / "server.py"
    source.write_text("print('pc')")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "pc source")
    monkeypatch.setattr(stage_release, "ROOT", repository)
    target = tmp_path / "release"
    app = target / "app"
    app.mkdir(parents=True)
    (app / "server.py").write_bytes(source.read_bytes())
    stage_release.stage_phone_web(dist, target)
    provenance = stage_release.record_release_provenance(target, [(source, app / "server.py")], mobile_manifest)
    assert provenance["pc"]["commit"] == git(repository, "rev-parse", "HEAD")
    assert provenance["android"]["commit"] == mobile_manifest["source"]["commit"]
    # Signing nested bundles must not invalidate the content/source record.
    (app / "phone_web" / ".signature").write_text("signed afterwards")
    assert stage_release.verify_release_provenance(app, check_sources=True) == provenance
    from tools import build_app_bundle
    output = tmp_path / "update"
    monkeypatch.setattr(build_app_bundle, "OUT", output)
    build(app)
    assert json.loads((output / stage_release.RELEASE_PROVENANCE_NAME).read_text()) == provenance
    (app / "server.py").write_text("stale artifact")
    with pytest.raises(ValueError, match="内容与来源清单"):
        stage_release.verify_release_provenance(app)


def test_manifest_only_is_not_enough_when_current_pc_source_changed(tmp_path, mobile_build, monkeypatch):
    _dist, mobile_manifest = mobile_build
    repository = tmp_path / "pc"
    initialize_repository(repository)
    source = repository / "server.py"
    source.write_text("original")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "pc source")
    monkeypatch.setattr(stage_release, "ROOT", repository)
    target = tmp_path / "release"
    (target / "app").mkdir(parents=True)
    destination = target / "app" / "server.py"
    destination.write_bytes(source.read_bytes())
    stage_release.record_release_provenance(target, [(source, destination)], mobile_manifest)
    source.write_text("uncommitted new pc code")
    with pytest.raises(ValueError, match="来源已变化"):
        stage_release.verify_release_provenance(target / "app", check_sources=True)
