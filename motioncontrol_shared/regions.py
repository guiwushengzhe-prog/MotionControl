"""每个游戏共用的独立触发框定义；与各模式的按键映射分开保存。"""

from __future__ import annotations

import math
import re

MAX_CUSTOM_REGIONS = 12
REGION_NAME_MAX = 24
_REGION_ID = re.compile(r"^user_[0-9a-f]{8}$")


def is_region_id(value) -> bool:
    return isinstance(value, str) and bool(_REGION_ID.fullmatch(value))


def normalize_region_name(value) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > REGION_NAME_MAX:
        raise ValueError(f"区域名称应为 1～{REGION_NAME_MAX} 个字")
    return value.strip()


def normalize_region_rect(raw) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("区域位置必须是矩形")
    values = {}
    for key in ("x1", "x2", "y1", "y2"):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("区域坐标必须是画面内的有限数值")
        values[key] = float(value)
    if values["x2"] - values["x1"] < .02 - 1e-9 or values["y2"] - values["y1"] < .02 - 1e-9:
        raise ValueError("区域宽高至少应为画面的 2%")
    return values


def normalize_follow_rect(raw) -> dict:
    """相对躯干的矩形；不是画面坐标，因此允许负值和超出画面。"""
    if not isinstance(raw, dict):
        raise ValueError("跟随区域缺少身体比例")
    out = {}
    for key in ("x1", "x2", "y1", "y2"):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > 100:
            raise ValueError("跟随区域比例无效")
        out[key] = float(value)
    if out["x2"] - out["x1"] < .001 or out["y2"] - out["y1"] < .001:
        raise ValueError("跟随区域宽高必须大于零")
    return out


def normalize_regions(raw) -> list[dict]:
    if not isinstance(raw, list) or len(raw) > MAX_CUSTOM_REGIONS:
        raise ValueError(f"每个游戏最多新增 {MAX_CUSTOM_REGIONS} 个区域")
    result = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict) or not is_region_id(item.get("id")) or item["id"] in seen:
            raise ValueError("自定义区域编号无效或重复，不能操作内置区域")
        seen.add(item["id"])
        normalized = {"id": item["id"], "name": normalize_region_name(item.get("name")),
                      "rect": normalize_region_rect(item.get("rect"))}
        if "mode" in item:
            if item["mode"] not in ("fixed", "following"):
                raise ValueError("区域模式只能是跟随或固定")
            normalized["mode"] = item["mode"]
            if item["mode"] == "following":
                normalized["follow"] = normalize_follow_rect(item.get("follow"))
        result.append(normalized)
    return result
