"""Read-only access to the offline game-profile library.

Both sides need to resolve a profile id against the same 200-entry library: the
desktop to apply a selection, the cloud to validate an upload against its base
profile and to turn ``steam-1659420-uncharted`` into a display name.

Read-only on purpose.  Nothing here writes, so no cloud code path can persist
into a deployed checkout.  These functions raise on a broken library; the
desktop store wraps them in its own fallback so a missing generated library
cannot brick a running controller, which is a desktop policy rather than a
property of the data.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from .profile_schema import normalize_bindings, with_default_bindings
from .profile_versions import CATALOG_SCHEMA, SCHEMA


def load_catalog(library_dir: Path) -> dict:
    """Return the sanitised catalog index found under *library_dir*."""
    path = Path(library_dir) / "catalog.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != CATALOG_SCHEMA or not isinstance(data.get("games"), list):
        raise ValueError("invalid game catalog")
    games = []
    for raw in data["games"]:
        if not isinstance(raw, dict):
            continue
        ident = str(raw.get("id", "")).strip()
        name = str(raw.get("name", "")).strip()
        profile = str(raw.get("profile", "")).replace("\\", "/").strip("/")
        if not ident or not name or not profile or ".." in profile.split("/"):
            continue
        item = dict(raw)
        item.update({"id": ident, "name": name, "profile": profile})
        games.append(item)
    return {"schema": CATALOG_SCHEMA, "count": len(games), "games": games}


def catalog_entry(catalog: dict, profile_id: str) -> dict:
    ident = str(profile_id).strip()
    for item in catalog["games"]:
        if item["id"] == ident:
            return item
    raise KeyError(f"unknown game profile: {ident}")


def load_profile(library_dir: Path, profile_id: str, catalog: dict | None = None) -> dict:
    """Load one profile by id, with its bindings already normalised."""
    library_dir = Path(library_dir)
    entry = catalog_entry(catalog if catalog is not None else load_catalog(library_dir), profile_id)
    path = (library_dir / entry["profile"]).resolve()
    # The catalog is data, so treat its paths as untrusted even after the
    # ".." screen in load_catalog: resolve first, then require containment.
    if library_dir.resolve() not in path.parents:
        raise ValueError("profile path escapes game_profiles directory")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA:
        raise ValueError(f"profile schema mismatch: {entry['id']}")
    profile = copy.deepcopy(data)
    profile["id"] = entry["id"]
    profile["name"] = str(profile.get("name") or entry["name"])
    profile["bindings"] = with_default_bindings(normalize_bindings(profile.get("bindings")))
    return profile
