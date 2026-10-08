"""无手环运动记录。采样只更新内存，定期在后台保存绝对累计。"""
from __future__ import annotations

import copy
import json
import math
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from motioncontrol.config_transaction import atomic_bytes
from motioncontrol_shared import fitness_schema

DEFAULT_PROFILE = {"weight_kg": 70.0, "goal_active_minutes": 20,
                   "goal_steps": 2000, "goal_kcal": 100, "primary_goal": "minutes",
                   # 按心率算热量要用。可以不填：没填按平均成年人算。
                   "age": None, "sex": None,
                   # 最后一次改的时刻。登录了云端账号时，两台电脑各改一次，后改的留下。
                   "updated_at_ms": 0}
# 2024 成人活动强度汇编，02140/02064/02056。根据动作作近似分类，并非个体测量。
# https://pacompendium.com/conditioning-exercise/
ESTIMATION_REFERENCE = "https://pacompendium.com/conditioning-exercise/"

# 手环心率（手机读「心率广播」发过来）。超过这么久没有新读数就当没有。
HR_FRESH_S = 5.0
# 心率曲线每段多长：给人看走势，也是同步到云端的粒度——不存每秒的读数。
HR_BUCKET_S = 30
# 按心率算热量：Keytel 等 2005（J Sports Sci 23:289），按心率、体重、年龄、性别估总消耗，
# 再扣掉 1 梅脱静息消耗，只留活动热量。这条公式在心率低的时候偏高，90 以下仍按动作估。
HR_KCAL_MIN_BPM = 90
HR_DEFAULT_AGE = 35


def heart_rate_active_kcal_per_min(bpm: float, weight_kg: float, age=None, sex=None) -> float:
    """心率估的活动热量（千卡/分钟）。性别没填取男女两条公式的平均。"""
    age = HR_DEFAULT_AGE if age is None else age
    male = -55.0969 + 0.6309 * bpm + 0.1988 * weight_kg + 0.2017 * age
    female = -20.4022 + 0.4472 * bpm - 0.1263 * weight_kg + 0.074 * age
    kilojoules = male if sex == "male" else female if sex == "female" else (male + female) / 2
    resting = 3.5 * weight_kg / 200.
    return max(0., kilojoules / 4.184 - resting)


def _point(pose, name):
    value = (pose or {}).get(name)
    if not isinstance(value, dict):
        return None
    try:
        x, y = float(value["x"]), float(value["y"])
        confidence = float(value.get("visibility", value.get("score", 1)))
        return (x, y) if all(map(math.isfinite, (x, y, confidence))) and confidence >= .5 else None
    except (KeyError, TypeError, ValueError):
        return None


