"""用可靠三维脚踝高度增强踏步；缺少深度时保持二维识别。"""
from __future__ import annotations

import math
import statistics


class DepthMarchLifts:
    def __init__(self):
        self.reset()

    def reset(self):
        self.tracking_id = None
        self.history = []
        self.baseline = None
        self.last_depth_at = None
        self.source = "image"
        self.last_lift = None
        self.source_changed = False

    @staticmethod
    def _measure(body):
        if not body:
            return None
        try:
            points = body["points"]
            names = ("left_ankle", "right_ankle", "left_hip", "right_hip",
                     "left_shoulder", "right_shoulder")
            vectors = {}
            for name in names:
                point = points[name]
                vector = tuple(float(point[k]) for k in ("x", "y", "z"))
                if point.get("score", 0) < .6 or not all(math.isfinite(v) for v in vector) or vector[2] <= 0:
                    return None
                vectors[name] = vector
            shoulder = tuple((vectors["left_shoulder"][i] + vectors["right_shoulder"][i]) / 2 for i in range(3))
            hip = tuple((vectors["left_hip"][i] + vectors["right_hip"][i]) / 2 for i in range(3))
            torso = math.dist(shoulder, hip)
            if not .15 <= torso <= 1.0:
                return None
            floor = body.get("floor")
            up = tuple(float(v) for v in floor[:3]) if floor and len(floor) >= 3 else tuple(shoulder[i] - hip[i] for i in range(3))
            length = math.sqrt(sum(v * v for v in up))
            if not math.isfinite(length) or length < .1:
                return None
            up = tuple(v / length for v in up)
            if sum(up[i] * (shoulder[i] - hip[i]) for i in range(3)) < 0:
                up = tuple(-v for v in up)
            difference = sum(up[i] * (vectors["left_ankle"][i] - vectors["right_ankle"][i]) for i in range(3)) / torso
            return difference
        except (KeyError, TypeError, ValueError, OverflowError):
            return None

    def update(self, body, image_lifts, now):
        self.source_changed = False
        if not image_lifts or not all(math.isfinite(image_lifts.get(side, math.nan)) for side in ("left", "right")):
            self.reset()
            return None
        measured = self._measure(body)
        ident = body.get("tracking_id") if body else None
        if measured is not None:
            if ident != self.tracking_id or self.last_depth_at is None or now - self.last_depth_at > .5:
                self.tracking_id = ident
                self.history = []
                self.baseline = None
            self.last_depth_at = now
            self.history.append((now, measured))
            self.history = [(t, v) for t, v in self.history if now - t <= .4]
            stable = (len(self.history) >= 3 and now - self.history[0][0] >= .25
                      and max(v for _, v in self.history) - min(v for _, v in self.history) <= .025
                      and max(abs(v) for v in image_lifts.values()) < .035)
            if stable:
                neutral = statistics.median(v for _, v in self.history)
                if self.baseline is None and abs(neutral) < .05:
                    self.baseline = neutral
                elif self.baseline is not None and abs(measured - self.baseline) < .035:
                    self.baseline += .04 * (neutral - self.baseline)
        # An isolated missing/inferred frame cannot repeatedly switch the rhythm
        # detector. Keep its last height briefly, without creating new movement.
        if (measured is None and self.source == "depth" and self.last_depth_at is not None
                and now - self.last_depth_at <= .15 and self.last_lift is not None):
            return {"left": self.last_lift, "right": -self.last_lift}
        depth_ready = measured is not None and self.baseline is not None
        source = "depth" if depth_ready else "image"
        lift = measured - self.baseline if depth_ready else image_lifts["left"]
        if source != self.source:
            # A real fallback starts a fresh alternating sequence rather than
            # carrying a pending first step across incompatible measurements.
            self.source_changed = True
            self.source = source
            lift = 0.0
        self.last_lift = lift
        # Relative height keeps a two-foot jump from becoming alternating steps.
        return {"left": lift, "right": -lift}
