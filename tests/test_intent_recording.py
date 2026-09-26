"""「录我的动作」：照提示一项一项录，存盘，再对着现在的框回放算冲突、比对和体检。"""

from __future__ import annotations

import json
import time

import pytest

from motioncontrol.control_kernel import ControlKernel
from motioncontrol.intent_library import analyse, learning_signature, ZoneLearner
from motioncontrol.intent_recording import (
    ACTION_COUNT, IDLE_S, PRESS_COUNT, READY_S, SETTLE_S, STEP_TIMEOUT_S, IntentRecordingSession,
    IntentRecordingStore, build_steps, pose_from_frame,
)
from test_minimal_controls import KernelOutput, _standing_pose, _zone_feeder
from test_zone_smart import into, standing, with_elbows

FPS = 30
HANDS_UP = [{"id": "hands_up", "enabled": True, "type": "gamepad", "target": "Y"}]
RAISE = [((.36, .60), (.64, .60), (.40, .52), (.60, .52)),
         ((.28, .46), (.72, .46), (.35, .45), (.65, .45)),
         ((.26, .34), (.74, .34), (.34, .38), (.66, .38)),
         ((.32, .24), (.68, .24), (.36, .31), (.64, .31)),
         ((.42, .18), (.58, .18), (.39, .30), (.61, .30))]


def raise_pose(lw, rw, le, re):
    return with_elbows(_standing_pose(left_wrist=lw, right_wrist=rw), left=le, right=re)


# ---------- 一轮录制怎么往下走 ----------

def run(session, frames, *, zones=None, triggers=None, start=0.0):
    t = start
    for _ in range(frames):
        t += 1 / FPS
        session.update(t, _standing_pose(), 640, 480, zones or {}, triggers or set())
    return t


def test_steps_cover_idle_every_zone_twice_and_every_action():
    steps = build_steps([("motion.march", "原地踏步"), ("motion.hands_up", "双手举过头")])
    keys = [step["key"] for step in steps]
    assert keys[0] == "idle"
    assert "press:leftHand" in keys and "tap:rightFoot" in keys
    assert keys[-2:] == ["action:motion.march", "action:motion.hands_up"]
    assert "duration_s" in steps[-2], "踏步没有「一下」，按时间录"
    assert [s["key"] for s in build_steps([], ["tap:leftHand"])] == ["tap:leftHand"]


def test_a_press_step_counts_entries_and_moves_on():
    session = IntentRecordingSession(build_steps([], ["press:leftHand"]), 0.0, prepare_s=0)
    t = run(session, int(READY_S * FPS) + 1)
    for _ in range(PRESS_COUNT):
        t = run(session, 3, zones={"leftHand": True}, start=t)
        t = run(session, 6, zones={"leftHand": False}, start=t)
    assert session.steps[0]["count"] == PRESS_COUNT
    run(session, int(SETTLE_S * FPS) + 2, start=t)
    assert session.state == "done"
    labelled = [frame for frame in session.frames if frame["step"] == 0]
    assert labelled and all(frame["pose"] for frame in labelled)


def test_the_ready_moment_is_not_counted_or_labelled():
    session = IntentRecordingSession(build_steps([], ["press:leftHand"]), 0.0, prepare_s=0)
    run(session, 5, zones={"leftHand": True})
    assert session.steps[0]["count"] == 0
    assert all(frame["step"] == -1 for frame in session.frames)


def test_idle_is_timed_and_an_action_counts_recognitions():
    session = IntentRecordingSession(build_steps([("motion.squat", "下蹲")], ["idle", "action:motion.squat"]),
                                     0.0, prepare_s=0)
    t = run(session, int((READY_S + IDLE_S + SETTLE_S) * FPS) + 3)
    assert session.index == 1
    t = run(session, int(READY_S * FPS) + 1, start=t)
    for _ in range(ACTION_COUNT):
        t = run(session, 5, triggers={"motion.squat"}, start=t)
        t = run(session, 12, start=t)
    assert session.steps[1]["count"] == ACTION_COUNT


def test_a_step_that_never_happens_times_out_and_skip_drops_it():
    session = IntentRecordingSession(build_steps([], ["press:leftFoot", "press:rightFoot"]), 0.0, prepare_s=0)
    t = run(session, int((READY_S + STEP_TIMEOUT_S) * FPS) + 3)
    assert session.index == 1 and session.steps[0]["count"] == 0
    session.skip(t)
    assert session.state == "done" and session.steps[1]["skipped"]
    assert [step["key"] for step in session.recorded_steps()] == ["press:leftFoot"]


# ---------- 存盘 ----------

def finished_session():
    session = IntentRecordingSession(build_steps([], ["press:leftHand"]), 0.0, prepare_s=0)
    t = run(session, int(READY_S * FPS) + 1)
    for _ in range(PRESS_COUNT):
        t = run(session, 3, zones={"leftHand": True}, start=t)
        t = run(session, 6, start=t)
    run(session, int(SETTLE_S * FPS) + 2, start=t)
    assert session.state == "done"
    return session


def test_store_keeps_only_the_latest_take_of_each_item(tmp_path):
    store = IntentRecordingStore(tmp_path)
    first = store.save(finished_session())
    second = store.save(finished_session())
    assert store.recorded_keys() == {"press:leftHand"}
    assert not first.exists() and second.exists(), "旧的那一遍没人指着，删掉"
    header, frames = store.load(second.name)
    assert header["steps"][0]["key"] == "press:leftHand"
    assert pose_from_frame(frames[0])["nose"]["score"] == pytest.approx(.95)
    store.forget(["press:leftHand"])
    assert store.recorded_keys() == set() and not second.exists()


