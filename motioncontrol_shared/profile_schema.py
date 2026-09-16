"""Pure binding validation and normalisation shared by desktop and cloud.

Everything here is a total function of its arguments: no file access, no
environment lookups, no Windows APIs.  That is what lets the cloud import this
module on Linux and reject exactly what the desktop would reject, instead of
maintaining a second copy of the rules that drifts.

Persistence lives in the desktop's ``game_profiles.GameProfileStore``, which is
deliberately not importable from here.
"""

from __future__ import annotations

import copy
import re

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


def action_catalog() -> dict:
    return {
        "keyboard": {"free_text": True},
        "mouse_button": {"targets": sorted(MOUSE_BUTTONS)},
        "mouse_wheel": {"targets": sorted(MOUSE_WHEEL), "behavior": "tap"},
        "gamepad": {"targets": sorted(GAMEPAD_BUTTONS), "allow_combo": True},
        "gamepad_trigger": {"targets": sorted(GAMEPAD_TRIGGERS)},
        "gamepad_axis": {"targets": sorted(GAMEPAD_AXES)},
    }


# _merge_bindings applies overrides on top of a base profile.  The cloud has to
# validate an overrides map on its own, before any base profile is in hand, so
# the same per-trigger rules are factored out here and used by both.
OVERRIDE_GROUPS = {"zone": "zones", "motion": "motions", "pose": "poses", "voice": "voice"}


def normalize_override_entry(trigger: str, value):
    """Normalise one ``"<prefix>.<id>": binding|None`` override entry.

    Returns ``(group, ident, normalized)`` or ``None`` when the trigger key is
    not addressable, which is how _merge_bindings already treats it.
    """
    trigger = str(trigger)
    if "." not in trigger:
        return None
    prefix, ident = trigger.split(".", 1)
    group = OVERRIDE_GROUPS.get(prefix)
    if not group or not ident:
        return None
    if value is None:
        return group, ident, {"disabled": True}
    default_behavior = "tap" if group in {"poses", "voice"} else "hold"
    normalized = normalize_binding(value, default_behavior=default_behavior)
    if group != "voice" and normalized.get("action", {}).get("behavior") == "release":
        raise ValueError("松开方式仅适用于语音映射")
    return group, ident, normalized


def normalize_overrides(overrides) -> dict:
    """Validate a standalone overrides map and return it in canonical form."""
    if not isinstance(overrides, dict):
        raise ValueError("overrides must be an object")
    out: dict = {}
    for trigger, value in overrides.items():
        entry = normalize_override_entry(trigger, value)
        if entry is None:
            continue
        group, ident, normalized = entry
        prefix = next(key for key, name in OVERRIDE_GROUPS.items() if name == group)
        out[f"{prefix}.{ident}"] = None if normalized.get("disabled") else normalized
    return out
