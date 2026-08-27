from __future__ import annotations

import copy
import json
import re
import threading
from pathlib import Path

SCHEMA = "motioncontrol.game_profile.v1"
CATALOG_SCHEMA = "motioncontrol.game_catalog.v1"
SELECTION_SCHEMA = "motioncontrol.profile_selection.v1"

GAMEPAD_BUTTONS = {
    "A", "B", "X", "Y", "LB", "RB", "L3", "R3",
    "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT", "START", "BACK",
}
GAMEPAD_AXES = {"LS_UP", "LS_DOWN", "LS_LEFT", "LS_RIGHT"}
GAMEPAD_TRIGGERS = {"LT", "RT"}
MOUSE_BUTTONS = {"LEFT", "RIGHT", "MIDDLE", "X1", "X2"}
MOUSE_WHEEL = {"SCROLL_UP", "SCROLL_DOWN"}
ACTION_TYPES = {
    "keyboard", "mouse_button", "mouse_wheel", "gamepad", "gamepad_trigger", "gamepad_axis",
}
KEYBOARD_KEYS = (
    {chr(code) for code in range(ord("A"), ord("Z") + 1)}
    | {str(code) for code in range(10)}
    | {"SPACE", "ENTER", "ESC", "TAB", "SHIFT", "CTRL", "ALT", "WIN",
       "BACKSPACE", "DELETE", "HOME", "END", "PAGEUP", "PAGEDOWN",
       "LEFT", "UP", "RIGHT", "DOWN"}
    | {f"F{i}" for i in range(1, 13)}
)
TRIGGER_GROUPS = ("zones", "motions", "poses", "voice")

_KEY_RE = re.compile(r"^[A-Z0-9_]+(?:\+[A-Z0-9_]+){0,3}$")


def normalize_action(action: dict, *, default_behavior: str = "hold") -> dict:
    if not isinstance(action, dict):
        raise ValueError("action must be an object")
    action_type = str(action.get("type", "")).strip().lower()
    aliases = {
        "gamepad_button": "gamepad",
        "xinput_button": "gamepad",
        "xbox": "gamepad",
        "mouse": "mouse_button",
        "wheel": "mouse_wheel",
        "trigger": "gamepad_trigger",
        "axis": "gamepad_axis",
    }
    action_type = aliases.get(action_type, action_type)
    if action_type not in ACTION_TYPES:
        raise ValueError(f"unsupported action type: {action_type}")
    target = str(action.get("target", "")).strip().upper()
    if not target:
        raise ValueError("action target must not be empty")
    if action_type == "gamepad" and target not in GAMEPAD_BUTTONS:
        raise ValueError(f"unsupported Xbox button: {target}")
    if action_type == "gamepad_axis" and target not in GAMEPAD_AXES:
        raise ValueError(f"unsupported Xbox axis: {target}")
    if action_type == "gamepad_trigger" and target not in GAMEPAD_TRIGGERS:
        raise ValueError(f"unsupported Xbox trigger: {target}")
    if action_type == "mouse_button" and target not in MOUSE_BUTTONS:
        raise ValueError(f"unsupported mouse button: {target}")
    if action_type == "mouse_wheel" and target not in MOUSE_WHEEL:
        raise ValueError(f"unsupported mouse wheel: {target}")
    if action_type == "keyboard":
        if not _KEY_RE.match(target):
            raise ValueError(f"invalid keyboard target: {target}")
        keys = [part for part in target.split("+") if part]
        invalid = [key for key in keys if key not in KEYBOARD_KEYS]
        if invalid:
            raise ValueError("unsupported keyboard target: " + ", ".join(invalid))
    behavior = str(action.get("behavior", default_behavior)).strip().lower()
    if behavior not in {"hold", "tap"}:
        raise ValueError("behavior must be hold or tap")
    # A wheel is an impulse by definition; allowing hold would create runaway scrolling.
    if action_type == "mouse_wheel":
        behavior = "tap"
    return {"type": action_type, "target": target, "behavior": behavior}


def normalize_binding(binding: dict, *, default_behavior: str) -> dict:
    if not isinstance(binding, dict):
        raise ValueError("binding must be an object")
    if bool(binding.get("disabled")):
        return {"disabled": True}
    action = binding.get("action") if isinstance(binding.get("action"), dict) else binding
    out = {"action": normalize_action(action, default_behavior=default_behavior)}
    label = str(binding.get("label", "")).strip()
    if label:
        out["label"] = label
    return out


def normalize_bindings(bindings: dict | None) -> dict:
    source = bindings if isinstance(bindings, dict) else {}
    out: dict[str, dict] = {group: {} for group in TRIGGER_GROUPS}
    for group in TRIGGER_GROUPS:
        items = source.get(group, {})
        if not isinstance(items, dict):
            continue
        default_behavior = "tap" if group in {"poses", "voice"} else "hold"
        for trigger_id, binding in items.items():
            ident = str(trigger_id).strip()
            if not ident or not isinstance(binding, dict):
                continue
            normalized = normalize_binding(binding, default_behavior=default_behavior)
            # Cross-pose actions are explicitly edge-triggered by product design.
            if group in {"poses", "voice"}:
                normalized["action"]["behavior"] = "tap"
            out[group][ident] = normalized
    return out


def flatten_bindings(bindings: dict | None) -> dict[str, dict]:
    grouped = normalize_bindings(bindings)
    flat: dict[str, dict] = {}
    prefixes = {"zones": "zone", "motions": "motion", "poses": "pose", "voice": "voice"}
    for group, items in grouped.items():
        prefix = prefixes[group]
        for ident, binding in items.items():
            flat[f"{prefix}.{ident}"] = copy.deepcopy(binding)
    return flat


