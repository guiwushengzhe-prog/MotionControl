"""简化模型方案的几何约定；不调用摄像头、真实模型或系统输出。"""

import json
from pathlib import Path

import pytest

from motioncontrol.head_control import HeadController
from test_hfhpe_head_control import PitchEstimator, frame, make_controller, step


def simple_controller(path=None, **config):
    controller = make_controller(path)
    controller.configure(horizontal_algorithm="hfhpe_simple", **config)
    return controller


@pytest.mark.parametrize("fps", [15, 30])
@pytest.mark.parametrize("sign", [-1, 1])
def test_outward_hold_return_and_same_side_restart_without_latch(fps, sign):
    c = simple_controller(deadzone=.25, sensitivity_x=120.)
    outward = [step(c, sign * 20 * i / fps, 20 + i / fps)[0] for i in range(fps + 1)]
    assert any(x * sign > .01 for x in outward)
    assert all(x * sign >= 0. for x in outward)
    # 已到达偏头姿态后，原始角不再外转，滤波尾巴也不能继续输出。
    assert step(c, sign * 20, 21 + 1 / fps) == (0., 0.)
    for i in range(1, fps + 1):
        assert step(c, sign * (20 - 10 * i / fps), 21 + (i + 1) / fps) == (0., 0.)
    assert not c.status()["yaw_return_latched"]
    # 只回到10度便再次外转，无需回到中心或停稳重新解锁。
    restarted = [step(c, sign * (10 + 10 * i / fps), 22 + (i + 1) / fps)[0]
                 for i in range(1, fps + 1)]
    assert any(x * sign > .01 for x in restarted)


@pytest.mark.parametrize("invert", [False, True])
def test_cross_center_frame_is_silent_and_next_outward_side_can_drive(invert):
    c = simple_controller(invert_x=invert)
    for i in range(31):
        step(c, i, 20 + i / 30)
    assert step(c, -35., 21 + 1 / 30) == (0., 0.)
    x, _ = step(c, -40., 21 + 2 / 30)
    assert x * (1 if invert else -1) > 0.
    assert not c.status()["yaw_return_latched"]


@pytest.mark.parametrize("failure", ["invalid", "gap"])
def test_reacquisition_and_mode_switch_do_not_reuse_motion(failure):
    c = simple_controller()
    for i in range(31):
        step(c, i, 20 + i / 30)
    if failure == "invalid":
        assert c.update({}, 640, 480, 21.1, head_pose=frame(valid=False)) == (0., 0.)
        next_at = 21.15
    else:
        next_at = 22.
    assert step(c, -35., next_at) == (0., 0.)
    assert step(c, -35., next_at + 1 / 30) == (0., 0.)
    c.configure(horizontal_algorithm="hfhpe")
    c.configure(horizontal_algorithm="hfhpe_simple")
    assert step(c, 40., next_at + 2 / 30) == (0., 0.)
    assert c.status()["calibrated"]


def test_curve_and_sensitivity_apply_only_while_outward():
    controllers = [simple_controller(horizontal_curve_gamma=gamma, sensitivity_x=80.) for gamma in (1., 3.)]
    outputs = [[], []]
    for i in range(31):
        for c, values in zip(controllers, outputs):
            values.append(step(c, i / 3, 20 + i / 30)[0])
    assert 0. < outputs[1][-1] < outputs[0][-1] <= .8
    for c in controllers:
        assert step(c, 9., 21 + 1 / 30) == (0., 0.)
        assert abs(c.status()["target_output_x"]) == 0.


def test_shared_model_calibration_survives_save_reload_and_legacy_switch(tmp_path):
    path = tmp_path / "head.json"
    c = simple_controller(path)
    saved = dict(c.profile_document()["hfhpe_calibration"])
    reloaded = HeadController(path)
    assert reloaded.config["horizontal_algorithm"] == "hfhpe_simple"
    assert reloaded.status()["calibrated"]
    for policy in ("hfhpe", "roll_tilt", "hfhpe_simple"):
        reloaded.configure(horizontal_algorithm=policy)
        assert reloaded.profile_document()["hfhpe_calibration"] == saved
    # 简化方案的重新校准仍写同一份模型中心，而非借旧几何估计中心。
    reloaded.estimator = PitchEstimator()
    reloaded.start_center(30.)
    for i in range(100):
        step(reloaded, 7., 30 + i / 30)
    assert reloaded.status()["center_yaw"] == pytest.approx(7.)
    reloaded.configure(horizontal_algorithm="hfhpe")
    assert reloaded.status()["center_yaw"] == pytest.approx(7.)


CASES = json.loads((Path(__file__).parent / "fixtures" / "hfhpe_return_20261018.json").read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", CASES)
def test_recorded_angles_have_no_inward_or_deadzone_output(case):
    c = simple_controller(deadzone=.25)
    previous = None
    active = 0
    for t, yaw in case["frames"]:
        x, _ = step(c, yaw, 100 + t)
        if abs(x) > 1e-12:
            active += 1
            assert x * yaw > 0.
            assert abs(yaw) > 4.5
            assert previous is not None and abs(yaw) > abs(previous)
        assert not c.status()["yaw_return_latched"]
        previous = yaw
    assert active > 0
