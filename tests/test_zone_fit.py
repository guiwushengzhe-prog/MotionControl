"""量身定区域：照人自己的动作放框，量完装上、存盘、重启还在。

这里的"人"是按提示做动作的假人：每一帧看量到哪一步了，就做那一步的动作，
和真人跟着教学卡片做是一回事。画面 640×480，人面对摄像头，没镜像——人的左手
在画面右边。
"""

from __future__ import annotations

import json
import time

import pytest

from motioncontrol.control_kernel import ControlKernel
from motioncontrol.zone_fit import (
    DEFAULT_ZONE_FIT,
    LIMITS,
    ZoneFitSession,
    body_frame,
    fit_grip,
    is_default,
    normalize_zone_fit,
)

W, H = 640, 480
FPS = 30.0

REST = {"left_wrist": (0.61, 0.58), "right_wrist": (0.39, 0.58),
        "left_ankle": (0.55, 0.90), "right_ankle": (0.45, 0.90)}
# 两手往两边抬到肩高，脚往外伸出去一大步。
UP = {"left_wrist": (0.78, 0.30), "right_wrist": (0.22, 0.30)}
OUT = {"left_ankle": (0.72, 0.84), "right_ankle": (0.28, 0.84)}
JUMP = 0.08


class Output:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def person(lift=0.0, **moved):
    places = {**REST, **moved}

    def point(x, y):
        return {"x": x, "y": y - lift, "z": 0.0, "score": 0.95}

    pose = {
        "nose": point(0.50, 0.20),
        "left_eye": point(0.515, 0.19), "right_eye": point(0.485, 0.19),
        "left_ear": point(0.535, 0.20), "right_ear": point(0.465, 0.20),
        "left_shoulder": point(0.58, 0.32), "right_shoulder": point(0.42, 0.32),
        "left_elbow": point(0.60, 0.45), "right_elbow": point(0.40, 0.45),
        "left_hip": point(0.545, 0.58), "right_hip": point(0.455, 0.58),
        "left_knee": point(0.55, 0.74), "right_knee": point(0.45, 0.74),
    }
    for name, (x, y) in places.items():
        pose[name] = point(x, y)
    return pose


def ramp(t, start, top, stay, end):
    """0 → 1 → 0：start 开始抬，top 到顶，stay 开始放，end 放完。"""
    if t <= start or t >= end:
        return 0.0
    if t < top:
        return (t - start) / (top - start)
    if t <= stay:
        return 1.0
    return (end - t) / (end - stay)


def between(a, b, k):
    return (a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k)


def act(phase, t):
    """这一步该做的动作。t 是看到这一步提示之后过了多久。"""
    if phase == "hands":
        k = ramp(t, 0.3, 0.6, 1.1, 1.4)
        return person(left_wrist=between(REST["left_wrist"], UP["left_wrist"], k),
                      right_wrist=between(REST["right_wrist"], UP["right_wrist"], k)), None
    if phase in ("leftFoot", "rightFoot"):
        name = "left_ankle" if phase == "leftFoot" else "right_ankle"
        k = ramp(t, 0.3, 0.6, 1.0, 1.3)
        return person(**{name: between(REST[name], OUT[name], k)}), None
    if phase == "jump":
        return person(lift=JUMP * ramp(t, 0.8, 0.95, 1.05, 1.2)), None
    if phase.endswith("Open"):
        return person(), {"curl": 1.90, "spread": None}
    if phase.endswith("Fist"):
        return person(), {"curl": 1.90 if t < 0.5 else 1.05, "spread": None}
    return person(), None


def run(session, *, start=100.0, limit_s=60.0, lazy=()):
    """按提示做到量完。lazy 里的那几步站着不动，等它自己跳过。"""
    now, seen, since = start, None, start
    while session.active and now - start < limit_s:
        phase = session.phase
        if phase != seen:
            seen, since = phase, now
        pose, grip = (person(), None) if phase in lazy else act(phase, now - since)
        hand = "left" if phase.startswith("left") else "right"
        session.update(pose, W, H, now, {hand: grip} if grip else None)
        now += 1.0 / FPS
    return now


def rect_for(fit, pose, zone):
    """用量出来的数装进内核，算这一帧的框。"""
    kernel = ControlKernel(Output())
    kernel.zone_fit = normalize_zone_fit(fit)
    kernel.width, kernel.height = W, H
    try:
        return kernel._compute_body_zones(pose, time.monotonic())[zone]
    finally:
        kernel.close()


