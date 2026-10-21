"""灵敏度实验配置；识别开关不改变既有游戏档案。"""

DEFAULTS = {
    "head_hold": False,
    "first_step": False,
    "lean_strafe": False,
    "lean_back": False,
    "body_source": "model",
    "compare": False,
}
LEAN_MOTIONS = ("lean_left", "lean_right", "lean_back")


def configure(current, changes):
    if not isinstance(changes, dict) or set(changes) - set(DEFAULTS):
        raise ValueError("灵敏度实验设置无效")
    result = {**DEFAULTS, **current}
    for key, value in changes.items():
        if key == "body_source":
            if value not in {"model", "depth"}:
                raise ValueError("请选择模型估计或深度实测")
        elif not isinstance(value, bool):
            raise ValueError("实验开关必须为开启或关闭")
        result[key] = value
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
        ("keyboard", "W"): ("A", "D", "S"),
        ("keyboard", "UP"): ("LEFT", "RIGHT", "DOWN"),
        ("gamepad_axis", "LS_UP"): ("LS_LEFT", "LS_RIGHT", "LS_DOWN"),
    }.get((kind, target))
    if directions is None or ident not in LEAN_MOTIONS:
        return None
    return {"action": {"type": kind, "target": directions[LEAN_MOTIONS.index(ident)], "behavior": "hold"}}
