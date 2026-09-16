"""Desktop side of the user-data layout: resolve the base directory, migrate once.

Everything the user can change now lives under %LOCALAPPDATA%\\MotionControl
instead of inside the program folder.  Two things forced this:

* Upgrading meant unzipping a new folder, which left every mapping, motion,
  voice binding and scene behind in the old one.
* 请先看.txt told people the folder could be copied to another computer.  Once
  a scene had been captured that was actively harmful: scene_layout.json is a
  homography onto one captured frame from one camera in one position, and
  scene_reference.jpg is the photo it refers to.  Carrying them to a different
  machine puts the six zones somewhere silently wrong rather than failing.

The shape of the layout is in motioncontrol_shared.user_layout, which stays
free of environment lookups so the cloud can import it on Linux.  This module
is the part that knows about %LOCALAPPDATA%.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from motioncontrol_shared.user_layout import (
    LEGACY_PROGRAM_FILES,
    USER_DATA_FILES,
    migration_plan,
    user_data_path,
)

_OVERRIDE_ENV = "MOTIONCONTROL_USER_DIR"


def user_data_root() -> Path:
    """The per-user data directory, created on demand.

    MOTIONCONTROL_USER_DIR overrides it, which is what the tests use so they
    never touch a developer's real configuration.
    """
    override = os.environ.get(_OVERRIDE_ENV, "").strip()
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
    return Path(base) / "MotionControl"


def user_path(key: str) -> Path:
    return user_data_path(user_data_root(), key)


def ensure_user_data_root() -> Path:
    root = user_data_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def migrate_legacy_user_data(program_root: Path | str) -> list[str]:
    """Copy 2.0-era files out of the program folder, once.

    Copies rather than moves, and never overwrites: if the user directory
    already has a file, that one wins.  Leaving the original in place means
    going back to an older build still finds its configuration, and it makes
    this safe to run on every start.

    Returns the keys that were migrated, for the startup log.
    """
    root = ensure_user_data_root()
    migrated = []
    for key, source, destination in migration_plan(root, program_root):
        if destination.exists() or not source.exists():
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            migrated.append(key)
        except OSError as exc:
            # A failed migration must not stop the app: the user simply keeps
            # the defaults, which is recoverable, unlike refusing to start.
            print(f"用户数据迁移失败（{key}）：{exc}")
    return migrated


def describe_layout() -> str:
    """One-line summary for the startup banner."""
    return str(user_data_root())


__all__ = [
    "LEGACY_PROGRAM_FILES",
    "USER_DATA_FILES",
    "describe_layout",
    "ensure_user_data_root",
    "migrate_legacy_user_data",
    "user_data_root",
    "user_path",
]
