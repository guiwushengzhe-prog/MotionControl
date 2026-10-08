"""运动累计只用临时目录、合成骨架和可控制时钟验证。"""

from datetime import datetime, timezone

import pytest

from motioncontrol.fitness import FitnessStore


class Clock:
    def __init__(self):
        self.now = 100.0
        self.wall_at = datetime(2026, 10, 8, 12, tzinfo=timezone.utc).timestamp()

    def monotonic(self):
        return self.now

    def wall(self):
        return self.wall_at

    def advance(self, seconds):
        self.now += seconds
        self.wall_at += seconds

    def date(self):
        return datetime.fromtimestamp(self.wall_at).astimezone().date().isoformat()


def pose():
    return {name: {"x": x, "y": y, "score": .99} for name, x, y in (
        ("left_shoulder", .3, .4), ("right_shoulder", .7, .4),
        ("left_wrist", .25, .65), ("right_wrist", .75, .65),
        ("left_knee", .4, .8), ("right_knee", .6, .8),
        ("left_ankle", .4, .95), ("right_ankle", .6, .95))}


@pytest.fixture
def factory(tmp_path):
    stores = []

    def make(clock, name="fitness.json"):
        store = FitnessStore(tmp_path / name, clock=clock.monotonic, wall=clock.wall, background=False)
        stores.append(store)
        return store

    yield make
    for store in stores:
        store.close()


def march_for(store, clock, seconds):
    store.observe_pose(pose(), clock.now, actions=("motion.march",))
    for _ in range(round(seconds * 2)):
        clock.advance(.5)
        store.observe_pose(pose(), clock.now, actions=("motion.march",))


def test_pause_resume_lost_pose_and_stream_gap_do_not_backfill(factory):
    clock = Clock()
    store = factory(clock)
    store.control({"action": "start"})
    march_for(store, clock, .5)
    store.control({"action": "pause"})
    clock.advance(20)
    store.observe_pose(pose(), clock.now, actions=("motion.march",))
    assert store.state()["active_seconds"] == pytest.approx(.5)
    store.control({"action": "resume"})
    march_for(store, clock, .5)
    clock.advance(10)
    march_for(store, clock, .5)
    store.observe_pose(None, clock.now)
    clock.advance(.25)
    march_for(store, clock, .5)
    state = store.state()
    assert state["active_seconds"] == pytest.approx(2)
    assert state["elapsed_seconds"] == pytest.approx(2)


def test_duplicate_backwards_steps_and_steps_while_paused_are_ignored(factory):
    clock = Clock()
    store = factory(clock)
    store.control({"action": "start"})
    for when in (clock.now, clock.now, clock.now - 1, clock.now + .05):
        store.step_event("left", when)
    store.step_event("right", clock.now)
    store.step_event("left", clock.now + .3)
    store.step_event("unknown", clock.now + 1)
    assert store.state()["steps"] == 3
    store.control({"action": "pause"})
    clock.advance(1)
    store.step_event("right", clock.now)
    assert store.state()["steps"] == 3
    store.control({"action": "resume"})
    store.step_event("right", clock.now)
    assert store.state()["steps"] == 4
    assert store.state()["today"]["steps"] == 4


def test_kernel_disconnect_clears_statistics_baseline_before_fast_reconnect(factory):
    from motioncontrol.control_kernel import ControlKernel
    from test_control_kernel import FakeOutput

    clock = Clock()
    store = factory(clock)
    store.control({"action": "start"})
    store.observe_pose(pose(), clock.now, actions=("motion.march",))
    clock.advance(.2)
    store.observe_pose(pose(), clock.now, actions=("motion.march",))
    kernel = ControlKernel(FakeOutput())
    try:
        kernel.fitness = store
        kernel.active_body_source = "mobile_pose:test"
        kernel.clear_source("mobile_pose:test")
        clock.advance(.2)
        store.observe_pose(pose(), clock.now, actions=("motion.march",))
        assert store.state()["active_seconds"] == pytest.approx(.2)
    finally:
        kernel.close()


def test_held_action_counts_transitions_instead_of_every_pose_frame(factory):
    clock = Clock()
    store = factory(clock)
    store.control({"action": "start"})
    store.observe_pose(pose(), clock.now)
    for _ in range(5):
        clock.advance(.1)
        store.observe_pose(pose(), clock.now, actions=("pose.arms_up",))
    assert store.state()["action_count"] == 1
    clock.advance(.1)
    store.observe_pose(pose(), clock.now)
    clock.advance(.1)
    store.observe_pose(pose(), clock.now, actions=("pose.arms_up",))
    assert store.state()["action_count"] == 2
    assert store.state()["today"]["action_count"] == 2


