"""The config lifecycle: upload, edit, history, rollback, download.

Every write goes through ``canonicalize`` from ``motioncontrol_shared``. That
is the whole point of the shared package: the cloud does not have its own idea
of what a valid config is, it runs the desktop's validator, and what gets
stored is the validator's output rather than what the client sent. A client
cannot widen the rules by sending something unusual, and it cannot smuggle a
key through by adding it -- normalisation drops anything it does not know.

Versions are immutable. Editing writes a new row; rollback writes a new row
holding an old row's bytes. Nothing ever rewrites a stored payload, so a
version id always means the same bytes and a link to one stays honest.

Concurrent edits are detected, never merged. A client sends the version it
started from; if that is no longer current, the write is refused with 409 and
the client is told what it missed. Silently overwriting would lose work
without anyone finding out, which is worse than an error.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Path, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from motioncontrol_shared.canonical import canonicalize
from motioncontrol_shared.describe import describe

from ..db import utcnow
from ..deps import CurrentUser, DbSession, MaybeUser
from ..models import Game, Profile, ProfileVersion, User
from ..schemas import (
    CreateProfileRequest,
    NewVersionRequest,
    ProfileOut,
    RollbackRequest,
    UpdateProfileRequest,
    VersionOut,
)
from ..settings import get_settings

router = APIRouter(tags=["profiles"])

ProfileId = Annotated[str, Path(max_length=36)]


def _version_out(version: ProfileVersion) -> VersionOut:
    return VersionOut(
        id=version.id,
        revision_no=version.revision_no,
        schema_version=version.schema_version,
        canonical_sha256=version.canonical_sha256,
        size_bytes=version.size_bytes,
        parent_version_id=version.parent_version_id,
        restored_from_id=version.restored_from_id,
        note=version.note,
        created_at=version.created_at,
    )


async def _profile_out(db: AsyncSession, profile: Profile) -> ProfileOut:
    owner = await db.get(User, profile.owner_id)
    game = await db.get(Game, profile.game_id) if profile.game_id else None
    current = (await db.get(ProfileVersion, profile.current_version_id)
               if profile.current_version_id else None)
    return ProfileOut(
        id=profile.id,
        doc_type=profile.doc_type,
        game_id=profile.game_id,
        game_name=game.name if game else None,
        title=profile.title,
        summary=profile.summary,
        visibility=profile.visibility,
        owner_id=profile.owner_id,
        owner_name=owner.display_name if owner else "",
        current_version=_version_out(current) if current else None,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


def _validate_document(doc_type: str, document) -> tuple[bytes, str, str]:
    """Run the shared validator and enforce the size cap on its *output*.

    Checking the normalised bytes rather than the request body is the right
    order: the body can be padded with whitespace and unknown keys that
    normalisation throws away, so a cap on the raw upload would reject configs
    that are actually well within it.
    """
    try:
        canonical = canonicalize(doc_type, document)
    except ValueError as exc:
        # The message is the desktop validator's own Chinese wording, e.g.
        # "不支持的 Xbox 按键：Z". It is what the user needs to read, so it is
        # passed through rather than replaced with a generic failure.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
    limit = get_settings().max_bundle_bytes
    if len(canonical.payload) > limit:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            f"配置过大：{len(canonical.payload)} 字节，上限 {limit}")
    return canonical.payload, canonical.sha256, str(canonical.data.get("schema", ""))


async def _load_owned(db: AsyncSession, profile_id: str, user: User) -> Profile:
    profile = await db.get(Profile, profile_id)
    # One message for "does not exist" and "is not yours", so the API does not
    # confirm the existence of other people's private configs.
    if profile is None or profile.owner_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "配置不存在")
    return profile


async def _load_readable(db: AsyncSession, profile_id: str, user: User | None) -> Profile:
    profile = await db.get(Profile, profile_id)
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "配置不存在")
    if user is not None and profile.owner_id == user.id:
        return profile
    if profile.visibility in {"public", "unlisted"} and profile.moderation_status == "clean":
        return profile
    raise HTTPException(status.HTTP_404_NOT_FOUND, "配置不存在")


async def _append_version(db: AsyncSession, profile: Profile, *, payload: bytes,
                          sha256: str, schema_version: str, author: User,
                          parent_id: str | None, restored_from_id: str | None,
                          note: str) -> ProfileVersion:
    next_no = (await db.execute(
        select(func.coalesce(func.max(ProfileVersion.revision_no), 0))
        .where(ProfileVersion.profile_id == profile.id)
    )).scalar_one() + 1
    version = ProfileVersion(
        profile_id=profile.id,
        revision_no=next_no,
        doc_type=profile.doc_type,
        schema_version=schema_version,
        payload=payload,
        canonical_sha256=sha256,
        size_bytes=len(payload),
        parent_version_id=parent_id,
        restored_from_id=restored_from_id,
        note=note.strip(),
        created_by=author.id,
    )
    db.add(version)
    try:
        await db.flush()
    except IntegrityError:
        # uq_profile_revision fired: another request took this number between
        # the max() above and the insert. The database settling it is the point
        # of the constraint -- the caller retries against the new current.
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "配置已被另一处修改，请刷新后重试") from None
    profile.current_version_id = version.id
    profile.updated_at = utcnow()
    return version


@router.post("/profiles", response_model=ProfileOut, status_code=status.HTTP_201_CREATED)
async def create_profile(payload: CreateProfileRequest, user: CurrentUser,
                         db: DbSession) -> ProfileOut:
    blob, sha256, schema_version = _validate_document(payload.doc_type, payload.document)
    if payload.game_id and await db.get(Game, payload.game_id) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"未知游戏：{payload.game_id}")

    profile = Profile(
        owner_id=user.id,
        doc_type=payload.doc_type,
        game_id=payload.game_id or None,
        title=payload.title.strip(),
        summary=payload.summary.strip(),
        visibility=payload.visibility,
    )
    db.add(profile)
    await db.flush()
    await _append_version(db, profile, payload=blob, sha256=sha256,
                          schema_version=schema_version, author=user,
                          parent_id=None, restored_from_id=None, note=payload.note)
    return await _profile_out(db, profile)


@router.get("/profiles", response_model=list[ProfileOut])
async def list_my_profiles(user: CurrentUser, db: DbSession,
                           doc_type: str | None = Query(default=None, max_length=32),
                           limit: int = Query(default=50, ge=1, le=200)) -> list[ProfileOut]:
    query = select(Profile).where(Profile.owner_id == user.id)
    if doc_type:
        query = query.where(Profile.doc_type == doc_type)
    rows = (await db.execute(
        query.order_by(Profile.updated_at.desc()).limit(limit)
    )).scalars().all()
    return [await _profile_out(db, row) for row in rows]


@router.get("/profiles/{profile_id}", response_model=ProfileOut)
async def get_profile(profile_id: ProfileId, user: MaybeUser, db: DbSession) -> ProfileOut:
    return await _profile_out(db, await _load_readable(db, profile_id, user))


@router.patch("/profiles/{profile_id}", response_model=ProfileOut)
async def update_profile(profile_id: ProfileId, payload: UpdateProfileRequest,
                         user: CurrentUser, db: DbSession) -> ProfileOut:
    """Metadata only. The document is changed by adding a version, never here."""
    profile = await _load_owned(db, profile_id, user)
    if payload.title is not None:
        profile.title = payload.title.strip()
    if payload.summary is not None:
        profile.summary = payload.summary.strip()
    if payload.visibility is not None:
        profile.visibility = payload.visibility
    if payload.game_id is not None:
        if payload.game_id and await db.get(Game, payload.game_id) is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"未知游戏：{payload.game_id}")
        profile.game_id = payload.game_id or None
    profile.updated_at = utcnow()
    return await _profile_out(db, profile)


@router.get("/profiles/{profile_id}/versions", response_model=list[VersionOut])
async def list_versions(profile_id: ProfileId, user: MaybeUser, db: DbSession,
                        limit: int = Query(default=50, ge=1, le=200)) -> list[VersionOut]:
    profile = await _load_readable(db, profile_id, user)
    rows = (await db.execute(
        select(ProfileVersion).where(ProfileVersion.profile_id == profile.id)
        .order_by(ProfileVersion.revision_no.desc()).limit(limit)
    )).scalars().all()
    return [_version_out(row) for row in rows]


@router.post("/profiles/{profile_id}/versions", response_model=VersionOut,
             status_code=status.HTTP_201_CREATED)
async def add_version(profile_id: ProfileId, payload: NewVersionRequest,
                      user: CurrentUser, db: DbSession) -> VersionOut:
    profile = await _load_owned(db, profile_id, user)
    if payload.base_version_id != profile.current_version_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "配置已被另一处修改。请先取回最新版本，再重新提交你的改动。"
            f"（你基于 {payload.base_version_id or '无'}，当前是 {profile.current_version_id}）",
        )

    blob, sha256, schema_version = _validate_document(profile.doc_type, payload.document)
    current = (await db.get(ProfileVersion, profile.current_version_id)
               if profile.current_version_id else None)
    if current is not None and current.canonical_sha256 == sha256:
        # Identical after normalisation, so there is nothing to record. This is
        # why the hash is taken over the canonical form: a re-save that only
        # reordered keys or changed whitespace does not become a version.
        return _version_out(current)

    version = await _append_version(db, profile, payload=blob, sha256=sha256,
                                    schema_version=schema_version, author=user,
                                    parent_id=profile.current_version_id,
                                    restored_from_id=None, note=payload.note)
    return _version_out(version)


@router.post("/profiles/{profile_id}/rollback", response_model=VersionOut,
             status_code=status.HTTP_201_CREATED)
async def rollback(profile_id: ProfileId, payload: RollbackRequest,
                   user: CurrentUser, db: DbSession) -> VersionOut:
    """Restore an old version's content as a *new* version.

    Deleting the versions in between would be the other way to do this, and it
    would destroy the record of what was tried. Going forward to reach an
    earlier state keeps the history a complete account of what happened.
    """
    profile = await _load_owned(db, profile_id, user)
    source = await db.get(ProfileVersion, payload.version_id)
    if source is None or source.profile_id != profile.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "版本不存在")
    if source.id == profile.current_version_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该版本已经是当前版本")

    note = payload.note.strip() or f"回滚到 v{source.revision_no}"
    version = await _append_version(
        db, profile, payload=source.payload, sha256=source.canonical_sha256,
        schema_version=source.schema_version, author=user,
        parent_id=profile.current_version_id, restored_from_id=source.id, note=note)
    return _version_out(version)


@router.get("/profiles/{profile_id}/versions/{version_id}/download")
async def download_version(profile_id: ProfileId, version_id: ProfileId,
                           user: MaybeUser, db: DbSession) -> Response:
    """The stored bytes, unchanged.

    The response is the payload exactly as it was hashed at upload, so a client
    can verify the digest itself. That check is worth doing on the desktop:
    it is the only way to know the file that arrived is the file that was
    published, rather than trusting the transport and this service.
    """
    profile = await _load_readable(db, profile_id, user)
    version = await db.get(ProfileVersion, version_id)
    if version is None or version.profile_id != profile.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "版本不存在")
    filename = f"{profile.doc_type}-v{version.revision_no}.json"
    return Response(
        content=version.payload,
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-MotionControl-Sha256": version.canonical_sha256,
            "X-MotionControl-Schema": version.schema_version,
            "X-MotionControl-Revision": str(version.revision_no),
            # A version is immutable, so its bytes may be cached forever.
            "ETag": f'"{version.canonical_sha256}"',
            "Cache-Control": "private, max-age=31536000, immutable",
        },
    )


@router.get("/public/profiles", response_model=list[ProfileOut])
async def browse_public(db: DbSession,
                        game_id: str | None = Query(default=None, max_length=80),
                        doc_type: str | None = Query(default=None, max_length=32),
                        limit: int = Query(default=30, ge=1, le=100)) -> list[ProfileOut]:
    """Only ``public`` appears here. ``unlisted`` is reachable by link, not by search."""
    query = select(Profile).where(Profile.visibility == "public",
                                  Profile.moderation_status == "clean")
    if game_id:
        query = query.where(Profile.game_id == game_id)
    if doc_type:
        query = query.where(Profile.doc_type == doc_type)
    rows = (await db.execute(
        query.order_by(Profile.updated_at.desc()).limit(limit)
    )).scalars().all()
    return [await _profile_out(db, row) for row in rows]


@router.get("/profiles/{profile_id}/versions/{version_id}/summary")
async def version_summary(profile_id: ProfileId, version_id: ProfileId,
                          user: MaybeUser, db: DbSession) -> dict:
    """What this version actually does, derived from the stored document.

    Not the uploader's description. A person deciding whether to install
    someone else's config needs to know which keys it rebinds, and a free-text
    summary can be empty, stale, or simply wrong about its own contents. This
    is generated from the validated bytes, so it cannot disagree with them.

    Separate from the profile response on purpose: it parses the payload, and
    doing that for every row of a listing would be paid on every page load for
    something only the detail page shows.
    """
    profile = await _load_readable(db, profile_id, user)
    version = await db.get(ProfileVersion, version_id)
    if version is None or version.profile_id != profile.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "版本不存在")
    document = json.loads(version.payload.decode("utf-8"))
    return {"revision_no": version.revision_no, **describe(version.doc_type, document)}


@router.post("/profiles/{profile_id}/validate", status_code=status.HTTP_200_OK)
async def validate_only(profile_id: ProfileId, user: CurrentUser, db: DbSession,
                        document: Annotated[object, Body(embed=True)]) -> dict:
    """Check a document without storing it, so an editor can warn before saving."""
    profile = await _load_owned(db, profile_id, user)
    blob, sha256, schema_version = _validate_document(profile.doc_type, document)
    current = (await db.get(ProfileVersion, profile.current_version_id)
               if profile.current_version_id else None)
    return {
        "ok": True,
        "canonical_sha256": sha256,
        "schema_version": schema_version,
        "size_bytes": len(blob),
        "changed": current is None or current.canonical_sha256 != sha256,
    }
