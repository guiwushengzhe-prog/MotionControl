"""电脑登录账号（设备码）和运动记录同步。"""

from __future__ import annotations

import uuid
from datetime import timedelta

import httpx
import pytest

from cloud.app.db import SessionLocal, utcnow
from cloud.app.main import app
from cloud.app.models import DeviceAuthorization
from cloud.app.security import token_digest

pytestmark = pytest.mark.asyncio


async def _register(client, invite_code: str) -> dict:
    response = await client.post("/api/v1/auth/register", json={
        "invite_code": invite_code,
        "email": f"{uuid.uuid4().hex[:12]}@example.com",
        "password": "a-long-enough-passphrase",
        "display_name": "练习的人",
    })
    assert response.status_code == 201, response.text
    return response.json()


async def _pc():
    """电脑那边：没有网页的 Cookie，只带自己的凭证。"""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _login_pc(client, invite_code) -> str:
    await _register(client, invite_code)
    async with await _pc() as pc:
        started = (await pc.post("/api/v1/device/authorize", json={"name": "MotionControl · 测试机"})).json()
        assert (await pc.post("/api/v1/device/token", json={"device_code": started["device_code"]})).json()["status"] == "pending"
        shown = await client.get("/api/v1/device/pending", params={"code": started["user_code"].lower().replace("-", "")})
        assert shown.json()["name"] == "MotionControl · 测试机" and shown.json()["scopes"] == ["fitness"]
        assert (await client.post("/api/v1/device/approve", json={"user_code": started["user_code"]})).status_code == 200
        got = (await pc.post("/api/v1/device/token", json={"device_code": started["device_code"]})).json()
        assert got["status"] == "approved" and got["display_name"] == "练习的人"
        again = await pc.post("/api/v1/device/token", json={"device_code": started["device_code"]})
        assert again.status_code == 410, "凭证只发一次"
        return got["access_token"]


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _session(session_id="s1", **override) -> dict:
    doc = {"session_id": session_id, "status": "active", "started_at_ms": 1_000, "updated_at_ms": 2_000,
           "elapsed_seconds": 60, "active_seconds": 50, "steps": 10, "action_count": 3, "estimated_kcal": 4.0,
           "days": {"2026-10-08": {"active_seconds": 50, "steps": 10, "action_count": 3, "estimated_kcal": 4.0}}}
    doc.update(override)
    return doc


async def test_a_pc_logs_in_through_the_browser_and_gets_a_sync_only_token(client, invite_code):
    token = await _login_pc(client, invite_code)
    async with await _pc() as pc:
        me = await pc.get("/api/v1/device/me", headers=_bearer(token))
        assert me.json() == {"display_name": "练习的人", "scopes": ["fitness"]}
        # 网页的接口不认电脑的凭证：它只能同步运动记录。
        assert (await pc.get("/api/v1/auth/me", headers=_bearer(token))).status_code == 401
        assert (await pc.post("/api/v1/device/logout", headers=_bearer(token))).status_code == 204
        assert (await pc.get("/api/v1/fitness", headers=_bearer(token))).status_code == 401


async def test_approving_needs_the_website_login_and_a_live_code(client, invite_code):
    async with await _pc() as pc:
        started = (await pc.post("/api/v1/device/authorize", json={})).json()
        assert (await pc.post("/api/v1/device/approve", json={"user_code": started["user_code"]})).status_code == 401
    await _register(client, invite_code)
    assert (await client.post("/api/v1/device/approve", json={"user_code": "AAAA-AAAA"})).status_code == 404
    async with SessionLocal() as db:
        row = await db.get(DeviceAuthorization, token_digest(started["device_code"]))
        row.expires_at = utcnow() - timedelta(seconds=1)
        await db.commit()
    assert (await client.post("/api/v1/device/approve", json={"user_code": started["user_code"]})).status_code == 404


