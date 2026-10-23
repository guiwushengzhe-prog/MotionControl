"""灵敏度实验配置；识别开关不改变既有游戏档案。"""
import math

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
    "monocular_forward_on_deg": 7.0,
    "monocular_back_on_deg": 7.0,
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
        if key in ("monocular_forward_on_deg", "monocular_back_on_deg"):
            if type(value) not in (int, float) or not math.isfinite(value) or not 2 <= value <= 15:
                raise ValueError("单目前后倾响应门槛需要在2°至15°之间")
            value = float(value)
        elif key in ("body_source", "side_source", "fore_aft_source"):
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
