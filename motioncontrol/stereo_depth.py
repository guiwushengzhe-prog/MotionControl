"""双目深度：手机（主身体源）+ 电脑摄像头（第二视角）三角化出手臂的前后位置。

单目 MediaPipe 的 z 是模型猜的相对值，手往前推基本看不出来；两台相机从不同角度看同
一个人，同一关节的两条视线交点就是它的真实三维位置。

标定不用棋盘格：用户在两台相机前活动半分钟，由人体关键点自己解出两台相机的关系
（见 stereo_solver）。之后每来一帧手机姿态，就取同一时刻的电脑姿态（两帧之间插值）
三角化。输出以肩宽为单位，不依赖任何人的实际身材。

两台相机任一台被挪动、换了分辨率或画面方向，标定就失效，需要重新标定。
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

from motioncontrol import stereo_solver as solver

DEFAULT_CALIBRATION_SECONDS = 30.0
# 电脑帧缓存：够覆盖手机帧的网络延迟。
PC_BUFFER_SECONDS = 2.0
# 某条骨长偏离标定中位数超过这个比例，说明这一帧三角化坏了，不输出那只手。
MAX_BONE_DEVIATION = 0.4
# 手机帧常常比同一时刻的电脑帧先到（电脑那帧还在识别）。挂起等它，最多等这么久。
PENDING_MAX_SECONDS = 0.25


class StereoDepth:
    def __init__(self, calibration_path: Path | None = None) -> None:
        self.path = Path(calibration_path) if calibration_path else None
        self._lock = threading.Lock()
        self._pc: deque[tuple[float, np.ndarray, tuple[int, int]]] = deque()
        self._cal_pc: list[tuple[float, np.ndarray, tuple[int, int]]] = []
        self._cal_phone: list[tuple[float, np.ndarray, tuple[int, int]]] = []
        self.collect_until = 0.0
        self.collect_seconds = DEFAULT_CALIBRATION_SECONDS
        self.message = ""
        self.latest: dict | None = None
        self.last_attempt: dict | None = None
        self._pending: tuple[np.ndarray, tuple[int, int], float] | None = None
        # 两台相机各自最近一帧的原始姿态，给面板画简略图用。
        self._views: dict[str, tuple[float, dict | None, tuple[int, int]]] = {}
        self.calibration = self._load()
        self.state = "ready" if self.calibration else "uncalibrated"
        self._matrices = self._build_matrices(self.calibration)

    # -- 标定文件 ---------------------------------------------------------------

    def _load(self) -> dict | None:
        if not self.path or not self.path.is_file():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not data.get("ok"):
            return None
        reasons = solver.acceptance_reasons(data)
        if reasons:
            # 早期版本的合格线松，放进来过拍不全手臂的标定。
            self.message = "上次保存的标定不可信（" + "；".join(reasons) + "），请重新标定"
            return None
        return data

    def _save(self, data: dict) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(self.path)

    @staticmethod
    def _build_matrices(cal: dict | None):
        if not cal:
            return None
        rotation = np.asarray(cal["rotation_phone_from_pc"], float)
        t = np.asarray(cal["translation_phone_from_pc"], float)
        pa, pb = solver.projection_matrices(cal["f_pc"], cal["f_phone"], rotation, t,
                                            tuple(cal["pc_size"]), tuple(cal["phone_size"]))
        return {"pa": pa, "pb": pb, "rotation": rotation}

    # -- 两路输入 ---------------------------------------------------------------

    def observe_pc(self, pose_map: dict | None, width: int, height: int, t: float) -> None:
        """电脑摄像头（辅助视角）的一帧；t 是采集时刻（time.monotonic 秒）。"""
        size = (int(width), int(height))
        points = solver.points_from_pose(pose_map, *size)
        with self._lock:
            self._views["pc"] = (t, pose_map, size)
            self._pc.append((t, points, size))
            while self._pc and self._pc[0][0] < t - PC_BUFFER_SECONDS:
                self._pc.popleft()
            if self.state == "collecting":
                self._cal_pc.append((t, points, size))
            elif self._pending and self.calibration:
                phone_points, phone_size, phone_t = self._pending
                result = self._measure_locked(phone_points, phone_size, phone_t)
                if result is not None:
                    self.latest, self._pending = result, None
                elif t - phone_t > PENDING_MAX_SECONDS:
                    self.latest = {"valid": False, "reason": "电脑摄像头画面跟不上"}
                    self._pending = None

    def observe_phone(self, pose_map: dict | None, width: int, height: int, t: float) -> dict | None:
        """手机（主身体源）的一帧；返回这一帧的双目结果，没有则 None。"""
        size = (int(width), int(height))
        points = solver.points_from_pose(pose_map, *size)
        with self._lock:
            self._views["phone"] = (t, pose_map, size)
            if self.state == "collecting":
                self._cal_phone.append((t, points, size))
                if t >= self.collect_until:
                    self._begin_solve_locked()
                return None
            if not self.calibration:
                return None
            result = self._measure_locked(points, size, t)
            if result is None:
                # 同一时刻的电脑帧还没识别完；它一到，observe_pc 就接着算。
                self._pending = (points, size, t)
                return None
            self.latest, self._pending = result, None
            return result

    # -- 标定 -------------------------------------------------------------------

    def start_calibration(self, seconds: float | None = None, *, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else now
        seconds = DEFAULT_CALIBRATION_SECONDS if seconds is None else float(seconds)
        if not 10.0 <= seconds <= 120.0:
            raise ValueError("标定时长必须在 10 到 120 秒之间")
        with self._lock:
            if self.state in {"collecting", "solving"}:
                raise ValueError("正在标定中")
            if not self._pc or now - self._pc[-1][0] > 1.0:
                raise ValueError("电脑摄像头没有画面：请先开启双目，确认电脑摄像头在运行")
            self._cal_pc = []
            self._cal_phone = []
            self.collect_seconds = seconds
            self.collect_until = now + seconds
            self.state = "collecting"
            self.message = "标定中：在两台相机前慢慢活动双臂，往前推、往两侧伸、举高"
            return self._status_locked(now)

    def cancel_calibration(self) -> dict:
        with self._lock:
            if self.state == "collecting":
                self.state = "ready" if self.calibration else "uncalibrated"
                self.message = "已取消标定"
                self._cal_pc, self._cal_phone = [], []
            return self._status_locked(time.monotonic())

    def _begin_solve_locked(self) -> None:
        pc, phone = self._cal_pc, self._cal_phone
        self._cal_pc, self._cal_phone = [], []
        self.state = "solving"
        self.message = "正在计算两台相机的位置关系…"
        threading.Thread(target=self._solve, args=(pc, phone), name="stereo-solve", daemon=True).start()

    def _solve(self, pc: list, phone: list) -> None:
        try:
            result = self._run_solver(pc, phone)
        except Exception as exc:  # 标定失败不能拖垮控制循环
            result = {"ok": False, "reason": f"计算出错：{exc}"}
        with self._lock:
            if result.get("ok"):
                result["calibrated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                try:
                    self._save(result)
                except OSError as exc:
                    result["save_error"] = str(exc)
                self.calibration = result
                self._matrices = self._build_matrices(result)
                self.state = "ready"
                q = result["holdout"]
                self.message = f"标定完成：检验误差 {q['reprojection_median_px']:.1f} 像素"
            else:
                self.state = "ready" if self.calibration else "failed"
                self.message = "标定没通过：" + result.get("reason", "未知原因") + (
                    "（仍在用上一次的标定）" if self.calibration else "")
            self.last_attempt = result

    @staticmethod
    def _run_solver(pc: list, phone: list) -> dict:
        if len(pc) < 30 or len(phone) < 30:
            return {"ok": False, "reason": f"帧数太少（电脑 {len(pc)}，手机 {len(phone)}）；两台相机都要在运行"}
        pc_size = max({s for _, _, s in pc}, key=lambda s: sum(1 for _, _, x in pc if x == s))
        phone_size = max({s for _, _, s in phone}, key=lambda s: sum(1 for _, _, x in phone if x == s))
        pc = [f for f in pc if f[2] == pc_size]
        phone = [f for f in phone if f[2] == phone_size]
        return solver.solve(
            np.array([f[0] * 1000 for f in pc]), np.stack([f[1] for f in pc]), pc_size,
            np.array([f[0] * 1000 for f in phone]), np.stack([f[1] for f in phone]), phone_size,
        )

    # -- 运行时三角化 -----------------------------------------------------------

    def _pc_at_locked(self, t: float):
        """t 时刻的电脑关键点（前后两帧插值）；还没有比 t 更新的电脑帧时返回 "wait"。"""
        frames = self._pc
        if len(frames) < 2:
            return None
        if t > frames[-1][0]:
            return "wait"
        times = np.array([f[0] for f in frames]) * 1000
        stack = np.stack([f[1] for f in frames])
        xy = solver.interpolate(times, stack, np.array([t * 1000]))[0]
        return xy, frames[-1][2]

    def _measure_locked(self, phone_points: np.ndarray, phone_size: tuple[int, int], t: float) -> dict | None:
        """一帧手机姿态的双目结果；同一时刻的电脑帧还没到时返回 None。"""
        cal = self.calibration
        if list(phone_size) != cal["phone_size"]:
            return {"valid": False, "reason": "手机画面尺寸和标定时不一样，需要重新标定"}
        pc = self._pc_at_locked(t + cal["phone_to_pc_offset_ms"] / 1000.0)
        if pc == "wait":
            return None
        if pc is None:
            return {"valid": False, "reason": "电脑摄像头没有同一时刻的画面"}
        pc_xy, pc_size = pc
        if list(pc_size) != cal["pc_size"]:
            return {"valid": False, "reason": "电脑画面尺寸或方向和标定时不一样，需要重新标定"}
        ok = np.isfinite(pc_xy[:, 0]) & (np.nan_to_num(phone_points[:, 2]) >= solver.MIN_SCORE)
        if not ok.any():
            return {"valid": False, "reason": "两台相机没有同时看到身体"}
        xyz = np.full((len(solver.JOINTS), 3), np.nan)
        xyz[ok] = solver.triangulate(self._matrices["pa"], self._matrices["pb"], pc_xy[ok], phone_points[ok, :2])
        return self._features(xyz)

    def _features(self, xyz: np.ndarray) -> dict:
        """肩宽为单位的手臂前伸量。"前"= 垂直于肩线和躯干上方向、朝手机那一侧。"""
        cal = self.calibration
        j = {name: i for i, name in enumerate(solver.JOINTS)}
        ls, rs = xyz[j["left_shoulder"]], xyz[j["right_shoulder"]]
        if not (np.isfinite(ls).all() and np.isfinite(rs).all()):
            return {"valid": False, "reason": "双肩不是两台相机都看得到"}
        unit = float(cal["shoulder_width"])
        across = (ls - rs) / max(np.linalg.norm(ls - rs), 1e-9)
        facing = -self._matrices["rotation"][2]
        up = cal.get("torso_up_pc")
        if up is not None:
            # 同时垂直于肩线和标定时的躯干上方向：水平朝前，手抬高放低不算前伸。
            forward = np.cross(across, np.asarray(up, float))
            if forward @ facing < 0:
                forward = -forward
        else:
            forward = facing - (facing @ across) * across
        forward /= max(np.linalg.norm(forward), 1e-9)
        medians = cal.get("bone_medians", {})
        hands, missing = {}, {}
        for side in ("left", "right"):
            shoulder, elbow, wrist = (xyz[j[f"{side}_{n}"]] for n in ("shoulder", "elbow", "wrist"))
            if not np.isfinite(wrist).all():
                missing[side] = "两台相机没有同时看到手腕"
                continue
            if np.isfinite(elbow).all():
                bad = False
                for bone, a, b in ((f"{side}_upper_arm", shoulder, elbow), (f"{side}_forearm", elbow, wrist)):
                    ref = medians.get(bone)
                    if ref and abs(np.linalg.norm(a - b) / ref - 1) > MAX_BONE_DEVIATION:
                        bad = True
                if bad:
                    missing[side] = "这一帧手臂长度不对，已丢弃"
                    continue
            hands[side] = {
                "forward": round(float((wrist - shoulder) @ forward / unit), 3),
                "reach": round(float(np.linalg.norm(wrist - shoulder) / unit), 3),
            }
        centre = (ls + rs) / 2
        points = {name: [round(float(v), 3) for v in (xyz[i] - centre) / unit]
                  for name, i in j.items() if np.isfinite(xyz[i]).all()}
        return {"valid": True, "hands": hands, "missing": missing, "points": points}

    # -- 状态 -------------------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            return self._status_locked(time.monotonic())

    @staticmethod
    def _view_payload(view, now: float) -> dict:
        t, pose_map, size = view
        points = {}
        for name, p in (pose_map or {}).items():
            if not isinstance(p, dict) or p.get("x") is None or p.get("y") is None:
                continue
            score = p.get("score", p.get("visibility", 0.0)) or 0.0
            points[name] = [round(float(p["x"]), 3), round(float(p["y"]), 3), round(float(score), 2)]
        return {"age_ms": round((now - t) * 1000), "size": list(size), "points": points}

    def _status_locked(self, now: float) -> dict:
        cal = self.calibration
        data = {
            "state": self.state,
            "message": self.message,
            "calibrated": bool(cal),
            "pc_fresh": bool(self._pc) and now - self._pc[-1][0] <= 1.0,
            "latest": self.latest,
            "joints": list(solver.JOINTS),
            "min_score": solver.MIN_SCORE,
            "views": {name: self._view_payload(view, now) for name, view in self._views.items()},
        }
        if self.state == "collecting":
            data["remaining_s"] = round(max(0.0, self.collect_until - now), 1)
            data["collected"] = {"pc": len(self._cal_pc), "phone": len(self._cal_phone)}
        if cal:
            data["calibration"] = {
                "calibrated_at": cal.get("calibrated_at"),
                "holdout_error_px": round(cal["holdout"]["reprojection_median_px"], 2),
                "left_right_ratio": {k: round(v, 3) for k, v in cal["fit"]["left_right_ratio"].items()},
                "optical_axis_angle_deg": round(cal["optical_axis_angle_deg"], 1),
                "phone_to_pc_offset_ms": cal["phone_to_pc_offset_ms"],
            }
        return data