async def test_unknown_scopes_are_refused(client):
    response = await client.post("/api/v1/device/authorize", json={"scopes": ["profiles", "fitness"]})
    assert response.status_code == 422


async def test_sessions_merge_and_a_finished_one_stays_finished(client, invite_code):
    token = await _login_pc(client, invite_code)
    async with await _pc() as pc:
        push = lambda *docs, checkins=(): pc.post("/api/v1/fitness/sessions", headers=_bearer(token),
                                                   json={"sessions": list(docs), "checkins": list(checkins)})
        assert (await push(_session(), checkins=["2026-10-08"])).json() == {"accepted": 1, "changed": 1}
        finished = _session(status="finished", updated_at_ms=3_000, ended_at_ms=3_000, steps=15,
                            hr_avg=121, hr_max=150, hr_seconds=50, hr_curve=[[0, 118], [30, 124]],
                            kcal_source="heart_rate")
        await push(finished)
        # 迟到的旧推送：不复活、不减少。同一份再传一遍什么都不变。
        assert (await push(_session(updated_at_ms=1_500, steps=4))).json()["changed"] == 0
        assert (await push(finished)).json()["changed"] == 0
        pulled = (await pc.get("/api/v1/fitness", headers=_bearer(token))).json()
        [session] = pulled["sessions"]
        assert session["status"] == "finished" and session["steps"] == 15
        assert session["hr_curve"] == [[0, 118.0], [30, 124.0]] and session["kcal_source"] == "heart_rate"
        assert pulled["checkins"] == ["2026-10-08"]
        assert "hr_bucket_n" not in session


async def test_pulling_pages_through_a_batch_without_losing_any(client, invite_code, monkeypatch):
    from cloud.app.routers import fitness
    monkeypatch.setattr(fitness, "PAGE", 3)
    token = await _login_pc(client, invite_code)
    async with await _pc() as pc:
        docs = [_session(f"s{index:02d}") for index in range(7)]
        await pc.post("/api/v1/fitness/sessions", headers=_bearer(token), json={"sessions": docs})
        seen, cursor = [], ""
        for _ in range(5):
            page = (await pc.get("/api/v1/fitness", headers=_bearer(token), params={"cursor": cursor})).json()
            seen += [item["session_id"] for item in page["sessions"]]
            cursor = page["cursor"]
            if not page["more"]:
                break
        assert sorted(seen) == [doc["session_id"] for doc in docs]
        later = (await pc.get("/api/v1/fitness", headers=_bearer(token), params={"cursor": cursor})).json()
        assert later["sessions"] == [] and later["cursor"] == cursor


async def test_the_profile_keeps_whichever_device_changed_it_last(client, invite_code):
    token = await _login_pc(client, invite_code)
    profile = {"weight_kg": 62.5, "goal_active_minutes": 20, "goal_steps": 2000, "goal_kcal": 100,
               "primary_goal": "minutes", "age": 34, "sex": "female", "updated_at_ms": 5_000}
    async with await _pc() as pc:
        put = lambda doc: pc.put("/api/v1/fitness/profile", headers=_bearer(token), json={"profile": doc})
        assert (await put(profile)).json()["profile"]["age"] == 34
        stale = (await put({**profile, "weight_kg": 80, "updated_at_ms": 4_000})).json()
        assert stale["profile"]["weight_kg"] == 62.5
        assert (await put({**profile, "age": 200, "updated_at_ms": 6_000})).status_code == 422
        assert (await pc.get("/api/v1/fitness", headers=_bearer(token))).json()["profile"]["weight_kg"] == 62.5


async def test_bad_sessions_are_refused_whole(client, invite_code):
    token = await _login_pc(client, invite_code)
    async with await _pc() as pc:
        response = await pc.post("/api/v1/fitness/sessions", headers=_bearer(token),
                                 json={"sessions": [_session(), _session("s2", hr_avg=400)]})
        assert response.status_code == 422
        assert (await pc.get("/api/v1/fitness", headers=_bearer(token))).json()["sessions"] == []
