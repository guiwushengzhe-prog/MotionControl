"""手机 App（APK）的更新通道：清单、发文件、没有包时的样子。"""

from __future__ import annotations

import hashlib

import pytest

from cloud.app.routers import app_update
from motioncontrol_shared.model_share import ModelShare

pytestmark = pytest.mark.asyncio


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    (tmp_path / "MotionControl.apk").write_bytes(b"PK" + b"\0" * 2048)
    (tmp_path / "apk.json").write_text('{"version_name": "9.8.7"}', encoding="utf-8")
    monkeypatch.setattr(app_update, "_android", ModelShare("android-apk", tmp_path))
    return tmp_path


async def test_without_a_bundle_there_is_simply_no_update(client, monkeypatch):
    monkeypatch.setattr(app_update, "_android", ModelShare("android-apk", None))
    response = await client.get("/api/v1/app-update/android")
    assert response.status_code == 200
    assert response.json()["available"] is False


async def test_the_manifest_lists_the_apk_and_its_description(client, bundle):
    manifest = (await client.get("/api/v1/app-update/android")).json()
    assert manifest["available"] is True
    assert {item["path"] for item in manifest["files"]} == {"MotionControl.apk", "apk.json"}
    apk = next(item for item in manifest["files"] if item["path"] == "MotionControl.apk")
    assert apk["sha256"] == hashlib.sha256((bundle / "MotionControl.apk").read_bytes()).hexdigest()


async def test_only_listed_files_are_served(client, bundle):
    response = await client.get("/api/v1/app-update/android/file", params={"path": "apk.json"})
    assert response.status_code == 200
    assert response.json() == {"version_name": "9.8.7"}
    for path in ("../app_update.py", "missing.apk", ".signature"):
        response = await client.get("/api/v1/app-update/android/file", params={"path": path})
        assert response.status_code == 404, path


async def test_the_pc_channel_is_separate(client, bundle):
    """APK 不会混进电脑端的更新包，反过来也一样。"""
    response = await client.get("/api/v1/app-update/pc/file", params={"path": "MotionControl.apk"})
    assert response.status_code == 404
