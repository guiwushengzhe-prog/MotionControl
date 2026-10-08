"""运动记录同步的规则：电脑和云端用同一份（motioncontrol_shared.fitness_schema）。"""

from __future__ import annotations

import pytest

from motioncontrol_shared.fitness_schema import merge_session, normalize_profile, normalize_session


def session(**override):
    doc = {"session_id": "s1", "status": "active", "started_at_ms": 1_000, "updated_at_ms": 2_000,
           "elapsed_seconds": 60, "active_seconds": 50, "steps": 10, "action_count": 3, "estimated_kcal": 4.0,
           "days": {"2026-10-08": {"active_seconds": 50, "steps": 10, "action_count": 3, "estimated_kcal": 4.0}}}
    doc.update(override)
    return normalize_session(doc)


def test_only_the_summary_travels():
    doc = session(hr_avg=120, hr_curve=[[0, 118]], hr_samples=55, hr_bucket_n=3, origin="local", source="x")
    assert {"hr_samples", "hr_bucket_n", "origin", "source"}.isdisjoint(doc)
    assert doc["hr_max"] == 120 and doc["kcal_source"] == "motion"


@pytest.mark.parametrize("change", [
    {"session_id": "../x"}, {"status": "running"}, {"steps": -1}, {"steps": 1.5}, {"hr_avg": 300},
    {"hr_avg": 100, "hr_curve": [[15, 100]]}, {"days": {"yesterday": {}}}, {"updated_at_ms": True},
])
def test_bad_summaries_are_refused(change):
    with pytest.raises(ValueError):
        session(**change)


def test_merging_is_order_independent_and_finished_is_final():
    early = session(steps=4, updated_at_ms=1_500)
    late = session(status="finished", ended_at_ms=3_000, updated_at_ms=3_000, steps=15,
                   hr_avg=121, hr_max=150, hr_curve=[[0, 118], [30, 124]], kcal_source="heart_rate")
    one, two = merge_session(early, late), merge_session(late, early)
    assert one == two
    assert one["status"] == "finished" and one["steps"] == 15 and one["hr_avg"] == 121
    # 结束之后又迟到一份"进行中"、而且更新时间更晚：还是结束，心率摘要也不丢。
    zombie = session(updated_at_ms=9_000, steps=1)
    merged = merge_session(one, zombie)
    assert merged["status"] == "finished" and merged["steps"] == 15 and merged["hr_avg"] == 121


def test_days_take_the_larger_count_each():
    a = session(days={"2026-10-08": {"active_seconds": 50, "steps": 10, "action_count": 3, "estimated_kcal": 4},
                      "2026-10-09": {"active_seconds": 5, "steps": 1, "action_count": 0, "estimated_kcal": 0.5}})
    b = session(updated_at_ms=2_500, days={"2026-10-08": {"active_seconds": 40, "steps": 12, "action_count": 3,
                                                           "estimated_kcal": 3}})
    merged = merge_session(a, b)
    assert merged["days"]["2026-10-08"]["steps"] == 12 and merged["days"]["2026-10-08"]["active_seconds"] == 50
    assert "2026-10-09" in merged["days"]


def test_profile_age_and_sex_are_optional():
    base = {"weight_kg": 70, "goal_active_minutes": 20, "goal_steps": 2000, "goal_kcal": 100,
            "primary_goal": "minutes", "updated_at_ms": 1}
    assert normalize_profile(base)["age"] is None
    assert normalize_profile({**base, "age": 40, "sex": "male"})["sex"] == "male"
    with pytest.raises(ValueError):
        normalize_profile({**base, "sex": "other"})
