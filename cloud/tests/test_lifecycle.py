"""The config lifecycle, driven against this installation's real configuration.

This is the acceptance run for the cloud service, written as a test so it keeps
being true: upload a real config, edit a real binding, roll back, download every
version, and check that what comes down is byte-for-byte what went up.

The three documents used here are the actual files this build reads -- the
whole point is that the cloud accepts what the desktop writes. A miniature
hand-written config would pass while proving nothing about the real ones.
"""

from __future__ import annotations

import copy
import json
from hashlib import sha256

import pytest

from motioncontrol_shared.canonical import canonicalize

from .conftest import REAL_CONFIG_DIR

pytestmark = pytest.mark.asyncio

REAL_FILES = {
    "profile_selection": "game_profile_selection.json",
    "motion_mappings": "motion_mappings.json",
    "voice_mappings": "voice_mappings.json",
}


def load_real(doc_type: str) -> dict:
    path = REAL_CONFIG_DIR / REAL_FILES[doc_type]
    if not path.is_file():
        pytest.skip(f"real config not present: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


async def sign_up(client, invite_code: str, email: str = "player@example.com"):
    response = await client.post("/api/v1/auth/register", json={
        "invite_code": invite_code,
        "email": email,
        "password": "a-long-enough-passphrase",
        "display_name": "玩家一号",
    })
    assert response.status_code == 201, response.text
    return response.json()


async def create(client, doc_type: str, document, **extra):
    body = {"doc_type": doc_type, "title": f"测试 {doc_type}", "document": document}
    body.update(extra)
    return await client.post("/api/v1/profiles", json=body)


# --- the full lifecycle ------------------------------------------------------

async def test_real_config_round_trip(client, invite_code):
    """Upload, edit, roll back, download -- on the real profile_selection."""
    await sign_up(client, invite_code, "roundtrip@example.com")
    original = load_real("profile_selection")

    response = await create(client, "profile_selection", original)
    assert response.status_code == 201, response.text
    profile = response.json()
    profile_id = profile["id"]
    v1 = profile["current_version"]
    assert v1["revision_no"] == 1
    assert v1["parent_version_id"] is None
    assert v1["schema_version"] == "motioncontrol.profile_selection.v2"

    # Change one real binding, the way the desktop UI would.
    edited = copy.deepcopy(original)
    game = edited["overrides_by_profile"]["generic-xbox"]
    trigger = next(iter(game))
    game[trigger] = {"action": {"type": "gamepad", "target": "Y", "behavior": "tap"}}

    response = await client.post(f"/api/v1/profiles/{profile_id}/versions", json={
        "document": edited, "base_version_id": v1["id"], "note": "改一个绑定"})
    assert response.status_code == 201, response.text
    v2 = response.json()
    assert v2["revision_no"] == 2
    assert v2["parent_version_id"] == v1["id"]
    assert v2["canonical_sha256"] != v1["canonical_sha256"]

    # Roll back to v1's content. It becomes v3, and v1 stays where it is.
    response = await client.post(f"/api/v1/profiles/{profile_id}/rollback",
                                 json={"version_id": v1["id"]})
    assert response.status_code == 201, response.text
    v3 = response.json()
    assert v3["revision_no"] == 3
    assert v3["restored_from_id"] == v1["id"]
    assert v3["id"] != v1["id"]
    # Same content, different version: that is what "roll forward to an earlier
    # state" means, as opposed to deleting the history in between.
    assert v3["canonical_sha256"] == v1["canonical_sha256"]

    # Every version is still downloadable, and the bytes still hash to what was
    # recorded when they were uploaded.
    for version in (v1, v2, v3):
        response = await client.get(
            f"/api/v1/profiles/{profile_id}/versions/{version['id']}/download")
        assert response.status_code == 200
        assert sha256(response.content).hexdigest() == version["canonical_sha256"]
        assert response.headers["x-motioncontrol-sha256"] == version["canonical_sha256"]
        assert b"\r\n" not in response.content

    # The history reads as three entries, newest first, nothing rewritten.
    response = await client.get(f"/api/v1/profiles/{profile_id}/versions")
    history = response.json()
    assert [item["revision_no"] for item in history] == [3, 2, 1]
    assert history[2]["canonical_sha256"] == v1["canonical_sha256"]


@pytest.mark.parametrize("doc_type", sorted(REAL_FILES))
async def test_every_real_document_uploads_and_downloads_intact(
        client, invite_code, doc_type):
    """All three whitelisted files, not just the game profile."""
    await sign_up(client, invite_code, f"{doc_type}@example.com")
    document = load_real(doc_type)

    response = await create(client, doc_type, document)
    assert response.status_code == 201, response.text
    profile = response.json()
    version = profile["current_version"]

    # The hash the server recorded is the one the shared library computes here.
    assert version["canonical_sha256"] == canonicalize(doc_type, document).sha256

    response = await client.get(
        f"/api/v1/profiles/{profile['id']}/versions/{version['id']}/download")
    assert response.status_code == 200
    assert response.content == canonicalize(doc_type, document).payload


async def test_a_cosmetic_resave_does_not_create_a_version(client, invite_code):
    """Reordered keys and different whitespace are the same config."""
    await sign_up(client, invite_code, "cosmetic@example.com")
    document = load_real("profile_selection")

    profile = (await create(client, "profile_selection", document)).json()
    v1 = profile["current_version"]

    # Same content, keys in a different order.
    shuffled = json.loads(json.dumps(document, sort_keys=True))
    response = await client.post(f"/api/v1/profiles/{profile['id']}/versions", json={
        "document": shuffled, "base_version_id": v1["id"]})
    assert response.status_code == 201
    assert response.json()["id"] == v1["id"], "a no-op edit became a new version"


# --- concurrency -------------------------------------------------------------

async def test_a_stale_edit_is_refused_rather_than_merged(client, invite_code):
    """Two people editing from v1: the second one is told, not overwritten."""
    await sign_up(client, invite_code, "concurrent@example.com")
    document = load_real("motion_mappings")
    profile = (await create(client, "motion_mappings", document)).json()
    v1 = profile["current_version"]

    first = copy.deepcopy(document)
    first["motions"][0]["target"] = "LS_DOWN"
    response = await client.post(f"/api/v1/profiles/{profile['id']}/versions", json={
        "document": first, "base_version_id": v1["id"]})
    assert response.status_code == 201

    second = copy.deepcopy(document)
    second["motions"][1]["target"] = "Y"
    response = await client.post(f"/api/v1/profiles/{profile['id']}/versions", json={
        "document": second, "base_version_id": v1["id"]})
    assert response.status_code == 409
    assert "已被另一处修改" in response.json()["detail"]


async def test_an_edit_without_a_base_version_is_refused(client, invite_code):
    await sign_up(client, invite_code, "nobase@example.com")
    document = load_real("motion_mappings")
    profile = (await create(client, "motion_mappings", document)).json()

    response = await client.post(f"/api/v1/profiles/{profile['id']}/versions",
                                 json={"document": document})
    assert response.status_code == 409


# --- server-side validation --------------------------------------------------

async def test_the_server_rejects_with_the_desktop_validator_wording(client, invite_code):
    """The error a user sees is the shared validator's, proving it really ran."""
    await sign_up(client, invite_code, "invalid@example.com")
    document = load_real("profile_selection")

    broken = copy.deepcopy(document)
    broken["overrides_by_profile"]["generic-xbox"]["zone.leftHandUpper"] = {
        "action": {"type": "gamepad", "target": "Z", "behavior": "hold"}}

    response = await create(client, "profile_selection", broken)
    assert response.status_code == 422
    assert "不支持的 Xbox 按键：Z" in response.json()["detail"]


@pytest.mark.parametrize("doc_type, document, fragment", [
    ("profile_selection",
     {"schema": "motioncontrol.profile_selection.v99", "selected_id": "x",
      "overrides_by_profile": {}},
     "unsupported selection schema"),
    ("motion_mappings",
     {"motions": [{"id": "squat", "name": "下蹲", "enabled": True,
                   "type": "gamepad", "target": "NOPE"}]},
     "Xbox 按键不支持"),
    ("voice_mappings",
     {"mappings": [{"phrase": "开火", "type": "keyboard", "target": "NOPE"}]},
     "不支持的键盘键"),
])
async def test_invalid_documents_are_refused(client, invite_code, doc_type,
                                             document, fragment):
    await sign_up(client, invite_code, f"bad-{doc_type}@example.com")
    response = await create(client, doc_type, document)
    assert response.status_code == 422, response.text
    assert fragment in response.json()["detail"]


async def test_unknown_keys_are_dropped_rather_than_stored(client, invite_code):
    """Normalisation output is what gets stored, so nothing can be smuggled in."""
    await sign_up(client, invite_code, "smuggle@example.com")
    document = load_real("profile_selection")

    tampered = copy.deepcopy(document)
    tampered["evil_extra_key"] = {"run": "rm -rf /"}

    profile = (await create(client, "profile_selection", tampered)).json()
    response = await client.get(
        f"/api/v1/profiles/{profile['id']}/versions/"
        f"{profile['current_version']['id']}/download")
    assert b"evil_extra_key" not in response.content
    # And the extra key made no difference to the identity of the document.
    assert (profile["current_version"]["canonical_sha256"]
            == canonicalize("profile_selection", document).sha256)


# --- access control ----------------------------------------------------------

async def test_a_private_config_is_invisible_to_everyone_else(client, invite_code):
    await sign_up(client, invite_code, "owner@example.com")
    profile = (await create(client, "motion_mappings",
                            load_real("motion_mappings"))).json()
    await client.post("/api/v1/auth/logout")

    response = await client.get(f"/api/v1/profiles/{profile['id']}")
    # 404 rather than 403: a 403 would confirm that this id exists.
    assert response.status_code == 404


async def test_writing_requires_being_signed_in(client):
    response = await create(client, "motion_mappings", {"motions": []})
    assert response.status_code == 401


async def test_a_public_config_is_readable_and_listed(client, invite_code):
    await sign_up(client, invite_code, "sharer@example.com")
    profile = (await create(client, "motion_mappings", load_real("motion_mappings"),
                            visibility="public")).json()
    await client.post("/api/v1/auth/logout")

    response = await client.get(f"/api/v1/profiles/{profile['id']}")
    assert response.status_code == 200
    assert response.json()["owner_name"] == "玩家一号"

    response = await client.get("/api/v1/public/profiles?doc_type=motion_mappings")
    assert profile["id"] in [item["id"] for item in response.json()]


# --- rate limiting -----------------------------------------------------------

async def test_registration_is_rate_limited(client, invite_code):
    """The limit is what stands in for abuse handling, so it has to actually bite."""
    last = None
    for index in range(7):
        last = await client.post("/api/v1/auth/register", json={
            "invite_code": invite_code,
            "email": f"flood{index}@example.com",
            "password": "a-long-enough-passphrase",
            "display_name": "flood",
        })
        if last.status_code == 429:
            break
    assert last.status_code == 429
    assert last.headers.get("retry-after", "").isdigit()


async def test_failed_logins_count_towards_the_limit(client, invite_code):
    """The whole point of the limiter, and easy to break without noticing.

    The counter lives outside the request transaction. If it did not, a wrong
    password -- which raises, and therefore rolls the request back -- would roll
    the increment back too, and password guessing would be unlimited.
    """
    await sign_up(client, invite_code, "guessme@example.com")
    await client.post("/api/v1/auth/logout")

    codes = []
    for _ in range(12):
        response = await client.post("/api/v1/auth/login", json={
            "email": "guessme@example.com", "password": "definitely-wrong"})
        codes.append(response.status_code)
        if response.status_code == 429:
            break
    assert 429 in codes, f"guessing was never rate-limited: {codes}"
    assert codes.count(401) <= 10
