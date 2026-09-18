from __future__ import annotations

"""Small deterministic Steam Input VDF reader used by MotionControl.

This module deliberately does not try to emulate Steam Input.  It only reads the
parts needed to convert physical controller controls (A/B/X/Y, bumpers, etc.)
into MotionControl's unified output actions.  Native Steam game actions are kept
as unsupported instead of being guessed.
"""

from dataclasses import dataclass
import re
from typing import Iterable, Iterator


VDFScalar = str
VDFObject = list[tuple[str, "VDFValue"]]
VDFValue = VDFScalar | VDFObject


@dataclass(frozen=True)
class ParsedBinding:
    physical: str
    raw: str
    action: dict | None
    label: str = ""
    source: str = ""
    reason: str = ""


# Canonical physical controls that are meaningful for our current body-zone model.
PHYSICAL_ALIASES = {
    "button_a": "button_a",
    "button_b": "button_b",
    "button_x": "button_x",
    "button_y": "button_y",
    "button_A": "button_a",
    "button_B": "button_b",
    "button_X": "button_x",
    "button_Y": "button_y",
    "left_bumper": "left_bumper",
    "right_bumper": "right_bumper",
    "left_shoulder": "left_bumper",
    "right_shoulder": "right_bumper",
    "button_menu": "button_menu",
    "button_escape": "button_escape",
    "button_start": "button_menu",
    "button_select": "button_escape",
    "left_trigger": "left_trigger",
    "right_trigger": "right_trigger",
    "trigger_left": "left_trigger",
    "trigger_right": "right_trigger",
    "left_stick_click": "left_stick_click",
    "right_stick_click": "right_stick_click",
    "joystick_left": "left_stick_click",
    "joystick_right": "right_stick_click",
    "dpad_up": "dpad_up",
    "dpad_down": "dpad_down",
    "dpad_left": "dpad_left",
    "dpad_right": "dpad_right",
}

XINPUT_ALIASES = {
    "A": ("gamepad", "A"),
    "B": ("gamepad", "B"),
    "X": ("gamepad", "X"),
    "Y": ("gamepad", "Y"),
    "SHOULDER_LEFT": ("gamepad", "LB"),
    "SHOULDER_RIGHT": ("gamepad", "RB"),
    "LEFT_SHOULDER": ("gamepad", "LB"),
    "RIGHT_SHOULDER": ("gamepad", "RB"),
    "BUMPER_LEFT": ("gamepad", "LB"),
    "BUMPER_RIGHT": ("gamepad", "RB"),
    "LEFT_BUMPER": ("gamepad", "LB"),
    "RIGHT_BUMPER": ("gamepad", "RB"),
    "TRIGGER_LEFT": ("gamepad_trigger", "LT"),
    "TRIGGER_RIGHT": ("gamepad_trigger", "RT"),
    "LEFT_TRIGGER": ("gamepad_trigger", "LT"),
    "RIGHT_TRIGGER": ("gamepad_trigger", "RT"),
    "JOYSTICK_LEFT": ("gamepad", "L3"),
    "JOYSTICK_RIGHT": ("gamepad", "R3"),
    "LEFT_STICK": ("gamepad", "L3"),
    "RIGHT_STICK": ("gamepad", "R3"),
    "DPAD_UP": ("gamepad", "DPAD_UP"),
    "DPAD_DOWN": ("gamepad", "DPAD_DOWN"),
    "DPAD_LEFT": ("gamepad", "DPAD_LEFT"),
    "DPAD_RIGHT": ("gamepad", "DPAD_RIGHT"),
    "START": ("gamepad", "START"),
    "SELECT": ("gamepad", "BACK"),
    "BACK": ("gamepad", "BACK"),
}

