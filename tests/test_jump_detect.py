"""跳跃：肩和胯一起比站着时高出一截。

真人那部分用的是 fixtures/jump_torso_20260926.json：从 intent-20260926-195125 里截的
肩、胯中点的 y 和膝盖弯没弯，8 次跳跃加上踏步、小腿后抬、提膝碰肘、侧步开合、随便动。
合成的那部分证明逻辑：踏步只抬胯、下蹲站起来、凑近镜头、站上台阶都不算。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from motioncontrol.control_kernel import ControlKernel
from motioncontrol.jump_detect import MAX_AIR_S, RISE_START, JumpDetector
from test_minimal_controls import KernelOutput, _standing_pose, _zone_feeder

FIXTURE = Path(__file__).parent / "fixtures" / "jump_torso_20260926.json"
FPS = 30
SHOULDER, HIP = 0.40, 0.64   # 躯干 0.24


def run(detector, samples, start=0.0):
    """samples: [(肩 y, 胯 y, 蹲着没有)]，30 帧每秒。返回每一帧在不在空中。"""
    return [detector.update(sh, hip, start + index / FPS, crouched=bent)
            for index, (sh, hip, bent) in enumerate(samples)]


def onsets(flags):
    return sum(1 for i, on in enumerate(flags) if on and (i == 0 or not flags[i - 1]))


def stand(seconds, sh=SHOULDER, hip=HIP):
    return [(sh, hip, False)] * int(seconds * FPS)


def jump(height=0.08):
    """往下沉一点，0.15 秒升到最高，停一下，落回来。"""
    dip = [(SHOULDER + 0.02, HIP + 0.02, True)] * 4
    up = [(SHOULDER - height * k / 5, HIP - height * k / 5, False) for k in range(1, 6)]
    top = [(SHOULDER - height, HIP - height, False)] * 2
    down = list(reversed(up))
    return dip + up + top + down


# ---------- 真人录像 ----------

@pytest.mark.parametrize("segment", json.loads(FIXTURE.read_text(encoding="utf-8"))["segments"],
                         ids=lambda segment: segment["step"])
def test_real_recording_counts_every_jump_and_nothing_else(segment):
    detector = JumpDetector()
    flags = [detector.update(sh, hip, t, crouched=bool(bent)) for t, sh, hip, bent in segment["frames"]]
    assert onsets(flags) == segment["jumps"]


# ---------- 合成 ----------

def test_a_jump_is_one_press_while_in_the_air():
    flags = run(JumpDetector(), stand(1) + jump() + stand(1))
    assert onsets(flags) == 1
    assert 3 <= sum(flags) <= 10, "只在空中那一小段按着（落回 RISE_END 以下才松）"


def test_nothing_before_the_player_has_stood_still_once():
    """一上来就在跳：还没有站着的基准，不知道高出多少。"""
    detector = JumpDetector()
    assert not any(run(detector, jump() + jump()))


def test_a_small_rise_is_not_a_jump():
    """踮脚、抬下巴：比站着高出一点点。"""
    assert not any(run(JumpDetector(), stand(1) + jump(height=0.6 * RISE_START * (HIP - SHOULDER)) + stand(1)))


def test_marching_lifts_the_hip_but_not_the_shoulders():
    lifted = [(SHOULDER, HIP - 0.06, False)] * 8
    assert not any(run(JumpDetector(), stand(1) + (lifted + stand(0.3)) * 4))


def test_standing_up_from_a_long_squat_is_not_a_jump():
    """蹲着的时候基准不往下跟，站起来只是回到原来的高度。"""
    squat = [(SHOULDER + 0.10, HIP + 0.10, True)] * (5 * FPS)
    rise = [(SHOULDER + 0.10 - 0.02 * k, HIP + 0.10 - 0.02 * k, False) for k in range(1, 6)]
    assert not any(run(JumpDetector(), stand(1) + squat + rise + stand(1)))


def test_leaning_towards_the_camera_is_not_a_jump():
    """凑近镜头：身体变大，肩往上、胯往下。"""
    lean = [(SHOULDER - 0.01 * k, HIP + 0.01 * k, False) for k in range(1, 10)]
    assert not any(run(JumpDetector(), stand(1) + lean + [lean[-1]] * 30))


def test_staying_up_is_a_new_stance_not_a_long_jump():
    """站上台阶、手机被碰歪：起来了就不下来，过一会儿认下现在的高度，不一直按着。"""
    detector = JumpDetector()
    high = [(SHOULDER - 0.08, HIP - 0.08, False)] * int((MAX_AIR_S + 0.5) * FPS)
    first = stand(1) + high
    flags = run(detector, first)
    assert flags[-1] is False
    assert onsets(flags) == 1
    # 认下以后，从新高度再跳一次照样认得出。
    later = [(sh - 0.08, hip - 0.08, bent) for sh, hip, bent in jump()]
    assert onsets(run(detector, later + high[:30], start=len(first) / FPS)) == 1


def test_a_gap_in_the_stream_starts_over():
    detector = JumpDetector()
    run(detector, stand(1))
    assert detector.base is not None
    detector.update(SHOULDER, HIP, 5.0)
    assert detector.base is None


# ---------- 内核 ----------

def jumping_pose(dy):
    return _standing_pose(dy=dy)


def test_jump_is_a_built_in_motion_that_presses_its_key(monkeypatch):
    kernel = ControlKernel(KernelOutput())
    try:
        kernel.configure_bindings({"motions": {"jump": {"action": {"type": "gamepad", "target": "Y"}}}})
        feed = _zone_feeder(kernel, monkeypatch)
        feed(jumping_pose(0.0), 30)
        assert "jump" not in kernel.motion_active
        for dy in (0.01, 0.01, -0.03, -0.06, -0.08):
            feed(jumping_pose(dy))
        assert "jump" in kernel.motion_active
        assert kernel._effective_bindings_locked()["motion.jump"]["action"]["target"] == "Y"
        feed(jumping_pose(0.0), 10)
        assert "jump" not in kernel.motion_active
    finally:
        kernel.close()


def test_an_unbound_zone_is_neither_judged_nor_drawn(monkeypatch):
    kernel = ControlKernel(KernelOutput())
    try:
        kernel.configure_bindings({"zones": {"headJump": {"disabled": True}}})
        feed = _zone_feeder(kernel, monkeypatch)
        feed(jumping_pose(0.0), 40)
        for dy in (-0.03, -0.06, -0.09, -0.10, -0.10, -0.10):
            feed(jumping_pose(dy))
        with kernel._lock:
            zones = kernel.runtime_zones_locked()
        assert zones["headJump"]["rect"] is None
        assert zones["headJump"]["pressed"] is False and zones["headJump"]["recognized"] is False
        assert zones["leftHand"]["rect"] is not None, "绑了键的照常画"
        # 跳跃动作不看头顶区绑没绑，照样认。
        assert "jump" in kernel.motion_raw and kernel.motion_raw["jump"]
    finally:
        kernel.close()
