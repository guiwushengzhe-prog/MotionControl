"""Configuration, read from the environment with local-development defaults.

The database URL is one setting on purpose. This machine has neither Docker nor
a native PostgreSQL, so local work runs on SQLite while the server runs
PostgreSQL, and the only difference between them is this string. Everything
above it is written dialect-neutral so that stays true:

    MC_DB_URL=sqlite+aiosqlite:///./cloud.db                  (local)
    MC_DB_URL=postgresql+psycopg://user:pass@host/mccloud     (server)

That split is a compromise forced by the environment, not a preference. It
carries a real risk -- a dialect difference would surface on deployment day
rather than here -- so the Alembic migration has to be run against the real
PostgreSQL before the first release, not assumed to work.
"""

from __future__ import annotations

import os
import secrets
from functools import lru_cache
from pathlib import Path

CLOUD_ROOT = Path(__file__).resolve().parent.parent


class Settings:
    """Plain object rather than BaseSettings: fewer moving parts, same job."""

    def __init__(self) -> None:
        self.env = os.environ.get("MC_ENV", "dev").strip().lower()
        # as_posix(), not str(): on Windows a Path renders with backslashes,
        # which a sqlite URL does not accept.
        self.database_url = os.environ.get(
            "MC_DB_URL",
            f"sqlite+aiosqlite:///{(CLOUD_ROOT / 'cloud.db').as_posix()}",
        )
        # Sessions and ip hashing are keyed off this. A generated key means
        # every restart invalidates sessions, which is fine for development and
        # unacceptable in production -- hence the check in require_production.
        self.secret_key = os.environ.get("MC_SECRET_KEY", "")
        self.site_origin = os.environ.get("MC_SITE_ORIGIN", "http://127.0.0.1:8000")
        self.session_days = int(os.environ.get("MC_SESSION_DAYS", "30"))
        # Bundles are ~11 KB in practice. The cap is ~20x headroom, and it is
        # enforced in the app as well as in nginx: FastAPI does not limit body
        # size on its own.
        self.max_bundle_bytes = int(os.environ.get("MC_MAX_BUNDLE_BYTES", str(256 * 1024)))
        self.versions_kept = int(os.environ.get("MC_VERSIONS_KEPT", "20"))
        # Argon2id. Drop memory_cost to 32 MiB if the server has 1 GB of RAM,
        # or concurrent logins will thrash it.
        self.argon2_memory_cost = int(os.environ.get("MC_ARGON2_MEMORY_KIB", str(64 * 1024)))
        self.argon2_time_cost = int(os.environ.get("MC_ARGON2_TIME_COST", "3"))
        self.argon2_parallelism = int(os.environ.get("MC_ARGON2_PARALLELISM", "2"))

        if not self.secret_key:
            if self.is_production:
                raise RuntimeError(
                    "MC_SECRET_KEY must be set in production: without it every "
                    "restart would invalidate all sessions and change every ip hash."
                )
            self.secret_key = secrets.token_urlsafe(32)

    @property
    def is_production(self) -> bool:
        return self.env in {"prod", "production"}

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
