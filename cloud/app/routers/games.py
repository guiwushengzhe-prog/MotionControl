"""The game library, read-only over the API.

Rows are seeded from ``game_profiles/catalog.json`` by ``cloud/tools/seed_games.py``
and keep the catalog's own ids, so a config uploaded from a desktop install
references a row that already exists. There is deliberately no write endpoint:
the library is part of the application, not user data, and letting it be edited
through the API would mean a config could reference a game the desktop has
never heard of.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from ..deps import DbSession
from ..models import Game, search_key
from ..schemas import GameOut

router = APIRouter(prefix="/games", tags=["games"])


@router.get("", response_model=list[GameOut])
async def list_games(db: DbSession,
                     q: str | None = Query(default=None, max_length=80),
                     limit: int = Query(default=50, ge=1, le=200)) -> list[GameOut]:
    query = select(Game)
    if q and q.strip():
        # Folded on both sides so "Red Dead" finds "reddead2". LIKE with a
        # leading wildcard cannot use an index, which is fine at 200 rows and
        # is the reason this is capped rather than paginated.
        needle = search_key(q)
        query = query.where(Game.search_key.like(f"%{needle}%"))
    rows = (await db.execute(query.order_by(Game.search_key).limit(limit))).scalars().all()
    return [GameOut(id=row.id, name=row.name) for row in rows]


@router.get("/{game_id}", response_model=GameOut)
async def get_game(game_id: str, db: DbSession) -> GameOut:
    row = await db.get(Game, game_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "游戏不存在")
    return GameOut(id=row.id, name=row.name)
