"""玩家自助邀请码及其防滥用规则。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from cloud.app.db import SessionLocal, utcnow
from cloud.app.models import Invite, User
from cloud.app.security import token_digest


pytestmark = pytest.mark.asyncio


async def _register(client, invite_code: str, email: str = "inviter@example.com") -> dict:
    response = await client.post("/api/v1/auth/register", json={
        "invite_code": invite_code,
        "email": email,
        "password": "a-long-enough-passphrase",
        "display_name": "邀请人",
    })
    assert response.status_code == 201, response.text
    return response.json()


async def _clear_click_cooldown(user_id: str) -> None:
    async with SessionLocal() as db:
        user = await db.get(User, user_id)
        assert user is not None
        user.last_invite_created_at = utcnow() - timedelta(seconds=11)
        await db.commit()


async def test_invite_endpoints_require_login(client):
    assert (await client.get("/api/v1/auth/invites/status")).status_code == 401
    assert (await client.post("/api/v1/auth/invites")).status_code == 401


async def test_new_account_can_create_immediately(client, invite_code):
    await _register(client, invite_code, "invite-new@example.com")

    response = await client.get("/api/v1/auth/invites/status")
    assert response.status_code == 200
    body = response.json()
    assert body["can_create"] is True
    assert body["daily_limit"] == 3
    assert body["expires_days"] == 7


async def test_player_creates_one_working_invite(client, invite_code):
    player = await _register(client, invite_code, "invite-working@example.com")

    response = await client.post("/api/v1/auth/invites")
    assert response.status_code == 201, response.text
    created = response.json()
    assert len(created["code"]) >= 32
    assert created["note"] == "玩家自助生成"

    async with SessionLocal() as db:
        stored = (await db.execute(
            select(Invite).where(Invite.code_hash == token_digest(created["code"]))
        )).scalar_one()
        player_row = await db.get(User, player["id"])
        assert stored.created_by == player["id"]
        assert stored.used_at is None
        assert stored.expires_at is not None
        assert player_row is not None and player_row.last_invite_created_at is not None

    await client.post("/api/v1/auth/logout")
    invited = await _register(client, created["code"], "friend@example.com")
    assert invited["display_name"] == "邀请人"


async def test_player_cannot_generate_again_during_cooldown(client, invite_code):
    player = await _register(client, invite_code, "invite-cooldown@example.com")

    first = await client.post("/api/v1/auth/invites")
    assert first.status_code == 201, first.text
    second = await client.post("/api/v1/auth/invites")
    assert second.status_code == 429

    status = (await client.get("/api/v1/auth/invites/status")).json()
    assert status["can_create"] is False

    async with SessionLocal() as db:
        count = (await db.execute(
            select(func.count()).select_from(Invite)
            .where(Invite.created_by == player["id"])
        )).scalar_one()
        assert count == 1


async def test_player_can_create_three_per_day_but_not_four(client, invite_code):
    player = await _register(client, invite_code, "invite-daily@example.com")

    for index in range(3):
        response = await client.post("/api/v1/auth/invites")
        assert response.status_code == 201, response.text
        if index < 2:
            await _clear_click_cooldown(player["id"])

    await _clear_click_cooldown(player["id"])
    response = await client.post("/api/v1/auth/invites")
    assert response.status_code == 429
    assert "24 小时内最多生成 3 个" in response.json()["detail"]
