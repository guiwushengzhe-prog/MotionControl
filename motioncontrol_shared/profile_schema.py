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
# Xbox 混合组合（按键 + 左摇杆）的按键领先时间。这个字段属于游戏动作，
# 不写入旧配置时仍按原来的约 80 毫秒处理。
DEFAULT_COMBO_STICK_LEAD_MS = 80
MIN_COMBO_STICK_LEAD_MS = 0
MAX_COMBO_STICK_LEAD_MS = 200
GAMEPAD_COMBO_TARGETS = GAMEPAD_BUTTONS | GAMEPAD_AXES | GAMEPAD_TRIGGERS
MOUSE_BUTTONS = {"LEFT", "RIGHT", "MIDDLE", "X1", "X2"}
MOUSE_WHEEL = {"SCROLL_UP", "SCROLL_DOWN"}
ACTION_TYPES = {
    "keyboard", "mouse_button", "mouse_wheel", "gamepad", "gamepad_trigger", "gamepad_axis",
    # 键盘宏：target 是宏库里的编号。这里**故意**不检查那条宏存不存在——本模块是
    # 纯函数，手上没有宏库；云端校验别人上传的配置时更不可能有。引用失效交给运行
    # 时：界面写「宏已丢失」，输出什么都不做。安静地不动，比按下一串说不清哪来的
    # 键安全得多。
    "macro",
    # 停住一条或多条语音"持续按住"：target 是口令编号，多个编号用列表保存（例如
    # ["game.profile_slot_01", "game.profile_slot_02"]）。按的是哪几个键看每条
    # 口令当时的绑定，这里不抄一份——抄了就会出现口令改了键、这边还在松旧键的情况。
    # 引用的口令不存在或者不是持续按住，运行时什么都不做，和宏丢失一样。任何触发器
    # （区域、动作、姿势、语音）都能绑它，永远是触发一次。
    "voice_release",
    # 系统功能：不按游戏里的键，让本程序自己做一件事（定住区域、视角回正……）。
    # target 必须在 BINDING_SYSTEM_TARGETS 里，电脑端按名字执行。永远是触发一次。
    "system",
}
# 映射表里能选的系统功能。白名单：电脑端按名字执行，云端校验别人上传的配置也照这
# 一份。语音那边另有一份（mapping_schema.VOICE_SYSTEM_TARGETS），两边都有的名字
# 意思一样。录姿势、记录参考场景这类只列在语音里：身体正摆着要录的姿势，没法再用
# 身体去按它。
BINDING_SYSTEM_TARGETS = {
    "ZONES.FREEZE_TOGGLE",  # 定住 / 恢复跟随，来回切
    "ZONES.FREEZE",         # 定住区域
    "ZONES.FOLLOW",         # 区域恢复跟随
    "ZONES.MOVE_HERE",      # 区域挪到我这里：没定住就定住，定住了就整组搬到人现在的位置
    "HEAD.CENTER",          # 视角回正
    "OUTPUT.TOGGLE",        # 开始 / 停止输出，来回切
    "OUTPUT.START",
    "OUTPUT.STOP",
}
# 宏编号的写法。macro_schema 里有同一条规则，那边管宏库自己，这边管绑定引用它，
# 两个入口都得认得同一种编号。
_MACRO_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,31}$")
# 语音口令编号：game.profile_slot_01 这种，小写、点和下划线。
_VOICE_COMMAND_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.]{0,63}$")


def _normalize_voice_command_targets(raw_target) -> str | list[str]:
    """规范化一条或多条要停住的本游戏口令编号。

    旧配置用一个字符串；多选界面保存为列表。保留单项字符串可以让旧快照、
    旧接口和新界面共存，多个编号则按用户选择的顺序保存，并去掉重复项。
    """
    if isinstance(raw_target, set):
        items = sorted(raw_target, key=str)
    elif isinstance(raw_target, (list, tuple)):
        items = list(raw_target)
    else:
        items = [raw_target]
    targets: list[str] = []
    for item in items:
        target = str(item).strip().lower()
        if target.startswith("voice."):
            target = target[len("voice."):]
        if not _VOICE_COMMAND_ID_RE.match(target):
            raise ValueError(f"要停的语音口令不对：{target or '(空)'}")
        if target not in targets:
            targets.append(target)
    if not targets:
        raise ValueError("要停的语音口令不对：(空)")
    return targets[0] if len(targets) == 1 else targets
KEYBOARD_KEYS = (
    {chr(code) for code in range(ord("A"), ord("Z") + 1)}
    | {str(code) for code in range(10)}
    | {"SPACE", "ENTER", "ESC", "TAB", "SHIFT", "CTRL", "ALT", "WIN",
       "BACKSPACE", "DELETE", "HOME", "END", "PAGEUP", "PAGEDOWN",
       "LEFT", "UP", "RIGHT", "DOWN"}
    | {f"F{i}" for i in range(1, 13)}
)
TRIGGER_GROUPS = ("zones", "motions", "poses", "voice")

