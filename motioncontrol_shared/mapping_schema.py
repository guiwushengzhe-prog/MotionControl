"""Validation for the two mapping documents that are not game profiles.

``profile_schema`` covers per-game bindings.  The other two files on the cloud
sync whitelist -- ``motion_mappings.json`` and ``voice_mappings.json`` -- have
their own shapes, and until now their rules lived only in desktop modules that
the cloud cannot import: ``voice_backend._validate_mappings`` reaches for
``KEY_CODES`` and ``KeyboardOutput`` out of ``output_backend``, which starts
with ``import ctypes.wintypes`` and raises on Linux.

Duplicating the rules in the cloud would be the worst option available.  The
failure it produces is silent and delayed: the cloud accepts an upload, the
desktop refuses it on the way back down, and the user sees a config that
downloads successfully and then does not work.  So the rules move here and both
sides call them.

Two ordering decisions are made deliberately and are not interchangeable:

* **Voice mappings keep their order.**  ``VoiceCommandBackend.handle_command``
  scans the list and stops at the first match (voice_backend.py:588), so a
  synonym on an earlier entry beats a phrase on a later one.  Order is meaning,
  and sorting would quietly rebind commands.
* **Motion mappings are sorted by id.**  The desktop rebuilds that list in
  ``DEFAULT_MOTIONS`` order every time it loads, so the stored order says
  nothing.  Sorting makes two uploads that differ only in list order hash the
  same, which is the same property key sorting gives the rest of the canonical
  form.
"""

from __future__ import annotations

from .profile_schema import GAMEPAD_AXES, GAMEPAD_BUTTONS, KEYBOARD_KEYS
from .text_norm import compact_text

DEFAULT_WAKE_WORD = "体感"
DEFAULT_EMERGENCY_STOP = "体感紧急停止"

# Spellings users type that mean an existing key.  Mirrors
# KeyboardOutput.normalize; kept here so the cloud accepts exactly what the
# desktop accepts rather than rejecting "RETURN" for being unknown.
KEYBOARD_ALIASES = {
    "CONTROL": "CTRL",
    "WINDOWS": "WIN",
    "RETURN": "ENTER",
    "DEL": "DELETE",
}

# Voice commands may drive the app itself rather than the game.  This is an
# allowlist because a system target is executed by name on the desktop.
VOICE_SYSTEM_TARGETS = {
    "HEAD_CALIBRATION_START",
    "HEAD.CALIBRATE",
    "HEAD.CENTER",
    "OUTPUT.START",
    "OUTPUT.STOP",
    # 录自定义动作。人站在镜头前几米外摆姿势，够不着鼠标——这三个按钮天生
    # 就该能用嘴按。不列在这里的话，界面上选得到、保存时却被校验器退回来。
    "POSE.RECORD",
    "POSE.ADD_FRAME",
    "POSE.CANCEL",
    # 定住跟随框 / 恢复跟随、输出来回切。映射表里也能选（profile_schema 的
    # BINDING_SYSTEM_TARGETS），名字相同意思相同。
    "ZONES.FREEZE_TOGGLE",
    "ZONES.FREEZE",
    "ZONES.FOLLOW",
    "ZONES.MOVE_HERE",
    "OUTPUT.TOGGLE",
}
# 参考场景删掉以后，原来绑着「记录参考场景」「重新匹配场景」的口令改做「区域挪到我
# 这里」——都是"让区域对上我现在的位置"。不认的话，存着旧写法的整份口令会被退回来。
LEGACY_SYSTEM_TARGETS = {
    "SCENE.CAPTURE_REFERENCE": "ZONES.MOVE_HERE",
    "SCENE.REMATCH": "ZONES.MOVE_HERE",
}

# Motions drive one output each, so the richer action vocabulary that game
# bindings use does not apply here.
MOTION_ACTION_TYPES = {"keyboard", "gamepad", "gamepad_axis"}

MAX_VOICE_MAPPINGS = 32
MAX_SYNONYMS = 8
MAX_PHRASE_CHARS = 24
MAX_EMERGENCY_PHRASES = 8
MAX_WAKE_WORD_CHARS = 12
MAX_KEY_COMBO = 4


def normalize_key_name(key: str) -> str:
    """Uppercase, trim, and resolve the alias spellings."""
    value = str(key).strip().upper()
    return KEYBOARD_ALIASES.get(value, value)


def normalize_key_combo(target: str) -> str:
    """Validate a ``"CTRL+S"`` style combo and return its canonical spelling."""
    parts = [normalize_key_name(part) for part in str(target).split("+") if part.strip()]
    if not parts or len(parts) > MAX_KEY_COMBO:
        raise ValueError(f"键盘映射格式错误：{target}")
    invalid = [part for part in parts if part not in KEYBOARD_KEYS]
    if invalid:
        raise ValueError("不支持的键盘键：" + ", ".join(invalid))
    return "+".join(parts)


