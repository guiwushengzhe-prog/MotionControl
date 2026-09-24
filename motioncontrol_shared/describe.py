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
from .profile_schema import ZONE_ID_MERGES

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
    "headJump": "头顶区",
}

# 四个历史手部 id 在运行时落到哪块物理区域。
#
# normalize_overrides 现在会在规范化时就把它们并成两个，所以新存的文档里不会再
# 出现。但**版本是不可变的**：合并之前存下来的那些永远带着旧 id，而且以后还会被
# 人打开、下载、安装。所以描述仍然要认它们，并且要指出两条落到同一块区域时哪条
# 才真正生效——不指出来，用户会盯着一条从不触发的绑定找原因。
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
    # 录自定义动作。人站在镜头前几米外摆姿势，够不着鼠标——这三个按钮天生就该能用嘴按。
    "pose.record": "录一个新姿势",
    "pose.add_frame": "给刚录的动作再加一个姿势",
    "pose.cancel": "取消录制倒计时",
    **{f"game.profile_slot_{index:02d}": f"当前游戏功能{index}" for index in range(1, 13)},
}

GROUP_NAMES = {
    "zone": "体感区域",
    "motion": "身体动作",
    "pose": "姿势",
    "voice": "本游戏口令",
}

BEHAVIOR_NAMES = {"hold": "持续按住", "tap": "点按", "release": "松开"}

# 语音里的系统命令。原样显示 HEAD_CALIBRATION_START 这种常量，对着屏幕的人
# 得先在脑子里翻译一遍——而这些名字本来就是给人看的。
VOICE_SYSTEM_NAMES = {
    "HEAD_CALIBRATION_START": "开始头控校准",
    "HEAD.CALIBRATE": "头控校准",
    "HEAD.CENTER": "设置头控中心",
    "OUTPUT.START": "开始输出",
    "OUTPUT.STOP": "停止输出",
    "SCENE.CAPTURE_REFERENCE": "记录场景参考图",
    "SCENE.REMATCH": "重新匹配场景",
    "POSE.RECORD": "录一个新姿势",
    "POSE.ADD_FRAME": "给刚录的动作再加一个姿势",
    "POSE.CANCEL": "取消录制倒计时",
}

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
        what = VOICE_SYSTEM_NAMES.get(target, f"系统命令 {target}")
    else:
        what = f"{kind} {target}"
    return f"{what} · {behavior}" if behavior else what


def _annotate_merged_zones(entries: list[dict]) -> None:
    """标出历史手部 id 里哪一条真正生效。

    合并之前存下来的配置可能同时有"上区"和"下区"，而运行时只有一块手部区域。
    内核按 ZONE_ID_MERGES 的顺序取第一条命中的（control_kernel.py:1688），另一条
    从来不触发。两条配的是同一个键时无所谓；配成不同键时，用户会盯着一条永远不
    响应的绑定找半天原因。
    """
    rank = {alias: index for index, (alias, _) in enumerate(ZONE_ID_MERGES)}
    by_runtime: dict[str, list[dict]] = {}
    for entry in entries:
        ident = str(entry["trigger"]).partition(".")[2]
        runtime = ZONE_RUNTIME_ALIASES.get(ident)
        if runtime:
            entry["runtime_zone"] = ZONE_NAMES.get(runtime, runtime)
            by_runtime.setdefault(runtime, []).append(entry)

    for runtime, group in by_runtime.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda e: rank[str(e["trigger"]).partition(".")[2]])
        winner, *losers = group
        same = all(other["action"] == winner["action"] for other in losers)
        for other in losers:
            other["shadowed_by"] = winner["name"]
            other["shadowed_matters"] = not same
        winner["shadows"] = [other["name"] for other in losers]
        winner["shadows_matter"] = not same


def _describe_overrides(overrides: dict) -> dict:
    """一个游戏的自定义绑定，按类别分好。"""
    groups: dict[str, list[dict]] = {}
    for trigger, binding in sorted(overrides.items()):
        prefix = str(trigger).partition(".")[0]
        entry = {
            "trigger": trigger,
            "name": trigger_name(trigger),
            "action": "已关闭" if binding is None else describe_action(
                binding.get("action") if isinstance(binding, dict) else None),
            "disabled": binding is None,
        }
        groups.setdefault(prefix, []).append(entry)
    _annotate_merged_zones(groups.get("zone", []))

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
    # 唤醒词不在可分享的配置里了（见 canonical._normalize_voice_mappings），
    # 所以也不在预览里说——说了反而像在告诉人"装了就会变成这个"。
    mappings = data.get("mappings", [])
    return {
        "kind": "voice_mappings",
        "headline": f"{len(mappings)} 条口令",
        "items": [{
            "phrase": m.get("phrase", ""),
            "synonyms": list(m.get("synonyms", [])),
            "action": describe_action(m),
        } for m in mappings],
    }


def _describe_game_bundle(data: dict) -> dict:
    """一个游戏的全部配置。按键和身体动作一起看，因为它们本来就是一件事。"""
    bindings = _describe_overrides(data.get("overrides", {}))
    motions = _describe_motion_mappings({"motions": data.get("motions", [])})
    enabled = sum(1 for item in motions["items"] if item["enabled"])
    return {
        "kind": "game_bundle",
        "headline": f"{bindings['total']} 条按键绑定，{enabled} 个身体动作",
        "game_id": data.get("game_id", ""),
        "groups": bindings["groups"],
        "total": bindings["total"],
        "motions": motions["items"],
    }


_DESCRIBERS = {
    "profile_selection": _describe_profile_selection,
    "game_bundle": _describe_game_bundle,
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