def test_restart_preserves_history_and_settings_and_pauses_unfinished_session(factory):
    clock = Clock()
    store = factory(clock)
    store.control({"action": "profile", "profile": {"weight_kg": 82, "goal_steps": 25}})
    store.control({"action": "start"})
    march_for(store, clock, 2)
    store.step_event("left", clock.now)
    before = store.state()
    store.close()
    clock.advance(3600)
    restored = factory(clock)
    state = restored.state()
    assert state["session_id"] == before["session_id"]
    assert state["status"] == "paused"
    assert state["active_seconds"] == pytest.approx(before["active_seconds"])
    assert state["estimated_kcal"] == pytest.approx(before["estimated_kcal"])
    assert state["profile"]["weight_kg"] == 82
    assert state["profile"]["goal_steps"] == 25
    restored.observe_pose(pose(), clock.now, actions=("motion.march",))
    assert restored.state()["active_seconds"] == pytest.approx(2)
    restored.control({"action": "resume"})
    march_for(restored, clock, .5)
    assert restored.state()["active_seconds"] == pytest.approx(2.5)
    history = restored.history()
    history[0]["steps"] = 999
    assert restored.state()["steps"] == 1


def test_estimated_activity_kcal_scales_with_weight_and_stationary_pose_adds_none(factory):
    values = []
    for weight in (50, 100):
        clock = Clock()
        store = factory(clock, f"weight-{weight}.json")
        store.control({"action": "profile", "profile": {"weight_kg": weight}})
        store.control({"action": "start"})
        store.observe_pose(pose(), clock.now)
        clock.advance(.5)
        store.observe_pose(pose(), clock.now)
        assert store.state()["estimated_kcal"] == 0
        march_for(store, clock, 30)
        state = store.state()
        assert state["source"] == "motion_estimate"
        assert state["active_seconds"] == pytest.approx(30)
        values.append(state["estimated_kcal"])
    assert values[0] == pytest.approx((3.8 - 1) * 3.5 * 50 / 200 * .5)
    assert values[1] == pytest.approx(2 * values[0])


def test_daily_goals_checkins_and_levels_aggregate_across_sessions(factory):
    clock = Clock()
    store = factory(clock)
    store.control({"action": "profile", "profile": {"goal_active_minutes": 1, "primary_goal": "minutes"}})
    first_day = clock.date()
    for _ in range(2):
        store.control({"action": "start"})
        march_for(store, clock, 30)
        store.control({"action": "finish"})
    state = store.state()
    assert len(store.history()) == 2
    assert state["today"]["active_seconds"] == pytest.approx(60)
    assert state["checkins"] == [first_day]
    assert state["experience"] == 60
    assert state["level"] == 1
    clock.advance(86400)
    second_day = clock.date()
    store.control({"action": "start"})
    march_for(store, clock, 60)
    state = store.state()
    assert state["checkins"] == [first_day, second_day]
    assert state["streak"] == 2
    assert state["experience"] == 120
    assert state["level"] == 2
    assert state["today"]["active_seconds"] == pytest.approx(60)
    assert state["daily"][first_day]["active_seconds"] == pytest.approx(60)


def test_raising_daily_goal_preserves_earned_checkins_and_levels_after_restart(factory):
    clock = Clock()
    store = factory(clock)
    store.control({"action": "profile", "profile": {"goal_active_minutes": 1}})
    dates = []
    for day in range(2):
        if day:
            clock.advance(86400)
        dates.append(clock.date())
        store.control({"action": "start"})
        march_for(store, clock, 60)
        store.control({"action": "finish"})
    earned = store.state()
    assert earned["checkins"] == dates
    assert earned["level"] == 2
    store.control({"action": "profile", "profile": {"goal_active_minutes": 30}})
    assert store.state()["checkins"] == earned["checkins"]
    assert store.state()["experience"] == earned["experience"]
    store.close()
    restored = factory(clock)
    state = restored.state()
    assert state["profile"]["goal_active_minutes"] == 30
    assert state["checkins"] == dates
    assert state["level"] == earned["level"]
    assert state["experience"] == earned["experience"]
