"""Serving the built site, and the one thing that can go wrong while doing it.

The catch-all route that makes vue-router's history mode work is also the route
most likely to serve something it should not: it takes a path straight out of
the URL and looks for a file. These tests pin both halves -- that a deep link
reaches the app, and that no spelling of ".." escapes the dist directory.
"""

from __future__ import annotations

import pytest

from cloud.app.main import WEB_DIST

pytestmark = pytest.mark.asyncio

needs_build = pytest.mark.skipif(
    not (WEB_DIST / "index.html").is_file(),
    reason="site not built (cd cloud/web && npm run build)",
)


@needs_build
async def test_the_root_serves_the_app(client):
    response = await client.get("/")
    assert response.status_code == 200
    assert 'id="app"' in response.text


@needs_build
@pytest.mark.parametrize("path", ["/browse", "/poses", "/login", "/config/whatever-id"])
async def test_deep_links_reach_the_app(client, path):
    """These are real URLs people reload and share, not just in-app navigation."""
    response = await client.get(path)
    assert response.status_code == 200
    assert 'id="app"' in response.text


@needs_build
@pytest.mark.parametrize("path", [
    "/../app/settings.py",
    "/../../../../etc/passwd",
    "/..%2fapp%2fsettings.py",
    "/../alembic.ini",
    "/../cloud.db",
])
async def test_no_spelling_of_dot_dot_escapes_the_site_directory(client, path):
    """A leak here would hand out settings.py, which names every secret's env var."""
    response = await client.get(path)
    # Falling through to index.html is the correct outcome: there is no file at
    # that path inside dist, so the app handles it and shows "not found".
    assert 'id="app"' in response.text
    assert "MC_SECRET_KEY" not in response.text
    assert "root:x:" not in response.text


@needs_build
async def test_an_unknown_api_path_is_still_a_404(client):
    """The catch-all must not swallow API 404s and answer them with HTML."""
    response = await client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert 'id="app"' not in response.text
