"""运动记录同步：电脑登录账号后，把身体数据和每次锻炼的摘要传上来，换台电脑再拉回去。

只收摘要（见 motioncontrol_shared.fitness_schema），规则和合并都在那里，这里只管存取。
同一次锻炼传几遍、乱序到达，合出来都一样；所以电脑断网攒着、联上再一起传，不会多算。

    GET  /fitness?cursor=...      身体数据、打卡日子、cursor 以后变过的锻炼
    PUT  /fitness/profile         身体数据，后改的留下
    POST /fitness/sessions        一批锻炼摘要和打卡日子
"""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_, select

from motioncontrol_shared import fitness_schema
from motioncontrol_shared.canonical import canonicalize

from ..db import as_utc, utcnow
from ..deps import DbSession, FitnessDevice, enforce_rate_limit, rate_limit_bucket
from ..models import FitnessCheckin, FitnessProfile, FitnessSession

router = APIRouter(prefix="/fitness", tags=["fitness"])

PAGE = 500


class ProfileRequest(BaseModel):
    profile: dict


class SessionsRequest(BaseModel):
    sessions: list[dict] = Field(default_factory=list, max_length=fitness_schema.MAX_SESSIONS_PER_UPLOAD)
    checkins: list[str] = Field(default_factory=list, max_length=fitness_schema.MAX_CHECKINS)


def _load(payload: bytes) -> dict:
    return json.loads(payload.decode("utf-8"))


def _cursor(value: str) -> tuple[datetime, str] | None:
    """「时刻|编号」。同一批传上来的锻炼时刻相同，只按时刻翻页会在页尾漏掉几条。"""
    if not value:
        return None
    moment, _, session_id = value.partition("|")
    try:
        return as_utc(datetime.fromisoformat(moment)), session_id
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "cursor 不对") from None


@router.get("")
async def pull(user: FitnessDevice, request: Request, db: DbSession, cursor: str = Query(default="", max_length=240)) -> dict:
    await enforce_rate_limit(rate_limit_bucket(request, "fitness-pull", user.id), limit=120, window_seconds=3600)
    since = _cursor(cursor)
    profile = await db.get(FitnessProfile, user.id)
    query = select(FitnessSession).where(FitnessSession.user_id == user.id)
    if since is not None:
        moment, session_id = since
        query = query.where(or_(FitnessSession.changed_at > moment,
                                and_(FitnessSession.changed_at == moment, FitnessSession.session_id > session_id)))
    rows = (await db.scalars(query.order_by(FitnessSession.changed_at, FitnessSession.session_id)
                             .limit(PAGE + 1))).all()
    more = len(rows) > PAGE
    rows = rows[:PAGE]
    checkins = (await db.scalars(select(FitnessCheckin.day).where(FitnessCheckin.user_id == user.id))).all()
    # 没有新东西就把 cursor 原样还回去；有就用最后一条的时刻，下次从它后面接着拉。
    next_cursor = f"{as_utc(rows[-1].changed_at).isoformat()}|{rows[-1].session_id}" if rows else (cursor or "")
    await db.commit()
    return {
        "profile": _load(profile.payload) if profile else None,
        "sessions": [_load(row.payload) for row in rows],
        "checkins": sorted(checkins),
        "cursor": next_cursor,
        "more": more,
    }


@router.put("/profile")
async def put_profile(body: ProfileRequest, user: FitnessDevice, request: Request, db: DbSession) -> dict:
    await enforce_rate_limit(rate_limit_bucket(request, "fitness-profile", user.id), limit=120, window_seconds=3600)
    try:
        doc = canonicalize("fitness_profile", body.profile)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
    row = await db.get(FitnessProfile, user.id)
    if row is None:
        db.add(FitnessProfile(user_id=user.id, payload=doc.payload, updated_at_ms=doc.data["updated_at_ms"]))
        stored = doc.data
    elif doc.data["updated_at_ms"] > row.updated_at_ms:
        row.payload, row.updated_at_ms, row.changed_at = doc.payload, doc.data["updated_at_ms"], utcnow()
        stored = doc.data
    else:
        # 云端这份是后改的：留着，告诉电脑以它为准。
        stored = _load(row.payload)
    await db.commit()
    return {"profile": stored}


@router.post("/sessions")
async def push_sessions(body: SessionsRequest, user: FitnessDevice, request: Request, db: DbSession) -> dict:
    await enforce_rate_limit(rate_limit_bucket(request, "fitness-push", user.id), limit=240, window_seconds=3600)
    try:
        incoming = [canonicalize("fitness_session", raw).data for raw in body.sessions]
        checkins = fitness_schema.normalize_checkins(body.checkins)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
    now = utcnow()
    changed = 0
    for doc in incoming:
        row = await db.get(FitnessSession, (user.id, doc["session_id"]))
        merged = fitness_schema.merge_session(_load(row.payload) if row else None, doc)
        payload = canonicalize("fitness_session", merged).payload
        if row is None:
            db.add(FitnessSession(user_id=user.id, session_id=doc["session_id"], payload=payload,
                                  started_at_ms=merged["started_at_ms"], changed_at=now))
            changed += 1
        elif payload != row.payload:
            row.payload, row.started_at_ms, row.changed_at = payload, merged["started_at_ms"], now
            changed += 1
    known = set((await db.scalars(select(FitnessCheckin.day).where(FitnessCheckin.user_id == user.id))).all())
    for day in checkins:
        if day not in known:
            db.add(FitnessCheckin(user_id=user.id, day=day, changed_at=now))
    await db.commit()
    return {"accepted": len(incoming), "changed": changed}