def inside(point, rect):
    x, y = point
    return rect["x1"] <= x <= rect["x2"] and rect["y1"] <= y <= rect["y2"]


def test_default_numbers_are_the_old_fixed_ones():
    """没量过身的人，框和以前一模一样。"""
    kernel = ControlKernel(Output())
    kernel.width, kernel.height = W, H
    try:
        assert is_default(kernel.zone_fit)
        pose = person()
        frame = body_frame(pose, W, H)
        rects = kernel._compute_body_zones(pose, time.monotonic())
        assert rects["leftHand"]["y2"] == pytest.approx(frame["hip"]["y"] - 0.40 * frame["uy"])
        foot = rects["leftFoot"]
        assert (foot["x1"] + foot["x2"]) / 2 == pytest.approx(0.545 + 0.64 * frame["ux"])
        assert foot["x2"] - foot["x1"] == pytest.approx(1.00 * frame["ux"])
    finally:
        kernel.close()


def test_a_full_pass_fits_every_zone_between_rest_and_reach():
    session = ZoneFitSession(normalize_zone_fit(None), 100.0)
    run(session)
    assert session.state == "done"
    assert session.measured == ["leftHand", "rightHand", "leftFoot", "rightFoot", "headJump"]
    fit = session.result()
    assert not is_default(fit)
    for zone, params in fit["zones"].items():
        for key, value in params.items():
            low, high = LIMITS[key]
            assert low <= value <= high, f"{zone}.{key}={value}"

    # 站着碰不到，做动作进得去。
    rest = person()
    for zone, name in (("leftHand", "left_wrist"), ("rightHand", "right_wrist")):
        rect = rect_for(fit, rest, zone)
        assert not inside(REST[name], rect), f"{zone} 垂着手就碰到了"
        assert inside(UP[name], rect), f"{zone} 挥手进不去"
    for zone, name in (("leftFoot", "left_ankle"), ("rightFoot", "right_ankle")):
        rect = rect_for(fit, person(**{name: OUT[name]}), zone)
        assert inside(OUT[name], rect), f"{zone} 伸脚进不去"
        assert not inside(REST[name], rect_for(fit, rest, zone)), f"{zone} 站着就碰到了"
    jump = rect_for(fit, rest, "headJump")
    assert rest["nose"]["y"] > jump["y2"], "站着鼻子就在头顶区里"
    assert rest["nose"]["y"] - JUMP < jump["y2"], "跳起来鼻子够不着头顶区"


def test_preparation_countdown_does_not_collect_until_it_ends():
    session = ZoneFitSession(normalize_zone_fit(None), 100.0, prepare_s=3.0)
    pose = person()

    status = session.status(now=100.0)
    assert status["state"] == "preparing"
    assert status["preparing"] is True
    assert status["remaining_s"] == pytest.approx(3.0)

    session.update(pose, W, H, 100.5)
    assert session.status(now=100.5)["state"] == "preparing"
    assert session._stand == []

    session.update(pose, W, H, 103.0)
    status = session.status(now=103.0)
    assert status["state"] == "measuring"
    assert status["remaining_s"] == 0.0
    assert len(session._stand) == 1


def test_kernel_start_reports_server_side_preparation():
    kernel = ControlKernel(Output())
    try:
        fit = kernel.start_zone_fit()["zone_fit"]
        assert fit["state"] == "preparing"
        assert fit["preparing"] is True
        assert 2.9 <= fit["remaining_s"] <= 3.0
    finally:
        kernel.close()


def test_a_step_nobody_does_is_skipped_and_keeps_the_old_zone():
    session = ZoneFitSession(normalize_zone_fit(None), 100.0)
    run(session, lazy=("leftFoot", "rightFoot"))
    assert session.state == "done"
    assert set(session.skipped) == {"leftFoot", "rightFoot"}
    fit = session.result()
    assert fit["zones"]["leftFoot"] == DEFAULT_ZONE_FIT["leftFoot"]
    assert fit["zones"]["leftHand"] != DEFAULT_ZONE_FIT["leftHand"]


def test_standing_still_is_not_a_jump():
    session = ZoneFitSession(normalize_zone_fit(None), 100.0)
    run(session, lazy=("jump",))
    assert "headJump" in session.skipped
    assert session.result()["zones"]["headJump"] == DEFAULT_ZONE_FIT["headJump"]


