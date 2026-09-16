"""Load the game library into the database.

    python -m cloud.tools.seed_games

Run from the repository root. Idempotent: running it again updates names that
changed and adds ids that are new, and never deletes. Deletion is left out on
purpose -- a row that disappears from the catalog may still be referenced by
someone's uploaded config, and breaking that reference to tidy up a table is a
bad trade.

The ids come straight from ``game_profiles/catalog.json`` and are not
regenerated. That is what lets a desktop install upload a config naming
``steam-1659420-uncharted`` and have it match a row that is already here.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sqlalchemy import select

from motioncontrol_shared.catalog import load_catalog

from cloud.app.db import SessionLocal, engine
from cloud.app.models import Game, search_key

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LIBRARY_DIR = REPO_ROOT / "game_profiles"


async def seed(library_dir: Path) -> tuple[int, int]:
    catalog = load_catalog(library_dir)
    added = updated = 0
    async with SessionLocal() as db:
        existing = {row.id: row for row in (await db.execute(select(Game))).scalars()}
        for entry in catalog["games"]:
            row = existing.get(entry["id"])
            if row is None:
                db.add(Game(id=entry["id"], name=entry["name"],
                            search_key=search_key(entry["name"])))
                added += 1
            elif row.name != entry["name"]:
                row.name = entry["name"]
                row.search_key = search_key(entry["name"])
                updated += 1
        await db.commit()
    # aiosqlite keeps a worker thread per connection; without this the
    # script finishes its work and then hangs instead of exiting.
    await engine.dispose()
    return added, updated


def main() -> int:
    library = Path(sys.argv[1]) if len(sys.argv) > 1 else LIBRARY_DIR
    if not (library / "catalog.json").is_file():
        print(f"找不到游戏库：{library / 'catalog.json'}", file=sys.stderr)
        return 1
    added, updated = asyncio.run(seed(library))
    print(f"游戏库已同步：新增 {added}，更新 {updated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
