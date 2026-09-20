"""Registration, login, logout.

Registration is invite-only. That is not a growth strategy, it is the thing
standing in for abuse handling: there is no email verification yet, so an open
signup form is an open invitation to fill the database with accounts nobody
owns. When verification and reporting land, the invite check becomes optional
rather than being ripped out.

Two deliberate non-features:

* **Login does not say which half was wrong.** "该邮箱未注册" turns the login
  form into a test for whether an address has an account here.
* **A wrong email still costs an Argon2 verification.** Returning early would
  make a miss measurably faster than a hit, which is the same disclosure by a
  side channel.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import or_, select, update

from ..db import as_utc, utcnow
from ..deps import (
    CurrentUser,
    DbSession,
    cleanup_expired_sessions,
    client_ip,
    enforce_rate_limit,
    rate_limit_bucket,
    session_cookie_name,
)
from ..models import Invite, User, UserEmail, WebSession
from ..schemas import InviteOut, InviteStatusOut, LoginRequest, RegisterRequest, UserOut
from ..security import (
    hash_password,
    keyed_digest,
    needs_rehash,
    new_token,
    normalize_email,
    token_digest,
    verify_password,
)
from ..settings import get_settings

router = APIRouter(prefix="/auth", tags=["auth"])

_INVITE_BURST_COOLDOWN = timedelta(seconds=10)
_INVITE_WINDOW = timedelta(days=1)
_INVITE_DAILY_LIMIT = 3
_INVITE_LIFETIME = timedelta(days=7)

# A dummy hash to verify against when the address is unknown, so a miss and a
# hit take the same time. Computed once at import; the password never matches.
_DECOY_HASH = hash_password(new_token())


async def _primary_email(db, user: User) -> str:
    row = (await db.execute(
        select(UserEmail).where(UserEmail.user_id == user.id,
                                UserEmail.is_primary.is_(True)).limit(1)
    )).scalar_one_or_none()
    return row.email if row else ""


async def _next_invite_at(db, user: User, now: datetime) -> datetime:
    """玩家最早可再生成的时间：防连点间隔与每日上限取较晚者。"""
    next_at = now
    last_created = as_utc(user.last_invite_created_at)
    if last_created is not None:
        next_at = max(next_at, last_created + _INVITE_BURST_COOLDOWN)

    recent = (await db.execute(
        select(Invite.created_at)
        .where(Invite.created_by == user.id,
               Invite.created_at > now - _INVITE_WINDOW)
        .order_by(Invite.created_at.asc())
    )).scalars().all()
    if len(recent) >= _INVITE_DAILY_LIMIT:
        oldest = as_utc(recent[0]) or now
        next_at = max(next_at, oldest + _INVITE_WINDOW)
    return next_at


def _set_session_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=session_cookie_name(),
        value=token,
        max_age=settings.session_days * 24 * 3600,
        httponly=True,              # script cannot read it, so XSS cannot steal it
        secure=settings.is_production,
        samesite="lax",             # survives following a link in; blocks cross-site POST
        path="/",
    )


async def _open_session(db, request: Request, user: User) -> str:
    token = new_token()
    settings = get_settings()
    db.add(WebSession(
        id=token_digest(token),
        user_id=user.id,
        expires_at=utcnow() + timedelta(days=settings.session_days),
        user_agent=str(request.headers.get("user-agent", ""))[:255],
        ip_hash=keyed_digest(client_ip(request)),
    ))
    user.last_login_at = utcnow()
    return token


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, request: Request,
                   response: Response, db: DbSession) -> UserOut:
    await enforce_rate_limit(rate_limit_bucket(request, "register"),
                             limit=5, window_seconds=3600)

    invite = (await db.execute(
        select(Invite).where(Invite.code_hash == token_digest(payload.invite_code.strip()))
    )).scalar_one_or_none()
    expires = as_utc(invite.expires_at) if invite else None
    if invite is None or invite.used_at is not None or (expires and expires <= utcnow()):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "邀请码无效或已被使用")

    email = normalize_email(payload.email)
    taken = (await db.execute(
        select(UserEmail.id).where(UserEmail.email == email)
    )).scalar_one_or_none()
    if taken is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "该邮箱已注册")

    user = User(display_name=payload.display_name.strip(),
                password_hash=hash_password(payload.password))
    db.add(user)
    await db.flush()
    db.add(UserEmail(user_id=user.id, email=email, is_primary=True))
    invite.used_by = user.id
    invite.used_at = utcnow()

    _set_session_cookie(response, await _open_session(db, request, user))
    return UserOut(id=user.id, display_name=user.display_name, email=email,
                   is_admin=user.is_admin, created_at=user.created_at)


@router.post("/login", response_model=UserOut)
async def login(payload: LoginRequest, request: Request,
                response: Response, db: DbSession) -> UserOut:
    email = normalize_email(payload.email)
    # Two buckets: one stops a single address being hammered from anywhere,
    # the other stops one source spraying many addresses.
    await enforce_rate_limit(rate_limit_bucket(request, "login-email", keyed_digest(email)),
                             limit=10, window_seconds=900)
    await enforce_rate_limit(rate_limit_bucket(request, "login-ip"),
                             limit=30, window_seconds=900)

    row = (await db.execute(
        select(UserEmail, User).join(User, User.id == UserEmail.user_id)
        .where(UserEmail.email == email)
    )).first()
    user = row[1] if row else None

    if not verify_password(user.password_hash if user else _DECOY_HASH, payload.password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "邮箱或密码不正确")
    if user is None or user.status != "active":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "邮箱或密码不正确")

    # The password is in hand exactly here, so this is the only moment the
    # stored hash can be upgraded to stronger parameters.
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)

    _set_session_cookie(response, await _open_session(db, request, user))
    return UserOut(id=user.id, display_name=user.display_name, email=email,
                   is_admin=user.is_admin, created_at=user.created_at)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, db: DbSession) -> Response:
    token = request.cookies.get(session_cookie_name())
    if token:
        row = await db.get(WebSession, token_digest(token))
        if row is not None:
            # Revoked, not deleted: the row is the record that the session
            # existed, and sweeping expired ones below keeps the table bounded.
            row.revoked_at = utcnow()
    await cleanup_expired_sessions(db)
    response.delete_cookie(session_cookie_name(), path="/")
    return Response(status_code=status.HTTP_204_NO_CONTENT,
                    headers=dict(response.headers))


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser, db: DbSession) -> UserOut:
    return UserOut(id=user.id, display_name=user.display_name,
                   email=await _primary_email(db, user),
                   is_admin=user.is_admin, created_at=user.created_at)


@router.get("/invites/status", response_model=InviteStatusOut)
async def invite_status(user: CurrentUser, db: DbSession) -> InviteStatusOut:
    now = utcnow()
    next_at = await _next_invite_at(db, user, now)
    return InviteStatusOut(
        can_create=now >= next_at,
        next_available_at=next_at,
        daily_limit=_INVITE_DAILY_LIMIT,
        expires_days=_INVITE_LIFETIME.days,
    )


@router.post("/invites", response_model=InviteOut,
             status_code=status.HTTP_201_CREATED)
async def create_invite(request: Request, user: CurrentUser,
                        db: DbSession) -> InviteOut:
    """让真实玩家邀请朋友，同时把批量注册的扩散速度锁在账号级别。"""
    await enforce_rate_limit(
        rate_limit_bucket(request, "create-invite-user", user.id),
        limit=8, window_seconds=86400,
    )
    await enforce_rate_limit(
        rate_limit_bucket(request, "create-invite-ip"),
        limit=20, window_seconds=3600,
    )

    now = utcnow()
    next_at = await _next_invite_at(db, user, now)
    if now < next_at:
        retry_after = max(1, int((next_at - now).total_seconds()) + 1)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "24 小时内最多生成 3 个邀请码，请在页面显示的时间后重试",
            headers={"Retry-After": str(retry_after)},
        )

    # 条件更新是并发闸门。即使同一账号同时点两次，也只有一个请求能把时间更新
    # 到现在；另一个请求不会创建第二个邀请码。
    eligible_last = now - _INVITE_BURST_COOLDOWN
    result = await db.execute(
        update(User)
        .where(
            User.id == user.id,
            or_(User.last_invite_created_at.is_(None),
                User.last_invite_created_at <= eligible_last),
        )
        .values(last_invite_created_at=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "邀请码刚刚已生成，请勿重复操作",
            headers={"Retry-After": str(int(_INVITE_BURST_COOLDOWN.total_seconds()))},
        )

    code = new_token()
    expires_at = now + _INVITE_LIFETIME
    invite = Invite(
        code_hash=token_digest(code),
        note="玩家自助生成",
        created_by=user.id,
        expires_at=expires_at,
    )
    db.add(invite)
    await db.flush()
    return InviteOut(id=invite.id, code=code, note=invite.note,
                     expires_at=expires_at)
