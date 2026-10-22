"""单目深度的同帧表面对照；相对逆深度的尺度校准与后续检验分开。"""
from __future__ import annotations

from collections import deque
import math
import time

import numpy as np

from .kinect_camera import rotate_point


def surface_references(body, metadata, rotation="none"):
    """只用原始深度采样得到的表面距离，不用SDK关节中心冒充表面。"""
    if not (isinstance(body, dict) and body.get("matched_to_model") and body.get("body_valid")):
        return []
    age = (metadata or {}).get("surface_age_ms")
    if not isinstance(age, (int, float)) or not 0 <= age <= 80:
        return []
    result = []
    for name, point in body.get("points", {}).items():
        surface = point.get("surface") if isinstance(point, dict) else None
        if not isinstance(surface, dict) or point.get("score", 0) < .8:
            continue
        try:
            x, y = rotate_point(float(surface["color_x"]), float(surface["color_y"]), rotation)
            if (metadata or {}).get("image_mirrored"):
                x = 1 - x
            z = float(surface["z"])
            if all(map(math.isfinite, (x, y, z))) and 0 <= x <= 1 and 0 <= y <= 1 and .4 <= z <= 8:
                result.append({"name": name, "x": x, "y": y, "z": z})
        except (TypeError, KeyError, ValueError):
            continue
    return result


def kinect_distance_summary(body, now):
    """直接报告新鲜原厂躯干表面距离，与模型推理及尺度校准无关。"""
    sample_at = body.get("sample_at") if isinstance(body, dict) else None
    if not isinstance(sample_at, (int, float)) or not 0 <= now - sample_at <= .25:
        return None
    torso = {"pelvis", "spine_mid", "chest", "neck", "left_shoulder", "right_shoulder", "left_hip", "right_hip"}
    references = [p for p in surface_references(body, body) if p["name"] in torso]
    if len(references) < 2:
        return None
    values = [p["z"] for p in references]
    joints = [float(body["points"][p["name"]]["z"]) for p in references
              if isinstance(body["points"][p["name"]].get("z"), (int, float))
              and math.isfinite(body["points"][p["name"]]["z"])]
    return {"surface_m": round(float(np.median(values)), 3), "surface_min_m": round(min(values), 3),
            "surface_max_m": round(max(values), 3), "joint_m": round(float(np.median(joints)), 3) if joints else None,
            "reference_points": len(values), "sample_age_ms": round((now - sample_at) * 1000),
            "surface_age_ms": body["surface_age_ms"]}


