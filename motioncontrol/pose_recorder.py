"""Capture a stretch of real skeleton data to a file.

Tuning anything pose-driven against a live preview is guesswork: the numbers
move faster than they can be read, and the interesting moment -- a fist
closing, a zone edge, a jitter spike -- is over before it can be examined. A
recording turns that into data that can be replayed and plotted.

The immediate reason it exists is the fist thresholds in hand_mouse_control:
they were reasoned from the geometry rather than measured, and the honest way
to set them is to record a hand opening and closing and look at what the
``spread`` figure actually does.

Two properties matter for a recorder that runs inside a real-time control loop:

*It must not touch the disk while recording.* Frames are appended to a list and
written once at the end. Fifteen seconds at 30 fps is roughly 450 frames, a
megabyte or so -- cheap in memory, and it keeps file I/O out of the path that
drives the gamepad.

*It must not be able to run forever.* The countdown and the duration are both
bounded, and the recorder stops itself on the frame that passes the deadline
rather than relying on anyone calling stop.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

SCHEMA = "motioncontrol.pose_recording.v1"

DEFAULT_DELAY_S = 3.0
DEFAULT_DURATION_S = 15.0
MAX_DELAY_S = 30.0
MAX_DURATION_S = 120.0
# 120 s at 60 fps with headroom.  A cap at all matters more than its exact
# value: without one a stuck recorder would grow until the process died.
MAX_FRAMES = 12_000


class PoseRecorder:
    """Records the expanded 33-point pose the kernel is actually working with."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self._lock = threading.RLock()
        self._frames: list[dict] = []
        self.state = "idle"
        self.starts_at = 0.0
        self.ends_at = 0.0
        self.delay_s = DEFAULT_DELAY_S
        self.duration_s = DEFAULT_DURATION_S
        self.last_path: Path | None = None
        self.last_frames = 0
        self.last_error = ""
        self.source = ""

    # -- control -----------------------------------------------------------

    def start(self, delay_s: float | None = None, duration_s: float | None = None,
              *, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else now
        delay = DEFAULT_DELAY_S if delay_s is None else float(delay_s)
        duration = DEFAULT_DURATION_S if duration_s is None else float(duration_s)
        if not 0.0 <= delay <= MAX_DELAY_S:
            raise ValueError(f"倒计时必须在 0 到 {MAX_DELAY_S:.0f} 秒之间")
        if not 0.1 <= duration <= MAX_DURATION_S:
            raise ValueError(f"录制时长必须在 0.1 到 {MAX_DURATION_S:.0f} 秒之间")
        with self._lock:
            if self.state in {"waiting", "recording"}:
                raise ValueError("已经在录制中")
            self._frames = []
            self.delay_s = delay
            self.duration_s = duration
            self.starts_at = now + delay
            self.ends_at = self.starts_at + duration
            self.state = "waiting"
            self.last_error = ""
            self.last_path = None
            self.last_frames = 0
            return self.status(now=now)

    def cancel(self) -> dict:
        with self._lock:
            if self.state in {"waiting", "recording"}:
                self.state = "cancelled"
                self._frames = []
            return self.status()

    # -- capture -----------------------------------------------------------

    def capture(self, pose_map: dict, now: float, *, width: int = 0, height: int = 0,
                source: str = "", extra: dict | None = None) -> None:
        """Called once per frame from the control loop.  Must stay cheap."""
        with self._lock:
            if self.state == "waiting":
                if now < self.starts_at:
                    return
                self.state = "recording"
                self.source = str(source)
            if self.state != "recording":
                return
            if now >= self.ends_at or len(self._frames) >= MAX_FRAMES:
                self._finish_locked()
                return
            self._frames.append({
                "t": round(now - self.starts_at, 4),
                "width": int(width),
                "height": int(height),
                # Copied rather than referenced: the kernel reuses and mutates
                # its pose dictionaries between frames.
                "pose": {
                    name: {
                        "x": round(float(point.get("x", 0.0)), 5),
                        "y": round(float(point.get("y", 0.0)), 5),
                        "z": round(float(point.get("z", 0.0)), 5),
                        "score": round(float(point.get("score", point.get("visibility", 0.0))), 4),
                    }
                    for name, point in (pose_map or {}).items()
                    if isinstance(point, dict)
                },
                **({"extra": extra} if extra else {}),
            })

    def _finish_locked(self) -> None:
        frames = self._frames
        self._frames = []
        self.state = "saving"
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = self.directory / f"pose-{stamp}.jsonl"
            header = {
                "schema": SCHEMA,
                "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "duration_s": round(self.duration_s, 3),
                "delay_s": round(self.delay_s, 3),
                "frames": len(frames),
                "source": self.source,
            }
            # JSON Lines: a header line then one line per frame, so a long
            # recording streams instead of needing to be parsed whole.
            with path.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(header, ensure_ascii=False) + "\n")
                for frame in frames:
                    stream.write(json.dumps(frame, ensure_ascii=False) + "\n")
            self.last_path = path
            self.last_frames = len(frames)
            self.state = "done"
        except OSError as exc:
            self.last_error = f"保存失败：{exc}"
            self.state = "error"

    # -- reporting ---------------------------------------------------------

    def status(self, *, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else now
        with self._lock:
            remaining = 0.0
            if self.state == "waiting":
                remaining = max(0.0, self.starts_at - now)
            elif self.state == "recording":
                remaining = max(0.0, self.ends_at - now)
            return {
                "state": self.state,
                "remaining_s": round(remaining, 2),
                "frames": len(self._frames) if self.state == "recording" else self.last_frames,
                "delay_s": round(self.delay_s, 2),
                "duration_s": round(self.duration_s, 2),
                "source": self.source,
                "file": str(self.last_path) if self.last_path else "",
                "directory": str(self.directory),
                "error": self.last_error,
            }
