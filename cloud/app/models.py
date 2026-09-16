"""The tables.

Three ideas run through the whole schema and are worth stating once:

**A config version is immutable.** ``profile_versions`` rows are written and
never updated. Editing produces a new row; rolling back produces a new row
whose payload is copied from an old one. History is therefore always complete
and a version id always means the same bytes, which is what makes a share link
safe to hand to someone else.

**The stored payload is the canonical bytes, not parsed JSON.** The download
contract is that the file a user gets back hashes to the ``canonical_sha256``
recorded at upload. Storing bytes makes that true by construction; storing
JSONB would make it depend on re-serialising identically every time, forever.
``LargeBinary`` is also native on both backends, so no dialect branch. If
content-based search is wanted later, a derived JSONB column can be added
alongside -- an addition, not a rewrite.

**Nothing here is enumerable.** Ids are random UUID4 text rather than counters,
so a public share link does not tell its holder how many configs exist or let
them walk the neighbours.

Secrets follow the same rule as passwords: session tokens and invite codes are
stored as SHA-256 hashes, so a database dump does not let the reader log in as
anyone or claim an unused invite. They are high-entropy random strings rather
than user-chosen ones, so a plain hash is the right tool -- Argon2 exists to
make *guessing* expensive, and there is nothing here to guess.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, utcnow


def new_id() -> str:
    return str(uuid.uuid4())


# Lengths are stated once so a column and its foreign keys cannot drift apart.
ID_LEN = 36
HASH_LEN = 64           # hex SHA-256
EMAIL_LEN = 254         # RFC 5321 maximum
NAME_LEN = 120
SCHEMA_LEN = 64
DOC_TYPE_LEN = 32
GAME_ID_LEN = 80        # e.g. "steam-1659420-uncharted"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True, default=new_id)
    display_name: Mapped[str] = mapped_column(String(NAME_LEN))
    # Argon2id, including its parameters; rehashing on login is what lets the
    # cost be raised later without asking anyone to change their password.
    password_hash: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default="active")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    emails: Mapped[list[UserEmail]] = relationship(back_populates="user")


class UserEmail(Base):
    """Addresses live apart from the account from the start.

    Email verification and address changes are both scheduled work, and both
    need a row per address with its own verified state. Putting the address on
    ``users`` now would mean moving every account's data later; an empty second
    table costs nothing today.
    """

    __tablename__ = "user_emails"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(ID_LEN), ForeignKey("users.id"), index=True)
    # Lower-cased on the way in. Uniqueness has to hold over the normalised
    # form or two accounts could differ only in capitalisation.
    email: Mapped[str] = mapped_column(String(EMAIL_LEN), unique=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship(back_populates="emails")


class WebSession(Base):
    """Server-side opaque sessions, so a logout is actually a logout.

    A JWT cannot be revoked before it expires without keeping exactly this
    table anyway, and there are no mutually distrusting services here to
    federate between. The id *is* the SHA-256 of the cookie value, so the
    cookie never appears in the database.
    """

    __tablename__ = "web_sessions"

    id: Mapped[str] = mapped_column(String(HASH_LEN), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(ID_LEN), ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str] = mapped_column(String(255), default="")
    # Keyed HMAC, not a bare hash: the IPv4 space is small enough to enumerate
    # in seconds, so an unkeyed digest is a reversible record of where someone
    # logged in from.
    ip_hash: Mapped[str] = mapped_column(String(HASH_LEN), default="")


class Invite(Base):
    """Registration is invite-only until abuse handling exists."""

    __tablename__ = "invites"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True, default=new_id)
    code_hash: Mapped[str] = mapped_column(String(HASH_LEN), unique=True)
    note: Mapped[str] = mapped_column(String(NAME_LEN), default="")
    created_by: Mapped[str | None] = mapped_column(String(ID_LEN), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_by: Mapped[str | None] = mapped_column(String(ID_LEN), ForeignKey("users.id"))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Game(Base):
    """Seeded from game_profiles/catalog.json, keeping the existing ids.

    Reusing ``steam-1659420-uncharted`` rather than minting new numbers means a
    config uploaded from a desktop install references a row that is already
    there, with no id translation layer between the two sides.
    """

    __tablename__ = "games"

    id: Mapped[str] = mapped_column(String(GAME_ID_LEN), primary_key=True)
    name: Mapped[str] = mapped_column(String(NAME_LEN))
    # Folded for case- and space-insensitive search; see search_key().
    search_key: Mapped[str] = mapped_column(String(NAME_LEN), index=True)
    source: Mapped[str] = mapped_column(String(32), default="catalog")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Profile(Base):
    """One shareable document and its pointer to the current version.

    ``doc_type`` is one of the three names on the cloud sync whitelist. Keeping
    all three in one table rather than three tables is deliberate: they need
    identical machinery -- versions, rollback, download, ownership -- and the
    only thing that differs is which normaliser validates the payload.
    """

    __tablename__ = "profiles"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(String(ID_LEN), ForeignKey("users.id"), index=True)
    doc_type: Mapped[str] = mapped_column(String(DOC_TYPE_LEN))
    # Null for the two account-wide documents, which are not about one game.
    game_id: Mapped[str | None] = mapped_column(String(GAME_ID_LEN), ForeignKey("games.id"))
    title: Mapped[str] = mapped_column(String(NAME_LEN))
    summary: Mapped[str] = mapped_column(Text, default="")
    # No index of its own: ix_profiles_public leads with this column, so a
    # lookup by visibility alone already uses it and a second index would
    # only cost write time.
    visibility: Mapped[str] = mapped_column(String(16), default="private")
    moderation_status: Mapped[str] = mapped_column(String(16), default="clean")
    # Denormalised pointer: without it, showing a list of profiles costs one
    # extra query per row to find the newest version.
    current_version_id: Mapped[str | None] = mapped_column(String(ID_LEN))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_profiles_public", "visibility", "game_id"),
    )


class ProfileVersion(Base):
    """Written once, never updated.

    ``revision_no`` counts from 1 within one profile and is the number a person
    sees. It is deliberately not the primary key and deliberately not shared
    with either of the other two version concepts: ``/api/v1`` is the protocol,
    ``schema_version`` is the document's structure, and this is the user's
    revision. Collapsing any two of them makes one of them impossible to change
    on its own later.
    """

    __tablename__ = "profile_versions"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True, default=new_id)
    profile_id: Mapped[str] = mapped_column(String(ID_LEN), ForeignKey("profiles.id"), index=True)
    revision_no: Mapped[int] = mapped_column(Integer)
    doc_type: Mapped[str] = mapped_column(String(DOC_TYPE_LEN))
    # The document's own schema string, e.g. motioncontrol.profile_selection.v2.
    schema_version: Mapped[str] = mapped_column(String(SCHEMA_LEN))
    payload: Mapped[bytes] = mapped_column(LargeBinary)
    canonical_sha256: Mapped[str] = mapped_column(String(HASH_LEN), index=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    # The version this one was edited from. Null only for revision 1.
    parent_version_id: Mapped[str | None] = mapped_column(String(ID_LEN))
    # Set when this version was produced by rolling back, naming the version
    # whose bytes were copied. Keeping it distinct from parent_version_id is
    # what lets the history read as "restored v1" rather than a mystery edit.
    restored_from_id: Mapped[str | None] = mapped_column(String(ID_LEN))
    note: Mapped[str] = mapped_column(String(NAME_LEN), default="")
    created_by: Mapped[str] = mapped_column(String(ID_LEN), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        # Two writers racing on the same profile must not both become v4.
        # The unique index makes the database settle it rather than the app.
        UniqueConstraint("profile_id", "revision_no", name="uq_profile_revision"),
    )


class RateLimit(Base):
    """Fixed-window counters, in the database because there is no Redis.

    In Postgres this is a row lock per bucket; at the traffic this service will
    see that is not a bottleneck, and it has the property a per-process
    in-memory counter lacks: it still works with more than one worker.
    """

    __tablename__ = "rate_limits"

    bucket: Mapped[str] = mapped_column(String(160), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    count: Mapped[int] = mapped_column(Integer, default=0)


def search_key(name: str) -> str:
    """Fold a game name so search ignores case and spacing."""
    return "".join(str(name or "").split()).lower()