class DepthMetrics:
    def __init__(self, relative=False):
        self.relative = relative
        self.mapping = None
        self.calibration = []
        self.calibration_until = None
        self.calibration_message = "相对逆深度，尚未校准米制尺度" if relative else "使用模型原生米制输出"
        self.errors = deque(maxlen=3000)
        self.calibrated_errors = deque(maxlen=3000)
        self.jitter = deque(maxlen=1500)
        self.previous = {}
        self.last_at = None
        self.calibration_attempted = False
        self.calibration_correlation = None

    def reset_statistics(self):
        self.errors.clear()
        self.calibrated_errors.clear()
        self.jitter.clear()
        self.previous.clear()

    def start_calibration(self, now=None):
        self.calibration = []
        self.calibration_attempted = True
        self.calibration_correlation = None
        self.calibration_until = (time.monotonic() if now is None else now) + 3
        self.calibration_message = "正在采集3秒尺度参考；校准数据不计入验证误差"
        self.reset_statistics()

    @staticmethod
    def sample(depth, x, y):
        h, w = depth.shape
        px, py = min(w - 1, int(x * w)), min(h - 1, int(y * h))
        patch = depth[max(0, py - 1):py + 2, max(0, px - 1):px + 2]
        values = patch[np.isfinite(patch) & (patch > 0)]
        return float(np.median(values)) if values.size >= 3 else None

    def _finish_calibration(self):
        pairs = np.asarray(self.calibration, dtype=np.float64)
        self.calibration_until = None
        if len(pairs) < 32 or (self.relative and (np.ptp(pairs[:, 1]) < .08 or np.ptp(pairs[:, 0]) < 1e-5)):
            self.calibration_message = "尺度参考不足：需可靠深度及有前后差异的表面，保留上次尺度"
            return
        if not self.relative:
            gain = float(np.median(pairs[:, 1] / pairs[:, 0]))
            residual = np.abs(gain * pairs[:, 0] - pairs[:, 1])
            if .05 <= gain <= 20 and float(np.median(residual)) <= .15:
                self.mapping = {"gain": gain}
                self.calibration_message = f"尺度系数{gain:.3f}已固定；原始误差保留，校正误差只使用后续新帧"
            else:
                self.calibration_message = "尺度参考不一致，未采用新校正；原始距离继续显示"
            self.reset_statistics()
            return
        x, target = pairs[:, 0], 1 / pairs[:, 1]
        keep = np.ones(len(x), dtype=bool)
        for _ in range(3):
            a, b = np.linalg.lstsq(np.column_stack((x[keep], np.ones(keep.sum()))), target[keep], rcond=None)[0]
            residual = target - (a * x + b)
            mad = np.median(np.abs(residual - np.median(residual)))
            keep = np.abs(residual - np.median(residual)) <= max(.015, 3 * 1.4826 * mad)
            if keep.sum() < 24:
                break
        fitted = a * x[keep] + b
        correlation = (float(np.corrcoef(x[keep], target[keep])[0, 1])
                       if keep.sum() >= 24 and np.std(x[keep]) > 1e-10 and np.std(target[keep]) > 1e-10 else 0.)
        self.calibration_correlation = correlation
        if a <= 0 or correlation < .5:
            self.calibration_message = "相对值未可靠跟随近大远小的变化，未采用新尺度；原始相对值仍可查看"
        elif keep.sum() >= 24 and np.all(fitted > 0) and np.median(np.abs(1 / fitted - pairs[keep, 1])) <= .15:
            self.mapping = {"gain": float(a), "offset": float(b), "min": float(x[keep].min()), "max": float(x[keep].max())}
            self.calibration_message = "尺度已固定，当前误差仅使用校准结束后的新帧"
        else:
            self.calibration_message = "尺度拟合不可靠，保留上次尺度；请换一些前后距离再试"
        self.reset_statistics()

    def evaluate(self, depth, references, now=None):
        raw = [(item, self.sample(depth, item["x"], item["y"])) for item in references]
        return self.evaluate_samples(raw, now)

    def evaluate_samples(self, raw, now=None):
        """同一统计路径也可重放已保存的逐点模型输出，不重复推理或拟合未来帧。"""
        now = time.monotonic() if now is None else now
        raw = [(item, value) for item, value in raw if value is not None]
        calibrating = self.calibration_until is not None
        if calibrating:
            self.calibration.extend((value, item["z"]) for item, value in raw)
            self.calibration = self.calibration[-512:]
            if now >= self.calibration_until:
                self._finish_calibration()
        points = []
        for item, value in raw:
            predicted = value
            corrected = value * self.mapping["gain"] if self.mapping and not self.relative else None
            if self.relative:
                m = self.mapping
                margin = (m["max"] - m["min"]) * .2 if m else 0
                inverse = m["gain"] * value + m["offset"] if m else -1
                predicted = (1 / inverse if m and m["min"] - margin <= value <= m["max"] + margin and inverse > 0 else None)
                corrected = predicted
            if predicted is not None and not .1 <= predicted <= 20:
                predicted = None
            if corrected is not None and not .1 <= corrected <= 20:
                corrected = None
            delta = predicted - item["z"] if predicted is not None else None
            corrected_delta = corrected - item["z"] if corrected is not None else None
            if delta is not None and not calibrating:
                self.errors.append(delta)
                if corrected_delta is not None:
                    self.calibrated_errors.append(corrected_delta)
                current_error = corrected_delta if corrected_delta is not None else delta
                previous = self.previous.get(item["name"])
                stable = previous and 0 < now - previous["at"] <= .6 and abs(item["z"] - previous["z"]) < .01 and math.hypot(item["x"] - previous["x"], item["y"] - previous["y"]) < .01
                count = previous["stable"] + 1 if stable else 0
                if count >= 4:
                    self.jitter.append(current_error - previous["error"])
                self.previous[item["name"]] = {**item, "error": current_error, "at": now, "stable": count}
            points.append({**item, "raw": round(value, 5), "model_m": round(predicted, 4) if predicted is not None else None,
                           "error_cm": round(delta * 100, 2) if delta is not None else None,
                           "calibrated_m": round(corrected, 4) if corrected is not None else None,
                           "calibrated_error_cm": round(corrected_delta * 100, 2) if corrected_delta is not None else None})
        errors = np.asarray(self.errors)
        corrected_errors = np.asarray(self.calibrated_errors)
        return {"reference_points": len(raw), "validation_samples": len(errors), "points": points,
                "raw_mae_cm": round(float(np.mean(np.abs(errors))) * 100, 2) if errors.size and not self.relative else None,
                "calibrated_mae_cm": round(float(np.mean(np.abs(corrected_errors))) * 100, 2) if corrected_errors.size else None,
                "calibrated_p95_cm": round(float(np.percentile(np.abs(corrected_errors), 95)) * 100, 2) if corrected_errors.size else None,
                "calibrated_validation_samples": len(corrected_errors),
                "scale_ready": self.mapping is not None,
                "calibration_attempted": self.calibration_attempted,
                "calibration_correlation": self.calibration_correlation,
                "scale_gain": self.mapping["gain"] if self.mapping else None,
                "mae_cm": round(float(np.mean(np.abs(errors))) * 100, 2) if errors.size else None,
                "p95_cm": round(float(np.percentile(np.abs(errors), 95)) * 100, 2) if errors.size else None,
                "bias_cm": round(float(np.mean(errors)) * 100, 2) if errors.size else None,
                "stationary_jitter_cm": round(float(np.sqrt(np.mean(np.square(self.jitter)))) * 100, 2) if self.jitter else None,
                "stationary_samples": len(self.jitter), "calibrating": self.calibration_until is not None,
                "calibration_remaining_s": round(max(0, self.calibration_until - now), 1) if self.calibration_until else 0,
                "metric_ready": not self.relative or self.mapping is not None, "calibration_message": self.calibration_message}