KEY_ALIASES = {
    "ESCAPE": "ESC",
    "RETURN": "ENTER",
    "LEFT_SHIFT": "SHIFT",
    "RIGHT_SHIFT": "SHIFT",
    "LSHIFT": "SHIFT",
    "RSHIFT": "SHIFT",
    "LEFT_CONTROL": "CTRL",
    "RIGHT_CONTROL": "CTRL",
    "LEFT_CTRL": "CTRL",
    "RIGHT_CTRL": "CTRL",
    "LCONTROL": "CTRL",
    "RCONTROL": "CTRL",
    "LCTRL": "CTRL",
    "RCTRL": "CTRL",
    "LEFT_ALT": "ALT",
    "RIGHT_ALT": "ALT",
    "LALT": "ALT",
    "RALT": "ALT",
    "WINDOWS": "WIN",
    "LEFT_WINDOWS": "WIN",
    "RIGHT_WINDOWS": "WIN",
    "DEL": "DELETE",
    "PGUP": "PAGEUP",
    "PGDN": "PAGEDOWN",
    "PAGE_UP": "PAGEUP",
    "PAGE_DOWN": "PAGEDOWN",
    "UP_ARROW": "UP",
    "DOWN_ARROW": "DOWN",
    "LEFT_ARROW": "LEFT",
    "RIGHT_ARROW": "RIGHT",
}

MOUSE_ALIASES = {
    "LEFT": "LEFT",
    "RIGHT": "RIGHT",
    "MIDDLE": "MIDDLE",
    "MOUSE4": "X1",
    "MOUSE5": "X2",
    "BUTTON4": "X1",
    "BUTTON5": "X2",
    "X1": "X1",
    "X2": "X2",
    "BACK": "X1",
    "FORWARD": "X2",
}

WHEEL_ALIASES = {
    "SCROLL_UP": "SCROLL_UP",
    "SCROLL_DOWN": "SCROLL_DOWN",
    "UP": "SCROLL_UP",
    "DOWN": "SCROLL_DOWN",
}


class VDFParseError(ValueError):
    pass


def _tokens(text: str) -> Iterator[str]:
    i = 0
    n = len(text)
    if text.startswith("\ufeff"):
        i = 1
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if ch in "{}":
            yield ch
            i += 1
            continue
        if ch == '"':
            i += 1
            buf: list[str] = []
            while i < n:
                ch = text[i]
                if ch == '"':
                    i += 1
                    break
                if ch == "\\" and i + 1 < n:
                    nxt = text[i + 1]
                    escapes = {'"': '"', "\\": "\\", "n": "\n", "r": "\r", "t": "\t"}
                    if nxt in escapes:
                        buf.append(escapes[nxt])
                        i += 2
                        continue
                    # Valve files often contain Windows paths. Preserve unknown
                    # backslash escapes rather than silently removing the slash.
                    buf.append("\\")
                    i += 1
                    continue
                buf.append(ch)
                i += 1
            else:
                raise VDFParseError("unterminated quoted string")
            yield "".join(buf)
            continue
        # Bare tokens are uncommon but legal enough to support here.
        start = i
        while i < n and (not text[i].isspace()) and text[i] not in '{}"':
            if text[i] == "/" and i + 1 < n and text[i + 1] == "/":
                break
            i += 1
        if i == start:
            raise VDFParseError(f"unexpected character at {i}: {text[i:i+8]!r}")
        yield text[start:i]


def parse_vdf(text: str) -> VDFObject:
    toks = list(_tokens(text))
    pos = 0

    def parse_object(expect_close: bool) -> VDFObject:
        nonlocal pos
        out: VDFObject = []
        while pos < len(toks):
            token = toks[pos]
            if token == "}":
                if not expect_close:
                    raise VDFParseError("unexpected closing brace")
                pos += 1
                return out
            if token == "{":
                raise VDFParseError("unexpected opening brace without key")
            key = token
            pos += 1
            if pos >= len(toks):
                raise VDFParseError(f"missing value for key {key!r}")
            if toks[pos] == "{":
                pos += 1
                value: VDFValue = parse_object(True)
            elif toks[pos] == "}":
                raise VDFParseError(f"missing value for key {key!r}")
            else:
                value = toks[pos]
                pos += 1
            out.append((key, value))
        if expect_close:
            raise VDFParseError("missing closing brace")
        return out

    parsed = parse_object(False)
    if pos != len(toks):
        raise VDFParseError("trailing tokens")
    return parsed


def _items(obj: VDFObject, key: str) -> list[VDFValue]:
    target = key.casefold()
    return [v for k, v in obj if k.casefold() == target]