def normalize_wake_word(value) -> str:
    word = str(value or DEFAULT_WAKE_WORD).strip()
    if not word or len(word) > MAX_WAKE_WORD_CHARS:
        raise ValueError(f"唤醒词必须是 1 到 {MAX_WAKE_WORD_CHARS} 个字符")
    return word


def normalize_emergency_phrases(items) -> list[str]:
    values = [str(item).strip() for item in (items if isinstance(items, list) else [])
              if str(item).strip()]
    values = list(dict.fromkeys(values[:MAX_EMERGENCY_PHRASES]))
    # The built-in stop phrase is not optional: it is the way out when output
    # is stuck, so it is reinstated rather than trusted to the uploaded file.
    if DEFAULT_EMERGENCY_STOP not in values:
        values.insert(0, DEFAULT_EMERGENCY_STOP)
    if any(len(value) > MAX_PHRASE_CHARS for value in values):
        raise ValueError("紧急停止命令过长")
    return values


def normalize_voice_mappings(items) -> list[dict]:
    """Validate the spoken-command list, preserving its first-match order."""
    if not isinstance(items, list):
        raise ValueError("mappings 必须是数组")
    result: list[dict] = []
    seen: set[str] = set()
    for raw in items[:MAX_VOICE_MAPPINGS]:
        if not isinstance(raw, dict):
            continue
        phrase = str(raw.get("phrase", "")).strip()
        if not phrase:
            continue
        if len(phrase) > MAX_PHRASE_CHARS:
            raise ValueError(f"命令词过长：{phrase}")
        key = compact_text(phrase)
        if key in seen:
            raise ValueError(f"命令词重复：{phrase}")
        seen.add(key)

        synonyms = raw.get("synonyms", [])
        if not isinstance(synonyms, list):
            synonyms = []
        aliases: list[str] = []
        for item in synonyms[:MAX_SYNONYMS]:
            alias = str(item).strip()
            if alias and compact_text(alias) not in {key, *(compact_text(x) for x in aliases)}:
                aliases.append(alias)

        action_type = str(raw.get("type", "keyboard")).lower()
        target = str(raw.get("target", "")).strip().upper()
        if action_type == "gamepad":
            if target not in GAMEPAD_BUTTONS:
                raise ValueError(f"暂不支持的 Xbox 键：{target}")
        elif action_type == "keyboard":
            target = normalize_key_combo(target)
        elif action_type == "system":
            target = LEGACY_SYSTEM_TARGETS.get(target, target)
            if target not in VOICE_SYSTEM_TARGETS:
                raise ValueError(f"暂不支持的系统命令：{target}")
        else:
            raise ValueError(f"未知输出类型：{action_type}")

        behavior = str(raw.get("behavior", "tap")).strip().lower()
        if behavior not in {"tap", "hold", "release"}:
            raise ValueError("语音动作方式必须为点按、持续按住或松开")
        if action_type == "system" and behavior != "tap":
            raise ValueError("系统命令只能点按")

        entry = {"phrase": phrase, "type": action_type, "target": target, "behavior": behavior}
        if aliases:
            entry["synonyms"] = aliases
        result.append(entry)
    return result


def normalize_motion_item(raw) -> dict:
    """Validate one entry of ``motion_mappings.json``."""
    if not isinstance(raw, dict):
        raise ValueError("动作映射必须是对象")
    ident = str(raw.get("id", "")).strip()
    if not ident:
        raise ValueError("动作映射缺少 id")
    name = str(raw.get("name", "")).strip() or ident
    action_type = str(raw.get("type", "")).strip().lower()
    if action_type not in MOTION_ACTION_TYPES:
        raise ValueError(f"{name} 的输出类型不支持：{action_type}")
    target = str(raw.get("target", "")).strip().upper()
    enabled = bool(raw.get("enabled", False))
    if enabled and not target:
        raise ValueError(f"{name} 已启用但没有设置输出")
    # A disabled motion may legitimately carry no target, so the vocabulary
    # checks below run only when there is something to check.
    if target:
        if action_type == "gamepad" and target not in GAMEPAD_BUTTONS:
            raise ValueError(f"{name} 的 Xbox 按键不支持：{target}")
        if action_type == "gamepad_axis" and target not in GAMEPAD_AXES:
            raise ValueError(f"{name} 的摇杆方向不支持：{target}")
        if action_type == "keyboard":
            target = normalize_key_combo(target)
    return {"id": ident, "name": name, "enabled": enabled,
            "type": action_type, "target": target}
