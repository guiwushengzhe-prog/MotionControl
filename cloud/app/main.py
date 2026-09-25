"""The ASGI application.

Run it from the repository root, because the shared package lives there:

    uvicorn cloud.app.main:app --host 127.0.0.1 --port 8000

Two things this app deliberately does not do.

**It does not create or migrate the schema on startup.** Two workers booting at
once would race each other, and a migration that runs automatically is a
migration nobody reviewed before it touched production data. ``alembic upgrade
head`` is a separate, deliberate step; ``/api/v1/health`` reports whether the
tables are actually there so a bad deploy is visible immediately rather than on
the first request that needs a missing column.

**It does not bind a public interface by itself.** In production uvicorn binds
127.0.0.1 and Caddy owns 443. The bind address is the security boundary, the
same reasoning as the desktop's 8765/8766 split.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select

from .db import SessionLocal
from .models import Game, Profile
from .routers import app_update, auth, feedback, games, pose_library, profiles
from .settings import get_settings

settings = get_settings()

app = FastAPI(
    title="MotionControl Cloud",
    version="1.0.0",
    # /api/v1 is the protocol version and is fixed from the first release. It
    # is not the config schema version and not a config's revision number --
    # three separate things that must be able to move independently.
    docs_url="/api/v1/docs" if not settings.is_production else None,
    redoc_url=None,
    openapi_url="/api/v1/openapi.json" if not settings.is_production else None,
)

app.include_router(auth.router, prefix="/api/v1")
app.include_router(games.router, prefix="/api/v1")
app.include_router(profiles.router, prefix="/api/v1")
app.include_router(feedback.router, prefix="/api/v1")
app.include_router(app_update.router, prefix="/api/v1")
app.include_router(pose_library.router, prefix="/api/v1")


@app.middleware("http")
async def reject_oversized_bodies(request: Request, call_next):
    """Refuse an over-long body before reading it.

    FastAPI happily buffers whatever arrives; without this a single request
    could take the process's memory with it. Content-Length can be absent or a
    lie, so this is the cheap first gate and the real limit is enforced on the
    normalised document in the profiles router.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit():
        # Generous next to max_bundle_bytes: the JSON envelope and any
        # whitespace are stripped by normalisation, so the raw body is
        # legitimately larger than the document it carries.
        if int(declared) > settings.max_bundle_bytes * 4:
            return JSONResponse(
                {"detail": "请求体过大"},
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    if settings.is_production:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """One shape for every error, so clients have one thing to parse.

    FastAPI's default 422 body is a list of per-field objects, while every
    HTTPException raised in this codebase produces ``{"detail": "..."}``. A
    client should not need two parsers to show one message.
    """
    first = exc.errors()[0] if exc.errors() else {}
    location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
    message = first.get("msg", "请求内容不合法")
    return JSONResponse(
        {"detail": f"{location}：{message}" if location else message},
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
    )


@app.get("/api/v1/health", tags=["meta"])
async def health() -> dict:
    """Is the process up, and has the schema actually been migrated?

    Reporting the counts rather than a bare "ok" is what makes a deploy against
    an empty or un-migrated database visible straight away.
    """
    async with SessionLocal() as db:
        try:
            game_count = (await db.execute(select(func.count()).select_from(Game))).scalar_one()
            profile_count = (await db.execute(
                select(func.count()).select_from(Profile))).scalar_one()
        except Exception as exc:
            return {"ok": False, "schema": "missing",
                    "hint": "先运行 alembic upgrade head", "error": type(exc).__name__}
    return {"ok": True, "schema": "ready", "env": settings.env,
            "games": game_count, "profiles": profile_count}


# --- the site ----------------------------------------------------------------
#
# Serving the built site from this process, rather than letting Caddy serve it,
# keeps the page and the API on one origin. That is why there is no CORS
# configuration anywhere in this project: there is nothing cross-origin to
# allow, and the session cookie needs no SameSite exception.

WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"

if (WEB_DIST / "index.html").is_file():
    # Hashed filenames, so they may be cached hard; index.html must not be,
    # or a returning visitor keeps loading the previous build's asset names.
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        """Hand any non-API path to the single-page app.

        vue-router uses history mode, so /config/<id> is a real URL that the
        browser may request directly on a reload or from a shared link. Without
        this the server would 404 a link that works perfectly once the app is
        loaded -- which is the confusing kind of broken.
        """
        if path.startswith("api/"):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
        candidate = (WEB_DIST / path).resolve()
        # Only serve something that is really inside dist: `path` comes from
        # the URL, so "../.." must not walk out of it.
        if path and WEB_DIST in candidate.parents and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(WEB_DIST / "index.html",
                            headers={"Cache-Control": "no-cache"})