def test_the_fist_is_measured_between_open_and_closed():
    session = ZoneFitSession(normalize_zone_fit(None), 100.0, grip_hands=("left",))
    assert session.phases[-2:] == ("leftOpen", "leftFist")
    run(session)
    assert session.state == "done"
    assert "leftGrip" in session.measured
    updates = session.grip_updates()
    assert 1.05 < updates["curl_close"] < updates["curl_open"] < 1.90
    assert updates["curl_close"] == pytest.approx(1.05 + 0.40 * 0.85, abs=0.02)


def test_only_the_fist_can_be_measured_on_its_own():
    """换了只手、握拳认不准时用：不用再挥手跳。"""
    session = ZoneFitSession(normalize_zone_fit(None), 100.0, grip_hands=("right",), body=False)
    assert session.phases == ("rightOpen", "rightFist")
    run(session)
    assert session.state == "done"
    assert is_default(session.result())
    assert session.grip_updates()["curl_open"] > session.grip_updates()["curl_close"]


def test_a_fist_that_never_closes_changes_nothing():
    session = ZoneFitSession(normalize_zone_fit(None), 100.0, grip_hands=("left",), body=False)
    run(session, lazy=("leftFist",))
    assert "leftGrip" in session.skipped
    assert session.grip_updates() == {}


def test_two_hands_share_one_pair_that_works_for_both():
    updates = fit_grip({"left": ("hand", 1.9, 1.0), "right": ("hand", 1.7, 1.2)})
    assert 1.2 < updates["curl_close"] < updates["curl_open"] < 1.7
    # 两只手差得太多，找不到对两只都成立的一段：不改。
    assert fit_grip({"left": ("hand", 1.9, 1.0), "right": ("hand", 1.3, 1.2)}) == {}


def test_damaged_saved_numbers_fall_back_to_defaults():
    assert is_default(normalize_zone_fit(None))
    assert is_default(normalize_zone_fit({"version": 99, "zones": {"leftHand": {"bottom": 0.9}}}))
    fit = normalize_zone_fit({"version": 1, "zones": {"leftHand": {"bottom": "坏了", "inset": 99}}})
    assert fit["zones"]["leftHand"]["bottom"] == DEFAULT_ZONE_FIT["leftHand"]["bottom"]
    assert fit["zones"]["leftHand"]["inset"] == LIMITS["inset"][1]


def _drive_kernel(kernel, *, limit_s=60.0):
    base = time.monotonic()
    now, seen, since = base, None, base
    while now - base < limit_s:
        with kernel._lock:
            session = kernel.zone_fit_session
            if not session.active:
                return
            phase = session.phase
            if phase != seen:
                seen, since = phase, now
            pose, _grip = act(phase, now - since)
            kernel.latest_pose = pose
            kernel._process_pose_locked(pose, now)
        now += 1.0 / FPS


def test_the_kernel_applies_saves_and_remembers_the_fit(isolated_user_data):
    kernel = ControlKernel(Output())
    kernel.width, kernel.height = W, H
    kernel.configure_hand_mouse({"enabled": False})
    try:
        kernel.start_zone_fit()
        _drive_kernel(kernel)
        status = kernel.status()["zone_fit"]
        assert status["state"] == "done"
        assert status["custom"] is True
        assert status["measured_at_unix"] is not None
        fitted = kernel.zone_fit["zones"]
    finally:
        kernel.close()

    saved = json.loads((isolated_user_data / "general_settings.json").read_text(encoding="utf-8"))
    assert saved["zone_fit"]["zones"] == fitted

    again = ControlKernel(Output())
    try:
        assert again.zone_fit["zones"] == fitted, "重启之后要还是量出来的那一份"
        again.reset_zone_fit()
        assert is_default(again.zone_fit)
    finally:
        again.close()
    third = ControlKernel(Output())
    try:
        assert is_default(third.zone_fit), "恢复默认也要存盘"
    finally:
        third.close()


def test_cancelling_changes_nothing(isolated_user_data):
    kernel = ControlKernel(Output())
    kernel.width, kernel.height = W, H
    try:
        kernel.start_zone_fit()
        kernel.cancel_zone_fit()
        assert kernel.status()["zone_fit"]["state"] == "cancelled"
        assert is_default(kernel.zone_fit)
    finally:
        kernel.close()
