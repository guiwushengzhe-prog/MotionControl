"""侧倾转向：保持倾斜持续输出，回正立即归零。"""

import math

TILT_SPAN_DEG = 18.0


def eye_line_tilt(pose, width, height):
    """以像素计算双眼连线；解剖学右眼更低时为右侧倾。"""
    if not isinstance(pose, dict) or width <= 0 or height <= 0:
        return math.nan
    points = []
    for name in ("left_eye", "right_eye"):
        point = pose.get(name)
        if not isinstance(point, dict):
            return math.nan
        try:
            confidence = next((float(point[k]) for k in ("score", "visibility", "presence") if k in point), 0.0)
            x, y = float(point["x"]), float(point["y"])
        except (KeyError, ValueError, TypeError):
            return math.nan
        if not all(math.isfinite(v) for v in (x, y, confidence)) or confidence < .4:
            return math.nan
        points.append((x * width, y * height))
    left, right = points
    dx, dy = abs(left[0] - right[0]), right[1] - left[1]
    if dx < 2.0:
        return math.nan
    return math.degrees(math.atan2(dy, dx))


class RollTiltControl:
    def __init__(self):
        self.reset()

    def reset(self):
        self.filtered = None
        self.last_at = None
        self.direction = 0
        self.candidate = 0
        self.candidate_since = 0.0
        self.state = "CENTER"
        self.threshold = 2.0

    def update(self, angle, now, *, center, noise, deadzone):
        if not all(math.isfinite(v) for v in (angle, center, noise, now)):
            self.reset()
            self.state = "TILT_UNAVAILABLE"
            return 0.0
        if self.last_at is not None and not 0 < now - self.last_at <= .25:
            self.reset()
        dt = now - self.last_at if self.last_at is not None else 0.0
        self.last_at = now
        delta = angle - center
        self.threshold = max(2.0, TILT_SPAN_DEG * deadzone, 3.0 * max(0.0, noise))
        release = self.threshold * .65
        if abs(delta) <= release:
            self.reset()
            return 0.0
        desired = 1 if delta > 0 else -1
        if self.direction and desired != self.direction:
            # Stop the old direction on the crossing frame, even if a frame
            # skipped the neutral zone. The other side must qualify afresh.
            self.reset()
            return 0.0
        alpha = 1.0 - math.exp(-dt / .045) if dt else 1.0
        self.filtered = delta if self.filtered is None else self.filtered + alpha * (delta - self.filtered)
        if not self.direction:
            if abs(delta) < self.threshold or abs(self.filtered) < self.threshold:
                self.candidate = 0
                self.state = "CENTER"
                return 0.0
            if self.candidate != desired:
                self.candidate, self.candidate_since = desired, now
            if now - self.candidate_since < .06:
                self.state = "TILT_CONFIRMING"
                return 0.0
            self.direction = desired
        amount = max(0.0, min(1.0, (abs(self.filtered) - self.threshold) / max(1.0, TILT_SPAN_DEG - self.threshold)))
        self.state = "TILT_RIGHT" if self.direction > 0 else "TILT_LEFT"
        return self.direction * amount ** 1.3
