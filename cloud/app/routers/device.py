"""电脑登录账号：电脑要一个码，人在浏览器里登录、点「允许」，电脑拿到同步凭证。

为什么不在电脑上直接输账号密码：电脑端是本机网页，密码经过它就多了一个能泄漏的
地方；也做不了网页上已有的那些（邀请码注册、以后的找回密码）。走浏览器，登录只有
网站这一处。

为什么不用跳回本机端口的授权码：要求电脑和浏览器在同一台机器上，还要把本机端口
登记成回调地址。这个做法（设备码，RFC 8628 那一类）只要电脑能连上网站。

    电脑   POST /device/authorize        → device_code（自己留着）、user_code、网址
    浏览器 GET  /device/pending?code=...  → 是哪台电脑在申请（要先登录网站）
    浏览器 POST /device/approve           → 允许
    电脑   POST /device/token             → 每隔几秒问一次，允许了就拿到凭证，只给一次
    电脑   GET  /device/me                → 现在登的是谁
    电脑   POST /device/logout            → 收回凭证
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from ..db import as_utc, utcnow
from ..deps import CurrentDevice, CurrentUser, DbSession, enforce_rate_limit, rate_limit_bucket
from ..models import DeviceAuthorization, DeviceToken, User
from ..security import new_token, token_digest
from ..settings import get_settings

router = APIRouter(prefix="/device", tags=["device"])

EXPIRES = timedelta(minutes=10)
POLL_INTERVAL_S = 3
# 现在只有运动记录。以后电脑要上传配置，就再加一个，旧凭证不会自动多出权限。
KNOWN_SCOPES = ("fitness",)
# 去掉了容易看错的 0/O、1/I/L、5/S、8/B。
_CODE_ALPHABET = "ACDEFGHJKMNPQRTUVWXY234679"


def _user_code() -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def _clean_code(code: str) -> str:
    raw = "".join(ch for ch in str(code or "").upper() if ch.isalnum())
    return f"{raw[:4]}-{raw[4:8]}" if len(raw) == 8 else ""


class AuthorizeRequest(BaseModel):
    name: str = Field(default="", max_length=120)
    scopes: list[str] = Field(default_factory=lambda: ["fitness"], max_length=8)


class TokenRequest(BaseModel):
    device_code: str = Field(min_length=20, max_length=200)


class ApproveRequest(BaseModel):
    user_code: str = Field(min_length=8, max_length=20)


@router.post("/authorize")
async def authorize(payload: AuthorizeRequest, request: Request, db: DbSession) -> dict:
    """电脑申请登录。不需要登录；同一个地址一小时最多 20 次。"""
    await enforce_rate_limit(rate_limit_bucket(request, "device-authorize"), limit=20, window_seconds=3600)
    scopes = sorted(set(payload.scopes))
    if not scopes or any(scope not in KNOWN_SCOPES for scope in scopes):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "申请的权限不认识")
    now = utcnow()
    # 顺手清掉过期的申请，表不会越长越大。
    await db.execute(delete(DeviceAuthorization).where(DeviceAuthorization.expires_at < now))
    device_code = new_token()
    for _ in range(5):
        user_code = _user_code()
        taken = await db.scalar(select(DeviceAuthorization).where(DeviceAuthorization.user_code == user_code))
        if taken is None:
            break
    else:  # pragma: no cover - 26^8 里撞五次
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "请稍后再试")
    db.add(DeviceAuthorization(id=token_digest(device_code), user_code=user_code, name=payload.name.strip(),
                               scopes=" ".join(scopes), created_at=now, expires_at=now + EXPIRES))
    await db.commit()
    origin = get_settings().site_origin.rstrip("/")
    return {
        "device_code": device_code,
        "user_code": user_code,
        "verification_uri": f"{origin}/device",
        "verification_uri_complete": f"{origin}/device?code={user_code}",
        "expires_in": int(EXPIRES.total_seconds()),
        "interval": POLL_INTERVAL_S,
    }


async def _pending(db, code: str) -> DeviceAuthorization:
    row = await db.scalar(select(DeviceAuthorization).where(DeviceAuthorization.user_code == _clean_code(code)))
    if row is None or (as_utc(row.expires_at) or utcnow()) <= utcnow() or row.consumed_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "这个码不对或者已经过期，请在电脑上重新点登录")
    return row


@router.get("/pending")
async def pending(code: str, user: CurrentUser, request: Request, db: DbSession) -> dict:
    """浏览器里先看一眼：是哪台电脑、要做什么。要先登录网站。"""
    await enforce_rate_limit(rate_limit_bucket(request, "device-pending", user.id), limit=30, window_seconds=600)
    row = await _pending(db, code)
    return {"user_code": row.user_code, "name": row.name, "scopes": row.scopes.split(),
            "approved": row.approved_by is not None}


@router.post("/approve")
async def approve(payload: ApproveRequest, user: CurrentUser, request: Request, db: DbSession) -> dict:
    await enforce_rate_limit(rate_limit_bucket(request, "device-approve", user.id), limit=30, window_seconds=600)
    row = await _pending(db, payload.user_code)
    if row.approved_by not in (None, user.id):
        raise HTTPException(status.HTTP_409_CONFLICT, "这台电脑已经由别的账号允许了")
    row.approved_by, row.approved_at = user.id, utcnow()
    await db.commit()
    return {"ok": True}


@router.post("/token")
async def token(payload: TokenRequest, request: Request, db: DbSession) -> dict:
    """电脑来问允许了没有。允许了就发凭证，只发一次。"""
    digest = token_digest(payload.device_code)
    await enforce_rate_limit(f"device-token:{digest}"[:160], limit=300, window_seconds=600)
    row = await db.get(DeviceAuthorization, digest)
    if row is None or row.consumed_at is not None or (as_utc(row.expires_at) or utcnow()) <= utcnow():
        raise HTTPException(status.HTTP_410_GONE, "登录申请已过期，请重新点登录")
    if row.approved_by is None:
        return {"status": "pending", "interval": POLL_INTERVAL_S}
    user = await db.get(User, row.approved_by)
    if user is None or user.status != "active":
        raise HTTPException(status.HTTP_410_GONE, "这个账号不能用了")
    raw = new_token()
    row.consumed_at = utcnow()
    db.add(DeviceToken(id=token_digest(raw), user_id=user.id, name=row.name, scopes=row.scopes))
    await db.commit()
    return {"status": "approved", "access_token": raw, "display_name": user.display_name,
            "scopes": row.scopes.split()}


@router.get("/me")
async def me(device: CurrentDevice, request: Request, db: DbSession) -> dict:
    await db.commit()  # 记下 last_used_at
    return {"display_name": request.state.device_user.display_name, "scopes": device.scopes.split()}


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(device: CurrentDevice, db: DbSession) -> None:
    device.revoked_at = utcnow()
    await db.commit()
