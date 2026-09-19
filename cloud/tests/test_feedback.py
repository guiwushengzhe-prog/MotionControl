"""反馈入口：谁能写，谁能读。

写这些测试是因为两边都会出人命，方向相反。

写的那边：如果哪天有人顺手给这个接口加上"要登录"，反馈量会直接归零，而且不会
有任何报错——只会安静地再也收不到东西。这个软件是发网盘的，绝大多数用的人没有
账号（注册还要邀请码），真正卡住的新手最不可能为了说一句话去注册。

读的那边：这张表里有别人留下的邮箱和 QQ。权限判反了就是把它们公开。
"""

from __future__ import annotations

import itertools

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update

from cloud.app.db import SessionLocal
from cloud.app.models import Feedback, User

pytestmark = pytest.mark.asyncio

# 测试库跨用例不重置（见 conftest），所以邮箱不能重名，否则第二个用例注册时 409。
_emails = itertools.count()


@pytest_asyncio.fixture(autouse=True)
async def _empty_feedback():
    """每个用例从空表开始，这样"表里应该只有一条"才是可断言的。"""
    async with SessionLocal() as db:
        await db.execute(delete(Feedback))
        await db.commit()
    yield


async def send(client, **overrides):
    body = {"kind": "bug", "message": "手机连不上，防火墙我点了拒绝"}
    body.update(overrides)
    return await client.post("/api/v1/feedback", json=body)


async def sign_up(client, invite_code: str):
    response = await client.post("/api/v1/auth/register", json={
        "invite_code": invite_code,
        "email": f"player{next(_emails)}@example.com",
        "password": "a-long-enough-passphrase",
        "display_name": "玩家一号",
    })
    assert response.status_code == 201, response.text
    return response.json()


async def test_anyone_can_send_without_an_account(client):
    """这一条是整个功能的意义所在，别让它悄悄回归。"""
    response = await send(client)
    assert response.status_code == 201, response.text

    async with SessionLocal() as db:
        rows = (await db.execute(select(Feedback))).scalars().all()
    assert len(rows) == 1
    assert rows[0].user_id is None, "匿名提交不该凭空绑上一个账号"
    assert "防火墙" in rows[0].message


async def test_the_address_is_stored_as_a_digest_not_as_an_address(client):
    """这张表泄漏了，不该等于泄漏一串 IP。"""
    await send(client)
    async with SessionLocal() as db:
        row = (await db.execute(select(Feedback))).scalars().one()
    assert len(row.ip_hash) == 64, "应当是 sha256 十六进制"
    assert "." not in row.ip_hash and ":" not in row.ip_hash


async def test_contact_and_version_are_kept_as_written(client):
    """联系方式不校验格式：QQ 号也要能收下。"""
    response = await send(client, contact="QQ 12345678", app_version="2.0.0")
    assert response.status_code == 201
    async with SessionLocal() as db:
        row = (await db.execute(select(Feedback))).scalars().one()
    assert row.contact == "QQ 12345678"
    assert row.app_version == "2.0.0"


async def test_a_signed_in_sender_is_linked_to_their_account(client, invite_code):
    user = await sign_up(client, invite_code)
    assert (await send(client)).status_code == 201
    async with SessionLocal() as db:
        row = (await db.execute(select(Feedback))).scalars().one()
    assert row.user_id == user["id"]


@pytest.mark.parametrize("message", ["", "   ", "啊" * 4001])
async def test_empty_or_oversized_is_refused(client, message):
    assert (await send(client, message=message)).status_code == 422


async def test_an_unknown_kind_is_refused(client):
    assert (await send(client, kind="随便写的")).status_code == 422


async def test_a_stranger_cannot_read_other_peoples_contact_details(client):
    await send(client, contact="secret@example.com")
    response = await client.get("/api/v1/feedback")
    assert response.status_code == 404, "不该是 403：连这个端点存在都不必告诉他"
    assert "secret@example.com" not in response.text


async def test_an_ordinary_account_cannot_read_them_either(client, invite_code):
    await send(client, contact="secret@example.com")
    await sign_up(client, invite_code)
    response = await client.get("/api/v1/feedback")
    assert response.status_code == 404
    assert "secret@example.com" not in response.text


async def test_an_admin_can_read_them(client, invite_code):
    await send(client, contact="secret@example.com")
    user = await sign_up(client, invite_code)
    async with SessionLocal() as db:
        await db.execute(update(User).where(User.id == user["id"]).values(is_admin=True))
        await db.commit()

    response = await client.get("/api/v1/feedback")
    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["contact"] == "secret@example.com"
    assert rows[0]["kind"] == "bug"


async def test_flooding_is_cut_off(client):
    """一小时 10 条。真心写反馈的人一次写完就走。"""
    codes = [(await send(client, message=f"压测 {i}")).status_code for i in range(12)]
    assert codes.count(201) == 10
    assert codes[-1] == 429
