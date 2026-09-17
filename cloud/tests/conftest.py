"""Test fixtures for the cloud service.

The database URL is set before anything from ``cloud.app`` is imported, because
``cloud.app.db`` builds its engine at import time from the cached settings. An
import that happened first would bind the tests to the developer's real
cloud.db and write test accounts into it.

The schema is built by running Alembic rather than ``Base.metadata.create_all``.
That is slower by a fraction of a second and buys something worth having: the
migration is what production will run, so a migration that has drifted from the
models fails here instead of on deployment day.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP_DIR = tempfile.mkdtemp(prefix="mc-cloud-tests-")
_DB_PATH = Path(_TMP_DIR) / "test.db"

os.environ["MC_DB_URL"] = f"sqlite+aiosqlite:///{_DB_PATH.as_posix()}"
os.environ["MC_SECRET_KEY"] = "test-key-not-a-secret"
os.environ["MC_ENV"] = "test"

import httpx  # noqa: E402
from sqlalchemy import delete  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from cloud.app.db import SessionLocal, engine  # noqa: E402
from cloud.app.main import app  # noqa: E402
from cloud.app.models import Game, Invite, RateLimit, search_key  # noqa: E402
from cloud.app.security import new_token, token_digest  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
# The real configuration this installation actually uses. Testing against
# hand-written miniatures would prove the API works on documents nobody has.
REAL_CONFIG_DIR = REPO_ROOT / "config"


@pytest.fixture(scope="session", autouse=True)
def _schema():
    config = Config(str(REPO_ROOT / "cloud" / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "cloud" / "migrations"))
    command.upgrade(config, "head")
    yield
    import asyncio
    asyncio.run(engine.dispose())


@pytest_asyncio.fixture
async def client():
    """An HTTP client wired straight to the ASGI app -- no socket, no port."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest_asyncio.fixture
async def invite_code() -> str:
    code = new_token()
    async with SessionLocal() as db:
        db.add(Invite(code_hash=token_digest(code), note="test"))
        await db.commit()
    return code


@pytest_asyncio.fixture(autouse=True)
async def _fresh_rate_limits():
    """Every test starts from an empty counter table.

    The limits themselves are left at their production values -- relaxing them
    for tests would mean the numbers that actually ship are never exercised.
    What has to go is the shared state: each test registers an account, and all
    of them arrive from the same client address, so without this the fourth
    test onwards would be rate-limited by the first three.
    """
    async with SessionLocal() as db:
        await db.execute(delete(RateLimit))
        await db.commit()
    yield


@pytest_asyncio.fixture
async def game() -> str:
    """一条真实的游戏记录。

    配置引用 game_id 时服务端会核对它存不存在——那是防止配置指向一个桌面端根本
    不认识的游戏。测试库跑的是迁移，不跑种子，所以要用到的那条得自己插。
    """
    ident = "steam-1659420-uncharted"
    async with SessionLocal() as db:
        if await db.get(Game, ident) is None:
            db.add(Game(id=ident, name="UNCHARTED", search_key=search_key("UNCHARTED")))
            await db.commit()
    return ident
