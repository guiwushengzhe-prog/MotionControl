"""深度辅助的个人前后倾角度校正；只学两个角度的线性映射，不改骨骼或模型权重。"""
from __future__ import annotations

from collections import defaultdict
import copy
import json
import math
from pathlib import Path
import statistics
import threading
import time

from .config_transaction import atomic_bytes
from .user_paths import user_data_root
from motioncontrol_shared.user_layout import user_data_path


SAMPLE_INTERVAL_S = .25
SAVE_INTERVAL_S = 30.
MAX_SYNC_GAP_S = .08
MAX_SAMPLE_AGE_S = .25
MAX_ANGLE_DEG = 35.
MAX_SAMPLES = 256
MAX_PER_BIN = 32
SAMPLE_LIFETIME_S = 7 * 24 * 3600
TRAIN_SAMPLES = 64
VALIDATION_SAMPLES = 32
MAX_VALIDATION_SAMPLES = 128
VALIDATION_GAP_S = 1.
PENDING_LIFETIME_S = 5 * 60


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _bin(value):
    return math.floor(value / 4.)


def _coverage(pairs, *, validation=False):
    """前倾、回正、后倾及多个角度段都需反复出现，不能由几帧极值撑开范围。"""
    minimum = VALIDATION_SAMPLES if validation else TRAIN_SAMPLES
    if len(pairs) < minimum:
        return False
    x, y = zip(*pairs)
    counts = defaultdict(int)
    for value in x:
        counts[_bin(value)] += 1
    per_bin, per_side, neutral = (3, 4, 4) if validation else (6, 8, 8)
    return (max(x) - min(x) >= 12 and min(x) <= -4 and max(x) >= 4
            and max(y) - min(y) >= 16 and min(y) <= -6 and max(y) >= 6
            and sum(count >= per_bin for count in counts.values()) >= 4
            and sum(a <= -4 and b <= -4 for a, b in pairs) >= per_side
            and sum(a >= 4 and b >= 4 for a, b in pairs) >= per_side
            and sum(abs(a) <= 3 and abs(b) <= 4 for a, b in pairs) >= neutral)


def _fit_bins(pairs):
    bins = defaultdict(list)
    for x, y in pairs:
        bins[_bin(x)].append((x, y))
    # 各角度段等权，避免站直的大量重复帧挤掉较少的前后倾样本。
    centers = [(statistics.median(p[0] for p in values), statistics.median(p[1] for p in values))
               for values in bins.values() if len(values) >= 3]
    if len(centers) < 4:
        return None
    mean_x, mean_y = (statistics.mean(p[i] for p in centers) for i in (0, 1))
    variance = sum((x - mean_x) ** 2 for x, _ in centers)
    if variance < 1:
        return None
    gain = sum((x - mean_x) * (y - mean_y) for x, y in centers) / variance
    return gain, mean_y - gain * mean_x


def _candidate(pairs):
    if not _coverage(pairs):
        return None
    fitted = _fit_bins(pairs)
    if fitted is None:
        return None
    gain, offset = fitted
    errors = [y - gain * x - offset for x, y in pairs]
    center = statistics.median(errors)
    deviation = statistics.median(abs(value - center) for value in errors)
    limit = max(1.5, 3 * 1.4826 * deviation)
    clean = [pair for pair, error in zip(pairs, errors) if abs(error - center) <= limit]
    if len(clean) < .8 * len(pairs) or not _coverage(clean):
        return None
    gain, offset = _fit_bins(clean)
    training_rmse = math.sqrt(statistics.mean((gain * x + offset - y) ** 2 for x, y in clean))
    if not (.5 <= gain <= 2 and abs(offset) <= 3 and training_rmse <= 2.5):
        return None
    return {"gain": gain, "offset_deg": offset, "model_min_deg": min(x for x, _ in clean),
            "model_max_deg": max(x for x, _ in clean), "training_samples": len(clean),
            "training_rmse_deg": training_rmse}


def _correct(value, mapping):
    anchor = max(mapping["model_min_deg"], min(mapping["model_max_deg"], value))
    # 超出实测范围只延续边界的修正量；不能把线性增益无限外推。
    delta = max(-10., min(10., (mapping["gain"] - 1.) * anchor + mapping["offset_deg"]))
    return value + delta


