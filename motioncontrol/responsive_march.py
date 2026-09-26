"""新版踏步：交替抬脚时提前响应，两脚落地后短时间内停止。"""

import math


class ResponsiveMarch:
    LIFT_START = .07
    LIFT_END = .04
    CONFIRM_S = .020
    GROUNDED_STOP_MIN_S = .12
    GROUNDED_STOP_MAX_S = .28
    STILL_STOP_S = .18

    def __init__(self):
        self.reset()

    def reset(self):
        self.legs = {side: {"since": None, "announced": False, "motion_height": None}
                     for side in ("left", "right")}
        self.last_side = ""
        self.last_event_at = 0.0
        self.last_motion_at = 0.0
        self.grounded_since = None
        self.grounded_stop_s = self.GROUNDED_STOP_MIN_S
        self.active_until = 0.0
        self.last_at = None

    def update(self, lifts, now, *, excluded=(), blocked=False, jumping=False):
        if not math.isfinite(now) or blocked or not lifts or not all(math.isfinite(lifts.get(side, math.nan))
                                          for side in self.legs):
            self.reset()
            return False
        if self.last_at is not None and not 0 < now - self.last_at <= .25:
            self.reset()
        self.last_at = now
        was_active = now < self.active_until
        moving = False
        for side, leg in self.legs.items():
            height = lifts[side]
            if side in excluded or height < self.LIFT_END:
                leg.update(since=None, announced=False, motion_height=height)
                continue
            if leg["motion_height"] is None or abs(height - leg["motion_height"]) >= .01:
                moving = True
                leg["motion_height"] = height
            if leg["since"] is None:
                if height < self.LIFT_START:
                    continue
                leg["since"] = now
                moving = True
            if leg["announced"] or now - leg["since"] < self.CONFIRM_S:
                continue
            leg["announced"] = True
            gap = now - self.last_event_at
            # 第一只脚仍只记节奏，避免单腿挪动启动。相反脚上来就确认，不等峰值。
            if self.last_side and side != self.last_side and .10 <= gap <= 1.50:
                # 跨到下一只脚的确认帧要留少量余量；落地/静止判据负责及时停步。
                self.active_until = now + min(.65, max(.22, 1.25 * gap + .02))
                # 真人慢踏步在两步之间会双脚落地约 0.2 秒，不能当成已经停下。
                self.grounded_stop_s = min(self.GROUNDED_STOP_MAX_S,
                                           max(self.GROUNDED_STOP_MIN_S, .4 * gap))
                self.last_motion_at = now
            self.last_side, self.last_event_at = side, now
        if moving:
            self.last_motion_at = now
        grounded = all(height < self.LIFT_END for height in lifts.values())
        if jumping and was_active:
            self.active_until = max(self.active_until, now + .15)
            self.last_motion_at = now
            self.grounded_since = None
        elif grounded:
            if self.grounded_since is None:
                self.grounded_since = now
            if now - self.grounded_since >= self.grounded_stop_s:
                self.active_until = 0.0
        else:
            self.grounded_since = None
        # 起脚前的 0.04~0.07 过渡不能突然套用悬空静止超时。
        if (any(height >= self.LIFT_START for height in lifts.values())
                and now - self.last_motion_at >= self.STILL_STOP_S):
            self.active_until = 0.0
        return now < self.active_until
