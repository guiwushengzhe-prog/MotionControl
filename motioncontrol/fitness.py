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

DEFAULT_PROFILE = {"weight_kg": 70.0, "goal_active_minutes": 20,
                   "goal_steps": 2000, "goal_kcal": 100, "primary_goal": "minutes"}
# 2024 成人活动强度汇编，02140/02064/02056。根据动作作近似分类，并非个体测量。
# https://pacompendium.com/conditioning-exercise/
ESTIMATION_REFERENCE = "https://pacompendium.com/conditioning-exercise/"


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
            if self.sessions and self.sessions[-1].get("status") in {"active", "paused"}:
                self.current = self.sessions[-1]
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
                self._profile(message.get("profile", {}))
            elif action == "start":
                if self.current and self.current["status"] != "finished":
                    self.current["status"] = "active"
                else:
                    self.current = {"session_id": uuid.uuid4().hex, "status": "active",
                                    "started_at_ms": round(self.wall() * 1000), "updated_at_ms": 0,
                                    "elapsed_seconds": 0., "active_seconds": 0., "steps": 0,
                                    "action_count": 0, "estimated_kcal": 0., "source": "motion_estimate", "days": {}}
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
                if moving:
                    met = 3.8 if "motion.march" in actions else 3.0 if held else 2.5
                    # 只估计活动热量：去掉1倍静息消耗，不把坐着等待算成锻炼。
                    kcal = (met - 1.) * 3.5 * self.profile["weight_kg"] / 200. * dt / 60.
                    for record in (self.current, day):
                        record["active_seconds"] += dt
                        record["estimated_kcal"] += kcal
                count = len(held - self._held)
                self.current["action_count"] += count
                day["action_count"] += count
                self.current["updated_at_ms"] = round(self.wall() * 1000)
                self._dirty = True
            self._last_at, self._previous, self._held = now, points, held

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
                               "checkins": sorted(self.checkins)}, ensure_ascii=False).encode("utf-8")
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
