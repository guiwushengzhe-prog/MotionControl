"""单目前后倾读出：三秒中立、固定几何读出，以及原厂监督后的独立留验学习。"""
from __future__ import annotations

from collections import Counter, deque
import hashlib
import json
import math
from pathlib import Path
import time

import numpy as np

from .config_transaction import atomic_bytes
from .user_paths import user_data_root
from motioncontrol_shared.user_layout import user_data_path


def kinect_pitch(body):
    if not isinstance(body, dict) or body.get("matched_to_model") is not True or body.get("body_valid") is not True:
        return None
    age = body.get("body_age_ms")
    if not isinstance(age, (int, float)) or not 0 <= age <= 80:
        return None
    try:
        p = [body["points"][n] for n in ("left_shoulder", "right_shoulder", "left_hip", "right_hip")]
        if any(v.get("score", 0) < .8 for v in p):
            return None
        xyz = np.asarray([[v[a] for a in ("x", "y", "z")] for v in p], dtype=float)
        up = xyz[:2].mean(axis=0) - xyz[2:].mean(axis=0)
        if not np.isfinite(xyz).all() or np.linalg.norm(up) < .15:
            return None
        return float(np.degrees(np.arctan2(up[2], up[1])))
    except (KeyError, TypeError, ValueError):
        return None


def fit_readout(samples, feature_indices=None):
    x = np.asarray([s["features"] for s in samples], dtype=float)
    y = np.asarray([s["reference"] for s in samples], dtype=float)
    indices = list(range(x.shape[1])) if feature_indices is None else list(feature_indices)
    mean, scale = x.mean(axis=0), np.maximum(x.std(axis=0), 1e-4)
    z = np.column_stack(((x[:, indices] - mean[indices]) / scale[indices], np.ones(len(x))))
    # 按参考角度段等权，久站样本不会淹没少量前倾、后倾。
    bins = np.floor(y / 4).astype(int)
    counts = Counter(bins)
    weights = np.asarray([1 / counts[b] for b in bins])
    weights *= len(weights) / weights.sum()
    penalty = np.eye(z.shape[1])
    penalty[-1, -1] = 0
    fitted = np.linalg.solve(z.T @ (z * weights[:, None]) + penalty, z.T @ (y * weights))
    coef = np.zeros(x.shape[1] + 1)
    coef[indices], coef[-1] = fitted[:-1], fitted[-1]
    return {"mean": mean.tolist(), "scale": scale.tolist(), "coef": coef.tolist(), "feature_indices": indices}


def readout(mapping, features):
    x = np.asarray(features, dtype=float)
    z = (x - mapping["mean"]) / mapping["scale"]
    return float(np.r_[z, 1.] @ mapping["coef"])


def coverage(samples, center, minimum):
    if len(samples) < minimum:
        return False
    labels = np.asarray([s["reference"] - center for s in samples])
    return bool((labels <= -5).sum() >= 4 and (labels >= 5).sum() >= 4 and (np.abs(labels) <= 3).sum() >= 8)


def validate_readout(mapping, samples, center, origin=None):
    if not coverage(samples, center, 64):
        return None
    true = np.asarray([s["reference"] - center for s in samples])
    predicted = np.asarray([readout(mapping, s["features"]) - (center if origin is None else origin) for s in samples])
    if not np.isfinite(true).all() or not np.isfinite(predicted).all():
        return None
    error = np.abs(predicted - true)
    active = np.abs(true) >= 5
    agreement = float(np.mean(predicted[active] * true[active] > 0))
    neutral_false_trigger = float(np.mean(np.abs(predicted[np.abs(true) <= 3]) >= 7))
    if error.mean() > 3 or np.percentile(error, 95) > 6 or agreement < .9 or neutral_false_trigger > .05:
        return None
    directional = {}
    for name, sign in (("forward", -1), ("back", 1)):
        mask = sign * true >= 7
        if mask.sum() < 8:
            return None
        gain = float(np.median(predicted[mask] / true[mask]))
        recall = float(np.mean(sign * predicted[mask] >= 7))
        # 两个方向单独验收，避免中立帧掩盖缩幅、漏检或单向误差。
        if not .75 <= gain <= 1.25 or recall < .8 or error[mask].mean() > 3:
            return None
        directional[name] = {"gain": gain, "recall": recall, "mae_deg": float(error[mask].mean())}
    return {"mae_deg": float(error.mean()), "p95_deg": float(np.percentile(error, 95)),
            "direction_agreement": agreement, "neutral_false_trigger": neutral_false_trigger,
            "directions": directional, "samples": len(samples)}


