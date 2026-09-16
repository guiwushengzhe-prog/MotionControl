"""Alembic environment.

The URL is read from the application's settings rather than alembic.ini, so
``MC_DB_URL`` is the single switch between the local SQLite file and the
server's database. Without that, a migration could be applied to a different
database than the one the app talks to -- which is a failure you discover
afterwards.

``render_as_batch`` is on because SQLite cannot ALTER a column: Alembic instead
rebuilds the table and copies the data. It is a no-op on PostgreSQL, so leaving
it on costs nothing and keeps one migration script working on both.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from cloud.app.db import Base
from cloud.app.settings import get_settings

# Importing the models is what populates Base.metadata; without it autogenerate
# would cheerfully report that every table should be dropped.
from cloud.app import models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)
target_metadata = Base.metadata


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
    )


def run_migrations_offline() -> None:
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
