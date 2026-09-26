"""新版头控：侧倾和转脸分别去死区，选较明确的一项，反向当帧生效。"""

import math

from .roll_tilt_control import TILT_SPAN_DEG


class ResponsiveHeadControl:
    def __init__(self):
        self.reset()

    def reset(self):
        self.last_at = None
        self.filtered = 0.0
        self.source = "none"
        self.state = "CENTER"
        self.tilt_stop_threshold = 3.0
        self.yaw_stop_threshold = .22
        self.tilt_threshold = 3.45
        self.yaw_threshold = .245
        self.directions = {"tilt": 0, "yaw": 0}
        self.raw = 0.0

    def _channel(self, delta, start, stop, source):
        if not math.isfinite(delta) or abs(delta) <= stop:
            self.directions[source] = 0
            return 0.0
        direction = 1 if delta > 0 else -1
        if self.directions[source] != direction and abs(delta) < start:
            self.directions[source] = 0
            return 0.0
        self.directions[source] = direction
        amount = min(1.0, (abs(delta) - stop) / max(.1, 1.0 - stop))
        # 中心附近便于细调，大幅动作加速；连续、单调，不突然跳到高速。
        curved = .60 * amount + .40 * amount * amount
        return math.copysign(curved, delta)

    def update(self, tilt, yaw, now, *, center_tilt, noise_tilt, noise_yaw, yaw_span, deadzone):
        if not math.isfinite(now):
            self.reset()
            return 0.0
        if self.last_at is not None and not 0 < now - self.last_at <= .25:
            self.reset()
        dt = now - self.last_at if self.last_at is not None else 0.0
        self.last_at = now
        # 转脸常和看旁边、点头起手相似，门槛比侧倾更大；噪声沿用站好校准所得。
        self.tilt_stop_threshold = max(3.0, TILT_SPAN_DEG * deadzone * 1.5,
                                       3.0 * max(0.0, noise_tilt))
        self.yaw_stop_threshold = max(.22, deadzone * 1.8,
                                      3.5 * max(0.0, noise_yaw) / yaw_span)
        # 起动线在停止线外面一点，回正后的小抖动不会立刻重新起动。
        self.tilt_threshold = self.tilt_stop_threshold + max(.45, .15 * max(0.0, noise_tilt))
        self.yaw_threshold = self.yaw_stop_threshold + .025
        tilt_delta = (tilt - center_tilt) / TILT_SPAN_DEG if all(
            math.isfinite(v) for v in (tilt, center_tilt)) else math.nan
        tilt_value = self._channel(tilt_delta, self.tilt_threshold / TILT_SPAN_DEG,
                                   self.tilt_stop_threshold / TILT_SPAN_DEG, "tilt")
        yaw_value = self._channel(yaw, self.yaw_threshold, self.yaw_stop_threshold, "yaw")
        self.source = "tilt" if abs(tilt_value) >= abs(yaw_value) else "yaw"
        desired = tilt_value if self.source == "tilt" else yaw_value
        self.raw = tilt_delta if self.source == "tilt" else yaw
        if desired == 0.0:
            self.filtered = 0.0
            self.raw = 0.0
            self.source = "none"
            self.state = "CENTER"
            return 0.0
        # 只平滑幅度，换方向不沿着旧值慢慢穿过零，也不重新等待 60 毫秒。
        if self.filtered * desired <= 0.0 or dt == 0.0:
            self.filtered = desired
        else:
            self.filtered += (1.0 - math.exp(-dt / .018)) * (desired - self.filtered)
        self.state = "TURN_RIGHT" if desired > 0 else "TURN_LEFT"
        return self.filtered
