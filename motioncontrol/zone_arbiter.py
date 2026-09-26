"""区域「智能」判定：身体进框不等于按下，先判断是故意伸进来的，还是做动作时顺路扫过。

## 为什么不是等一个固定时间

以前的「防误触」是一组写死的数：手进框 0.04 个躯干深、待够 0.07 秒才按，两只手
一起进来要 0.30 秒；框和某个绑了键的动作冲突时，每次都先等 0.25 秒，动作做完再
封 0.30 秒。每一次按都按最坏的情况等，而最坏的情况只占少数。

这里改成边看边决定：进框的那一刻先记成「判断中」，之后每一帧看新来的证据，
证据够了就按、或者判定是扫过，拿不准才继续看。大多数时候第一帧就能定下来。

## 看哪些证据

- **框有没有冲突。** 没有任何绑了键、又会扫过这个框的动作，就没什么可判断的：
  进框那一帧就按，和「进去就按」一样快。冲突是从录的动作里算出来的（见
  intent_library），没录过的才看动作文件里写的 passes_zones。
- **那个动作是不是已经在做了**（规则成立或已经认出来）。是的话这次就是扫过，
  一直到手离开框都不按。
- **身体别的地方在不在动。** 故意去按一个框，通常只有那一只手（脚）在动；做动作
  时是两只手一起往上、腿在跳在跨。看的是最近 0.1 秒里别的肢体动得多快，既看绝对
  值，也看和伸进框的那只手比。
- **伸进来的那只手停没停住。** 停在框里是故意的；扫过的手会一直穿过去。
- **和你录的比。** 录过「故意按」和「做动作」的话，这次进框前后的样子更像哪一种
  （最近邻，见 SnippetBank）。

最多看 T_MAX_S。到时候还停在框里，而身体也没在大幅度地动，就按——扫过去的手不会
在框里停下来。身体还在大动作里就接着等，等它停下来再按，或者等动作被认出来。

## 两种特别的框

- **要跳才碰得到的框**（头顶跳跃）：人在空中只停一瞬间，等不起。默认「做动作时
  也要按」，不判断，进去就按。用户关掉的话，最多只等 T_MAX_JUMP_S。
- **输出是系统功能的框**（定住区域、挪动区域……）：误触一次代价大，也不赶时间。
  要稳稳停在框里 SYSTEM_HOLD_S 才按，有任何动作正在做就不按。

## 这些数怎么来的

下面的阈值是按人体运动的常识先定的初值，不是真人数据量出来的。录了「录我的动作」
之后，tools/eval_zone_arbiter.py 会在录下来的数据上算误触率、漏按率和延迟，阈值要
照那个结果调。时间一律用真实时间戳，不数帧：30 帧和 60 帧下「3 帧」差了一倍。
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

# 速度的单位都是「躯干每秒」（肩中点到胯中点的长度），位置是「躯干」。和量身同一
# 把尺子（zone_fit.body_frame），人离镜头远近都一样。

# 最近多长一段算一次速度。太短全是骨架抖动，太长跟不上。
SPEED_WINDOW_S = 0.10
# 别的肢体动得比这个慢，算没在动；比 BUSY 快，算在做大动作。
QUIET_SPEED = 0.6
BUSY_SPEED = 1.8
# 别的肢体和伸进框的那只手比：不到三成算只有这只手在动，到八成算两边一起在动。
# 伸进框的那只手停住时拿 TARGET_FLOOR 当它的速度，免得除以一个接近零的数。
RATIO_QUIET = 0.30
RATIO_BUSY = 0.80
TARGET_FLOOR = 0.8
# 伸进框的那只手慢到这个以下，算停在框里了。
SETTLED_SPEED = 0.8
# 胯本身在动（跳、跨步、走动）按这个权重算进「别的肢体」。原地踏步时胯只上下晃一
# 点，乘完之后在 QUIET 以下。
HIP_WEIGHT = 0.7

# 扫过的分数（0 像故意，1 像扫过）低于这个就按。
COMMIT_BELOW = 0.35
# 高于这个算身体还在大动作里。
BUSY_ABOVE = 0.75
# 最多看这么久；之后只要身体不在大动作里就按。
T_MAX_S = 0.25
T_MAX_JUMP_S = 0.08
# 冲突的动作做完这么久之内再进框，多半是动作收回来的那一下，分数加一截。
TAIL_S = 0.50
TAIL_BOOST = 0.30
# 已经按下了，这么短时间之内认出了会扫过这里的动作：那一下多半是误按，松开。
RECALL_S = 0.50
# 系统功能：稳住多久才按。
SYSTEM_HOLD_S = 0.60
# 手进框至少这么深（躯干）才按：人正好停在框边上时，骨架一抖就进进出出。只管手：
# 脚区本来就要脚确实往外抬起来（control_kernel 的 FOOT_ZONE_LIFT），头顶区要鼻子
# 抬过站着时的高度，都不会停在边上抖。
ENTRY_MARGIN = 0.02
MARGIN_ZONES = frozenset({"leftHand", "rightHand", "lookGate"})
# 录的数据和这次的比，分数里占多少。
KNN_WEIGHT = 0.6
# 扫过之后框闪一下红，界面要看得到。
SWEPT_FLASH_S = 0.6

MIN_SCORE = 0.42

# 每个框：谁伸进去算数（第一个点代表这只手或脚），别的肢体看哪些。
ZONE_LIMBS = {
    "leftHand": {"target": ("left_wrist",), "others": ("right_wrist", "hip")},
    "rightHand": {"target": ("right_wrist",), "others": ("left_wrist", "hip")},
    "leftFoot": {"target": ("left_ankle", "left_heel"), "others": ("right_ankle", "left_wrist", "right_wrist", "hip")},
    "rightFoot": {"target": ("right_ankle", "right_heel"), "others": ("left_ankle", "left_wrist", "right_wrist", "hip")},
    "headJump": {"target": ("nose",), "others": ("left_wrist", "right_wrist")},
}
# 要跳才碰得到的框。默认「做动作时也要按」。
JUMP_ZONES = frozenset({"headJump"})
TRACKED_POINTS = ("nose", "left_wrist", "right_wrist", "left_ankle", "right_ankle", "left_heel", "right_heel")


def _score(point) -> float:
    if not isinstance(point, dict):
        return 0.0
    try:
        return float(point.get("score", point.get("visibility", 0.0)))
    except (TypeError, ValueError):
        return 0.0


def _clamp01(value: float) -> float:
    return 0.0 if value <= 0.0 else 1.0 if value >= 1.0 else value


class Kinematics:
    """最近一小段里几个关键点相对胯在哪、动得多快，还有胯本身动得多快。

    每帧由内核喂一次。点看不清（分数低）就当这一帧没有它，不拿一个乱跳的点去算速度。
    """

    def __init__(self, keep_s: float = 0.4) -> None:
        self.keep_s = keep_s
        self._samples: deque[tuple[float, dict[str, tuple[float, float]], tuple[float, float] | None]] = deque()
        self._scale: tuple[float, float] | None = None

    def reset(self) -> None:
        self._samples.clear()
        self._scale = None

    def update(self, pose_map: dict | None, frame: dict | None, now: float) -> None:
        if not pose_map or frame is None:
            self.reset()
            return
        hip, ux, uy = frame["hip"], frame["ux"], frame["uy"]
        if ux <= 0 or uy <= 0:
            self.reset()
            return
        points = {}
        for name in TRACKED_POINTS:
            point = pose_map.get(name)
            if point and _score(point) >= MIN_SCORE:
                points[name] = ((point["x"] - hip["x"]) / ux, (point["y"] - hip["y"]) / uy)
        # 胯本身按画面坐标记，算速度时再除以当下的尺子：尺子每帧都会抖一点，先除再
        # 相减的话，人站着不动胯也像在动。
        self._samples.append((now, points, (hip["x"], hip["y"])))
        self._scale = (ux, uy)
        while self._samples and now - self._samples[0][0] > self.keep_s:
            self._samples.popleft()

    def _pair(self, name: str):
        """最近一帧和大约 SPEED_WINDOW_S 之前那一帧里都有这个点的一对。"""
        if not self._samples:
            return None
        now, latest, hip_now = self._samples[-1]
        current = hip_now if name == "hip" else latest.get(name)
        if current is None:
            return None
        older = None
        for t, points, hip in reversed(self._samples):
            if t >= now:
                continue
            value = hip if name == "hip" else points.get(name)
            if value is None:
                continue
            older = (t, value)
            if now - t >= SPEED_WINDOW_S:
                break
        if older is None:
            return None
        return now - older[0], older[1], current

    def velocity(self, name: str) -> tuple[float, float] | None:
        pair = self._pair(name)
        if pair is None or pair[0] <= 1e-6:
            return None
        dt, old, new = pair
        vx, vy = (new[0] - old[0]) / dt, (new[1] - old[1]) / dt
        if name == "hip" and self._scale is not None:
            vx, vy = vx / self._scale[0], vy / self._scale[1]
        return vx, vy

    def speed(self, name: str) -> float | None:
        velocity = self.velocity(name)
        return None if velocity is None else math.hypot(*velocity)

    def position(self, name: str) -> tuple[float, float] | None:
        if not self._samples:
            return None
        return self._samples[-1][1].get(name)

    def target_of(self, zone: str) -> str | None:
        """这个框的「那只手（脚）」这一帧用哪个点：脚踝看不清就用脚跟。"""
        for name in ZONE_LIMBS.get(zone, {}).get("target", ()):
            if self.position(name) is not None:
                return name
        return None

    def features(self, zone: str) -> list[float]:
        """和录的数据比的时候用的那一串数。长度只由框决定，缺的点填 0。"""
        limbs = ZONE_LIMBS[zone]
        target = self.target_of(zone)
        pos = self.position(target) if target else None
        vel = self.velocity(target) if target else None
        out = [*(value / 0.5 for value in (pos or (0.0, 0.0))),
               *(value / 2.0 for value in (vel or (0.0, 0.0)))]
        for name in limbs["others"]:
            out.extend(value / 2.0 for value in (self.velocity(name) or (0.0, 0.0)))
        return out

    def activity(self, zone: str) -> tuple[float, float | None]:
        """(别的肢体里动得最快的那个, 伸进框的那只手的速度)。"""
        limbs = ZONE_LIMBS[zone]
        others = []
        for name in limbs["others"]:
            speed = self.speed(name)
            if speed is not None:
                others.append(speed * (HIP_WEIGHT if name == "hip" else 1.0))
        target = self.target_of(zone)
        return max(others, default=0.0), (self.speed(target) if target else None)


def sweep_score(activity: float, target_speed: float | None) -> float:
    """0 像故意去按，1 像做动作时扫过。只看身体动作，不看录的数据。

    两个角度取大的：别的肢体本身动得快（跳、跨），或者别的肢体和伸进框的这只手动得
    一样快（两只手一起往上举）。慢慢地一起举，绝对值不大，但比值是 1。
    """
    absolute = _clamp01((activity - QUIET_SPEED) / (BUSY_SPEED - QUIET_SPEED))
    ratio = activity / max(target_speed if target_speed is not None else 0.0, TARGET_FLOOR)
    relative = _clamp01((ratio - RATIO_QUIET) / (RATIO_BUSY - RATIO_QUIET))
    return max(absolute, relative)


@dataclass
class Snippet:
    """录的数据里的一次进框：进框后每一帧的那串数。"""
    zone: str
    label: str            # "intent" 故意按 / "sweep" 做动作时扫过
    tag: str              # 扫过时是哪个动作的触发名，站着随便动是 "idle"
    source: str           # 从哪一段录的来，体检时拿来把自己排除掉
    frames: list[tuple[float, list[float]]] = field(default_factory=list)  # (进框后多久, 那串数)


class SnippetBank:
    """录下来的进框，按框分开存。问的时候找最像的几次，看它们多半是哪一种。"""

    K = 5
    MIN_EACH = 3        # 两种各至少有这么多次，才拿来比
    MATCH_S = 0.05      # 进框后的时刻要对得上

    def __init__(self, snippets: list[Snippet] | None = None) -> None:
        self.by_zone: dict[str, list[Snippet]] = {}
        for snippet in snippets or ():
            self.add(snippet)
        self.exclude: str | None = None

    def add(self, snippet: Snippet) -> None:
        if snippet.frames:
            self.by_zone.setdefault(snippet.zone, []).append(snippet)

    def __len__(self) -> int:
        return sum(len(items) for items in self.by_zone.values())

    def counts(self, zone: str, sweep_tags) -> tuple[int, int]:
        intent = sweep = 0
        for snippet in self._usable(zone, sweep_tags):
            if snippet.label == "intent":
                intent += 1
            else:
                sweep += 1
        return intent, sweep

    def _usable(self, zone: str, sweep_tags):
        for snippet in self.by_zone.get(zone, ()):
            if self.exclude and snippet.source == self.exclude:
                continue
            if snippet.label == "sweep" and snippet.tag != "idle" and snippet.tag not in sweep_tags:
                continue
            yield snippet

    def sweep_probability(self, zone: str, elapsed: float, vector: list[float], sweep_tags) -> float | None:
        """这次更像扫过的概率；录的不够就是 None。

        sweep_tags 是现在真的会扫过这个框的那些动作（绑了键、又没设成一起按）。没绑
        键的动作录得再多也不算：人在这个游戏里不会去做它。
        """
        usable = list(self._usable(zone, sweep_tags))
        if sum(1 for s in usable if s.label == "intent") < self.MIN_EACH or \
                sum(1 for s in usable if s.label == "sweep") < self.MIN_EACH:
            return None
        scored = []
        for snippet in usable:
            best = min(snippet.frames, key=lambda item: abs(item[0] - elapsed))
            if abs(best[0] - elapsed) > self.MATCH_S:
                continue
            distance = math.sqrt(sum((a - b) ** 2 for a, b in zip(vector, best[1])))
            scored.append((distance, snippet.label))
        if len(scored) < self.MIN_EACH:
            return None
        scored.sort(key=lambda item: item[0])
        weights = {"intent": 0.0, "sweep": 0.0}
        for distance, label in scored[:self.K]:
            weights[label] += 1.0 / (distance + 0.25)
        total = weights["intent"] + weights["sweep"]
        return weights["sweep"] / total if total > 0 else None


@dataclass
class ZoneInput:
    """内核每帧给一个框的判断材料。"""
    inside: bool
    depth: float = math.inf       # 进了框多深（躯干），量不出来就是无穷大
    conflict: bool = False        # 有会扫过它、又绑了键的动作，框要让路
    busy: bool = False            # 那些动作现在正在做
    tail: bool = False            # 那些动作刚做完不到 TAIL_S
    deliberate: bool = False      # 输出是系统功能：要稳住才按
    jump: bool = False            # 要跳才碰得到
    exit_frames: int = 2
    sweep_tags: tuple[str, ...] = ()


def fresh_zone_state() -> dict:
    return {"inside": 0, "outside": 0, "pressed": False, "phase": "idle",
            "entered_at": None, "pressed_at": None, "last_pressed_at": None, "still_since": None,
            "swept_at": None, "reason": "", "progress": 0.0, "score": None}


class ZoneArbiter:
    """每个框一个小状态机：idle → pending（判断中）→ pressed / swept（判定扫过）。

    swept 一直保持到那只手（脚）离开框：扫过去的手可能在框里多待几帧，不能一过判断
    期就又按下去。
    """

    def __init__(self, bank: SnippetBank | None = None) -> None:
        self.bank = bank or SnippetBank()

    def update(self, name: str, state: dict, now: float, given: ZoneInput, kin: Kinematics) -> bool:
        """推进一帧，返回按下状态有没有变。"""
        before = state["pressed"]
        if not given.inside:
            self._outside(state, now, given)
        else:
            state["outside"] = 0
            state["inside"] += 1
            if state["phase"] == "idle":
                state.update(phase="pending", entered_at=now, still_since=None, progress=0.0, score=None, reason="")
            if state["phase"] == "pressed":
                # 刚按下就认出了会扫过这里的动作：那一下多半是误按，赶紧松开。
                if given.busy and state["pressed_at"] is not None and now - state["pressed_at"] < RECALL_S:
                    self._sweep(state, now, "recalled")
            elif state["phase"] == "pending":
                self._decide(name, state, now, given, kin)
        return state["pressed"] != before

    def _outside(self, state: dict, now: float, given: ZoneInput) -> None:
        state["outside"] += 1
        state["inside"] = 0
        if state["outside"] < given.exit_frames:
            return
        if state["phase"] == "pending" and (given.conflict or given.deliberate):
            # 没等到按就出去了：这一下是扫过去的。框闪一下红，人看得出是被拦下了。
            state["swept_at"] = now
            state["reason"] = "passed"
        state.update(phase="idle", pressed=False, entered_at=None, pressed_at=None,
                     still_since=None, progress=0.0)

    def _press(self, state: dict, now: float, reason: str) -> None:
        state.update(phase="pressed", pressed=True, pressed_at=now, last_pressed_at=now,
                     reason=reason, progress=1.0)

    def _sweep(self, state: dict, now: float, reason: str) -> None:
        state.update(phase="swept", pressed=False, pressed_at=None, swept_at=now, reason=reason, progress=0.0)

    def _decide(self, name: str, state: dict, now: float, given: ZoneInput, kin: Kinematics) -> None:
        elapsed = now - state["entered_at"]
        if given.busy:
            self._sweep(state, now, "action")
            return
        if name in MARGIN_ZONES and given.depth < ENTRY_MARGIN:
            return
        activity, target_speed = kin.activity(name) if name in ZONE_LIMBS else (0.0, None)
        settled = target_speed is not None and target_speed < SETTLED_SPEED
        if given.deliberate:
            # 系统功能：稳住才算。动一下就重新计时。
            state["still_since"] = (state["still_since"] or now) if settled else None
            held = now - state["still_since"] if state["still_since"] is not None else 0.0
            state["progress"] = min(1.0, held / SYSTEM_HOLD_S)
            if held >= SYSTEM_HOLD_S:
                self._press(state, now, "held")
            return
        if not given.conflict:
            self._press(state, now, "clear")
            return
        score = sweep_score(activity, target_speed)
        if name in ZONE_LIMBS:
            learned = self.bank.sweep_probability(name, elapsed, kin.features(name), given.sweep_tags)
            if learned is not None:
                score = (1.0 - KNN_WEIGHT) * score + KNN_WEIGHT * learned
        if given.tail:
            score = min(1.0, score + TAIL_BOOST)
        state["score"] = round(score, 3)
        limit = T_MAX_JUMP_S if given.jump else T_MAX_S
        if score < COMMIT_BELOW:
            self._press(state, now, "quiet")
        elif settled and score < BUSY_ABOVE:
            self._press(state, now, "settled")
        elif elapsed >= limit and score < BUSY_ABOVE:
            self._press(state, now, "timeout")
        # 否则：身体还在大动作里，接着看。


def zone_phase_for_display(state: dict, now: float) -> str:
    """界面上画成什么样：idle 灰、pending 黄、pressed 亮、swept 闪一下红。"""
    if state.get("pressed"):
        return "pressed"
    phase = state.get("phase", "idle")
    if phase == "pressed":
        return "idle"
    if phase == "idle" and state.get("swept_at") is not None and now - state["swept_at"] < SWEPT_FLASH_S:
        return "swept"
    return phase
