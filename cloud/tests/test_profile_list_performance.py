"""List size must not increase database round trips or load config payloads."""

import uuid

import pytest
from sqlalchemy import event

from cloud.app.db import SessionLocal, engine
from cloud.app.models import Game, Profile, ProfileVersion, User
from cloud.app.routers.profiles import browse_public, list_my_profiles


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 30, 100])
async def test_lists_load_metadata_in_at_most_four_queries(count):
    user = User(display_name="list-owner", password_hash="unused-test-hash")
    game_id = "list-test-" + uuid.uuid4().hex
    async with SessionLocal() as db:
        db.add_all([user, Game(id=game_id, name="list-game", search_key="list-game")])
        await db.flush()
        profiles = [Profile(owner_id=user.id, doc_type="motion_mappings", game_id=game_id,
                            title=f"list-{index}", visibility="public") for index in range(count)]
        db.add_all(profiles)
        await db.flush()
        for profile in profiles:
            version = ProfileVersion(profile_id=profile.id, revision_no=1,
                                     doc_type=profile.doc_type, schema_version="test.v1",
                                     payload=b"x" * 4096, canonical_sha256="a" * 64,
                                     size_bytes=4096, created_by=user.id)
            db.add(version)
            await db.flush()
            profile.current_version_id = version.id
        await db.commit()
        profile_ids = {profile.id for profile in profiles}

    for own in (False, True):
        statements = []

        def observed(_connection, _cursor, statement, _parameters, _context, _executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", observed)
        try:
            async with SessionLocal() as db:
                if own:
                    result = await list_my_profiles(user=user, db=db, doc_type=None, limit=count)
                else:
                    result = await browse_public(db=db, game_id=game_id,
                                                 doc_type="motion_mappings", limit=count)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", observed)
        assert len(result) == count
        assert {profile.id for profile in result} == profile_ids
        assert len(statements) <= 4, statements
        assert all("profile_versions.payload" not in statement for statement in statements)
        assert all(profile.owner_name == "list-owner" and profile.game_name == "list-game"
                   and profile.current_version.revision_no == 1 for profile in result)
