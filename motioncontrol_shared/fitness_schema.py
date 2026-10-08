"""运动记录同步到云端的那部分：身体数据和每次锻炼的摘要。电脑和云端用同一份规则。

只存摘要：每次锻炼的时长、步数、动作次数、热量，平均和最高心率，每 30 秒一个点的
心率曲线，按天的小计。不存每秒的心率、骨架、画面——换设备时要的是"我练了多少"，
不是原始数据。

合并规则也在这里，电脑拉回云端的记录、云端收到电脑上传的记录，走的是同一条：
同一次锻炼（session_id 相同）累计值取大的，结束了就不会再变回进行中，心率摘要跟着
更新时间新的那一份。所以同一份记录传两遍、乱序到达，结果都一样。
"""

from __future__ import annotations

import math
import re

PROFILE_SCHEMA = "motioncontrol.fitness_profile.v1"
SESSION_SCHEMA = "motioncontrol.fitness_session.v1"

# 一次最多传多少条：攒了很久没联网，分几次传完。
MAX_SESSIONS_PER_UPLOAD = 200
# 心率曲线 30 秒一个点，一天也就 2880 个。
MAX_CURVE_POINTS = 2880
HR_CURVE_STEP_S = 30
# 一次锻炼跨过的日子：通宵玩也就两三天，多了是坏数据。
MAX_DAYS_PER_SESSION = 7
# 打卡日期：十年也就三千多天。
MAX_CHECKINS = 4000

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
STATUSES = ("active", "paused", "finished")
GOALS = ("minutes", "steps", "kcal")
SEXES = ("male", "female")

# 数值字段：名字、下限、上限、要不要整数。上限是"不可能更大"，不是"一般不会更大"。
_WEEK_S = 7 * 24 * 3600.0
_COUNTERS = (
    ("elapsed_seconds", 0.0, _WEEK_S, False),
    ("active_seconds", 0.0, _WEEK_S, False),
    ("steps", 0, 10_000_000, True),
    ("action_count", 0, 10_000_000, True),
    ("estimated_kcal", 0.0, 100_000.0, False),
)
_DAY_COUNTERS = (
    ("active_seconds", 0.0, 24 * 3600.0, False),
    ("steps", 0, 10_000_000, True),
    ("action_count", 0, 10_000_000, True),
    ("estimated_kcal", 0.0, 100_000.0, False),
)
_PROFILE_NUMBERS = (
    ("weight_kg", 20.0, 300.0, False),
    ("goal_active_minutes", 1, 300, True),
    ("goal_steps", 1, 100_000, True),
    ("goal_kcal", 1, 5000, True),
)
_MAX_MS = 10 ** 14  # 公元 5000 年以前


def _number(raw, name: str, low, high, integer: bool):
    value = raw.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} 必须是数字")
    if integer and value != int(value):
        raise ValueError(f"{name} 必须是整数")
    if not low <= value <= high:
        raise ValueError(f"{name} 超出范围")
    return int(value) if integer else round(float(value), 3)


def _ms(raw, name: str, *, required: bool = True):
    value = raw.get(name)
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_MS:
        raise ValueError(f"{name} 必须是毫秒时间戳")
    return value


def _bpm(value, name: str):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or not 25 <= value <= 250:
        raise ValueError(f"{name} 不是合理的心率")
    return round(float(value), 1)


def normalize_profile(raw) -> dict:
    """身体数据和每日目标。年龄、性别可以不填（null）。"""
    if not isinstance(raw, dict):
        raise ValueError("身体数据必须是一个对象")
    out = {"schema": PROFILE_SCHEMA}
    for name, low, high, integer in _PROFILE_NUMBERS:
        out[name] = _number(raw, name, low, high, integer)
    if raw.get("primary_goal") not in GOALS:
        raise ValueError("每日主目标必须是运动分钟、步数或热量")
    out["primary_goal"] = raw["primary_goal"]
    age = raw.get("age")
    if age is not None and (isinstance(age, bool) or not isinstance(age, int) or not 10 <= age <= 100):
        raise ValueError("年龄要在 10 到 100 岁之间")
    out["age"] = age
    if raw.get("sex") not in (None, *SEXES):
        raise ValueError("性别只能是男、女或不填")
    out["sex"] = raw.get("sex")
    # 哪台设备最后改的算数：同一个人在两台电脑上各改一次，后改的那次留下。
    out["updated_at_ms"] = _ms(raw, "updated_at_ms")
    return out


