from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
from pathlib import Path

from motion_conflicts import validate_motion_bindings

SCHEMA = "motioncontrol.game_profile.v1"
CATALOG_SCHEMA = "motioncontrol.game_catalog.v1"
SELECTION_SCHEMA = "motioncontrol.profile_selection.v2"


class ProfileSelectionChanged(ValueError):
    """The editor belongs to a different game than the active selection."""

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
    raw_target = action.get("target", "")
    if action_type == "gamepad":
        if isinstance(raw_target, (list, tuple, set)):
            parts = [str(item).strip().upper() for item in raw_target]
        else:
            parts = [part.strip().upper() for part in str(raw_target).replace(",", "+").split("+")]
        parts = [part for part in parts if part]
        if not parts:
            raise ValueError("Xbox 按键不能为空")
        # Buttons and the left stick are separate channels on the pad, so a
        # combo may mix them: "LB+LS_UP" holds the bumper and pushes the stick
        # at the same time.  A lone direction still belongs to gamepad_axis.
        invalid = [part for part in parts if part not in GAMEPAD_BUTTONS and part not in GAMEPAD_AXES]
        if invalid:
            raise ValueError("不支持的 Xbox 按键：" + ", ".join(sorted(set(invalid))))
        if len(parts) == 1 and parts[0] in GAMEPAD_AXES:
            raise ValueError("单独的摇杆方向请选择“Xbox 左摇杆”类型")
        target = parts[0] if len(parts) == 1 else parts
    else:
        target = str(raw_target).strip().upper()
        if not target:
            raise ValueError("action target must not be empty")
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
    if behavior not in {"hold", "tap", "release"}:
        raise ValueError("动作方式必须为点按、持续按住或松开")
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
            if group != "voice" and normalized.get("action", {}).get("behavior") == "release":
                raise ValueError("松开方式仅适用于语音映射")
            # Poses default to edge-triggered above; holding is opt-in, the
            # same as it already was for the continuous motions.
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
        if group != "voice" and normalized.get("action", {}).get("behavior") == "release":
            raise ValueError("松开方式仅适用于语音映射")
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
            original = self.selection_path.read_bytes()
        except FileNotFoundError:
            return {"schema": SELECTION_SCHEMA, "selected_id": "generic-xbox", "overrides_by_profile": {}}
        data = json.loads(original.decode("utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("游戏配置格式无法读取，原文件已保留")
        if data.get("schema") == "motioncontrol.profile_selection.v1":
            if not isinstance(data.get("overrides", {}), dict):
                raise ValueError("游戏映射数据无效，原文件已保留")
            selected = str(data.get("selected_id", "generic-xbox"))
            backup = self.selection_path.with_name(self.selection_path.name + ".v1.bak")
            try:
                with backup.open("xb") as stream:
                    stream.write(original)
            except FileExistsError:
                pass
            data = {
                "schema": SELECTION_SCHEMA,
                "selected_id": selected,
                "overrides_by_profile": {selected: data.get("overrides", {})},
            }
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


def action_catalog() -> dict:
    return {
        "keyboard": {"free_text": True},
        "mouse_button": {"targets": sorted(MOUSE_BUTTONS)},
        "mouse_wheel": {"targets": sorted(MOUSE_WHEEL), "behavior": "tap"},
        "gamepad": {"targets": sorted(GAMEPAD_BUTTONS), "allow_combo": True},
        "gamepad_trigger": {"targets": sorted(GAMEPAD_TRIGGERS)},
        "gamepad_axis": {"targets": sorted(GAMEPAD_AXES)},
    }
