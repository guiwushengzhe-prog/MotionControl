"""Engine, session factory and the declarative base.

Two rules keep the PostgreSQL door open, and neither is enforced by the
library, so they are written down here instead:

* No dialect-specific SQL in application code. Not ``ON CONFLICT``, not
  ``RETURNING``, not a JSONB operator. If something can only be expressed one
  way on one backend, it belongs behind a function in this module.
* Every ``String`` column gets an explicit length. SQLite does not care;
  PostgreSQL rejects an unbounded ``VARCHAR`` in some positions and, more to
  the point, an unbounded column is almost always an unconsidered one.

Sessions are handed out by ``get_session`` as a FastAPI dependency so that one
request is one transaction: it commits when the handler returns and rolls back
when it raises. Handlers therefore never call ``commit`` themselves, which is
what keeps a half-written profile version from surviving a later failure.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .settings import get_settings


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    """Timezone-aware UTC, always.

    Naive datetimes are how a schedule ends up an hour wrong twice a year. The
    read side needs care too: SQLite has no timestamp type and hands back naive
    values regardless of what went in, so anything that leaves the database on
    its way to a client goes through ``as_utc``.
    """
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """Re-attach UTC to a value that lost it in storage."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _engine_kwargs(url: str) -> dict:
    if url.startswith("sqlite"):
        # SQLite's default is one writer at a time with an immediate "database
        # is locked"; a short wait turns a spurious failure into a brief pause.
        return {"connect_args": {"timeout": 15}}
    return {"pool_size": 5, "max_overflow": 5, "pool_pre_ping": True}


_settings = get_settings()
engine = create_async_engine(
    _settings.database_url,
    echo=False,
    future=True,
    **_engine_kwargs(_settings.database_url),
)

SessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,  # handlers read attributes after the commit
    autoflush=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """One request, one transaction."""
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()
