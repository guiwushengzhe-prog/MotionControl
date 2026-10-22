"""灵敏度实验配置；识别开关不改变既有游戏档案。"""

DEFAULTS = {
    "head_hold": False,
    "first_step": False,
    "lean_strafe": False,
    "lean_back": False,
    "lean_forward": False,
    "body_source": "model",
    "side_source": "model",
    "fore_aft_source": "model",
    "learn_from_depth": False,
    "learn_monocular": False,
    "compare": False,
}
LEAN_MOTIONS = ("lean_left", "lean_right", "lean_back", "lean_forward")


def configure(current, changes):
    if not isinstance(changes, dict) or set(changes) - set(DEFAULTS):
        raise ValueError("灵敏度实验设置无效")
    result = {**DEFAULTS, **current}
    # 旧配置只有一个来源，首次读取时两轴继承它；已分开选择的轴保持独立。
    for key in ("side_source", "fore_aft_source"):
        if key not in current:
            result[key] = result["body_source"]
    linked = result["side_source"] == result["fore_aft_source"] == result["body_source"]
    for key, value in changes.items():
        if key in ("body_source", "side_source", "fore_aft_source"):
            if value not in ({"model", "depth", "monocular"} if key == "fore_aft_source" else {"model", "depth"}):
                raise ValueError("请选择有效的身体识别来源；单目深度仅用于前后倾")
        elif not isinstance(value, bool):
            raise ValueError("实验开关必须为开启或关闭")
        result[key] = value
    if "body_source" in changes and linked:
        for key in ("side_source", "fore_aft_source"):
            if key not in changes:
                result[key] = result["body_source"]
    return result


def load(raw):
    result = dict(DEFAULTS)
    if isinstance(raw, dict):
        for key in DEFAULTS:
            try:
                result = configure(result, {key: raw[key]}) if key in raw else result
            except (ValueError, TypeError):
                pass
    return result


def lean_binding(forward, ident):
    """只为明确的前进方向派生默认方向；自定义绑定优先，特殊键位不猜。"""
    action = (forward or {}).get("action", {})
    if not isinstance(action, dict):
        return None
    kind, target = action.get("type"), action.get("target")
    directions = {
        ("keyboard", "W"): ("A", "D", "S", "W"),
        ("keyboard", "UP"): ("LEFT", "RIGHT", "DOWN", "UP"),
        ("gamepad_axis", "LS_UP"): ("LS_LEFT", "LS_RIGHT", "LS_DOWN", "LS_UP"),
    }.get((kind, target))
    if directions is None or ident not in LEAN_MOTIONS:
        return None
    return {"action": {"type": kind, "target": directions[LEAN_MOTIONS.index(ident)], "behavior": "hold"}}
