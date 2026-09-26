"""把「录我的动作」录下来的骨架，对着现在的框重新算一遍。

录的时候存的是整段骨架和「这一段在做什么」（intent_recording），不存碰没碰到框：
框以后还会量身、定住、拖动。这里拿骨架按现在的设置回放，算出三样东西：

1. **每个动作扫过每个框几次**（rates）。框要不要给这个动作让路以它为准，替代动作
   文件里手写的 passes_zones。录的时候一次都没扫过的，就不让。
2. **每次进框的样子**（SnippetBank）。「故意按」那几项里的进框标成 intent，「做
   动作」「随便动动」里的标成 sweep。判定拿这次进框和它们比，见 zone_arbiter。
3. **体检报告**：按现在这个游戏的绑定、用「智能」判定把录的东西完整重放一遍，数
   做动作时误按了几次、故意按时按出来几次、慢了多少毫秒。回放某一段时，那一段自己
   录的进框从比较对象里拿掉（SnippetBank.exclude），不然等于拿答案考自己。

回放用的是一个不读写设置的临时内核（ControlKernel(persist=False)），走的是和实时
完全一样的代码，所以回放出来的就是实时会发生的。tools/eval_zone_arbiter.py 也是用
这里的函数。
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import threading
import time
from dataclasses import dataclass, field

from motioncontrol.intent_recording import IntentRecordingStore, pose_from_frame
from motioncontrol.zone_arbiter import T_MAX_S, ZONE_LIMBS, Snippet, SnippetBank

ZONES = ("leftHand", "rightHand", "headJump", "leftFoot", "rightFoot")
# 进框之后录多久的样子。比判定最多看的时间多一点。
SNIPPET_S = T_MAX_S + 0.10
REENTRY_S = 0.10
BASE_T = 1000.0


class _NullOutput:
    """回放时的输出：什么都不做。"""
    enabled = True

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


# ---------- 读录的东西 ----------

@dataclass
class Segment:
    """录的一项：哪个文件的第几项、做的是什么、那一段的帧。"""
    key: str
    kind: str
    source: str
    step: dict
    frames: list[dict] = field(default_factory=list)


def load_segments(store: IntentRecordingStore) -> list[Segment]:
    """索引里每一项最新录的那一段。同一个文件只读一次。"""
    index = store.index()
    by_file: dict[str, list[tuple[str, int]]] = {}
    for key, item in index.items():
        if isinstance(item, dict) and item.get("file"):
            by_file.setdefault(item["file"], []).append((key, int(item.get("step", -1))))
    segments = []
    for name, wanted in sorted(by_file.items()):
        loaded = store.load(name)
        if loaded is None:
            continue
        header, frames = loaded
        steps = header.get("steps") or []
        for key, position in wanted:
            if not 0 <= position < len(steps) or steps[position].get("key") != key:
                continue
            step = steps[position]
            # 这一项前后各多带一点：判定需要进框前 0.1 秒的速度，动作收尾也要。
            start = float(step.get("start_t") or 0.0) - 0.5
            end = float(step.get("end_t") or 0.0)
            chosen = [frame for frame in frames if start <= float(frame.get("t", 0.0)) <= end]
            segments.append(Segment(key=key, kind=str(step.get("kind", "")), source=f"{name}#{position}",
                                    step=step, frames=chosen))
    return segments


# ---------- 回放 ----------

def make_replay_kernel(snapshot: dict):
    """照着实时内核的设置建一个临时内核。snapshot 见 ControlKernel.replay_snapshot。"""
    from motioncontrol.control_kernel import ControlKernel
    from motioncontrol.custom_poses import CustomPoseStore

    kernel = ControlKernel(_NullOutput(), persist=False)
    with kernel._lock:
        kernel.control_bindings = copy.deepcopy(snapshot.get("control_bindings") or {})
        kernel.motion_config = copy.deepcopy(snapshot.get("motion_config") or [])
        kernel._install_pose_actions_locked(copy.deepcopy(snapshot.get("pose_actions") or []))
        poses = snapshot.get("custom_poses")
        if poses:
            store = CustomPoseStore.__new__(CustomPoseStore)
            store.path, store.poses, store.last_error, store._progress = None, copy.deepcopy(poses), "", {}
            kernel.custom_pose_store = store
        kernel.zone_fit = copy.deepcopy(snapshot["zone_fit"])
        kernel.zones_frozen = bool(snapshot.get("zones_frozen"))
        kernel.frozen_rects = copy.deepcopy(snapshot.get("frozen_rects") or {})
        kernel.frozen_anchor = copy.deepcopy(snapshot.get("frozen_anchor"))
        kernel.zone_trigger_mode = snapshot.get("zone_trigger_mode", "smart")
        kernel.march_algorithm = snapshot.get("march_algorithm", "legacy")
        kernel.vertical_look.update(copy.deepcopy(snapshot.get("vertical_look") or {}))
        if snapshot.get("hand_mouse"):
            kernel.hand_mouse_controller.configure(copy.deepcopy(snapshot["hand_mouse"]))
        if snapshot.get("action_chain"):
            kernel.action_chain.configure(copy.deepcopy(snapshot["action_chain"]))
    return kernel


# 在后台算的时候每回放这么多帧歇一下，把 GIL 让给控制线程：人可能正在玩，
# 回放抢着跑会让实时识别一顿一顿的。
THROTTLE_EVERY = 10
THROTTLE_S = 0.001
_throttle = threading.local()


def replay_segment(kernel, segment: Segment, on_frame=None) -> list[dict]:
    """把一段喂给内核，每帧记下各个框的状态。时间用录的时间戳，不用现在的钟。"""
    rows = []
    gentle = getattr(_throttle, "on", False)
    for count, frame in enumerate(segment.frames):
        if gentle and count % THROTTLE_EVERY == 0:
            time.sleep(THROTTLE_S)
        pose_map = pose_from_frame(frame)
        now = BASE_T + float(frame.get("t", 0.0))
        with kernel._lock:
            kernel.width, kernel.height = int(frame.get("w") or 640), int(frame.get("h") or 480)
            # 手机录的帧另带认出时刻，速度照它算，和实时一样。
            kernel.pose_sample_at = BASE_T + float(frame["c"]) if "c" in frame else None
            kernel._process_pose_locked(pose_map or None, now)
            row = {"t": now, "step": int(frame.get("step", -1)),
                   "inside": {zone: bool(kernel.zone_state[zone].get("raw_inside")) for zone in ZONES},
                   "pressed": {zone: bool(kernel.zone_state[zone]["pressed"]) for zone in ZONES}}
            if on_frame is not None:
                on_frame(kernel, row)
        rows.append(row)
    return rows


def _entries(rows: list[dict], zone: str, labelled_only: bool = True) -> list[tuple[int, int]]:
    """这一段里进这个框的每一次：(进框那一帧, 出框那一帧)。只算这一项正式在做的那几帧。"""
    out, start, out_since = [], None, None
    for position, row in enumerate(rows):
        inside = row["inside"][zone] and (row["step"] >= 0 or not labelled_only)
        if inside and start is None:
            if out_since is None or row["t"] - out_since >= REENTRY_S or not out:
                start = position
            else:
                start, _ = out.pop()  # 抖了一下，还是上一次
        elif not inside and start is not None:
            out.append((start, position))
            start, out_since = None, row["t"]
    if start is not None:
        out.append((start, len(rows)))
    return out


def conflict_rates(kernel, segments: list[Segment]) -> dict[str, dict[str, dict]]:
    """每个录过的动作扫过每个框几次。录过但一次都没扫过的也在里面（空的），表示「录过，不冲突」。"""
    rates: dict[str, dict[str, dict]] = {}
    for segment in segments:
        if segment.kind != "action":
            continue
        trigger = segment.step.get("trigger")
        rows = replay_segment(kernel, segment)
        reps = max(1, int(segment.step.get("count") or 0))
        rates[trigger] = {}
        for zone in ZONES:
            hits = min(reps, len(_entries(rows, zone)))
            if hits:
                rates[trigger][zone] = {"hits": hits, "reps": reps}
    return rates


def collect_snippets(kernel, segments: list[Segment]) -> SnippetBank:
    """每次进框之后 SNIPPET_S 里每一帧的样子，按这一项在做什么标上故意或扫过。"""
    bank = SnippetBank()
    for segment in segments:
        if segment.kind in {"press", "tap"}:
            labels = {segment.step.get("zone"): ("intent", "")}
        elif segment.kind == "action":
            labels = {zone: ("sweep", segment.step.get("trigger")) for zone in ZONES}
        elif segment.kind == "idle":
            labels = {zone: ("sweep", "idle") for zone in ZONES}
        else:
            continue
        open_snippets: dict[str, tuple[float, Snippet]] = {}

        def on_frame(k, row, labels=labels, open_snippets=open_snippets, segment=segment):
            for zone, (label, tag) in labels.items():
                if zone not in ZONE_LIMBS:
                    continue
                inside = row["inside"][zone] and row["step"] >= 0
                if inside and zone not in open_snippets:
                    snippet = Snippet(zone, label, tag, segment.source)
                    open_snippets[zone] = (row["t"], snippet)
                if not inside:
                    begun = open_snippets.pop(zone, None)
                    if begun and begun[1].frames:
                        bank.add(begun[1])
                    continue
                entered, snippet = open_snippets[zone]
                elapsed = row["t"] - entered
                if elapsed <= SNIPPET_S:
                    snippet.frames.append((round(elapsed, 4), k.zone_kin.features(zone)))

        replay_segment(kernel, segment, on_frame)
        for _, snippet in open_snippets.values():
            bank.add(snippet)
    return bank


# ---------- 体检 ----------

def _percentile(values: list[float], share: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = min(len(ordered) - 1, max(0, math.ceil(share * len(ordered)) - 1))
    return ordered[position]


def health_report(kernel, segments: list[Segment], bank: SnippetBank) -> dict:
    """按现在的绑定、用智能判定重放一遍：误按几次、按出来几次、慢多少。

    只数这个游戏里绑了键的框和动作：没绑键的框按不出东西，没绑键的动作在这个游戏
    里人也不会去做。
    """
    with kernel._lock:
        bound_zones = {zone for zone in ZONES if kernel._effective_binding_locked(f"zone.{zone}")}
        bound_actions = set(kernel._bound_action_triggers_locked())
        with_motion = {zone for zone in ZONES
                       if kernel._zone_with_motion_locked(zone, kernel._effective_binding_locked(f"zone.{zone}"))}
    actions, zones, idle = [], [], {}
    latencies: list[float] = []
    attempts_total = missed_total = reps_total = misfired_reps = 0
    for segment in segments:
        bank.exclude = segment.source
        rows = replay_segment(kernel, segment)
        presses = {zone: _press_starts(rows, zone) for zone in ZONES}
        if segment.kind == "idle":
            idle = {zone: len(presses[zone]) for zone in bound_zones if presses[zone]}
        elif segment.kind == "action":
            trigger = segment.step.get("trigger")
            if trigger not in bound_actions:
                continue
            counted = sorted(bound_zones - with_motion)
            misfires = {zone: len(presses[zone]) for zone in counted if presses[zone]}
            reps = max(1, int(segment.step.get("count") or 0))
            reps_total += reps
            misfired_reps += min(reps, sum(misfires.values()))
            actions.append({"trigger": trigger, "name": segment.step.get("name", trigger),
                            "reps": reps, "misfires": misfires})
        elif segment.kind in {"press", "tap"}:
            zone = segment.step.get("zone")
            if zone not in bound_zones:
                continue
            hit, missed, delays = 0, 0, []
            for start, end in _entries(rows, zone):
                pressed = next((i for i in range(start, end) if rows[i]["pressed"][zone]), None)
                if pressed is None:
                    missed += 1
                else:
                    hit += 1
                    delays.append((rows[pressed]["t"] - rows[start]["t"]) * 1000.0)
            attempts_total += hit + missed
            missed_total += missed
            latencies += delays
            zones.append({"zone": zone, "kind": segment.kind, "attempts": hit + missed, "pressed": hit,
                          "missed": missed, "p50_ms": _round(_percentile(delays, .5)),
                          "p95_ms": _round(_percentile(delays, .95))})
    bank.exclude = None
    return {
        "actions": actions, "zones": zones, "idle": idle,
        "summary": {
            "misfire_rate": round(misfired_reps / reps_total, 3) if reps_total else None,
            "miss_rate": round(missed_total / attempts_total, 3) if attempts_total else None,
            "p50_ms": _round(_percentile(latencies, .5)), "p95_ms": _round(_percentile(latencies, .95)),
            "action_reps": reps_total, "press_attempts": attempts_total,
        },
    }


def _round(value):
    return None if value is None else round(value)


def _press_starts(rows: list[dict], zone: str) -> list[int]:
    starts, was = [], False
    for position, row in enumerate(rows):
        pressed = row["pressed"][zone]
        if pressed and not was and row["step"] >= 0:
            starts.append(position)
        was = pressed
    return starts


def analyse(snapshot: dict, store: IntentRecordingStore, *, report: bool = True, mode: str = "smart") -> dict:
    """全部算一遍。每一样用一个新的临时内核，免得上一样留下的状态带进下一样。

    体检默认按「智能」算（界面上要回答的是「智能判定对你的动作好不好使」），评测工具
    可以换成 simple 拿来对比。
    """
    segments = load_segments(store)
    if not segments:
        return {"rates": {}, "bank": SnippetBank(), "report": None, "segments": 0}
    kernel = make_replay_kernel(snapshot)
    try:
        rates = conflict_rates(kernel, segments)
    finally:
        kernel.close()
    kernel = make_replay_kernel(snapshot)
    try:
        bank = collect_snippets(kernel, segments)
    finally:
        kernel.close()
    result = {"rates": rates, "bank": bank, "report": None, "segments": len(segments)}
    if report:
        kernel = make_replay_kernel({**snapshot, "zone_trigger_mode": mode})
        try:
            kernel.configure_zone_learning(rates, bank)
            result["report"] = health_report(kernel, segments, bank)
        finally:
            kernel.close()
    return result


# ---------- 后台：设置变了就重算 ----------

class ZoneLearner:
    """后台线程：录的东西、框、绑定有变化就重新算一遍，装进实时内核。

    不去每个改设置的地方挂钩子，而是隔一会儿比一下「签名」（见
    ControlKernel.learning_signature）：漏掉一个钩子，框改了冲突却还是旧的，这种错最
    难查。正在录的时候不算，录完再算。
    """

    POLL_S = 2.0

    def __init__(self, kernel) -> None:
        self.kernel = kernel
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._signature = None
        self._thread = threading.Thread(target=self._run, name="zone-learner", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def poke(self) -> None:
        """录完了：别等下一轮，马上算。"""
        self._wake.set()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()

    def run_once(self, force: bool = False) -> bool:
        snapshot = self.kernel.replay_snapshot()
        if snapshot is None:
            return False
        signature = learning_signature(snapshot, self.kernel.intent_store.index())
        if signature == self._signature and not force:
            return False
        started = time.monotonic()
        self.kernel.set_zone_learning_state("computing")
        _throttle.on = threading.current_thread() is self._thread
        try:
            result = analyse(snapshot, self.kernel.intent_store)
        except Exception as exc:  # noqa: BLE001 - 算不出来就用原来的，不影响玩
            self.kernel.set_zone_learning_state("error", error=str(exc))
            self._signature = signature
            return False
        finally:
            _throttle.on = False
        self._signature = signature
        self.kernel.configure_zone_learning(result["rates"], result["bank"])
        self.kernel.set_zone_learning_state("ready", report=result["report"],
                                            took_s=round(time.monotonic() - started, 2))
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self.POLL_S)
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - 后台线程不能因为一次异常就停掉
                pass


def learning_signature(snapshot: dict, index: dict) -> str:
    payload = {key: snapshot.get(key) for key in (
        "control_bindings", "motion_config", "zone_fit", "zones_frozen", "frozen_rects",
        "frozen_anchor", "zone_trigger_mode", "hand_mouse", "action_chain")}
    payload["pose_actions"] = sorted((doc.get("id"), doc.get("revision")) for doc in snapshot.get("pose_actions") or [])
    payload["custom_poses"] = snapshot.get("custom_poses")
    payload["index"] = index
    return hashlib.sha1(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()