def _first_scalar(obj: VDFObject, key: str, default: str = "") -> str:
    for value in _items(obj, key):
        if isinstance(value, str):
            return value
    return default


def _first_object(obj: VDFObject, key: str) -> VDFObject | None:
    for value in _items(obj, key):
        if isinstance(value, list):
            return value
    return None


def _all_objects(obj: VDFObject, key: str) -> list[VDFObject]:
    return [value for value in _items(obj, key) if isinstance(value, list)]


def _split_binding_payload(raw: str) -> tuple[str, str]:
    """Return command payload and human label.

    Steam's common syntax is ``command ARG, Human Label``.  Only the first comma
    is structural here; labels may contain further commas.
    """
    raw = str(raw).strip()
    if "," not in raw:
        return raw, ""
    command, label = raw.split(",", 1)
    return command.strip(), label.strip()


def parse_binding_action(raw: str) -> tuple[dict | None, str, str]:
    command, label = _split_binding_payload(raw)
    parts = command.split()
    if not parts:
        return None, label, "empty binding"
    verb = parts[0].casefold()
    arg = " ".join(parts[1:]).strip()

    if verb == "key_press":
        if not arg:
            return None, label, "key_press without key"
        key = KEY_ALIASES.get(arg.upper(), arg.upper())
        # Keep normalization deterministic. Runtime validation owns the final
        # supported-key decision so the parser itself stays reusable.
        if not re.fullmatch(r"[A-Z0-9_]+", key):
            return None, label, f"unsupported key syntax: {arg}"
        return {"type": "keyboard", "target": key, "behavior": "hold"}, label, ""

    if verb == "mouse_button":
        target = MOUSE_ALIASES.get(arg.upper())
        if not target:
            return None, label, f"unsupported mouse button: {arg}"
        return {"type": "mouse_button", "target": target, "behavior": "hold"}, label, ""

    if verb == "mouse_wheel":
        target = WHEEL_ALIASES.get(arg.upper())
        if not target:
            return None, label, f"unsupported mouse wheel: {arg}"
        return {"type": "mouse_wheel", "target": target, "behavior": "tap"}, label, ""

    if verb == "xinput_button":
        mapped = XINPUT_ALIASES.get(arg.upper())
        if not mapped:
            return None, label, f"unsupported xinput button: {arg}"
        action_type, target = mapped
        return {"type": action_type, "target": target, "behavior": "hold"}, label, ""

    # Steam Input API/native actions do not tell us which keyboard/XInput event
    # the game consumes. Guessing here would create incorrect profiles.
    if verb in {"game_action", "controller_action", "action_set", "action_layer"}:
        return None, label, f"native Steam action is not directly convertible: {verb}"

    return None, label, f"unsupported Steam binding command: {parts[0]}"


def _controller_root(tree: VDFObject) -> VDFObject:
    root = _first_object(tree, "controller_mappings")
    return root if root is not None else tree


def _physical_name(name: str) -> str | None:
    # Preserve case-insensitive Steam variants.
    cf = name.casefold()
    for raw, canonical in PHYSICAL_ALIASES.items():
        if raw.casefold() == cf:
            return canonical
    return None


def _binding_strings(obj: VDFObject | None) -> list[str]:
    if obj is None:
        return []
    out: list[str] = []
    for key, value in obj:
        if key.casefold() == "binding" and isinstance(value, str):
            out.append(value)
    return out


def _input_binding_strings(input_obj: VDFObject) -> list[tuple[str, str]]:
    """Return normal-press bindings from a Steam Input v3 input object.

    V3 stores ordinary button actions under
    ``inputs/<button>/activators/Full_Press/bindings/binding``.  Other
    activators such as long/double press are intentionally not promoted to a
    body-zone occupancy action because doing so would change their semantics.
    """
    out: list[tuple[str, str]] = []
    direct = _first_object(input_obj, "bindings")
    out.extend((raw, "inputs:bindings") for raw in _binding_strings(direct))
    activators = _first_object(input_obj, "activators")
    if activators is None:
        return out
    for activator_name, activator in activators:
        if not isinstance(activator, list):
            continue
        if activator_name.casefold() not in {"full_press", "regular_press", "press"}:
            continue
        bindings = _first_object(activator, "bindings")
        out.extend((raw, f"activator:{activator_name}") for raw in _binding_strings(bindings))
    return out