# ---------- 在内核里录，再回放 ----------

@pytest.fixture()
def kernel():
    made = ControlKernel(KernelOutput())
    yield made
    made.close()


def wait_saved(kernel):
    deadline = time.time() + 5
    while kernel.status()["intent_recording"]["saving"] and time.time() < deadline:
        time.sleep(.02)


def record_press_and_raise(kernel, monkeypatch):
    kernel.configure_motions(HANDS_UP)
    feed = _zone_feeder(kernel, monkeypatch)
    feed(standing(), 10)
    kernel.start_intent_recording(["press:leftHand", "action:motion.hands_up"])
    status = kernel.status()["intent_recording"]
    assert status["active"] and status["state"] == "preparing"
    feed(standing(), int((3.0 + READY_S) * FPS) + 3)
    # 故意按：一只手伸进去停一下，收回来。
    for _ in range(PRESS_COUNT):
        feed(into(kernel, standing(), "leftHand", .12), 6)
        feed(standing(), 8)
    feed(standing(), int((SETTLE_S + READY_S) * FPS) + 3)
    assert kernel.status()["intent_recording"]["index"] == 1
    # 双手举过头，五遍。
    for _ in range(ACTION_COUNT):
        for lw, rw, le, re in RAISE:
            feed(raise_pose(lw, rw, le, re), 3)
        feed(raise_pose(*RAISE[-1]), 8)
        for lw, rw, le, re in reversed(RAISE):
            feed(raise_pose(lw, rw, le, re), 2)
        feed(standing(), 10)
    feed(standing(), int(SETTLE_S * FPS) + 3)
    wait_saved(kernel)
    return feed


def test_recording_in_the_kernel_saves_and_learns(kernel, monkeypatch):
    record_press_and_raise(kernel, monkeypatch)
    status = kernel.status()
    assert status["intent_recording"]["state"] == "done"
    steps = status["intent_recording"]["steps"]
    assert steps[0]["count"] == PRESS_COUNT and steps[1]["count"] == ACTION_COUNT
    assert {"press:leftHand", "action:motion.hands_up"} <= set(status["intent_items"]["recorded"])
    assert "action:motion.hands_up" not in status["intent_items"]["missing"]

    result = analyse(kernel.replay_snapshot(), kernel.intent_store)
    assert result["rates"]["motion.hands_up"]["leftHand"]["hits"] >= 4, "举双手每遍都扫过左手框"
    assert result["bank"].counts("leftHand", ("motion.hands_up",)) >= (PRESS_COUNT, ACTION_COUNT)
    report = result["report"]
    press = next(item for item in report["zones"] if item["zone"] == "leftHand")
    assert press["attempts"] == PRESS_COUNT and press["missed"] == 0
    raise_row = next(item for item in report["actions"] if item["trigger"] == "motion.hands_up")
    assert raise_row["reps"] == ACTION_COUNT
    assert report["summary"]["misfire_rate"] == 0.0, f"举双手误按了：{raise_row}"


def test_the_learner_installs_what_it_learned(kernel, monkeypatch):
    record_press_and_raise(kernel, monkeypatch)
    learner = ZoneLearner(kernel)
    assert learner.run_once()
    assert not learner.run_once(), "什么都没变就不重算"
    status = kernel.status()
    assert status["zone_learning"]["state"] == "ready" and status["zone_learning"]["report"]
    assert status["zone_overlaps"]["leftHand"]["rates"]["motion.hands_up"]["source"] == "recorded"
    kernel.configure_zone_trigger_mode("simple")
    assert learner.run_once(), "换了判定方式，体检要重算"


def test_nothing_is_replayed_while_recording(kernel):
    kernel.start_intent_recording(["idle"])
    assert kernel.replay_snapshot() is None
    kernel.cancel_intent_recording()
    snapshot = kernel.replay_snapshot()
    assert snapshot is not None
    assert learning_signature(snapshot, {}) == learning_signature(kernel.replay_snapshot(), {})


def test_replay_never_touches_the_players_settings(kernel, tmp_path):
    from motioncontrol.user_paths import user_path
    before = user_path("general_settings").read_text(encoding="utf-8") if user_path("general_settings").exists() else None
    analyse(kernel.replay_snapshot(), kernel.intent_store)
    after = user_path("general_settings").read_text(encoding="utf-8") if user_path("general_settings").exists() else None
    assert before == after


# ---------- 评测工具 ----------

def test_eval_tool_reports_both_modes(kernel, monkeypatch, capsys):
    import importlib.util
    from pathlib import Path

    record_press_and_raise(kernel, monkeypatch)
    spec = importlib.util.spec_from_file_location(
        "eval_zone_arbiter", Path(__file__).resolve().parent.parent / "tools" / "eval_zone_arbiter.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    monkeypatch.setattr("sys.argv", ["eval", "--repo-poses", "--json"])
    assert tool.main() == 0
    out = json.loads(capsys.readouterr().out)
    smart, simple = out["smart"]["report"]["summary"], out["simple"]["report"]["summary"]
    assert smart["action_reps"] == ACTION_COUNT and smart["press_attempts"] == PRESS_COUNT
    assert smart["misfire_rate"] == 0.0, "智能判定下举双手不误按"
    assert simple["misfire_rate"] > 0.0, "进去就按下举双手会扫过左右手框按下去"
    assert simple["p50_ms"] == 0