def _rmse(pairs, mapping=None):
    return math.sqrt(statistics.mean(((_correct(x, mapping) if mapping else x) - y) ** 2 for x, y in pairs))


def _validate(mapping, pairs, previous=None):
    if not _coverage(pairs, validation=True):
        return None
    raw, corrected = _rmse(pairs), _rmse(pairs, mapping)
    errors = sorted(abs(_correct(x, mapping) - y) for x, y in pairs)
    if (raw - corrected < .35 or corrected > raw * .8 or corrected > 1.8
            or errors[math.ceil(.95 * len(errors)) - 1] > 3.5):
        return None
    # 整体改善也不能以恶化前倾或后倾为代价。
    for side in (-1, 1):
        segment = [(x, y) for x, y in pairs if side * x >= 4]
        if _rmse(segment, mapping) > _rmse(segment) + .15:
            return None
    prior = _rmse(pairs, previous) if previous else raw
    if previous and (prior - corrected < .1 or corrected > prior * .95):
        return None
    return {"raw_rmse_deg": raw, "corrected_rmse_deg": corrected,
            "previous_rmse_deg": prior, "improvement_ratio": (raw - corrected) / raw,
            "validation_samples": len(pairs)}


def _restore_mapping(active):
    if active is None:
        return None
    current = time.time()
    required = ("gain", "offset_deg", "model_min_deg", "model_max_deg", "training_samples", "training_rmse_deg", "promoted_at")
    if (not isinstance(active, dict) or not all(_number(active.get(k)) for k in required)
            or not .5 <= active["gain"] <= 2 or abs(active["offset_deg"]) > 3
            or not -MAX_ANGLE_DEG <= active["model_min_deg"] <= -4
            or not 4 <= active["model_max_deg"] <= MAX_ANGLE_DEG
            or active["model_max_deg"] - active["model_min_deg"] < 12
            or not TRAIN_SAMPLES <= active["training_samples"] <= MAX_SAMPLES
            or not 0 <= active["training_rmse_deg"] <= 2.5
            or not 0 < active["promoted_at"] <= current + 300):
        raise ValueError("生效校正参数无效")
    pairs = active.get("validation_pairs")
    if (not isinstance(pairs, list) or not VALIDATION_SAMPLES <= len(pairs) <= MAX_VALIDATION_SAMPLES
            or any(not isinstance(p, list) or len(p) != 2 or not all(_number(v) for v in p)
                   or max(abs(p[0]), abs(p[1])) > MAX_ANGLE_DEG for p in pairs)):
        raise ValueError("校正留验数据无效")
    metrics = _validate(active, pairs)
    if metrics is None:
        raise ValueError("校正未通过留验复核")
    return {**copy.deepcopy(active), "validation": metrics}


