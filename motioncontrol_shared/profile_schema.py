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
    # Voice trigger text belongs to the binding, not to the shipped command
    # catalog.  Keep it in the shared schema so cloud validation and desktop
    # validation preserve the same player setting.
    if default_behavior == "tap":
        phrase = str(binding.get("phrase", "")).strip()
        if phrase:
            if len(phrase) > 24:
                raise ValueError("语音触发词过长")
            out["phrase"] = phrase
        synonyms = binding.get("synonyms", [])
        if isinstance(synonyms, list):
            aliases = []
            for item in synonyms[:8]:
                alias = str(item).strip()
                if alias and len(alias) <= 24 and alias not in aliases and alias != phrase:
                    aliases.append(alias)
            if aliases:
                out["synonyms"] = aliases
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

# The hands used to be four zones and are now two.  The desktop UI already
# shows only the merged pair (web/app.js lists zone.leftHand / zone.rightHand),
# and the kernel only ever dispatches the merged ids -- when a saved config has
# no entry for one it falls back to the historical ids *in this order* and
# takes the first hit (control_kernel.py:1688).  So a config carrying both
# "upper" and "lower" already has only one of them doing anything; the other is
# dead weight that looks live on screen.
#
# Normalising them away here is what makes the stored config say what actually
# happens.  The order below is the kernel's, so the surviving binding is the
# same one that was already in effect: this changes what a config *says*, never
# what it *does*.
ZONE_ID_MERGES = (
    ("leftHandUpper", "leftHand"),
    ("leftHandLower", "leftHand"),
    ("rightHandUpper", "rightHand"),
    ("rightHandLower", "rightHand"),
)
_ZONE_MERGE_TARGET = dict(ZONE_ID_MERGES)


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
    """Validate a standalone overrides map and return it in canonical form.

    Historical hand-zone ids are merged into the pair the app actually uses.
    Which one survives is decided by ZONE_ID_MERGES order, which is the kernel's
    own fallback order -- so the binding left standing is the one that was
    already taking effect, and a config's behaviour does not change when it goes
    through here.
    """
    if not isinstance(overrides, dict):
        raise ValueError("overrides must be an object")

    # Sorted so the outcome does not depend on the order keys happen to sit in
    # the uploaded file: two files with the same bindings must normalise the
    # same way, or they would hash differently and look like different configs.
    entries = []
    for trigger, value in sorted(overrides.items()):
        entry = normalize_override_entry(trigger, value)
        if entry is not None:
            entries.append((str(trigger).partition(".")[2], entry))

    merge_rank = {alias: index for index, (alias, _) in enumerate(ZONE_ID_MERGES)}
    out: dict = {}
    claimed: dict[str, int] = {}
    for raw_ident, (group, ident, normalized) in entries:
        prefix = next(key for key, name in OVERRIDE_GROUPS.items() if name == group)
        target = _ZONE_MERGE_TARGET.get(raw_ident) if prefix == "zone" else None
        key = f"{prefix}.{target or ident}"

        if target is not None:
            rank = merge_rank[raw_ident]
            # A real entry for the merged id outranks any historical one, and
            # among the historical ones the kernel's order decides.
            if key in claimed and claimed[key] <= rank:
                continue
            claimed[key] = rank
        elif prefix == "zone" and ident in {t for _, t in ZONE_ID_MERGES}:
            claimed[key] = -1

        out[key] = None if normalized.get("disabled") else normalized
    return out
