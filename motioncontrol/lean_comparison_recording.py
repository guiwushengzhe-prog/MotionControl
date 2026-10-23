"""单目与 Kinect 的同帧三状态记录；只存小量标量，不保存画面或运行额外模型。"""
from __future__ import annotations

from collections import deque
from datetime import datetime
import copy
import json
import math
from pathlib import Path
import threading
import time

from .body_lean import BACK_ON


STATES = ("forward", "neutral", "back")
LABELS = {"forward": "前倾", "neutral": "中立", "back": "后倾"}
SCHEMA = "motioncontrol.lean_comparison.v1"
MAX_FILE_BYTES = 16 * 1024 ** 2


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _state(angle):
    return "forward" if angle <= -BACK_ON else "back" if angle >= BACK_ON else "neutral"


class LeanComparisonRecorder:
    def __init__(self, directory):
        self.directory = Path(directory)
        self._lock = threading.RLock()
        self.enabled = False
        self.token = 0
        self.error = ""
        self._stream = None
        self.path = None
        self._reset()

    def _reset(self):
        self.frames = self.valid = self.agree = self.invalid = 0
        self.mismatch_seconds = 0.
        self.matrix = {depth: dict.fromkeys(STATES, 0) for depth in STATES}
        self.latest = None
        self.recent = deque(maxlen=6)
        self._span = self._previous = self._context = None
        self._last_flush = 0.
        self._written_bytes = self._part = 0

    def start(self):
        with self._lock:
            if self.enabled:
                return
            at = datetime.now().astimezone()
            folder = self.directory.resolve() / at.strftime("%Y-%m-%d")
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / (at.strftime("%H-%M-%S-%f") + "_前后倾对照.jsonl")
            stream = path.open("x", encoding="utf-8", buffering=65536)
            try:
                stream.write(json.dumps({"schema": SCHEMA, "type": "header", "started_at": at.isoformat(),
                    "states": LABELS, "threshold_deg": BACK_ON,
                    "comparison": "同帧倾角均按同一进入阈值划分三状态；非游戏控制滞回后的按键状态",
                    "reference": "Kinect 匹配同人的可靠肩髋；沿用单目三秒中立期间的深度基准",
                    "contains_images": False}, ensure_ascii=False) + "\n")
                stream.flush()
            except OSError:
                stream.close()
                raise
            self._reset()
            self._stream, self.path = stream, path
            self._base_path = path
            self.error, self.enabled = "", True
            self.token += 1

    def _write(self, row):
        line = json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
        self._stream.write(line)
        self._written_bytes += len(line.encode("utf-8"))

    def _end_span(self):
        if self._span is not None:
            self.recent.appendleft(self._span)
            self._span = None

    def pause(self, reason):
        with self._lock:
            if not self.enabled:
                return
            self._end_span()
            self._previous = None
            if self.latest:
                self.latest = {**self.latest, "comparable": False, "reason": reason}
            try:
                self._write({"type": "gap", "at": datetime.now().astimezone().isoformat(), "reason": reason})
                self._stream.flush()
            except OSError as exc:
                self._fail(exc)

    def _fail(self, error):
        self.error = "对照记录写入失败：" + str(error)
        self.enabled = False
        self.token += 1
        try:
            self._stream.close()
        except OSError:
            pass
        self._stream = None

    def stop(self):
        with self._lock:
            if not self.enabled:
                return
            self._end_span()
            try:
                self._write({"type": "summary", "at": datetime.now().astimezone().isoformat(), **self._summary()})
                self._stream.close()
            except OSError as exc:
                self._fail(exc)
            finally:
                self._stream = None
                self.enabled = False
                self.token += 1
                self._previous = None

    def observe(self, meta, mono, neutral_reference, *, finished, revision):
        with self._lock:
            comparison = meta.get("lean_comparison", {})
            if not self.enabled or comparison.get("token") != self.token:
                return
            at = meta["sample_at"]
            context = (str(meta["signature"]), meta["neutral_revision"])
            if context != self._context:
                self._end_span()
                self._previous = None
                self._context = context
            if self._previous and at <= self._previous["sample_at"]:
                return
            if self._previous and at - self._previous["sample_at"] > .25:
                self._end_span()
            teacher, depth_at = meta.get("teacher"), comparison.get("depth_sample_at")
            angle = mono.get("angle_deg") if mono.get("valid") and mono.get("calibrated") else None
            depth_angle = teacher - neutral_reference if _finite(teacher) and _finite(neutral_reference) else None
            reason = ""
            if meta["neutral_revision"] != revision:
                reason = "校准已更新，忽略旧结果"
            elif not 0 <= finished - at <= .25:
                reason = "单目结果已过时"
            elif not _finite(angle):
                reason = mono.get("reason") or "单目仍在校准或无有效结果"
            elif not _finite(teacher):
                reason = "没有可靠的同人深度参考"
            elif not _finite(depth_at) or abs(at - depth_at) > .08:
                reason = "深度与彩色帧不同步"
            elif not _finite(neutral_reference):
                reason = "缺少深度中立基准，请站直后重设站姿"
            comparable = not reason
            wall_at = datetime.fromtimestamp(time.time() - (finished - at)).astimezone().isoformat(timespec="milliseconds")
            row = {"type": "frame", "at": wall_at, "sample_at": at, "sequence": meta["sequence"],
                "model": comparison["model"], "neutral_revision": meta["neutral_revision"],
                "tracking_id": comparison.get("tracking_id"),
                "monocular": {"state": _state(angle) if _finite(angle) else None,
                              "angle_deg": round(angle, 3) if _finite(angle) else None},
                "depth": {"state": _state(depth_angle) if _finite(depth_angle) else None,
                          "angle_deg": round(depth_angle, 3) if _finite(depth_angle) else None,
                          "raw_deg": round(teacher, 3) if _finite(teacher) else None,
                          "sample_at": depth_at if _finite(depth_at) else None,
                          "neutral_deg": neutral_reference if _finite(neutral_reference) else None},
                "latency_ms": round(max(0., finished - at) * 1000, 1),
                "comparable": comparable, "agree": None, "reason": reason}
            if self._previous and self._previous["comparable"] and comparable:
                dt = at - self._previous["sample_at"]
                if 0 < dt <= .25 and self._previous["agree"] is False:
                    self.mismatch_seconds += dt
                    if self._span:
                        self._span["seconds"] += dt
            self.frames += 1
            if comparable:
                self.valid += 1
                a, b = row["monocular"]["state"], row["depth"]["state"]
                row["agree"] = a == b
                self.agree += int(row["agree"])
                self.matrix[b][a] += 1
                if a != b:
                    if not self._span or (a, b) != (self._span["monocular"], self._span["depth"]):
                        self._end_span()
                        self._span = {"at": wall_at, "monocular": a, "depth": b, "seconds": 0., "frames": 0}
                    self._span["frames"] += 1
                else:
                    self._end_span()
            else:
                self.invalid += 1
                self._end_span()
            self.latest = self._previous = row
            try:
                self._write(row)
                if finished - self._last_flush >= 1.:
                    self._stream.flush()
                    self._last_flush = finished
                # 持续开启时分卷，避免一个文件无限增长；累计统计不因分卷清零。
                if self._written_bytes >= MAX_FILE_BYTES:
                    self._write({"type": "summary", **self._summary()})
                    self._stream.close()
                    self._part += 1
                    self.path = self._base_path.with_name(f"{self._base_path.stem}_续{self._part}.jsonl")
                    self._stream = self.path.open("x", encoding="utf-8", buffering=65536)
                    self._written_bytes = 0
                    self._write({"type": "header", "schema": SCHEMA, "continued": True, "threshold_deg": BACK_ON})
            except OSError as exc:
                self._fail(exc)

    def _summary(self):
        return {"frames": self.frames, "valid": self.valid, "agree": self.agree,
                "disagree": self.valid - self.agree, "invalid": self.invalid,
                "agreement_percent": round(100 * self.agree / self.valid, 1) if self.valid else None,
                "mismatch_seconds": round(self.mismatch_seconds, 2), "matrix": copy.deepcopy(self.matrix)}

    def status(self):
        with self._lock:
            recent = ([self._span] if self._span else []) + list(self.recent)
            return {"enabled": self.enabled, "error": self.error, "file": str(self.path) if self.path else "",
                    "directory": str(self.directory.resolve()), "latest": copy.deepcopy(self.latest),
                    "recent_disagreements": copy.deepcopy(recent[:6]), **self._summary()}