# 每个游戏都先带着这两个：原地踏步往前走、小腿向后抬起往后退，不用人自己绑。
# 游戏档里写了这两个的（包括写明不绑的）以游戏档为准。其余动作默认不绑，到界面的
# 「动作库」里自己挑。
DEFAULT_MOTION_BINDINGS = {
    "march": {"action": {"type": "gamepad_axis", "target": "LS_UP", "behavior": "hold"}},
    "calf_back": {"action": {"type": "gamepad_axis", "target": "LS_DOWN", "behavior": "hold"}},
}
# 游戏档里一个手柄输出都没有的，多半是不认手柄的键鼠游戏——推左摇杆没反应，
# 走路后退就改按 W / S。
DEFAULT_KEYBOARD_MOTION_BINDINGS = {
    "march": {"action": {"type": "keyboard", "target": "W", "behavior": "hold"}},
    "calf_back": {"action": {"type": "keyboard", "target": "S", "behavior": "hold"}},
}
_GAMEPAD_ACTION_TYPES = {"gamepad", "gamepad_trigger", "gamepad_axis"}

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
        "key_macro": "macro",
        "keyboard_macro": "macro",
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
        # 手柄按键、左摇杆和扳机是三个独立通道，组合可以同时驱动它们。
        # 单独的摇杆方向和扳机仍使用各自的动作类型，保持旧配置含义不变。
        invalid = [part for part in parts if part not in GAMEPAD_COMBO_TARGETS]
        if invalid:
            raise ValueError("不支持的 Xbox 按键：" + ", ".join(sorted(set(invalid))))
        if len(parts) == 1 and parts[0] in GAMEPAD_AXES:
            raise ValueError("单独的摇杆方向请选择“Xbox 左摇杆”类型")
        if len(parts) == 1 and parts[0] in GAMEPAD_TRIGGERS:
            raise ValueError("单独的扳机请选择“Xbox 扳机”类型")
        target = parts[0] if len(parts) == 1 else parts
    elif action_type == "macro":
        # 宏编号是小写的。别的类型一律转大写（键名、按钮名本来就是大写），这里必须
        # 单独走一条路，否则界面上顺手大写一下就再也认不出是哪条宏了。
        target = str(raw_target).strip().lower()
        if not _MACRO_ID_RE.match(target):
            raise ValueError(f"宏编号不对：{target or '(空)'}")
    elif action_type == "voice_release":
        target = _normalize_voice_command_targets(raw_target)
    elif action_type == "system":
        target = str(raw_target).strip().upper()
        if target not in BINDING_SYSTEM_TARGETS:
            raise ValueError(f"不支持的系统功能：{target or '(空)'}")
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
    if action_type in {"mouse_wheel", "voice_release", "system"}:
        behavior = "tap"
    out = {"type": action_type, "target": target, "behavior": behavior}
    # 只给同时含 Xbox 按键和左摇杆方向的组合保存领先时间；普通动作不带这
    # 个字段，保持旧配置的规范化结果和云端文档兼容。接受旧实验字段 lead_ms
    # 作为读取别名，写回时统一为 combo_stick_lead_ms。
    if action_type == "gamepad" and isinstance(target, list):
        parts = set(target)
        if parts & GAMEPAD_BUTTONS and parts & GAMEPAD_AXES:
            raw_lead = action.get("combo_stick_lead_ms", action.get("lead_ms"))
            if raw_lead is not None:
                try:
                    number = float(raw_lead)
                except (TypeError, ValueError):
                    raise ValueError("Xbox 组合领先时间必须是 0 到 200 毫秒") from None
                if not number == number or number in {float("inf"), float("-inf")}:
                    raise ValueError("Xbox 组合领先时间必须是 0 到 200 毫秒")
                out["combo_stick_lead_ms"] = int(round(max(
                    MIN_COMBO_STICK_LEAD_MS, min(MAX_COMBO_STICK_LEAD_MS, number))))
    return out


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
    # 区域的「做动作时也要按」：设过才存，没设过的由内核按框定默认（要跳才碰得到的
    # 框默认是）。只对区域有意义，别的触发带着也不碍事。
    if isinstance(binding.get("with_motion"), bool):
        out["with_motion"] = binding["with_motion"]
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


def with_default_bindings(bindings: dict) -> dict:
    """给一份已经规范化的游戏档绑定补上默认走路、后退里它没写的那几个。

    用手柄那套还是键盘那套，看这份游戏档自己有没有手柄输出。
    """
    out = copy.deepcopy(bindings)
    uses_gamepad = any(
        item.get("action", {}).get("type") in _GAMEPAD_ACTION_TYPES
        for group in out.values() if isinstance(group, dict)
        for item in group.values() if isinstance(item, dict)
    )
    defaults = DEFAULT_MOTION_BINDINGS if uses_gamepad else DEFAULT_KEYBOARD_MOTION_BINDINGS
    motions = out.setdefault("motions", {})
    for ident, binding in defaults.items():
        motions.setdefault(ident, copy.deepcopy(binding))
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
        "gamepad": {
            "targets": sorted(GAMEPAD_BUTTONS),
            "combo_targets": sorted(GAMEPAD_COMBO_TARGETS),
            "allow_combo": True,
        },
        "gamepad_trigger": {"targets": sorted(GAMEPAD_TRIGGERS)},
        "gamepad_axis": {"targets": sorted(GAMEPAD_AXES)},
        # 目标不是固定的一组键，而是用户自己建的宏。界面要另外去宏库拿列表，
        # 所以这里既不给 targets 也不给 free_text。
        "macro": {"library": "macros"},
        # 目标是本游戏里设成"持续按住"的口令，界面从映射表自己的语音行里列。
        "voice_release": {"library": "voice_holds", "behavior": "tap"},
        "system": {"targets": sorted(BINDING_SYSTEM_TARGETS), "behavior": "tap"},
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
