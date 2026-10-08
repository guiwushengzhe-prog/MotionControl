"""跳跃：肩和胯一起往上起，整个身体离开站着的高度。

不看脚：真人录像（intent-20260926-195125）里跳起来时，脚的下缘在画面上只抬了
0.01~0.04，模型对脚尖跟得慢，靠它认不准。肩和胯跟得紧：8 次跳跃里两个都比站着时
高出 0.23~0.39 个躯干。

两个都要起来，是为了和这些分开（同一份录像）：
- 踏步、提膝碰对侧肘：抬腿那一侧的胯往上走，胯的中点最多高出 0.23，肩几乎不动（≤0.03）；
- 侧步开合、双手交叉：两个都在 0.10 以内；
- 下蹲站起来：只是回到站着的高度，不会高出去（蹲着的时候基准不往下跟）；
- 往前凑近镜头、往后退：身体变大变小，肩和胯往相反方向走，取小的那个是负的。

基准是"站着时肩和胯在哪"，只在人站直、躯干没整体上下动的时候慢慢跟，跟的办法和
头顶区的锚点一样（control_kernel 里 HEAD_JUMP_*）。
"""

import math

# 肩和胯都比站着时高出这么多个躯干，算起跳；落回这以下算落地。
RISE_START = 0.18
RISE_END = 0.08
# 起来以后一直不落，就不是跳（站上了台阶、手机被碰歪了）：当成换了站位。
MAX_AIR_S = 1.5
# 站着的基准：人站直、躯干整体上下速度不到 FREEZE_VY 躯干/秒时，往现在的位置跟。
FOLLOW_S = 1.5
FREEZE_VY = 0.35
# 躯干比平时短一成，或者任一膝盖弯过 KNEE_BENT 度，算蹲着：基准不往下跟。
CROUCH_RATIO = 0.90
# "平时的躯干"变长跟得快（凑近镜头），变短跟得慢（蹲久了才认）。
TORSO_RISE_S = 0.30
TORSO_FORGET_S = 20.0
# 离基准超过这么多个躯干，是人挪了地方或者镜头动了，直接搬过去。
SNAP_TORSO = 1.20
# 刚开始或者断了一会儿：先站稳这么久，才有基准。
SETTLE_S = 0.30
MAX_GAP_S = 0.25


class JumpDetector:
    def __init__(self):
        self.reset()

    def reset(self):
        self.base: dict | None = None      # 站着时 {"shoulder": y, "hip": y}
        self.torso_ref: float | None = None
        self.prev: tuple[float, float, float] | None = None
        self.still_since: float | None = None
        self.air_since: float | None = None
        self.rise: float | None = None     # 这一帧肩和胯里高出得少的那个，躯干为单位

    @property
    def airborne(self) -> bool:
        return self.air_since is not None

    def update(self, shoulder_y: float, hip_y: float, now: float, *, crouched: bool = False) -> bool:
        """这一帧人在不在空中。shoulder_y、hip_y 是两肩、两胯中点在画面上的 y（向下为正）。"""
        torso = hip_y - shoulder_y
        if not (math.isfinite(shoulder_y) and math.isfinite(hip_y) and math.isfinite(now)) or torso <= 1e-3:
            self.reset()
            return False
        dt = now - self.prev[2] if self.prev else 0.0
        if self.prev and not 0.0 < dt <= MAX_GAP_S:
            self.reset()
            dt = 0.0
        vy = 0.0
        if self.prev and dt > 0.0:
            shoulder_vy = (shoulder_y - self.prev[0]) / torso / dt
            hip_vy = (hip_y - self.prev[1]) / torso / dt
            # 同号才是整个躯干一起动；耸肩、举手两者不一起走。
            if shoulder_vy * hip_vy > 0.0:
                vy = min(abs(shoulder_vy), abs(hip_vy))
        self.prev = (shoulder_y, hip_y, now)

        ref = self.torso_ref = torso if self.torso_ref is None else self.torso_ref
        if dt > 0.0:
            settle = TORSO_RISE_S if torso > ref else TORSO_FORGET_S
            ref = self.torso_ref = ref + (1.0 - math.exp(-dt / settle)) * (torso - ref)
        crouched = crouched or torso < CROUCH_RATIO * ref

        if self.base is None:
            # 先站稳一小会儿再记基准：一上来就在动的话，记下的位置是错的。
            if vy >= FREEZE_VY or crouched:
                self.still_since = None
            elif self.still_since is None:
                self.still_since = now
            elif now - self.still_since >= SETTLE_S:
                self.base = {"shoulder": shoulder_y, "hip": hip_y}
            self.rise = None
            return False

        base = self.base
        span = base["hip"] - base["shoulder"]
        if span <= 1e-3:
            self.reset()
            return False
        if max(abs(shoulder_y - base["shoulder"]), abs(hip_y - base["hip"])) > SNAP_TORSO * span:
            base.update(shoulder=shoulder_y, hip=hip_y)
            self.air_since = None
        rise = self.rise = min(base["shoulder"] - shoulder_y, base["hip"] - hip_y) / span

        if self.air_since is None:
            if rise >= RISE_START:
                self.air_since = now
        elif rise < RISE_END:
            self.air_since = None
        elif now - self.air_since > MAX_AIR_S:
            # 起来了就不下来：不是跳，是换了高度。认下现在的位置。
            base.update(shoulder=shoulder_y, hip=hip_y)
            self.air_since = None
            self.rise = 0.0
            return False

        if self.air_since is None and dt > 0.0 and vy < FREEZE_VY:
            # 往上跟总可以（只会让基准离人更远）；往下跟要人站直着，蹲下去不跟。
            for key, value in (("shoulder", shoulder_y), ("hip", hip_y)):
                if value < base[key] or not crouched:
                    base[key] += (1.0 - math.exp(-dt / FOLLOW_S)) * (value - base[key])
        return self.air_since is not None