def normalize_session(raw) -> dict:
    """一次锻炼的摘要。认不得的字段丢掉（电脑内部记账用的那些不上云）。"""
    if not isinstance(raw, dict):
        raise ValueError("运动记录必须是一个对象")
    session_id = raw.get("session_id")
    if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
        raise ValueError("运动记录编号不对")
    if raw.get("status") not in STATUSES:
        raise ValueError("运动记录状态不对")
    out = {"schema": SESSION_SCHEMA, "session_id": session_id, "status": raw["status"],
           "started_at_ms": _ms(raw, "started_at_ms"), "updated_at_ms": _ms(raw, "updated_at_ms")}
    ended = _ms(raw, "ended_at_ms", required=False)
    if ended is not None:
        out["ended_at_ms"] = ended
    for name, low, high, integer in _COUNTERS:
        out[name] = _number(raw, name, low, high, integer)
    out["kcal_source"] = "heart_rate" if raw.get("kcal_source") == "heart_rate" else "motion"

    if raw.get("hr_avg") is not None:
        out["hr_avg"] = _bpm(raw["hr_avg"], "hr_avg")
        out["hr_max"] = _bpm(raw.get("hr_max", raw["hr_avg"]), "hr_max")
        out["hr_seconds"] = _number(raw, "hr_seconds", 0.0, _WEEK_S, False) if "hr_seconds" in raw else 0.0
        curve = raw.get("hr_curve", [])
        if not isinstance(curve, list) or len(curve) > MAX_CURVE_POINTS:
            raise ValueError("心率曲线太长或格式不对")
        points = []
        for point in curve:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError("心率曲线的点要是 [第几秒, 心率]")
            offset = point[0]
            if isinstance(offset, bool) or not isinstance(offset, (int, float)) or not 0 <= offset <= _WEEK_S \
                    or offset % HR_CURVE_STEP_S:
                raise ValueError("心率曲线的时间要是 30 秒的整数倍")
            points.append([int(offset), _bpm(point[1], "hr_curve")])
        out["hr_curve"] = points

    days = raw.get("days", {})
    if not isinstance(days, dict) or len(days) > MAX_DAYS_PER_SESSION:
        raise ValueError("按天的小计格式不对")
    out["days"] = {}
    for day, counters in sorted(days.items()):
        if not isinstance(day, str) or not DAY_RE.match(day) or not isinstance(counters, dict):
            raise ValueError("按天的小计格式不对")
        out["days"][day] = {name: _number(counters, name, low, high, integer)
                            for name, low, high, integer in _DAY_COUNTERS}
    return out


def normalize_checkins(raw) -> list[str]:
    if not isinstance(raw, list) or len(raw) > MAX_CHECKINS:
        raise ValueError("打卡日期格式不对")
    if not all(isinstance(day, str) and DAY_RE.match(day) for day in raw):
        raise ValueError("打卡日期格式不对")
    return sorted(set(raw))


def merge_session(old: dict | None, new: dict) -> dict:
    """同一次锻炼的两份摘要合成一份。两份都要先过 normalize_session。"""
    if old is None:
        return dict(new)
    newer, older = (new, old) if new["updated_at_ms"] >= old["updated_at_ms"] else (old, new)
    out = dict(newer)
    out["started_at_ms"] = min(old["started_at_ms"], new["started_at_ms"])
    out["updated_at_ms"] = newer["updated_at_ms"]
    # 结束了就是结束了：迟到的"进行中"不能把它复活。
    if "finished" in (old["status"], new["status"]):
        out["status"] = "finished"
        ended = max((item["ended_at_ms"] for item in (old, new) if "ended_at_ms" in item), default=None)
        if ended is not None:
            out["ended_at_ms"] = ended
    for name, _, _, integer in _COUNTERS:
        out[name] = max(old[name], new[name])
    days = {}
    for day in sorted(set(old["days"]) | set(new["days"])):
        a, b = old["days"].get(day), new["days"].get(day)
        days[day] = dict(a or b) if not (a and b) else {name: max(a[name], b[name]) for name, *_ in _DAY_COUNTERS}
    out["days"] = days
    # 心率摘要是整份算出来的，拆开取大会拼出一份谁也没测过的数：跟着新的那份走，
    # 新的没有心率就留旧的。
    if "hr_avg" not in newer and "hr_avg" in older:
        for name in ("hr_avg", "hr_max", "hr_seconds", "hr_curve"):
            out[name] = older[name]
        out["kcal_source"] = older["kcal_source"]
    return out
