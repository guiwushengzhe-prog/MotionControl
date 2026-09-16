"""Desktop-side persistence for the selected game profile and its overrides.

The validation rules themselves live in ``motioncontrol_shared`` so the cloud
can apply exactly the same ones.  What stays here is everything that touches
this machine: reading and atomically rewriting
``game_profile_selection.json``, caching the catalog against its mtime, and
falling back to the generic profile when a generated library is missing.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from pathlib import Path

from motioncontrol_shared.catalog import catalog_entry, load_catalog, load_profile
from user_paths import user_path
from motioncontrol_shared.motion_conflicts import validate_motion_bindings
from motioncontrol_shared.profile_schema import _merge_bindings, normalize_bindings
from motioncontrol_shared.profile_versions import (
    CATALOG_SCHEMA,
    SELECTION_SCHEMA,
    is_selection_v1,
    migrate_selection_v1_to_v2,
)


class ProfileSelectionChanged(ValueError):
    """The editor belongs to a different game than the active selection."""


class GameProfileStore:
    """Tiny lazy-loaded offline game-profile library.

    Only catalog.json is loaded eagerly. Individual profile JSON files are read
    when selected/opened, so hundreds or thousands of games do not inflate the
    runtime working set.
    """

    def __init__(self, root: Path, selection_path: Path | None = None) -> None:
        self.root = Path(root)
        self.library_dir = self.root / "game_profiles"
        self.profile_dir = self.library_dir / "profiles"
        self.catalog_path = self.library_dir / "catalog.json"
        # User data, so it lives outside the program folder: unzipping an
        # upgrade must not leave a player's per-game mappings behind.
        self.selection_path = (Path(selection_path) if selection_path is not None
                               else user_path("profile_selection"))
        self._lock = threading.RLock()
        self._catalog: dict | None = None
        self._catalog_mtime_ns = -1
        self._selection = self._load_selection()

    def _load_selection(self) -> dict:
        try:
            original = self.selection_path.read_bytes()
        except FileNotFoundError:
            return {"schema": SELECTION_SCHEMA, "selected_id": "generic-xbox", "overrides_by_profile": {}}
        data = json.loads(original.decode("utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("游戏配置格式无法读取，原文件已保留")
        if is_selection_v1(data):
            migrated = migrate_selection_v1_to_v2(data)
            backup = self.selection_path.with_name(self.selection_path.name + ".v1.bak")
            try:
                with backup.open("xb") as stream:
                    stream.write(original)
            except FileExistsError:
                pass
            data = migrated
            self._save_selection(data)
        if data.get("schema") != SELECTION_SCHEMA or not isinstance(data.get("overrides_by_profile"), dict):
            raise ValueError("游戏配置格式无法读取，原文件已保留")
        if not isinstance(data.get("selected_id"), str) or any(
            not isinstance(value, dict) for value in data["overrides_by_profile"].values()
        ):
            raise ValueError("游戏映射数据无效，原文件已保留")
        return data

    def _save_selection(self, selection: dict) -> None:
        self.selection_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.selection_path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(selection, stream, ensure_ascii=False, indent=2)
            os.replace(temporary, self.selection_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self._selection = selection

    def _load_catalog(self) -> dict:
        try:
            stat = self.catalog_path.stat()
            if self._catalog is not None and stat.st_mtime_ns == self._catalog_mtime_ns:
                return self._catalog
            self._catalog = load_catalog(self.library_dir)
            self._catalog_mtime_ns = stat.st_mtime_ns
            return self._catalog
        except Exception:
            self._catalog = {"schema": CATALOG_SCHEMA, "count": 0, "games": []}
            self._catalog_mtime_ns = -1
            return self._catalog

    def list_games(self, query: str = "") -> dict:
        with self._lock:
            catalog = self._load_catalog()
            q = str(query).strip().casefold()
            games = catalog["games"]
            if q:
                games = [g for g in games if q in g["name"].casefold() or q in str(g.get("appid", ""))]
            return {
                "schema": CATALOG_SCHEMA,
                "count": len(games),
                "library_count": catalog["count"],
                "selected_id": self._selection["selected_id"],
                "games": copy.deepcopy(games),
            }

    def _catalog_entry(self, profile_id: str) -> dict:
        return catalog_entry(self._load_catalog(), profile_id)

    def get_profile(self, profile_id: str) -> dict:
        with self._lock:
            return load_profile(self.library_dir, profile_id, self._load_catalog())

    def effective_profile(self) -> dict:
        with self._lock:
            selected_id = self._selection["selected_id"]
            try:
                profile = self.get_profile(selected_id)
            except Exception:
                # A missing generated library must not brick the existing controller.
                profile = self.get_profile("generic-xbox")
                selected_id = "generic-xbox"
                self._save_selection({**self._selection, "selected_id": selected_id})
            overrides = self._selection["overrides_by_profile"].get(selected_id, {})
            profile["bindings"] = _merge_bindings(profile.get("bindings", {}), overrides)
            profile["selected_id"] = selected_id
            profile["overrides"] = copy.deepcopy(overrides)
            return profile

    def select(self, profile_id: str) -> dict:
        with self._lock:
            profile = self.get_profile(profile_id)  # validate before persisting
            overrides = self._selection["overrides_by_profile"].get(profile_id, {})
            validate_motion_bindings(_merge_bindings(profile.get("bindings", {}), overrides))
            self._save_selection({**self._selection, "selected_id": str(profile_id)})
            return self.effective_profile()

    def set_overrides(self, overrides: dict, profile_id: str | None = None) -> dict:
        if not isinstance(overrides, dict):
            raise ValueError("overrides must be an object")
        # Validate by applying to the currently selected base profile before saving.
        with self._lock:
            if profile_id is not None and profile_id != self._selection["selected_id"]:
                raise ProfileSelectionChanged("当前游戏已改变，映射未保存。请重新选择游戏后重试")
            base = self.get_profile(self._selection["selected_id"])
            merged = _merge_bindings(base.get("bindings", {}), overrides)
            validate_motion_bindings(merged)
            selection = copy.deepcopy(self._selection)
            selection["overrides_by_profile"][selection["selected_id"]] = copy.deepcopy(overrides)
            self._save_selection(selection)
            return self.effective_profile()