class FitnessStore:
    def __init__(self, path: Path, *, clock=time.monotonic, wall=time.time, background=True):
        self.path, self.clock, self.wall = Path(path), clock, wall
        self.lock = threading.RLock()
        self.profile = dict(DEFAULT_PROFILE)
        self.sessions = []
        self.checkins = set()
        self.current = None
        self.error = ""
        self._dirty = False
        self._last_at = None
        self._previous = {}
        self._held = set()
        self._step_at = {"left": -math.inf, "right": -math.inf}
        self._hr = None  # (心率, 收到的时刻)
        # 云端同步记的账：每次锻炼传到了哪一版、拉到了哪里、身体数据传到了哪一版。
        self.cloud = {"uploaded": {}, "cursor": "", "profile_pushed_ms": 0}
        self._stop = threading.Event()
        self._load()
        self._thread = None
        if background:
            self._thread = threading.Thread(target=self._save_loop, name="motion-fitness-save", daemon=True)
            self._thread.start()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("sessions"), list):
                raise ValueError("运动记录格式无法识别")
            self._profile(data.get("profile", {}))
            self.sessions = [s for s in data["sessions"] if isinstance(s, dict) and s.get("session_id")]
            self.checkins = {date for date in data.get("checkins", []) if isinstance(date, str)}
            cloud = data.get("cloud")
            if isinstance(cloud, dict) and isinstance(cloud.get("uploaded"), dict):
                self.cloud = {"uploaded": dict(cloud["uploaded"]), "cursor": str(cloud.get("cursor") or ""),
                              "profile_pushed_ms": int(cloud.get("profile_pushed_ms") or 0)}
            # 从云端拉回来的（别的电脑上的）锻炼不能被这台电脑接着记。
            mine = [s for s in self.sessions if s.get("origin") != "remote"]
            if mine and mine[-1].get("status") in {"active", "paused"}:
                self.current = mine[-1]
                self.current["status"] = "paused"
                self._dirty = True
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as exc:
            self.error = "历史记录读取失败，原文件保留：" + str(exc)

    def _profile(self, patch):
        if not isinstance(patch, dict):
            raise ValueError("运动目标必须是一组设置")
        result = dict(self.profile)
        for key, low, high in (("weight_kg", 20, 300), ("goal_active_minutes", 1, 300),
                               ("goal_steps", 1, 100000), ("goal_kcal", 1, 5000)):
            if key in patch:
                value = patch[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                    raise ValueError("体重或目标数值超出有效范围")
                result[key] = float(value) if key == "weight_kg" else int(value)
        if "age" in patch:
            age = patch["age"]
            if age is not None and (isinstance(age, bool) or not isinstance(age, int) or not 10 <= age <= 100):
                raise ValueError("年龄要在 10 到 100 岁之间")
            result["age"] = age
        if "sex" in patch:
            if patch["sex"] not in {None, "male", "female"}:
                raise ValueError("性别只能是男、女或不填")
            result["sex"] = patch["sex"]
        if isinstance(patch.get("updated_at_ms"), int) and not isinstance(patch["updated_at_ms"], bool):
            result["updated_at_ms"] = max(0, patch["updated_at_ms"])
        if "primary_goal" in patch:
            if patch["primary_goal"] not in {"minutes", "steps", "kcal"}:
                raise ValueError("每日主目标必须是运动分钟、步数或热量")
            result["primary_goal"] = patch["primary_goal"]
        self.profile = result

    def control(self, message):
        with self.lock:
            action = message.get("action")
            if action == "profile":
                self.state()  # 已经达标的日子保留，不因提高目标而撤销打卡。
                before = dict(self.profile)
                patch = message.get("profile", {})
                self._profile({key: value for key, value in patch.items() if key != "updated_at_ms"}
                              if isinstance(patch, dict) else patch)
                if self.profile != before:
                    self.profile["updated_at_ms"] = round(self.wall() * 1000)
            elif action == "start":
                if self.current and self.current["status"] != "finished":
                    self.current["status"] = "active"
                else:
                    self.current = {"session_id": uuid.uuid4().hex, "status": "active",
                                    "started_at_ms": round(self.wall() * 1000), "updated_at_ms": 0,
                                    "elapsed_seconds": 0., "active_seconds": 0., "steps": 0,
                                    "action_count": 0, "estimated_kcal": 0., "source": "motion_estimate", "days": {},
                                    "origin": "local"}
                    self.sessions.append(self.current)
            elif action in {"pause", "resume", "finish"}:
                requested = message.get("session_id")
                if requested and (not self.current or requested != self.current["session_id"]):
                    raise ValueError("这次运动已改变，请刷新后重试")
                if self.current and self.current["status"] != "finished":
                    self.current["status"] = {"pause": "paused", "resume": "active", "finish": "finished"}[action]
                    if action == "finish":
                        self.current["ended_at_ms"] = round(self.wall() * 1000)
            else:
                raise ValueError("不支持的运动记录操作")
            self._last_at = None
            self._previous.clear()
            self._held.clear()
            if self.current:
                self.current["updated_at_ms"] = round(self.wall() * 1000)
            self._dirty = True
            return self.state()

    def output_changed(self, enabled):
        if enabled:
            self.control({"action": "start"})
        else:
            self.control({"action": "pause"})

    def _day(self):
        key = datetime.fromtimestamp(self.wall()).astimezone().date().isoformat()
        return self.current["days"].setdefault(key, {"active_seconds": 0., "steps": 0,
                                                   "estimated_kcal": 0., "action_count": 0})

    def step_event(self, side, now):
        with self.lock:
            if not self.current or self.current["status"] != "active" or side not in self._step_at:
                return
            if not math.isfinite(now) or now - self._step_at[side] < .15:
                return
            self._step_at[side] = now
            self.current["steps"] += 1
            self._day()["steps"] += 1
            self.current["updated_at_ms"] = round(self.wall() * 1000)
            self._dirty = True

    def heart_rate(self, message):
        """手机发来的一次心率。只在记录中才用：并进本次的平均、最高和曲线。"""
        bpm = message.get("bpm")
        if isinstance(bpm, bool) or not isinstance(bpm, int) or not 25 <= bpm <= 250:
            raise ValueError("心率读数无效")
        with self.lock:
            if not self.current or self.current["status"] != "active":
                self._hr = None
                return
            self._hr = (bpm, self.clock())
            session = self.current
            count = session.get("hr_samples", 0)
            session["hr_avg"] = round((session.get("hr_avg", 0.) * count + bpm) / (count + 1), 1)
            session["hr_max"] = max(session.get("hr_max", 0), bpm)
            session["hr_samples"] = count + 1
            curve = session.setdefault("hr_curve", [])
            offset = int(session["elapsed_seconds"] // HR_BUCKET_S * HR_BUCKET_S)
            if curve and curve[-1][0] == offset:
                n = session.get("hr_bucket_n", 1)
                curve[-1][1] = round((curve[-1][1] * n + bpm) / (n + 1), 1)
                session["hr_bucket_n"] = n + 1
            else:
                curve.append([offset, float(bpm)])
                session["hr_bucket_n"] = 1
            session["updated_at_ms"] = round(self.wall() * 1000)
            self._dirty = True

    def _fresh_hr(self, now):
        if self._hr is None or not 0 <= now - self._hr[1] <= HR_FRESH_S:
            return None
        return self._hr[0]

    def observe_pose(self, pose, now, *, actions=()):
        with self.lock:
            if not self.current or self.current["status"] != "active":
                self._last_at = None
                return
            shoulder_l, shoulder_r = _point(pose, "left_shoulder"), _point(pose, "right_shoulder")
            if not shoulder_l or not shoulder_r or not math.isfinite(now):
                self._last_at = None
                self._previous.clear()
                self._held.clear()
                return
            anchor = ((shoulder_l[0] + shoulder_r[0]) / 2, (shoulder_l[1] + shoulder_r[1]) / 2)
            scale = max(.05, math.dist(shoulder_l, shoulder_r))
            points = {name: ((p[0] - anchor[0]) / scale, (p[1] - anchor[1]) / scale)
                      for name in ("left_wrist", "right_wrist", "left_knee", "right_knee", "left_ankle", "right_ankle")
                      if (p := _point(pose, name))}
            dt = now - self._last_at if self._last_at is not None else 0.
            held = {str(a) for a in actions if a != "motion.march"}
            if 0 < dt <= .5:
                speed = max((math.dist(p, self._previous[n]) / dt for n, p in points.items() if n in self._previous), default=0.)
                # 上肢微小检测噪声和单纯转头不作为运动；仍保留已经识别的身体动作。
                moving = speed >= .18 or bool(held) or "motion.march" in actions
                day = self._day()
                self.current["elapsed_seconds"] += dt
                bpm = self._fresh_hr(self.clock())
                if bpm is not None:
                    self.current["hr_seconds"] = self.current.get("hr_seconds", 0.) + dt
                kcal = 0.
                if bpm is not None and bpm >= HR_KCAL_MIN_BPM:
                    # 读到手环心率就按心率算：停下来喘气时心跳还高，也是这次锻炼消耗的。
                    kcal = heart_rate_active_kcal_per_min(bpm, self.profile["weight_kg"], self.profile.get("age"),
                                                          self.profile.get("sex")) * dt / 60.
                elif moving:
                    met = 3.8 if "motion.march" in actions else 3.0 if held else 2.5
                    # 只估计活动热量：去掉1倍静息消耗，不把坐着等待算成锻炼。
                    kcal = (met - 1.) * 3.5 * self.profile["weight_kg"] / 200. * dt / 60.
                for record in (self.current, day):
                    if moving:
                        record["active_seconds"] += dt
                    record["estimated_kcal"] += kcal
                # 一半以上的时间读到了心率，这次的热量就算「按心率估算」。
                self.current["kcal_source"] = ("heart_rate" if self.current.get("hr_seconds", 0.)
                                               >= .5 * self.current["elapsed_seconds"] else "motion")
                count = len(held - self._held)
                self.current["action_count"] += count
                day["action_count"] += count
                self.current["updated_at_ms"] = round(self.wall() * 1000)
                self._dirty = True
            self._last_at, self._previous, self._held = now, points, held

    # ---------- 云端同步（见 fitness_sync） ----------

    def cloud_pending(self, limit=fitness_schema.MAX_SESSIONS_PER_UPLOAD):
        """还没传上去的：改过的锻炼摘要、改过的身体数据、打卡日子。"""
        with self.lock:
            uploaded = self.cloud["uploaded"]
            sessions, bad = [], 0
            for session in self.sessions:
                if session.get("updated_at_ms", 0) <= uploaded.get(session["session_id"], -1):
                    continue
                try:
                    sessions.append(fitness_schema.normalize_session(session))
                except ValueError:
                    bad += 1  # 坏的一条不拖累别的
                if len(sessions) >= limit:
                    break
            profile = dict(self.profile) if self.profile.get("updated_at_ms", 0) > self.cloud["profile_pushed_ms"] else None
            return {"sessions": sessions, "profile": profile, "checkins": sorted(self.checkins), "skipped": bad}

    def cloud_mark_uploaded(self, sessions):
        with self.lock:
            for doc in sessions:
                self.cloud["uploaded"][doc["session_id"]] = doc["updated_at_ms"]
            self._dirty = True

    def cloud_mark_profile(self, updated_at_ms):
        with self.lock:
            self.cloud["profile_pushed_ms"] = max(self.cloud["profile_pushed_ms"], int(updated_at_ms))
            self._dirty = True

    def cloud_reset(self):
        """换了账号（或者退出再登录）：从头拉、从头传。"""
        with self.lock:
            self.cloud = {"uploaded": {}, "cursor": "", "profile_pushed_ms": 0}
            self._dirty = True

    def merge_remote(self, payload, cursor=None):
        """把云端拉回来的并进本机。规则和云端合并同一份（fitness_schema.merge_session）。"""
        with self.lock:
            local = {session["session_id"]: session for session in self.sessions}
            for raw in payload.get("sessions", []):
                remote = fitness_schema.normalize_session(raw)
                mine = local.get(remote["session_id"])
                if mine is None:
                    imported = {key: value for key, value in remote.items() if key != "schema"}
                    imported.update(source="motion_estimate", origin="remote")
                    self.sessions.append(imported)
                    local[remote["session_id"]] = imported
                    self.cloud["uploaded"][remote["session_id"]] = remote["updated_at_ms"]
                    continue
                try:
                    merged = fitness_schema.merge_session(fitness_schema.normalize_session(mine), remote)
                except ValueError:
                    continue
                mine.update({key: value for key, value in merged.items() if key != "schema"})
                if remote["updated_at_ms"] >= mine.get("updated_at_ms", 0):
                    self.cloud["uploaded"][remote["session_id"]] = mine["updated_at_ms"]
            self.sessions.sort(key=lambda session: session.get("started_at_ms", 0))
            remote_profile = payload.get("profile")
            if isinstance(remote_profile, dict) and remote_profile.get("updated_at_ms", 0) > self.profile.get("updated_at_ms", 0):
                try:
                    self._profile({key: value for key, value in remote_profile.items() if key != "schema"})
                    self.cloud["profile_pushed_ms"] = self.profile["updated_at_ms"]
                except ValueError:
                    pass
            self.checkins.update(day for day in payload.get("checkins", []) if isinstance(day, str))
            if cursor is not None:
                self.cloud["cursor"] = cursor
            self._dirty = True

    def history(self):
        with self.lock:
            return copy.deepcopy(self.sessions)

    def state(self):
        with self.lock:
            result = copy.deepcopy(self.current) if self.current else {"session_id": "", "status": "idle"}
            days = {}
            for session in self.sessions:
                for date, values in session.get("days", {}).items():
                    row = days.setdefault(date, {"active_seconds": 0., "steps": 0, "estimated_kcal": 0., "action_count": 0})
                    for key in row:
                        row[key] += values.get(key, 0)
            metric, goal = {"minutes": ("active_seconds", self.profile["goal_active_minutes"] * 60),
                            "steps": ("steps", self.profile["goal_steps"]),
                            "kcal": ("estimated_kcal", self.profile["goal_kcal"])}[self.profile["primary_goal"]]
            today = datetime.fromtimestamp(self.wall()).astimezone().date()
            day = days.get(today.isoformat(), {"active_seconds": 0., "steps": 0, "estimated_kcal": 0., "action_count": 0})
            if day[metric] >= goal and today.isoformat() not in self.checkins:
                self.checkins.add(today.isoformat())
                self._dirty = True
            checkins = sorted(self.checkins)
            cursor = today if today.isoformat() in checkins else today - timedelta(days=1)
            streak = 0
            while cursor.isoformat() in checkins:
                streak += 1
                cursor -= timedelta(days=1)
            experience = int(sum(d["active_seconds"] for d in days.values()) / 60) * 10 + len(checkins) * 50
            result.update(type="fitness_state_v1", profile=dict(self.profile), today=day, daily=days,
                          checkins=checkins, streak=streak, experience=experience, level=1 + experience // 100,
                          error=self.error, estimation_reference=ESTIMATION_REFERENCE)
            return result

    def flush(self):
        with self.lock:
            self.state()
            if not self._dirty or self.error.startswith("历史记录读取失败"):
                return
            data = json.dumps({"schema": 1, "profile": self.profile, "sessions": self.sessions,
                               "checkins": sorted(self.checkins), "cloud": self.cloud},
                              ensure_ascii=False).encode("utf-8")
            self._dirty = False
        try:
            atomic_bytes(self.path, data)
            self.error = ""
        except OSError as exc:
            with self.lock:
                self._dirty = True
                self.error = "运动记录暂未保存：" + str(exc)

    def _save_loop(self):
        while not self._stop.wait(3):
            self.flush()

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=4)
        self.flush()