class MonocularLean:
    def __init__(self, root=None, *, persist=False, feature_dim=7, feature_indices=None):
        if feature_dim not in (7, 42):
            raise ValueError("单目前后倾特征维度不支持")
        self.feature_dim = feature_dim
        self.feature_indices = tuple(range(feature_dim)) if feature_indices is None else tuple(feature_indices)
        if (not self.feature_indices or len(set(self.feature_indices)) != len(self.feature_indices)
                or any(type(i) is not int or not 0 <= i < feature_dim for i in self.feature_indices)):
            raise ValueError("单目前后倾学习范围无效")
        self.path = user_data_path(root if root is not None else user_data_root(), "monocular_lean_learning")
        self.persist = persist
        self.context = ""
        self.profiles = {}
        self.profile = {"samples": [], "active": None}
        self.pending = None
        self.dirty = False
        self.last_save = self.last_pair = -math.inf
        self.learning_message = "开启学习后，随自然前倾、回正和后倾积累原厂参考"
        self.last_error = ""
        if persist and self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                if saved.get("version") == 1 and isinstance(saved.get("profiles"), dict):
                    self.profiles = saved["profiles"]
            except (OSError, ValueError, KeyError, TypeError):
                self.last_error = "上次学习文件未能读取，使用几何读出"
        self.reset()

    def reset(self):
        self.pending = None
        self.neutral = None
        self.neutral_reference = None
        self.collect = deque()
        self.started = self.last_at = self.filtered = None
        self.revision = None

    def set_context(self, value):
        signature = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
        if signature != self.context:
            self.flush()
            self.context = signature
            candidate = self.profiles.get(signature, {})
            self.profile = {"samples": [], "active": None}
            try:
                samples = candidate.get("samples", [])
                if isinstance(samples, list) and all(self._valid_sample(s) for s in samples):
                    self.profile["samples"] = samples[-512:]
                active = candidate.get("active")
                if (active and self._mapping_allowed(active["mapping"])
                        and all(self._valid_sample(s) for s in active["validation"])
                        and validate_readout(active["mapping"], active["validation"], active["center"], active.get("origin")) is not None):
                    self.profile["active"] = active
                elif active:
                    self.learning_message = "上次个人学习未通过躯干范围或双向幅度复核，使用基础估计"
            except (KeyError, TypeError, ValueError, IndexError, AttributeError):
                self.last_error = "已保存模型未通过核对，使用几何读出"
            self.profiles[signature] = self.profile
            self.pending = None
            self.last_pair = -math.inf
            self.reset()

    def _mapping_allowed(self, mapping):
        mean, scale, coef = (np.asarray(mapping[key], dtype=float) for key in ("mean", "scale", "coef"))
        if (mean.shape != (self.feature_dim,) or scale.shape != (self.feature_dim,)
                or coef.shape != (self.feature_dim + 1,) or not np.isfinite(mean).all()
                or not np.isfinite(scale).all() or not np.isfinite(coef).all() or (scale <= 0).any()):
            return False
        excluded = [i for i in range(self.feature_dim) if i not in self.feature_indices]
        return bool(np.all(coef[excluded] == 0))

    def _valid_sample(self, sample):
        try:
            f = np.asarray(sample["features"], dtype=float)
            return (f.shape == (self.feature_dim,) and np.isfinite(f).all()
                    and math.isfinite(sample["reference"]) and abs(sample["reference"]) <= 70
                    and ("base_deg" not in sample or math.isfinite(sample["base_deg"])))
        except (TypeError, ValueError, KeyError):
            return False

    def _learn(self, torso, reference, at):
        if reference is None or self.neutral_reference is None or at - self.last_pair < .25:
            return
        sample = {"features": list(torso["features"]), "reference": float(reference)}
        if torso.get("readout_kind") == "local_torso_v1":
            sample["base_deg"] = float(torso["geometric_deg"])
        if not self._valid_sample(sample):
            return
        self.last_pair = at
        if self.pending and at - self.pending["at"] >= 1:
            self.pending["validation"].append(sample)
            check = validate_readout(self.pending["mapping"], self.pending["validation"], self.pending["center"], self.pending["origin"])
            if check is not None:
                prior = self.profile["active"]
                prior_origin = readout(prior["mapping"], self.neutral["features"]) if prior else 0
                prior_errors = ([abs(readout(prior["mapping"], s["features"]) - prior_origin - (s["reference"] - self.neutral_reference)) for s in self.pending["validation"]]
                                if prior else None)
                if prior is None and torso.get("personal_research"):
                    if torso.get("readout_kind") == "local_torso_v1":
                        prior_errors = [abs(s["base_deg"] - self.neutral["geometric_deg"]
                                            - (s["reference"] - self.neutral_reference)) for s in self.pending["validation"]]
                    else:
                        coef = np.asarray(torso["linear_coef"], dtype=float)
                        prior_errors = [abs(float((np.asarray(s["features"]) - self.neutral["features"]) @ coef)
                                            - (s["reference"] - self.neutral_reference)) for s in self.pending["validation"]]
                if prior_errors is None or check["mae_deg"] < float(np.mean(prior_errors)) - .1:
                    self.profile["active"] = {k: v for k, v in self.pending.items() if k != "at"}
                    self.profile["active"]["metrics"] = check
                self.pending = None
                self.learning_message = "已通过后续独立动作验证，保留表现更好的个人读出"
                self.dirty = True
                self.flush()
                return
            if len(self.pending["validation"]) >= 128:
                self.pending = None
                self.learning_message = "本轮读出未通过后续验证，继续使用当前方案"
        samples = self.profile["samples"]
        bucket = math.floor((reference - self.neutral_reference) / 4)
        same = [i for i, s in enumerate(samples) if math.floor((s["reference"] - self.neutral_reference) / 4) == bucket]
        if len(same) >= 48:
            samples.pop(same[0])
        samples.append(sample)
        del samples[:-512]
        self.dirty = True
        if self.pending is None and coverage(samples, self.neutral_reference, 64):
            mapping = fit_readout(samples, self.feature_indices)
            self.pending = {"mapping": mapping, "center": self.neutral_reference,
                            "origin": readout(mapping, self.neutral["features"]), "validation": [], "at": at}
            self.learning_message = "候选已冻结，等待后续前倾、回正和后倾验证"
        elif self.pending is None:
            self.learning_message = "继续积累前倾、回正和后倾；久站只增加中立样本"
        if time.monotonic() - self.last_save >= 30:
            self.flush()

    def update(self, torso, at, *, reference=None, learn=False, revision=0):
        if revision != self.revision:
            self.reset()
            self.revision = revision
        if torso is None:
            self.filtered = self.last_at = None
            return {"valid": False, "calibrated": self.neutral is not None, "reason": "等待可靠的肩髋骨骼点" if self.feature_dim == 42 else "等待可靠的胸腹内部深度区域", "sample_at": at}
        if self.last_at is not None and (at <= self.last_at or at - self.last_at > .6):
            self.filtered = None
        if self.neutral is None:
            if self.started is None or (self.collect and at - self.collect[-1][0] > .6):
                self.started = at
                self.collect.clear()
            self.collect.append((at, torso, reference))
            if at - self.started < 3:
                self.last_at = at
                return {"valid": False, "calibrated": False, "calibrating": True,
                        "remaining_s": round(max(0, 3 - (at - self.started)), 1), "sample_at": at,
                        "reason": "自然站直，正在记录三秒中立姿态"}
            recent = [s for s in self.collect if at - s[0] <= 1.1]
            self.neutral = {"features": np.median([s[1]["features"] for s in recent], axis=0).tolist(),
                            "geometric_deg": float(np.median([s[1]["geometric_deg"] for s in recent]))}
            refs = [s[2] for s in recent if s[2] is not None]
            self.neutral_reference = float(np.median(refs)) if refs else None
            self.collect.clear()
            active = self.profile["active"]
            if (active and self.neutral_reference is not None and validate_readout(active["mapping"],
                    active["validation"], self.neutral_reference, readout(active["mapping"], self.neutral["features"])) is None):
                self.profile["active"] = None
                self.dirty = True
                self.learning_message = "旧个人读出不适合本次站姿，使用基础估计并等待新的双向验证"
        if learn:
            self._learn(torso, reference, at)
        active = self.profile["active"]
        if active:
            value = readout(active["mapping"], torso["features"]) - readout(active["mapping"], self.neutral["features"])
        elif torso.get("personal_research") and torso.get("readout_kind") != "local_torso_v1":
            coef = np.asarray(torso.get("linear_coef"), dtype=float)
            if coef.shape != (self.feature_dim,) or not np.isfinite(coef).all():
                return {"valid": False, "calibrated": True, "sample_at": at, "reason": "个人倾角读出无效"}
            value = float((np.asarray(torso["features"]) - self.neutral["features"]) @ coef)
        else:
            value = torso["geometric_deg"] - self.neutral["geometric_deg"]
        value = max(-45., min(45., value))
        # 走位控制器已有一次短时平滑；这里不再叠加滤波。
        self.filtered = value
        self.last_at = at
        learning = {"enabled": bool(learn), "samples": len(self.profile["samples"]), "active": active is not None,
                    "feature_count": len(self.feature_indices),
                    "validation_samples": len(self.pending["validation"]) if self.pending else 0,
                    "metrics": active.get("metrics") if active else None,
                    "reason": self.learning_message if self.neutral_reference is not None else "需要Kinect在重设中立姿态时提供可靠参考，单目识别可独立运行",
                    "error": self.last_error}
        return {"valid": True, "calibrated": True, "calibrating": False, "remaining_s": 0,
                "sample_at": at, "angle_deg": float(self.filtered), "raw_deg": float(value),
                "feature_dim": self.feature_dim, "personal_research": bool(torso.get("personal_research")),
                "strength": round(min(1., abs(self.filtered) / 20), 3),
                "state": "forward" if self.filtered <= -7 else "back" if self.filtered >= 7 else "neutral",
                "reason": "已验证的个人读出" if active else "个人研究倾角读出；待更多独立条件检验" if torso.get("personal_research") else "实验几何估计；尚未通过独立动作验收",
                "readout": "learned" if active else "personal_research" if torso.get("personal_research") else "geometric", "learning": learning,
                "sampled_regions": torso["sampled_regions"], "reference_used_for_prediction": False}

    def flush(self):
        if not self.persist or not self.dirty:
            return
        try:
            if self.context:
                self.profiles[self.context] = self.profile
            atomic_bytes(self.path, json.dumps({"version": 1, "profiles": self.profiles}, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            self.dirty = False
            self.last_error = ""
        except (OSError, ValueError, TypeError) as error:
            self.last_error = str(error)
        self.last_save = time.monotonic()