def _merge_bindings(base: dict, overrides: dict) -> dict:
    merged = normalize_bindings(base)
    if not isinstance(overrides, dict):
        return merged
    for trigger, value in overrides.items():
        trigger = str(trigger)
        if "." not in trigger:
            continue
        prefix, ident = trigger.split(".", 1)
        group = {"zone": "zones", "motion": "motions", "pose": "poses", "voice": "voice"}.get(prefix)
        if not group or not ident:
            continue
        if value is None:
            merged[group][ident] = {"disabled": True}
            continue
        default_behavior = "tap" if group in {"poses", "voice"} else "hold"
        normalized = normalize_binding(value, default_behavior=default_behavior)
        if group in {"poses", "voice"}:
            normalized["action"]["behavior"] = "tap"
        merged[group][ident] = normalized
    return merged


class GameProfileStore:
    """Tiny lazy-loaded offline game-profile library.

    Only catalog.json is loaded eagerly. Individual profile JSON files are read
    when selected/opened, so hundreds or thousands of games do not inflate the
    runtime working set.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.library_dir = self.root / "game_profiles"
        self.profile_dir = self.library_dir / "profiles"
        self.catalog_path = self.library_dir / "catalog.json"
        self.selection_path = self.root / "config" / "game_profile_selection.json"
        self._lock = threading.RLock()
        self._catalog: dict | None = None
        self._catalog_mtime_ns = -1
        self._selection = self._load_selection()

    def _load_selection(self) -> dict:
        try:
            data = json.loads(self.selection_path.read_text(encoding="utf-8"))
            if data.get("schema") != SELECTION_SCHEMA:
                raise ValueError("selection schema mismatch")
            return {
                "schema": SELECTION_SCHEMA,
                "selected_id": str(data.get("selected_id", "generic-xbox")),
                "overrides": data.get("overrides", {}) if isinstance(data.get("overrides"), dict) else {},
            }
        except Exception:
            return {"schema": SELECTION_SCHEMA, "selected_id": "generic-xbox", "overrides": {}}

    def _save_selection(self) -> None:
        self.selection_path.parent.mkdir(parents=True, exist_ok=True)
        self.selection_path.write_text(json.dumps(self._selection, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_catalog(self) -> dict:
        try:
            stat = self.catalog_path.stat()
            if self._catalog is not None and stat.st_mtime_ns == self._catalog_mtime_ns:
                return self._catalog
            data = json.loads(self.catalog_path.read_text(encoding="utf-8"))
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
            self._catalog = {"schema": CATALOG_SCHEMA, "count": len(games), "games": games}
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
        ident = str(profile_id).strip()
        for item in self._load_catalog()["games"]:
            if item["id"] == ident:
                return item
        raise KeyError(f"unknown game profile: {ident}")

    def get_profile(self, profile_id: str) -> dict:
        with self._lock:
            entry = self._catalog_entry(profile_id)
            path = (self.library_dir / entry["profile"]).resolve()
            if self.library_dir.resolve() not in path.parents:
                raise ValueError("profile path escapes game_profiles directory")
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("schema") != SCHEMA:
                raise ValueError(f"profile schema mismatch: {entry['id']}")
            profile = copy.deepcopy(data)
            profile["id"] = entry["id"]
            profile["name"] = str(profile.get("name") or entry["name"])
            profile["bindings"] = normalize_bindings(profile.get("bindings"))
            return profile

    def effective_profile(self) -> dict:
        with self._lock:
            selected_id = self._selection["selected_id"]
            try:
                profile = self.get_profile(selected_id)
            except Exception:
                # A missing generated library must not brick the existing controller.
                profile = self.get_profile("generic-xbox")
                selected_id = "generic-xbox"
                self._selection["selected_id"] = selected_id
                self._selection["overrides"] = {}
                self._save_selection()
            profile["bindings"] = _merge_bindings(profile.get("bindings", {}), self._selection.get("overrides", {}))
            profile["selected_id"] = selected_id
            profile["overrides"] = copy.deepcopy(self._selection.get("overrides", {}))
            return profile

    def select(self, profile_id: str) -> dict:
        with self._lock:
            self.get_profile(profile_id)  # validate before persisting
            self._selection = {"schema": SELECTION_SCHEMA, "selected_id": str(profile_id), "overrides": {}}
            self._save_selection()
            return self.effective_profile()

    def set_overrides(self, overrides: dict) -> dict:
        if not isinstance(overrides, dict):
            raise ValueError("overrides must be an object")
        # Validate by applying to the currently selected base profile before saving.
        with self._lock:
            base = self.get_profile(self._selection["selected_id"])
            _merge_bindings(base.get("bindings", {}), overrides)
            self._selection["overrides"] = copy.deepcopy(overrides)
            self._save_selection()
            return self.effective_profile()


def action_catalog() -> dict:
    return {
        "keyboard": {"free_text": True},
        "mouse_button": {"targets": sorted(MOUSE_BUTTONS)},
        "mouse_wheel": {"targets": sorted(MOUSE_WHEEL), "behavior": "tap"},
        "gamepad": {"targets": sorted(GAMEPAD_BUTTONS)},
        "gamepad_trigger": {"targets": sorted(GAMEPAD_TRIGGERS)},
        "gamepad_axis": {"targets": sorted(GAMEPAD_AXES)},
    }
