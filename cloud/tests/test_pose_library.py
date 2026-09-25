"""官方动作库接口：只发布签了名、签完没再改过的动作，发出去的字节验得过签名。"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from cloud.app.routers import pose_library as router
from motioncontrol_shared.pose_library import canonical_bytes, normalize_action

cryptography = pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

REPO = Path(__file__).resolve().parent.parent.parent
OFFICIAL = REPO / "cloud" / "official_poses"
KEY = ec.generate_private_key(ec.SECP256R1())


def sign(folder: Path, ids: list[str]) -> None:
    actions = {}
    for ident in ids:
        doc = normalize_action(json.loads((folder / f"{ident}.json").read_text(encoding="utf-8")))
        payload = canonical_bytes(doc)
        actions[ident] = {
            "revision": doc["revision"],
            "sha256": hashlib.sha256(payload).hexdigest(),
            "signature": base64.b64encode(KEY.sign(payload, ec.ECDSA(hashes.SHA256()))).decode("ascii"),
        }
    (folder / "signatures.json").write_text(
        json.dumps({"schema": router.SIGNATURES_SCHEMA, "actions": actions}), encoding="utf-8")


@pytest.fixture()
def official(tmp_path, monkeypatch):
    folder = tmp_path / "official_poses"
    folder.mkdir()
    for path in OFFICIAL.glob("*.json"):
        if path.name != "signatures.json":
            shutil.copy(path, folder / path.name)
    monkeypatch.setattr(router, "OFFICIAL_DIR", folder)
    router.catalog.cache_clear()
    yield folder
    router.catalog.cache_clear()


@pytest.mark.asyncio
async def test_only_signed_actions_are_published(client, official):
    sign(official, ["jumping_jack", "squat"])
    data = (await client.get("/api/v1/pose-library")).json()
    assert {item["id"] for item in data["actions"]} == {"jumping_jack", "squat"}
    assert data["rating_names"]["intensity"] == "运动强度"
    item = next(item for item in data["actions"] if item["id"] == "jumping_jack")
    assert item["name"] == "开合跳" and item["ratings"]["intensity"] >= 1
    assert item["demo"]["frames"], "列表要能直接画出火柴人"
    assert "rule" not in item, "浏览用的列表不带识别规则"


@pytest.mark.asyncio
async def test_an_action_changed_after_signing_is_not_published(client, official):
    sign(official, ["squat"])
    doc = json.loads((official / "squat.json").read_text(encoding="utf-8"))
    doc["how"] = "改过了"
    (official / "squat.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    router.catalog.cache_clear()
    data = (await client.get("/api/v1/pose-library")).json()
    assert data["actions"] == []
    assert (await client.get("/api/v1/pose-library/squat")).status_code == 404


@pytest.mark.asyncio
async def test_the_download_is_exactly_the_signed_bytes(client, official):
    sign(official, ["hands_cross"])
    data = (await client.get("/api/v1/pose-library/hands_cross")).json()
    payload = base64.b64decode(data["document"])
    assert hashlib.sha256(payload).hexdigest() == data["sha256"]
    KEY.public_key().verify(base64.b64decode(data["signature"]), payload, ec.ECDSA(hashes.SHA256()))
    doc = json.loads(payload)
    assert doc["id"] == "hands_cross" and "rule" in doc


@pytest.mark.asyncio
async def test_an_unknown_action_is_404(client, official):
    sign(official, ["squat"])
    assert (await client.get("/api/v1/pose-library/nope")).status_code == 404


@pytest.mark.asyncio
async def test_without_signatures_nothing_is_published(client, official):
    data = (await client.get("/api/v1/pose-library")).json()
    assert data["actions"] == []


def test_every_official_file_in_the_repo_is_valid_and_named_after_its_id():
    """发布前的最后一道：坏文件云端会悄悄跳过，这里让它当场红。"""
    for path in OFFICIAL.glob("*.json"):
        if path.name == "signatures.json":
            continue
        doc = normalize_action(json.loads(path.read_text(encoding="utf-8")))
        assert doc["id"] == path.stem
