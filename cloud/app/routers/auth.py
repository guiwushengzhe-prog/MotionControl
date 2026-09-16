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

from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import select

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
from ..schemas import LoginRequest, RegisterRequest, UserOut
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

# A dummy hash to verify against when the address is unknown, so a miss and a
# hit take the same time. Computed once at import; the password never matches.
_DECOY_HASH = hash_password(new_token())


async def _primary_email(db, user: User) -> str:
    row = (await db.execute(
        select(UserEmail).where(UserEmail.user_id == user.id,
                                UserEmail.is_primary.is_(True)).limit(1)
    )).scalar_one_or_none()
    return row.email if row else ""


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
