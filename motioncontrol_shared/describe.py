"""把一份配置翻译成人话。

云端要在配置详情页上说清楚"这份配置到底干什么"，而这件事不能靠上传者自己写
简介——简介可能是空的、可能过时、也可能和文件内容对不上。唯一可靠的来源是文件
本身，所以这里从**校验并规范化之后的文档**生成描述，逐条对应，不会说谎。

桌面端也要用：安装别人的配置之前，应该能看到它会改掉哪些键，而不是点一个
"安装"就听天由命。所以它放在共享包里，两边同一套说法。

里面的中文名是数据不是逻辑，来源分散在三处（web/app.js 的触发器表、
control_kernel.py 的 BODY_ZONES、config/voice_commands_v094.json），云端一处
都读不到。tests/test_describe.py 断言这里的名字和那三处一一对得上——改了那边忘了
改这边，测试会红，而不是让云端显示一个过时的名字。
"""

from __future__ import annotations

from .mapping_schema import normalize_key_combo  # noqa: F401  (re-export 方便调用方)

# 体感区域。四个"上/下"是历史 id：运行时它们已经合并成左手区/右手区两块，
# 见 ZONE_RUNTIME_ALIASES。旧配置仍然写着它们，所以两套都要认。
ZONE_NAMES = {
    "leftHand": "左手区",
    "rightHand": "右手区",
    "leftHandUpper": "左手上区",
    "leftHandLower": "左手下区",
    "rightHandUpper": "右手上区",
    "rightHandLower": "右手下区",
    "leftFoot": "左脚区",
    "rightFoot": "右脚区",
    "headJump": "头顶跳跃区",
}

# 四个历史手部 id 在运行时落到哪块物理区域。两个 id 指向同一块区域时，它们会
# 同时触发，这是描述里必须说出来的事——否则用户会以为自己分到了四块。
ZONE_RUNTIME_ALIASES = {
    "leftHandUpper": "leftHand",
    "leftHandLower": "leftHand",
    "rightHandUpper": "rightHand",
    "rightHandLower": "rightHand",
}

MOTION_NAMES = {
    "march": "原地踏步",
    "calf_back": "小腿向后",
    "squat": "下蹲",
    "hands_up": "双手举过头",
    "jumping_jack": "开合跳",
    "side_step_jack": "侧步开合",
    "cross_knee_elbow": "提膝碰对侧肘",
}

POSE_NAMES = {
    "hands_cross": "双手交叉",
}

VOICE_COMMAND_NAMES = {
    "system.emergency_stop": "紧急停止",
    "output.start": "开始输出",
    "output.stop": "停止输出",
    "scene.capture": "截图/记录场景",
    "scene.rematch": "重新匹配场景",
    "head.calibrate": "开始头控校准",
    "head.center": "设置头控中心",
    "ui.confirm": "确认",
    "ui.back": "返回",
    "nav.up": "菜单上",
    "nav.down": "菜单下",
    "nav.left": "菜单左",
    "nav.right": "菜单右",
    "game.accelerate": "加速/前进",
    "game.left_move": "左移",
    "game.brake": "刹车/后退",
    "game.right_move": "右移",
    "game.jump": "跳跃",
    "game.interact": "互动",
    "game.reload": "换弹",
    "ui.map": "地图",
    "ui.inventory": "背包",
    "game.attack": "攻击",
    "game.dodge": "闪避",
    "game.skill": "技能",
    "game.use_item": "使用物品",
    "ui.menu": "打开菜单",
    **{f"game.profile_slot_{index:02d}": f"当前游戏功能{index}" for index in range(1, 13)},
}

GROUP_NAMES = {
    "zone": "体感区域",
    "motion": "身体动作",
    "pose": "姿势",
    "voice": "语音口令",
}

BEHAVIOR_NAMES = {"hold": "持续按住", "tap": "点按", "release": "松开"}

_AXIS_NAMES = {"LS_UP": "向上", "LS_DOWN": "向下", "LS_LEFT": "向左", "LS_RIGHT": "向右"}
_MOUSE_NAMES = {"LEFT": "左键", "RIGHT": "右键", "MIDDLE": "中键", "X1": "侧键1", "X2": "侧键2"}
_WHEEL_NAMES = {"SCROLL_UP": "向上滚", "SCROLL_DOWN": "向下滚"}


def trigger_name(trigger: str) -> str:
    """``"zone.leftHandUpper"`` -> ``"左手上区"``，认不出就原样返回。"""
    prefix, _, ident = str(trigger).partition(".")
    table = {"zone": ZONE_NAMES, "motion": MOTION_NAMES,
             "pose": POSE_NAMES, "voice": VOICE_COMMAND_NAMES}.get(prefix)
    if table is None:
        return str(trigger)
    return table.get(ident, ident)