def _group_source_map(root: VDFObject) -> dict[str, str]:
    """Read v2 root or v3 preset group-source ownership."""
    candidates: list[VDFObject] = []
    direct = _first_object(root, "group_source_bindings")
    if direct is not None:
        candidates.append(direct)
    presets = _all_objects(root, "preset")
    # Prefer preset id=0 / Default, then any remaining preset.
    presets.sort(key=lambda obj: (0 if _first_scalar(obj, "id") == "0" or _first_scalar(obj, "name").casefold() == "default" else 1))
    for preset in presets:
        group_sources = _first_object(preset, "group_source_bindings")
        if group_sources is not None:
            candidates.append(group_sources)
    out: dict[str, str] = {}
    for obj in candidates:
        for key, value in obj:
            if isinstance(value, str) and str(key) not in out:
                out[str(key)] = value.casefold()
    return out


def extract_physical_bindings(text: str) -> dict[str, ParsedBinding]:
    """Extract best-known physical-control bindings from a Steam Input VDF.

    The result intentionally prefers direct ``switch_bindings`` and then
    ``button_diamond`` group bindings.  Other group modes are ignored unless we
    can prove which physical source owns the group.
    """
    root = _controller_root(parse_vdf(text))
    result: dict[str, ParsedBinding] = {}

    def put(physical_raw: str, raw_binding: str, source: str, *, replace: bool = False) -> None:
        physical = _physical_name(physical_raw)
        if physical is None:
            return
        action, label, reason = parse_binding_action(raw_binding)
        parsed = ParsedBinding(physical, raw_binding, action, label, source, reason)
        if replace or physical not in result or (result[physical].action is None and action is not None):
            result[physical] = parsed

    # Direct controls (bumpers, menu/back, rear buttons) are explicit.
    switch = _first_object(root, "switch_bindings")
    switch_bindings = _first_object(switch, "bindings") if switch is not None else None
    if switch_bindings is not None:
        for key, value in switch_bindings:
            if isinstance(value, str):
                put(key, value, "switch_bindings", replace=True)

    # Resolve group id -> physical source. V2 keeps this object at the root;
    # V3 usually nests it under preset/Default.
    group_sources = _group_source_map(root)

    for group in _all_objects(root, "group"):
        group_id = _first_scalar(group, "id")
        source_name = group_sources.get(group_id, "")
        group_is_active = not group_sources or (bool(source_name) and "active" in source_name and "inactive" not in source_name)

        # Steam Input v2: face buttons are scalar entries under bindings.
        bindings = _first_object(group, "bindings")
        if bindings is not None and "button_diamond" in source_name:
            for key, value in bindings:
                if isinstance(value, str):
                    put(key, value, f"group:{group_id}:button_diamond")

        # Steam Input v3: each physical input owns an activator object. Because
        # the input key itself is an explicit physical control (button_a,
        # left_bumper, ...), recognized keys are safe to consume even if a
        # community file omitted group_source_bindings.
        inputs = _first_object(group, "inputs")
        if inputs is not None and group_is_active:
            for key, input_value in inputs:
                if _physical_name(key) is None or not isinstance(input_value, list):
                    continue
                candidates = _input_binding_strings(input_value)
                for raw_binding, detail in candidates:
                    before = result.get(_physical_name(key) or "")
                    put(key, raw_binding, f"group:{group_id}:{detail}")
                    after = result.get(_physical_name(key) or "")
                    # Stop at the first convertible normal-press binding. If the
                    # first one is unsupported, later binding entries may still
                    # provide an exact keyboard/XInput action.
                    if after is not None and after.action is not None and after is not before:
                        break

    return result


def binding_summary(bindings: dict[str, ParsedBinding]) -> dict:
    supported = {key: value for key, value in bindings.items() if value.action is not None}
    unsupported = {key: value for key, value in bindings.items() if value.action is None}
    return {
        "count": len(bindings),
        "supported": len(supported),
        "unsupported": len(unsupported),
        "unsupported_reasons": {k: v.reason for k, v in unsupported.items()},
    }
