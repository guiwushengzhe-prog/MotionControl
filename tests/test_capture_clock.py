"""手机来的帧按手机认出它的时刻算速度，不按电脑收到的时刻：WiFi 抖动不该改变判定。"""

from __future__ import annotations

import pytest

from motioncontrol.control_kernel import ControlKernel
from motioncontrol.intent_library import BASE_T, Segment, make_replay_kernel, replay_segment
from motioncontrol.intent_recording import IntentRecordingSession, build_steps
from motioncontrol.zone_arbiter import CLOCK_MAX_LAG_S, CaptureClock
from test_minimal_controls import KernelOutput, _standing_pose

FPS = 30
BASE_LATENCY = 0.05
# 每帧在网上多耽误了多久。挤在一起到、隔一阵才到，都有。
JITTER = [0.0, 0.07, 0.03, 0.0, 0.06, 0.01, 0.0, 0.08, 0.02, 0.04]


def arrivals(count):
    """(手机时间, 电脑收到时刻)。TCP 不会乱序，后一帧不会比前一帧先到。"""
    out, last = [], 0.0
    for i in range(count):
        captured = i / FPS
        last = max(last, 10.0 + captured + BASE_LATENCY + JITTER[i % len(JITTER)])
        out.append((captured, last))
    return out


def test_clock_follows_the_phone_spacing_not_the_network():
    clock = CaptureClock()
    mapped = [clock.map(captured, arrival) for captured, arrival in arrivals(60)]
    for (captured, arrival), value in zip(arrivals(60), mapped):
        assert value <= arrival
        assert value == pytest.approx(10.0 + captured + BASE_LATENCY), "基准是到得最快的那一帧"
    assert mapped == sorted(mapped)


def test_clock_starts_over_when_the_phone_clock_jumps():
    clock = CaptureClock()
    clock.map(100.0, 10.0)
    clock.map(100.033, 10.05)
    # 重连后重新对钟：手机时间倒退了。不能把新帧算到很久以前去。
    value = clock.map(40.0, 10.1)
    assert 10.1 - CLOCK_MAX_LAG_S <= value <= 10.1
    assert value >= 10.05 - CLOCK_MAX_LAG_S


def swing(i):
    """右手平着往外甩，速度恒定。"""
    return _standing_pose(right_wrist=(.60 + .01 * i, .66))


def speeds(monkeypatch, frames, *, with_capture):
    kernel = ControlKernel(KernelOutput())
    clock = [0.0]
    monkeypatch.setattr("motioncontrol.control_kernel.time.monotonic", lambda: clock[0])
    out = []
    try:
        for i, (captured, arrival) in enumerate(frames):
            clock[0] = arrival
            kernel.handle_pose_map("mobile_pose:phone", swing(i), width=640, height=480,
                                   captured_at_ms=captured * 1000.0 if with_capture else None)
            out.append(kernel.zone_kin.speed("right_wrist"))
    finally:
        kernel.close()
    return out


def test_kernel_speed_ignores_network_jitter(monkeypatch):
    jittered = arrivals(40)
    ideal = [(captured, 10.0 + captured + BASE_LATENCY) for captured, _ in jittered]
    truth = speeds(monkeypatch, ideal, with_capture=False)
    phone = speeds(monkeypatch, jittered, with_capture=True)
    naive = speeds(monkeypatch, jittered, with_capture=False)
    for want, got in zip(truth[5:], phone[5:]):
        assert got == pytest.approx(want, rel=1e-6)
    worst = max(abs(got - want) / want for want, got in zip(truth[5:], naive[5:]))
    assert worst > 0.2, "不换算的话，抖动会把速度算错两成以上——这个测试才有意义"


def test_recording_keeps_the_capture_time_and_replay_uses_it():
    session = IntentRecordingSession(build_steps([], ["idle"]), 0.0, prepare_s=0)
    session.update(1.0, _standing_pose(), 640, 480, {}, set(), sample_at=0.93)
    session.update(1.1, _standing_pose(), 640, 480, {}, set())
    first, second = session.frames
    assert first["c"] == pytest.approx(0.93) and "c" not in second

    live = ControlKernel(KernelOutput())
    try:
        kernel = make_replay_kernel(live.replay_snapshot())
    finally:
        live.close()
    seen = []
    try:
        replay_segment(kernel, Segment(key="idle", kind="idle", source="x", step={}, frames=[first, second]),
                       on_frame=lambda k, row: seen.append(k.pose_sample_at))
    finally:
        kernel.close()
    assert seen == [pytest.approx(BASE_T + 0.93), None]