def describe_action(action) -> str:
    """一个动作的人类说法，例如 ``"Xbox X 键 · 持续按住"``。"""
    if not isinstance(action, dict):
        return "（无效）"
    kind = str(action.get("type", ""))
    raw = action.get("target", "")
    target = "+".join(str(part) for part in raw) if isinstance(raw, (list, tuple)) else str(raw)
    behavior = BEHAVIOR_NAMES.get(str(action.get("behavior", "")), "")

    if kind == "keyboard":
        what = f"键盘 {target}"
    elif kind == "gamepad":
        what = f"Xbox {target} 键"
    elif kind == "gamepad_axis":
        what = f"左摇杆 {_AXIS_NAMES.get(target, target)}"
    elif kind == "gamepad_trigger":
        what = f"扳机 {target}"
    elif kind == "mouse_button":
        what = f"鼠标 {_MOUSE_NAMES.get(target, target)}"
    elif kind == "mouse_wheel":
        what = f"滚轮 {_WHEEL_NAMES.get(target, target)}"
    elif kind == "system":
        what = f"系统命令 {target}"
    else:
        what = f"{kind} {target}"
    return f"{what} · {behavior}" if behavior else what


def _describe_overrides(overrides: dict) -> dict:
    """一个游戏的自定义绑定，按类别分好。"""
    groups: dict[str, list[dict]] = {}
    for trigger, binding in sorted(overrides.items()):
        prefix = str(trigger).partition(".")[0]
        ident = str(trigger).partition(".")[2]
        entry = {
            "trigger": trigger,
            "name": trigger_name(trigger),
            "action": "已关闭" if binding is None else describe_action(
                binding.get("action") if isinstance(binding, dict) else None),
            "disabled": binding is None,
        }
        # 两个历史 id 落到同一块物理区域时会同时触发。不说的话，用户会以为
        # 自己分到了四块独立的手部区域。
        runtime = ZONE_RUNTIME_ALIASES.get(ident)
        if prefix == "zone" and runtime:
            entry["runtime_zone"] = ZONE_NAMES.get(runtime, runtime)
        groups.setdefault(prefix, []).append(entry)

    return {
        "total": sum(len(items) for items in groups.values()),
        "groups": [
            {"key": key, "name": GROUP_NAMES.get(key, key), "items": items}
            for key, items in sorted(groups.items())
        ],
    }


def _describe_profile_selection(data: dict) -> dict:
    by_profile = data.get("overrides_by_profile", {})
    games = [
        {"game_id": game_id, **_describe_overrides(overrides)}
        for game_id, overrides in sorted(by_profile.items())
    ]
    return {
        "kind": "profile_selection",
        "headline": f"{len(games)} 个游戏，共 {sum(g['total'] for g in games)} 条自定义绑定",
        "selected_id": data.get("selected_id"),
        "games": games,
    }


def _describe_motion_mappings(data: dict) -> dict:
    motions = data.get("motions", [])
    enabled = [m for m in motions if m.get("enabled")]
    return {
        "kind": "motion_mappings",
        "headline": f"{len(motions)} 个身体动作，启用 {len(enabled)} 个",
        "items": [{
            "name": MOTION_NAMES.get(m.get("id"), m.get("name") or m.get("id")),
            "enabled": bool(m.get("enabled")),
            "action": describe_action({"type": m.get("type"), "target": m.get("target")})
                      if m.get("target") else "未设置输出",
        } for m in motions],
    }


def _describe_voice_mappings(data: dict) -> dict:
    mappings = data.get("mappings", [])
    return {
        "kind": "voice_mappings",
        "headline": f"唤醒词「{data.get('wake_word', '')}」，{len(mappings)} 条口令",
        "wake_word": data.get("wake_word", ""),
        "emergency_stop_phrases": list(data.get("emergency_stop_phrases", [])),
        "items": [{
            "phrase": m.get("phrase", ""),
            "synonyms": list(m.get("synonyms", [])),
            "action": describe_action(m),
        } for m in mappings],
    }


_DESCRIBERS = {
    "profile_selection": _describe_profile_selection,
    "motion_mappings": _describe_motion_mappings,
    "voice_mappings": _describe_voice_mappings,
}


def describe(doc_type: str, data: dict) -> dict:
    """从**规范化之后**的文档生成描述。

    只接受规范化输出，不接受原始上传内容：规范化会丢掉未知键、补齐默认值、
    统一大小写，对着原始内容描述会说出文件里写着但实际不生效的东西。
    """
    try:
        describer = _DESCRIBERS[doc_type]
    except KeyError:
        raise ValueError(f"unknown document type: {doc_type!r}") from None
    return describer(data if isinstance(data, dict) else {})
