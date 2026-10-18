"""已录角度的回归：回正可解除，小幅漂移及连续穿越不能重新触发。"""
import json
from pathlib import Path

import pytest

from motioncontrol.head_control import HeadController, _RelativeYawAxisV153
from test_hfhpe_head_control import PitchEstimator


CASES = json.loads((Path(__file__).parent / "fixtures" / "hfhpe_return_20261018.json").read_text(encoding="utf-8"))["cases"]


def replay(case, *, legacy=False, only_relax_idle=False):
    controller = HeadController(None)
    controller.estimator = PitchEstimator()
    controller.configure(horizontal_algorithm="hfhpe", deadzone=.25, sensitivity_x=200.)
    controller._hfhpe_calibration = {"version": 1, "center_yaw_deg": 0.,
                                     "noise_yaw_deg": .16585, "sample_count": 34}
    if legacy:
        controller._hfhpe_intent = _RelativeYawAxisV153()
    elif only_relax_idle:
        controller._hfhpe_intent.return_recovery_step = 0.
    rows = []
    for t, yaw in case["frames"]:
        x, _ = controller.update({"pitch": 0.}, 640, 360, 100 + t,
                                 head_pose={"source": "hfhpe", "valid": True,
                                            "yaw_deg": yaw, "pitch_deg": 0., "roll_deg": 0.})
        rows.append((t, yaw, x, controller.yaw_intent_state))
    return rows


@pytest.mark.parametrize(("case_index", "window", "direction"), [(0, (7.2, 8.0), 1), (1, (5.25, 6.2), -1)])
def test_recorded_new_displacement_can_follow_a_settled_return(case_index, window, direction):
    # 窗口来自模型实际角度变化，只验证状态机缺陷，不作人体动作正确率标签。
    old, current = replay(CASES[case_index], legacy=True), replay(CASES[case_index])
    assert all(abs(x) < .01 for t, yaw, x, state in old if window[0] <= t <= window[1])
    assert any(direction * x > .1 for t, yaw, x, state in current if window[0] <= t <= window[1])


def test_relaxing_return_alone_must_not_rearm_small_same_side_drift():
    relaxed = replay(CASES[1], only_relax_idle=True)
    current = replay(CASES[1])
    assert any(abs(x) > .01 for t, yaw, x, state in relaxed if 7.35 <= t <= 7.9)
    assert all(abs(x) < .01 for t, yaw, x, state in current if 7.35 <= t <= 7.9)
    assert any(x > .1 for t, yaw, x, state in current if 8.2 <= t <= 8.85)


@pytest.mark.parametrize("case", CASES)
def test_recorded_replay_retains_neutral_and_direction_constraints(case):
    rows = replay(case)
    for t, yaw, x, state in rows:
        if abs(yaw) <= 4.5:
            assert x == 0.
        if abs(x) > .01:
            assert x * yaw > 0.


def test_legacy_controller_keeps_original_return_thresholds():
    controller = HeadController(None)
    axis = controller._x_intent_v153
    assert axis.return_idle_max_net == .023
    assert axis.return_idle_max_span == .040
    assert axis.return_recovery_step == 0.
