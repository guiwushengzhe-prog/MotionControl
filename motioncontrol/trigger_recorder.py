"""只保存选中触发器附近的骨架，空闲数据只保留在短缓冲中。"""

from __future__ import annotations

import copy
import json
import math
import queue
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

SCHEMA = "motioncontrol.trigger_recording.v1"
DEFAULT_CONFIG = {"enabled": False, "triggers": [], "pre_s": 1.0, "post_s": 1.0}


def normalize_config(raw):
    if not isinstance(raw, dict):
        raise ValueError("录制设置必须是一组设置")
    triggers = raw.get("triggers", [])
    if not isinstance(triggers, list) or len(triggers) > 200:
        raise ValueError("请选择要录制的动作")
    if any(not isinstance(key, str) or not key.startswith(("zone.", "motion.", "pose."))
           or len(key) > 120 or not key.split(".", 1)[1] for key in triggers):
        raise ValueError("只支持身体区域、身体动作和自定义动作")
    config = {"enabled": bool(raw.get("enabled", False)), "triggers": list(dict.fromkeys(triggers))}
    for key in ("pre_s", "post_s"):
        value = float(raw.get(key, 1.0))
        if not math.isfinite(value) or not 0 <= value <= 5:
            raise ValueError("前后保留时长必须在 0 到 5 秒之间")
        config[key] = value
    return config


class _ClipWriter:
    """一段文件的后台写入器；识别线程只投递帧，不等待磁盘。"""

    def __init__(self, directory, frames, header, done):
        self.path = directory / ("trigger-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".jsonl")
        self.frames = frames
        self.header = header
        self.done = done
        self.pending = queue.Queue(maxsize=1200)
        self.finished = threading.Event()
        self.reason = "动作结束"
        self.thread = threading.Thread(target=self._run, name="trigger-record-save", daemon=True)

    def append(self, frame):
        try:
            self.pending.put_nowait(frame)
            return True
        except queue.Full:
            self.finish("保存太慢，录制已中止")
            return False

    def finish(self, reason="动作结束"):
        self.reason = reason
        self.finished.set()

    def _run(self):
        error, count, duration = "", 0, 0.0
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            start = self.frames[0]["at"]
            with self.path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps({"schema": SCHEMA, **self.header}, ensure_ascii=False) + "\n")

                def write(frame):
                    nonlocal count, duration
                    row = {key: value for key, value in frame.items() if key not in {"at", "sample_at"}}
                    duration = max(0., frame["at"] - start)
                    row["t"] = round(duration, 4)
                    row["sample_t"] = round(frame["sample_at"] - start, 4)
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                    count += 1

                for frame in self.frames:
                    write(frame)
                self.frames = []
                while True:
                    try:
                        frame = self.pending.get(timeout=.05)
                    except queue.Empty:
                        if self.finished.is_set():
                            break
                        continue
                    write(frame)
                stream.write(json.dumps({"type": "end", "frames": count,
                                         "duration_s": round(duration, 4), "reason": self.reason},
                                        ensure_ascii=False) + "\n")
        except (OSError, ValueError, TypeError) as exc:
            error = f"保存失败：{exc}"
        finally:
            self.frames = []
            self.done(self, count, error)