class BodyAngleLearning:
    """最多每秒采四对角度；候选冻结后，等待未来独立样本留验再应用。"""

    def __init__(self, root=None, *, persist=True):
        self.path = user_data_path(root if root is not None else user_data_root(), "body_angle_learning")
        self._persist = bool(persist)
        self._lock = threading.RLock()
        self._save_lock = threading.Lock()
        self._save_thread = None
        self._revision = 0
        self._context = ""
        self._samples = []
        self._active = self._pending = None
        self._accepted = self._rejected = 0
        self._last_seen = self._last_sample = self._last_save_attempt = None
        self._last_pair = None
        self._dirty = False
        self._reason = "等待可靠的同步深度对照"
        self._last_error = ""
        if self._persist:
            self._load()

    def set_context(self, signature):
        """签名绑定两路站姿及相机坐标；站姿变更后不得沿用旧角度映射。"""
        signature = str(signature or "")
        if len(signature) > 512:
            raise ValueError("站姿基准签名过长")
        with self._lock:
            if signature == self._context:
                return False
            self._context = signature
            self._samples = []
            self._active = self._pending = None
            self._accepted = self._rejected = 0
            self._last_seen = self._last_sample = self._last_pair = None
            self._reason = "站姿基准已变更，等待新的可靠深度对照"
            self._dirty = True
            self._revision += 1
            return True

    def update(self, model_deg, depth_deg, now=None, *, valid=True, sync_gap_s=0.,
               sample_age_s=0., quality=1.):
        now = time.monotonic() if now is None else now
        with self._lock:
            if (valid is not True or not self._context or not all(_number(v) for v in (
                    model_deg, depth_deg, now, sync_gap_s, sample_age_s, quality))
                    or not 0 <= sync_gap_s <= MAX_SYNC_GAP_S
                    or not 0 <= sample_age_s <= MAX_SAMPLE_AGE_S or not .8 <= quality <= 1
                    or max(abs(model_deg), abs(depth_deg)) > MAX_ANGLE_DEG
                    or abs(model_deg - depth_deg) > 15
                    or (model_deg * depth_deg < 0 and min(abs(model_deg), abs(depth_deg)) >= 3)):
                return self._reject("未采样：需要同人、同步且可靠的前后倾角度")
            if self._last_seen is not None and now <= self._last_seen:
                return self._reject("未采样：重复或过时的角度")
            self._last_seen = now
            if self._last_sample is not None:
                interval = now - self._last_sample
                if interval < SAMPLE_INTERVAL_S:
                    return False
                if interval <= 2 and max(abs(model_deg - self._last_pair[0]),
                                         abs(depth_deg - self._last_pair[1])) / interval > 120:
                    return self._reject("未采样：角度发生不可靠的突变")
            self._last_sample, self._last_pair = now, (float(model_deg), float(depth_deg))
            self._accepted += 1
            self._append([float(model_deg), float(depth_deg), time.time()])
            self._dirty = True
            self._revision += 1
            if self._pending is not None and (now - self._pending["started"] > PENDING_LIFETIME_S
                    or time.time() - self._pending["created_at"] > PENDING_LIFETIME_S):
                self._pending = None
                self._reason = "候选与留验窗口已过时，等待新的独立对照"
            if self._pending is not None:
                if now - self._pending["started"] >= VALIDATION_GAP_S:
                    pairs = self._pending["pairs"]
                    pairs.append(self._last_pair)
                    if _coverage(pairs, validation=True):
                        mapping = self._pending["mapping"]
                        metrics = _validate(mapping, pairs, self._active)
                        if metrics is not None:
                            self._active = {**mapping, "validation": metrics,
                                            "validation_pairs": [list(pair) for pair in pairs],
                                            "promoted_at": time.time()}
                            self._reason = "已通过独立留验，沿用个人角度校正"
                        else:
                            self._reason = "新校正未优于原始或现有角度，保留当前方案"
                            if self._active is not None:
                                raw, old = _rmse(pairs), _rmse(pairs, self._active)
                                if old - raw >= .35 and raw <= old * .8 and all(
                                        _rmse([(x, y) for x, y in pairs if side * x >= 4]) <=
                                        _rmse([(x, y) for x, y in pairs if side * x >= 4], self._active) + .15
                                        for side in (-1, 1)):
                                    self._active = None
                                    self._reason = "可靠独立对照显示原始角度更准，已撤回旧校正"
                        self._pending = None
                    elif len(pairs) >= MAX_VALIDATION_SAMPLES:
                        self._pending = None
                        self._reason = "留验角度覆盖不足，保留当前方案并等待新样本"
            elif self._accepted >= TRAIN_SAMPLES:
                mapping = _candidate([(p[0], p[1]) for p in self._samples])
                if mapping is not None:
                    self._pending = {"mapping": mapping, "pairs": [], "started": now, "created_at": time.time()}
                    self._reason = "候选参数已冻结，等待未来独立角度留验"
                else:
                    self._reason = "继续积累前倾、回正和后倾的可靠角度覆盖"
            else:
                self._reason = "正在积累个人角度对照"
            if self._last_save_attempt is None:
                self._last_save_attempt = now
            elif self._persist and now - self._last_save_attempt >= SAVE_INTERVAL_S:
                self._last_save_attempt = now
                if self._save_thread is None or not self._save_thread.is_alive():
                    self._save_thread = threading.Thread(target=self.flush, name="body-angle-save", daemon=True)
                    self._save_thread.start()
            return True

    def _reject(self, reason):
        self._rejected += 1
        self._reason = reason
        return False

    def _append(self, sample):
        cutoff = time.time() - SAMPLE_LIFETIME_S
        self._samples = [p for p in self._samples if p[2] >= cutoff]
        same_bin = [i for i, p in enumerate(self._samples) if _bin(p[0]) == _bin(sample[0])]
        if len(same_bin) >= MAX_PER_BIN:
            del self._samples[same_bin[0]]
        self._samples.append(sample)
        self._samples = self._samples[-MAX_SAMPLES:]

    def correct(self, value):
        with self._lock:
            return _correct(value, self._active) if self._active and _number(value) else value

    def snapshot(self):
        """回放只带生效版和留验证据，不带持续积累的学习样本。"""
        with self._lock:
            return {"version": 1, "context": self._context, "active": copy.deepcopy(self._active)}

    def load_snapshot(self, raw):
        """给隔离回放恢复当时参数；无效快照不覆盖当前生效版。"""
        try:
            if (not isinstance(raw, dict) or raw.get("version") != 1
                    or not isinstance(raw.get("context"), str) or len(raw["context"]) > 512
                    or (raw.get("active") is not None and not raw["context"])):
                return False
            active = _restore_mapping(raw.get("active"))
        except (ValueError, TypeError, KeyError):
            return False
        with self._lock:
            self._context, self._active = raw["context"], active
            self._samples = []
            self._pending = None
            self._accepted = self._rejected = 0
            self._last_seen = self._last_sample = self._last_pair = None
            self._dirty = False
            self._revision += 1
            self._reason = "已恢复回放时的个人角度校正" if active else "回放未使用个人角度校正"
            return True

    def status(self):
        with self._lock:
            active, pending = self._active, self._pending
            return {"active": active is not None,
                    "state": "validating" if pending else "validated" if active else "collecting" if self._samples else "waiting",
                    "samples": len(self._samples), "accepted_samples": self._accepted,
                    "rejected_samples": self._rejected,
                    "training_samples": pending["mapping"]["training_samples"] if pending else active["training_samples"] if active else 0,
                    "validation_samples": len(pending["pairs"]) if pending else active["validation"]["validation_samples"] if active else 0,
                    "gain": active["gain"] if active else 1., "offset_deg": active["offset_deg"] if active else 0.,
                    "validation": copy.deepcopy(active["validation"]) if active else {},
                    "reason": self._reason, "last_error": self._last_error,
                    "pending_save": self._dirty and self._persist}

    def flush(self):
        """显式关停可立即保存；自动保存由 update 限制为每三十秒一次。"""
        if not self._persist:
            return False
        # 磁盘等待不持有角度锁，控制线程仍可校正和积累新样本。
        with self._save_lock:
            with self._lock:
                if not self._dirty:
                    return False
                self._last_save_attempt = self._last_seen if self._last_seen is not None else time.monotonic()
                revision = self._revision
                payload = {"version": 1, "context": self._context, "saved_at": time.time(),
                           "samples": copy.deepcopy(self._samples), "accepted_samples": self._accepted,
                           "active": copy.deepcopy(self._active)}
            try:
                atomic_bytes(self.path, json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            except (OSError, ValueError, TypeError):
                with self._lock:
                    self._last_error = "个人角度校正保存失败，保留已有有效参数"
                return False
            with self._lock:
                if revision == self._revision:
                    self._dirty = False
                self._last_error = ""
            return True

    def _load(self):
        try:
            if not self.path.exists():
                return
            if self.path.stat().st_size > 128 * 1024:
                raise ValueError("校正文件过大")
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(raw, dict) or raw.get("version") != 1
                    or not isinstance(raw.get("context"), str) or not 0 < len(raw["context"]) <= 512):
                raise ValueError("校正文件格式无效")
            self._context = raw["context"]
            current = time.time()
            samples = raw.get("samples", [])
            if not isinstance(samples, list) or len(samples) > MAX_SAMPLES:
                raise ValueError("角度样本数量无效")
            for p in samples:
                if (isinstance(p, list) and len(p) == 3 and all(_number(v) for v in p)
                        and max(abs(p[0]), abs(p[1])) <= MAX_ANGLE_DEG and abs(p[0] - p[1]) <= 15
                        and current - SAMPLE_LIFETIME_S <= p[2] <= current + 300):
                    self._append(p)
            count = raw.get("accepted_samples", 0)
            self._accepted = max(len(self._samples), count) if isinstance(count, int) and 0 <= count <= 10**12 else len(self._samples)
            self._active = _restore_mapping(raw.get("active"))
            self._reason = "已载入通过留验的个人角度校正" if self._active else "继续积累个人角度对照"
        except (OSError, ValueError, TypeError, KeyError):
            self._active = self._pending = None
            self._samples = []
            self._accepted = 0
            self._last_error = "个人角度校正文件无效，继续使用原始角度"
