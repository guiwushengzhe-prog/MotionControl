"""「录我的动作」：系统说做什么，人就做什么，把骨架连同「这一段在做什么」一起存下来。

区域的智能判定（zone_arbiter）要回答「这次进框是故意按，还是做动作时扫过」。光有
平时玩的录像回答不了：录像里看不出人当时想干什么。所以这里让人照着提示做：

- **自然站着随便动一下**：不去碰任何框。这段里碰到框的都是不该按的。
- **每个框故意按几次、快速点几次**：这段里进这个框的都是故意的。
- **每个动作做几遍**：这段里碰到框的都是做动作时扫过的。

录的东西和游戏无关——动作怎么做、框在身体的哪儿，换游戏都不变——所以存在用户
目录（intent_recordings/），不跟游戏配置走。每一项只留最近录的那一遍，下了新动作
只要补录那一项，见 missing_items。

框在哪、多大以后还会变（量身、定住、拖动），所以这里存的是整段骨架，不存「碰没
碰到哪个框」：框变了拿骨架重新算（intent_library）。

这一轮每帧在控制循环里跑，只往内存里追加；存盘在后台线程里做。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

SCHEMA = "motioncontrol.intent_recording.v1"
INDEX_SCHEMA = "motioncontrol.intent_index.v1"
INDEX_NAME = "index.json"

ZONE_NAMES = {"leftHand": "左手", "rightHand": "右手", "headJump": "头顶", "leftFoot": "左脚", "rightFoot": "右脚"}
ZONE_ORDER = ("leftHand", "rightHand", "headJump", "leftFoot", "rightFoot")
# 怎么把那只手（脚、头）送进框里。
ZONE_HOW = {
    "leftHand": "左手伸进左手框", "rightHand": "右手伸进右手框", "headJump": "往上跳，头碰头顶框",
    "leftFoot": "左脚往左伸进左脚框", "rightFoot": "右脚往右伸进右脚框",
}
# 一直做着才算的动作（踏步没有「一下」），按时间录。
CONTINUOUS_TRIGGERS = frozenset({"motion.march"})

PRESS_COUNT = 5
TAP_COUNT = 3
ACTION_COUNT = 5
IDLE_S = 8.0
CONTINUOUS_S = 6.0
READY_S = 1.5        # 每一项开头给人看提示、摆好姿势的时间，这段不算数
SETTLE_S = 1.0       # 做够次数之后再录一会儿：动作收回来那一下也要录进去
STEP_TIMEOUT_S = 25.0
PREPARE_S = 3.0      # 点开始之后留给人走回镜头前
LOST_ISSUE_S = 1.5
ZONE_REENTRY_S = 0.10    # 出框这么久再进来才算新的一下，骨架抖一下不算
ACTION_REPEAT_S = 0.30   # 动作松开这么久再认出来才算新的一遍
MAX_FRAMES = 30_000      # 约 16 分钟 30 帧，远超一轮；只是防止卡住时无限涨


def zone_steps(zone: str) -> list[dict]:
    name = ZONE_NAMES[zone]
    return [
        {"key": f"press:{zone}", "kind": "press", "zone": zone, "target": PRESS_COUNT,
         "name": f"{name}框", "say": f"{ZONE_HOW[zone]}，停一下再收回来",
         "hint": f"照平时玩的时候那样按，一共 {PRESS_COUNT} 次"},
        {"key": f"tap:{zone}", "kind": "tap", "zone": zone, "target": TAP_COUNT,
         "name": f"{name}框（快速）", "say": f"{ZONE_HOW[zone]}，碰一下马上收回",
         "hint": f"越快越好，一共 {TAP_COUNT} 次"},
    ]


def action_step(trigger: str, name: str) -> dict:
    if trigger in CONTINUOUS_TRIGGERS:
        return {"key": f"action:{trigger}", "kind": "action", "trigger": trigger, "name": name,
                "duration_s": CONTINUOUS_S, "say": f"{name}", "hint": f"照平时的样子做 {CONTINUOUS_S:.0f} 秒，框亮不亮都不用管"}
    return {"key": f"action:{trigger}", "kind": "action", "trigger": trigger, "name": name,
            "target": ACTION_COUNT, "say": f"做{name}",
            "hint": f"照平时的样子做 {ACTION_COUNT} 遍，框亮不亮都不用管"}


IDLE_STEP = {"key": "idle", "kind": "idle", "name": "随便动动", "duration_s": IDLE_S,
             "say": "自然站着，随便动动", "hint": "挠挠头、换换脚、调整一下站姿，别去碰框"}


def build_steps(actions: list[tuple[str, str]], keys=None) -> list[dict]:
    """全部要录的项目，或者其中 keys 这几项。actions 是 [(触发名, 名字)]。"""
    steps = [dict(IDLE_STEP)]
    for zone in ZONE_ORDER:
        steps += zone_steps(zone)
    steps += [action_step(trigger, name) for trigger, name in actions]
    if keys:
        wanted = set(keys)
        steps = [step for step in steps if step["key"] in wanted]
    return steps


class IntentRecordingSession:
    """一轮录制。preparing（倒计时）→ recording（一项一项往下）→ done / cancelled。"""

    def __init__(self, steps: list[dict], now: float, *, prepare_s: float = PREPARE_S) -> None:
        if not steps:
            raise ValueError("没有要录的项目")
        self.steps = [dict(step, count=0, skipped=False, start_t=None, end_t=None) for step in steps]
        self.index = 0
        self.state = "preparing" if prepare_s > 0 else "recording"
        self.started_at = now
        self.prepare_until = now + max(0.0, prepare_s)
        self.step_phase = "ready"          # ready → doing → settle
        self.phase_since = now if prepare_s <= 0 else self.prepare_until
        self.issue = ""
        self.lost_since: float | None = None
        self.frames: list[dict] = []
        self.origin = now if prepare_s <= 0 else self.prepare_until
        self._zone_out_since: dict[str, float] = {}
        self._zone_in: dict[str, bool] = {}
        self._trigger_off_since: dict[str, float] = {}
        self._trigger_on: dict[str, bool] = {}

    @property
    def active(self) -> bool:
        return self.state in {"preparing", "recording"}

    @property
    def step(self) -> dict | None:
        return self.steps[self.index] if self.state == "recording" and self.index < len(self.steps) else None

    # ---------- 每一帧 ----------

    def update(self, now: float, pose_map: dict | None, width: int, height: int,
               zone_inside: dict[str, bool], active_triggers: set[str], *,
               sample_at: float | None = None) -> None:
        if not self.active:
            return
        if self.state == "preparing":
            if now < self.prepare_until:
                return
            self.state = "recording"
            self.phase_since = now
        if pose_map:
            self.lost_since = None
            self.issue = ""
        else:
            self.lost_since = self.lost_since if self.lost_since is not None else now
            if now - self.lost_since >= LOST_ISSUE_S:
                self.issue = "not_visible"
        step = self.steps[self.index]
        label = self.index if self.step_phase in {"doing", "settle"} else -1
        if pose_map and len(self.frames) < MAX_FRAMES:
            self.frames.append(self._frame(now, pose_map, width, height, label, sample_at))
        entered = self._rising_zones(now, zone_inside)
        started = self._rising_triggers(now, active_triggers)
        if self.step_phase == "ready":
            if now - self.phase_since >= READY_S:
                self.step_phase = "doing"
                self.phase_since = now
                step["start_t"] = round(now - self.origin, 4)
            return
        if self.step_phase == "doing":
            if step["kind"] in {"press", "tap"} and step["zone"] in entered:
                step["count"] += 1
            elif step["kind"] == "action" and step["trigger"] in started:
                step["count"] += 1
            done = (now - self.phase_since >= step["duration_s"]) if "duration_s" in step \
                else step["count"] >= step["target"]
            if done:
                self.step_phase = "settle"
                self.phase_since = now
            elif now - self.phase_since >= STEP_TIMEOUT_S:
                self._next(now)
            return
        if now - self.phase_since >= SETTLE_S:
            self._next(now)

    def _frame(self, now: float, pose_map: dict, width: int, height: int, label: int,
               sample_at: float | None = None) -> dict:
        frame = {
            "t": round(now - self.origin, 4), "w": int(width), "h": int(height), "step": label,
            "pose": {
                name: [round(float(point.get("x", 0.0)), 5), round(float(point.get("y", 0.0)), 5),
                       round(float(point.get("score", point.get("visibility", 0.0))), 3)]
                for name, point in pose_map.items() if isinstance(point, dict)
            },
        }
        # t 是电脑收到这一帧的时刻，c 是认出它的时刻（手机来的帧才有，见
        # ControlKernel.pose_sample_at）。回放时 t 管判断等了多久，c 管速度。
        if sample_at is not None and abs(sample_at - now) >= 0.0005:
            frame["c"] = round(sample_at - self.origin, 4)
        return frame

    def _rising_zones(self, now: float, zone_inside: dict[str, bool]) -> set[str]:
        entered = set()
        for zone in set(zone_inside) | set(self._zone_in):
            inside = bool(zone_inside.get(zone))
            was = self._zone_in.get(zone, False)
            if inside and not was:
                out_since = self._zone_out_since.get(zone)
                if out_since is None or now - out_since >= ZONE_REENTRY_S:
                    entered.add(zone)
                self._zone_in[zone] = True
            elif not inside and was:
                self._zone_in[zone] = False
                self._zone_out_since[zone] = now
        return entered

    def _rising_triggers(self, now: float, active: set[str]) -> set[str]:
        started = set()
        for trigger in set(active) | set(self._trigger_on):
            on = trigger in active
            was = self._trigger_on.get(trigger, False)
            if on and not was:
                off_since = self._trigger_off_since.get(trigger)
                if off_since is None or now - off_since >= ACTION_REPEAT_S:
                    started.add(trigger)
            elif was and not on:
                self._trigger_off_since[trigger] = now
            self._trigger_on[trigger] = on
        return started

    def _next(self, now: float) -> None:
        step = self.steps[self.index]
        if step["start_t"] is not None and step["end_t"] is None:
            step["end_t"] = round(now - self.origin, 4)
        self.index += 1
        self.step_phase = "ready"
        self.phase_since = now
        if self.index >= len(self.steps):
            self.state = "done"

    # ---------- 人点的 ----------

    def skip(self, now: float) -> None:
        """这一项不录了（比如脚拍不到）。录了一半的也不要。"""
        if self.state == "preparing":
            self.state = "recording"
        if self.state != "recording":
            return
        self.steps[self.index]["skipped"] = True
        self._next(now)

    def cancel(self) -> None:
        if self.active:
            self.state = "cancelled"
            self.frames = []

    # ---------- 给界面、给存盘 ----------

    def status(self, now: float) -> dict:
        step = self.step
        remaining = 0.0
        if self.state == "preparing":
            remaining = max(0.0, self.prepare_until - now)
        elif step and self.step_phase == "doing" and "duration_s" in step:
            remaining = max(0.0, step["duration_s"] - (now - self.phase_since))
        return {
            "active": self.active, "state": self.state, "index": self.index,
            "phase": self.step_phase, "remaining_s": round(remaining, 2), "issue": self.issue,
            "steps": [{key: item.get(key) for key in (
                "key", "kind", "name", "zone", "trigger", "target", "duration_s", "count", "skipped", "say", "hint")}
                for item in self.steps],
        }

    def recorded_steps(self) -> list[dict]:
        """真录到了的那些项（开始了、没被跳过）。"""
        return [step for step in self.steps if step["start_t"] is not None and not step["skipped"]
                and step["end_t"] is not None]


class IntentRecordingStore:
    """intent_recordings/ 目录：每轮一个文件，外加一份「每一项最新录在哪」的索引。"""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self._lock = threading.Lock()

    def index(self) -> dict:
        path = self.directory / INDEX_NAME
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict) or data.get("schema") != INDEX_SCHEMA:
            return {}
        items = data.get("items")
        return items if isinstance(items, dict) else {}

    def recorded_keys(self) -> set[str]:
        return set(self.index())

    def save(self, session: IntentRecordingSession) -> Path | None:
        """存下这一轮，把录到的那几项在索引里指向它。一项都没录到就不存。"""
        steps = session.recorded_steps()
        if not steps:
            return None
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            path = self.directory / f"intent-{stamp}.jsonl"
            header = {"schema": SCHEMA, "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                      "steps": [{key: step.get(key) for key in (
                          "key", "kind", "name", "zone", "trigger", "target", "duration_s",
                          "count", "skipped", "start_t", "end_t")} for step in session.steps]}
            temp = path.with_suffix(".tmp")
            with temp.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(header, ensure_ascii=False) + "\n")
                for frame in session.frames:
                    stream.write(json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n")
            temp.replace(path)
            items = self.index()
            for position, step in enumerate(session.steps):
                if step in steps:
                    items[step["key"]] = {"file": path.name, "step": position, "count": step["count"],
                                          "target": step.get("target"), "recorded_at": header["recorded_at"]}
            self._write_index(items)
            self._prune(items)
            return path

    def forget(self, keys) -> None:
        with self._lock:
            items = {key: value for key, value in self.index().items() if key not in set(keys)}
            self._write_index(items)
            self._prune(items)

    def _write_index(self, items: dict) -> None:
        path = self.directory / INDEX_NAME
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps({"schema": INDEX_SCHEMA, "items": items}, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        temp.replace(path)

    def _prune(self, items: dict) -> None:
        """索引里谁都不指向的旧文件删掉：每一项只留最新一遍，目录不会越录越大。"""
        used = {item.get("file") for item in items.values()}
        for path in self.directory.glob("intent-*.jsonl"):
            if path.name not in used:
                try:
                    path.unlink()
                except OSError:
                    pass

    def load(self, name: str) -> tuple[dict, list[dict]] | None:
        """读一个文件：(开头那行, 每一帧)。坏文件返回 None。"""
        path = self.directory / Path(name).name
        try:
            with path.open("r", encoding="utf-8") as stream:
                header = json.loads(stream.readline())
                if header.get("schema") != SCHEMA:
                    return None
                frames = [json.loads(line) for line in stream if line.strip()]
        except (OSError, ValueError):
            return None
        return header, frames


def pose_from_frame(frame: dict) -> dict[str, dict]:
    """存盘的紧凑写法换回内核用的 pose_map。"""
    return {name: {"x": value[0], "y": value[1], "score": value[2]}
            for name, value in (frame.get("pose") or {}).items()
            if isinstance(value, list) and len(value) >= 3}