class TriggerRecorder:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self._lock = threading.RLock()
        self._buffer = deque(maxlen=1200)
        self._selected = set()
        self._active = set()
        self._writer = None
        self._writers = set()
        self._tail_until = None
        self._last_at = None
        self._queued_at = -math.inf
        self.saved_clips = 0
        self.last_file = ""
        self.last_frames = 0
        self.error = ""

    def configure(self, raw):
        if not isinstance(raw, dict):
            raise ValueError("录制设置必须是一组设置")
        config = normalize_config({**self.config, **raw})
        with self._lock:
            if config != self.config or self.error:
                self._finish_locked("录制设置改变" if config["enabled"] else "关闭录制")
                self._buffer.clear()
                self._active.clear()
                self._last_at = None
                self.error = ""
                self.config = config
                self._selected = set(config["triggers"])
            return self.status()

    def _saved(self, writer, count, error):
        with self._lock:
            self._writers.discard(writer)
            if error:
                self.error = error
            else:
                self.saved_clips += 1
                self.last_file = str(writer.path)
                self.last_frames = count

    def _finish_locked(self, reason="动作结束"):
        if self._writer is not None:
            self._writer.finish(reason)
            self._writer = None
        self._tail_until = None

    def end_active(self, now):
        with self._lock:
            if self._active:
                self._active.clear()
                self._tail_until = now + self.config["post_s"]

    def tick(self, now):
        with self._lock:
            # 额外等一段前置窗口再关文件，下一次触发的前置窗口重叠时才能合并。
            # 这段等待本身不写入空闲帧。
            if (self._writer is not None and self._tail_until is not None
                    and now > self._tail_until + self.config["pre_s"]):
                self._finish_locked()

    def capture(self, pose, now, active, *, width=0, height=0, source="", sample_at=None,
                extra=None, world_pose=None, snapshot_factory=None):
        with self._lock:
            if not self.config["enabled"] or self.error:
                return
            self.tick(now)
            if self._last_at is not None and now <= self._last_at:
                return
            self._last_at = now
            selected = set(active) & self._selected
            frame = {"at": now, "sample_at": now if sample_at is None else sample_at,
                     "width": int(width), "height": int(height), "source": str(source),
                     "pose": {name: {"x": round(float(point.get("x", 0)), 5),
                                     "y": round(float(point.get("y", 0)), 5),
                                     "z": round(float(point.get("z", 0)), 5),
                                     "score": round(float(point.get("score", point.get("visibility", 0))), 4)}
                              for name, point in (pose or {}).items() if isinstance(point, dict)},
                     "active_triggers": sorted(active), "selected_active": sorted(selected),
                     "extra": copy.deepcopy(extra or {})}
            if world_pose:
                frame["world_pose"] = copy.deepcopy(world_pose)
            self._buffer.append(frame)
            while self._buffer and self._buffer[0]["at"] < now - self.config["pre_s"]:
                self._buffer.popleft()
            if selected:
                self._tail_until = None
                if self._writer is None:
                    if len(self._writers) >= 4:
                        self.error = "保存太慢，录制已中止"
                        return
                    header = {"recorded_at": datetime.now().astimezone().isoformat(),
                              "config": copy.deepcopy(self.config),
                              "first_trigger_t": round(now - self._buffer[0]["at"], 4),
                              "snapshot": snapshot_factory() if snapshot_factory else {}}
                    writer = _ClipWriter(self.directory, list(self._buffer), header, self._saved)
                    self._writers.add(writer)
                    self._writer = writer
                    self._queued_at = now
                    writer.thread.start()
                else:
                    # 若已过后置窗口，补上新触发的前置窗口；同一帧不会重复写。
                    for row in self._buffer:
                        if row["at"] > self._queued_at:
                            if not self._append_locked(row):
                                break
            elif self._active:
                self._tail_until = now + self.config["post_s"]
            self._active = selected
            if self._writer is not None and not selected and now <= self._tail_until:
                self._append_locked(frame)

    def _append_locked(self, frame):
        if not self._writer.append(frame):
            self.error = "保存太慢，录制已中止"
            self._finish_locked(self.error)
            return False
        self._queued_at = frame["at"]
        return True

    def status(self):
        with self._lock:
            state = ("error" if self.error else "off" if not self.config["enabled"]
                     else "recording" if self._active else "tail" if self._writer else "waiting")
            return {"config": copy.deepcopy(self.config), "state": state,
                    "active": sorted(self._active), "saved_clips": self.saved_clips,
                    "saving": bool(self._writers - ({self._writer} if self._writer else set())),
                    "file": self.last_file, "frames": self.last_frames,
                    "directory": str(self.directory), "error": self.error}

    def close(self):
        with self._lock:
            self._finish_locked("服务停止")
            self._buffer.clear()
            self._active.clear()
            writers = list(self._writers)
        for writer in writers:
            writer.thread.join(timeout=5.)
