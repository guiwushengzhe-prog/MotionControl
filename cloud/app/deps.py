"""Request-scoped dependencies: who is calling, and may they call this often.

The session cookie is read here and nowhere else. Handlers take ``CurrentUser``
and never touch the cookie, so there is one place that decides what counts as
authenticated -- and one place to change when device tokens arrive alongside
browser sessions.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import SessionLocal, as_utc, get_session, utcnow
from .models import RateLimit, User, WebSession
from .security import keyed_digest, token_digest
from .settings import get_settings

DbSession = Annotated[AsyncSession, Depends(get_session)]

# Sliding refresh: touching last_seen_at on every request would be a write per
# read, so it is only rewritten once the recorded value is this stale.
_TOUCH_AFTER = timedelta(hours=1)


def session_cookie_name() -> str:
    """``__Host-`` in production, plain in development.

    The prefix is worth having -- it forces Secure, host-only and Path=/, so a
    subdomain cannot overwrite the cookie -- but a browser refuses a Secure
    cookie over plain http, which would make local development impossible.
    """
    return "__Host-mc_session" if get_settings().is_production else "mc_session"


def client_ip(request: Request) -> str:
    """The peer address, trusting no proxy header we have not been told to.

    uvicorn is started with ``--forwarded-allow-ips=127.0.0.1`` so that it
    rewrites ``request.client`` from ``X-Forwarded-For`` only when the hop
    really is the local reverse proxy. Reading the header here as well would
    undo that and let any caller pick their own rate-limit bucket.
    """
    return request.client.host if request.client else ""


async def _load_session(db: AsyncSession, raw_token: str) -> WebSession | None:
    row = await db.get(WebSession, token_digest(raw_token))
    if row is None or row.revoked_at is not None:
        return None
    expires = as_utc(row.expires_at)
    if expires is None or expires <= utcnow():
        return None
    return row


async def current_user_optional(request: Request, db: DbSession) -> User | None:
    token = request.cookies.get(session_cookie_name())
    if not token:
        return None
    row = await _load_session(db, token)
    if row is None:
        return None
    user = await db.get(User, row.user_id)
    if user is None or user.status != "active":
        return None
    now = utcnow()
    last_seen = as_utc(row.last_seen_at)
    if last_seen is None or now - last_seen > _TOUCH_AFTER:
        row.last_seen_at = now
    return user


async def current_user(
    user: Annotated[User | None, Depends(current_user_optional)],
) -> User:
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "请先登录")
    return user


CurrentUser = Annotated[User, Depends(current_user)]
MaybeUser = Annotated[User | None, Depends(current_user_optional)]


async def enforce_rate_limit(bucket: str, *, limit: int, window_seconds: int) -> None:
    """Fixed-window counter, raising 429 once *limit* is reached in a window.

    This deliberately runs in its own session rather than the request's. The
    request transaction rolls back whenever a handler raises -- which is
    exactly what a failed login does -- and that rollback would undo the
    counter increment along with everything else. The result would be a limiter
    that only counts *successful* attempts, i.e. one that does not limit
    password guessing at all, which is the single thing it most needs to do.

    Fixed windows allow up to twice the limit across a boundary, and two
    concurrent requests can read the same count and each write count+1. Both
    are real and both are acceptable here: this exists to make guessing and
    bulk signup impractical, not to meter a paid API, and the alternative costs
    a row per request or a lock on every call.
    """
    now = utcnow()
    async with SessionLocal() as db:
        row = await db.get(RateLimit, bucket)
        if row is None:
            db.add(RateLimit(bucket=bucket, window_start=now, count=1))
            await db.commit()
            return
        started = as_utc(row.window_start) or now
        if (now - started).total_seconds() >= window_seconds:
            row.window_start = now
            row.count = 1
            await db.commit()
            return
        if row.count >= limit:
            retry_after = int(window_seconds - (now - started).total_seconds()) + 1
            # Counted before refusing, so hammering the endpoint keeps it shut
            # rather than letting the window drain while the caller retries.
            row.count += 1
            await db.commit()
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "操作过于频繁，请稍后再试",
                headers={"Retry-After": str(retry_after)},
            )
        row.count += 1
        await db.commit()


def rate_limit_bucket(request: Request, name: str, subject: str = "") -> str:
    """One bucket per (endpoint, caller). Address is keyed, never stored raw."""
    who = subject or keyed_digest(client_ip(request))
    return f"{name}:{who}"[:160]


async def cleanup_expired_sessions(db: AsyncSession) -> int:
    """Delete sessions that can no longer authenticate anyone.

    Called on logout rather than from a scheduler: it is the one moment the
    table is already being written, and it keeps the service free of background
    jobs for now.
    """
    cutoff = utcnow()
    rows = (await db.execute(
        select(WebSession).where(WebSession.expires_at < cutoff).limit(500)
    )).scalars().all()
    for row in rows:
        await db.delete(row)
    return len(rows)
