"""Fist-gated hand steering.

The pose model has no finger joints -- only wrist, thumb, index and pinky -- so
a fist is inferred from how far the fingertips sit from the wrist, normalised
by the forearm. These cover the properties that make that usable rather than
the exact threshold values, which are defaults meant to be tuned against the
live `spread` reading.
"""

from __future__ import annotations

import pytest

from hand_mouse_control import DEFAULT_CONFIG, HandMouseController, merge_config


def pose(hand="right", *, spread=0.5, wrist=(0.5, 0.5), forearm=0.2, visibility=0.9):
    """A pose map with the fingertips placed at a chosen spread."""
    wx, wy = wrist
    tip_distance = spread * forearm
    points = {
        f"{hand}_elbow": {"x": wx, "y": wy + forearm, "score": visibility},
        f"{hand}_wrist": {"x": wx, "y": wy, "score": visibility},
        f"{hand}_thumb": {"x": wx - tip_distance, "y": wy, "score": visibility},
        f"{hand}_index": {"x": wx, "y": wy - tip_distance, "score": visibility},
        f"{hand}_pinky": {"x": wx + tip_distance, "y": wy, "score": visibility},
    }
    return points


def controller(**overrides):
    ctl = HandMouseController()
    ctl.configure({"enabled": True, **overrides})
    return ctl


# --- fist detection -------------------------------------------------------


def test_open_hand_does_not_engage():
    ctl = controller()
    state = ctl.update(pose(spread=0.6), now=1.0)
    assert state["engaged"] is False
    assert state["state"] == "open"


def test_closed_fist_engages():
    ctl = controller()
    state = ctl.update(pose(spread=0.2), now=1.0)
    assert state["engaged"] is True


def test_spread_is_normalised_by_forearm_not_pixels():
    """The same gesture at a different distance from the camera must read alike."""
    ctl = controller()
    near, _ = ctl.measure_spread(pose(spread=0.25, forearm=0.30), "right")
    far, _ = ctl.measure_spread(pose(spread=0.25, forearm=0.08), "right")
    assert near == pytest.approx(far, abs=1e-6)


def test_hysteresis_prevents_chatter():
    """A value between the two thresholds keeps whatever state we were in."""
    ctl = controller(fist_close=0.30, fist_open=0.40)
    ctl.update(pose(spread=0.20), now=1.0)
    assert ctl.engaged is True
    # In the band: still closed.
    ctl.update(pose(spread=0.35), now=1.1)
    assert ctl.engaged is True
    # Past the open threshold: released.
    ctl.update(pose(spread=0.45), now=1.2)
    assert ctl.engaged is False
    # Back in the band from open: stays open.
    ctl.update(pose(spread=0.35), now=1.3)
    assert ctl.engaged is False


def test_overlapping_thresholds_are_rejected():
    with pytest.raises(ValueError, match="抖动"):
        merge_config(DEFAULT_CONFIG, {"fist_close": 0.4, "fist_open": 0.3})


def test_too_few_visible_tips_reads_as_unknown_not_open():
    """Claiming "open" on missing data would silently drop an active fist."""
    ctl = controller(min_tips=3)
    points = pose(spread=0.2)
    points["right_thumb"]["score"] = 0.0
    state = ctl.update(points, now=1.0)
    assert state["spread"] is None
    assert state["state"] == "lost"
    assert state["engaged"] is False


# --- movement -------------------------------------------------------------


def test_closing_captures_an_anchor_and_emits_nothing_yet():
    ctl = controller()
    state = ctl.update(pose(spread=0.2, wrist=(0.5, 0.5)), now=1.0)
    assert state["output_x"] == 0.0 and state["output_y"] == 0.0
    assert ctl.anchor == (0.5, 0.5)


def test_moving_while_closed_drives_both_axes():
    """画面里的手往 +x 走，是玩家把手往自己的左边移，所以指针往左。

    内核用的是没镜像过的原始画面：面对镜头的人，他的右手在画面的左半边。直接
    拿 x 的增减当方向，玩家往右挥手指针会往左走——所以这一路是反的。
    """
    ctl = controller(deadzone=0.0)
    ctl.update(pose(spread=0.2, wrist=(0.5, 0.5)), now=1.0)
    state = ctl.update(pose(spread=0.2, wrist=(0.58, 0.56)), now=1.1)
    assert state["output_x"] < 0
    assert state["output_y"] > 0


def test_image_down_moves_pointer_down():
    """Image y grows downward and so does mouse dy, so no flip belongs here."""
    ctl = controller(deadzone=0.0)
    ctl.update(pose(spread=0.2, wrist=(0.5, 0.5)), now=1.0)
    state = ctl.update(pose(spread=0.2, wrist=(0.5, 0.62)), now=1.1)
    assert state["output_y"] > 0
    assert state["output_x"] == 0.0


def test_invert_x_flips_only_the_horizontal_axis():
    """invert_x 已经不是给用户的开关了，但翻转这件事本身还得成立。"""
    ctl = controller(deadzone=0.0, invert_x=True)
    ctl.update(pose(spread=0.2, wrist=(0.5, 0.5)), now=1.0)
    state = ctl.update(pose(spread=0.2, wrist=(0.60, 0.60)), now=1.1)
    assert state["output_x"] < 0
    assert state["output_y"] > 0


def test_deadzone_suppresses_small_drift():
    ctl = controller(deadzone=0.5, range=0.5)
    ctl.update(pose(spread=0.2, wrist=(0.5, 0.5)), now=1.0)
    state = ctl.update(pose(spread=0.2, wrist=(0.51, 0.5)), now=1.1)
    assert state["output_x"] == 0.0


def test_output_is_clamped():
    ctl = controller(deadzone=0.0, sensitivity=200.0, range=0.1)
    ctl.update(pose(spread=0.2, wrist=(0.5, 0.5)), now=1.0)
    state = ctl.update(pose(spread=0.2, wrist=(0.9, 0.9)), now=1.1)
    assert -1.0 <= state["output_x"] <= 1.0
    assert -1.0 <= state["output_y"] <= 1.0
    # 这条测的是"到顶就不再涨"，不是方向，所以只看大小。
    assert abs(state["output_x"]) == 1.0


def test_reopening_and_closing_recentres():
    """Like lifting a mouse off the desk: the anchor follows the new position."""
    ctl = controller(deadzone=0.0)
    ctl.update(pose(spread=0.2, wrist=(0.5, 0.5)), now=1.0)
    ctl.update(pose(spread=0.2, wrist=(0.7, 0.5)), now=1.1)
    ctl.update(pose(spread=0.6, wrist=(0.7, 0.5)), now=1.2)  # open
    state = ctl.update(pose(spread=0.2, wrist=(0.7, 0.5)), now=1.3)  # close again
    assert ctl.anchor == (0.7, 0.5)
    assert state["output_x"] == 0.0


def test_releasing_zeroes_the_output():
    ctl = controller(deadzone=0.0)
    ctl.update(pose(spread=0.2, wrist=(0.5, 0.5)), now=1.0)
    ctl.update(pose(spread=0.2, wrist=(0.8, 0.8)), now=1.1)
    state = ctl.update(pose(spread=0.7, wrist=(0.8, 0.8)), now=1.2)
    assert state["output_x"] == 0.0 and state["output_y"] == 0.0
    assert state["state"] == "opened"


def test_losing_the_hand_releases_rather_than_freezing():
    """A stuck engagement would keep steering from a stale anchor."""
    ctl = controller(deadzone=0.0)
    ctl.update(pose(spread=0.2, wrist=(0.5, 0.5)), now=1.0)
    ctl.update(pose(spread=0.2, wrist=(0.7, 0.7)), now=1.1)
    state = ctl.update({}, now=1.2)
    assert state["engaged"] is False
    assert state["output_x"] == 0.0 and state["output_y"] == 0.0
    assert state["state"] == "lost"


# --- hand selection and config -------------------------------------------


def test_the_other_hand_is_ignored():
    ctl = controller(hand="left")
    state = ctl.update(pose(hand="right", spread=0.2), now=1.0)
    assert state["engaged"] is False
    assert state["state"] == "lost"


def test_left_hand_works_the_same():
    ctl = controller(hand="left", deadzone=0.0)
    ctl.update(pose(hand="left", spread=0.2, wrist=(0.5, 0.5)), now=1.0)
    state = ctl.update(pose(hand="left", spread=0.2, wrist=(0.6, 0.5)), now=1.1)
    assert state["engaged"] is True
    assert state["output_x"] < 0


def test_disabled_controller_emits_nothing():
    ctl = HandMouseController()
    state = ctl.update(pose(spread=0.1), now=1.0)
    assert state["engaged"] is False
    assert state["state"] == "disabled"


def test_unknown_setting_is_rejected():
    with pytest.raises(ValueError, match="unknown hand mouse setting"):
        merge_config(DEFAULT_CONFIG, {"nonsense": 1})


def test_invalid_hand_is_rejected():
    with pytest.raises(ValueError, match="hand must be"):
        merge_config(DEFAULT_CONFIG, {"hand": "foot"})


def test_reconfiguring_drops_the_anchor():
    """Keeping it would make the pointer jump when the next fist closes."""
    ctl = controller(deadzone=0.0)
    ctl.update(pose(spread=0.2, wrist=(0.5, 0.5)), now=1.0)
    assert ctl.anchor is not None
    ctl.configure({"sensitivity": 80.0})
    assert ctl.anchor is None
    assert ctl.engaged is False


def test_status_reports_spread_for_calibration():
    ctl = controller()
    state = ctl.update(pose(spread=0.33), now=1.0)
    assert state["spread"] == pytest.approx(0.33, abs=1e-3)
    assert state["tips_seen"] == 3


# --- 画面外的手 ---------------------------------------------------------------

def out_of_frame_hand():
    """一只举在画面左侧外面的右手，数值取自真机。

    2026-09-17 从跑着的服务上读到的实况：手在画面外，MediaPipe 仍然给出 0.68 的
    置信度和 x≈-0.15 的坐标——它是从手臂外推的，不是看到的。外推的指尖挨着外推的
    手腕，张开度算出来约 0.24，低于 fist_close，于是"一直在握拳"。
    """
    return {
        "right_elbow":  {"x": -0.05, "y": 0.62, "score": 0.74},
        "right_wrist":  {"x": -0.127, "y": 0.720, "score": 0.74},
        "right_pinky":  {"x": -0.191, "y": 0.759, "score": 0.68},
        "right_index":  {"x": -0.157, "y": 0.749, "score": 0.68},
        "right_thumb":  {"x": -0.113, "y": 0.747, "score": 0.68},
    }


def test_a_hand_outside_the_picture_is_not_a_fist():
    """这是真机上出过的 bug：手看不见时，鼠标被判定为一直按着。

    置信度那道门拦不住——外推出来的点带着 0.68 的分数，比 min_visibility 高得多。
    坐标出画是硬事实，不是可调的阈值，所以用它来拦。
    """
    ctl = controller(hand="right")
    spread, tips = ctl.measure_spread(out_of_frame_hand(), "right")
    assert spread is None, f"画面外的手仍然给出了张开度 {spread}"
    assert tips == 0


def test_an_out_of_frame_hand_does_not_engage_the_mouse(monkeypatch):
    """误判方向要紧：判成"张开"只是不响应，判成"握拳"会让鼠标一直拖着。"""
    ctl = controller(hand="right")
    state = ctl.update(out_of_frame_hand(), now=1.0)
    assert state["engaged"] is False
    assert state["spread"] is None


def test_the_elbow_may_leave_the_picture():
    """手举到画面下缘外时手肘出画是常事，不该因此整只手作废。

    手肘只是长度基准；判断握没握拳靠的是指尖相对手腕的位置，那两样仍然看得见。
    """
    ctl = controller(hand="right")
    low = pose(spread=0.2, wrist=(0.9, 0.9), forearm=0.2)   # 手肘在 y=1.1
    spread, tips = ctl.measure_spread(low, "right")
    assert spread is not None and tips == 3


def test_a_hand_at_the_very_edge_still_counts():
    """贴着边缘的手是真看得见的，容差要留住它。"""
    ctl = controller(hand="right")
    spread, _ = ctl.measure_spread(pose(spread=0.2, wrist=(0.01, 0.5)), "right")
    assert spread is not None


# --- grip read from real finger joints ------------------------------------
#
# 真机实测（荣耀 200，姿态模型 full）：手部模型每帧 23 毫秒，帧率 15.3 掉到
# 13.9，认出手的比例 111/112。开销可接受，读数可靠，所以只要设备给了 21 个点
# 就用它，指尖张开度退为没有手部模型时的兜底。


def hand_points(*, curl, wrist=(0.5, 0.9), reach=0.10, fan_degrees=12.0):
    """21 个手部点，四根手指从手腕呈扇形张开，按给定的伸展倍数摆好。

    ``curl`` 就是这套算法读出来的那个比值：指尖到手腕的距离是指根到手腕距离的
    几倍。摊平的手约 2.0，攥紧的拳约 1.0。指根和指尖放在同一条从手腕出发的射
    线上，读出来的比值才正好等于 ``curl``。
    """
    import math

    from hand_mouse_control import _FINGER_KNUCKLES, _FINGER_TIPS

    wx, wy = wrist
    points = [{"x": wx, "y": wy, "score": 0.9} for _ in range(21)]
    for index, (knuckle_index, tip_index) in enumerate(zip(_FINGER_KNUCKLES, _FINGER_TIPS)):
        angle = math.radians(-90.0 + (index - 1.5) * fan_degrees)
        ux, uy = math.cos(angle), math.sin(angle)
        points[knuckle_index] = {"x": wx + ux * reach, "y": wy + uy * reach, "score": 0.9}
        points[tip_index] = {"x": wx + ux * reach * curl, "y": wy + uy * reach * curl, "score": 0.9}
    return points


def test_flat_fingers_read_near_two_and_a_fist_near_one():
    """这两个数就是默认阈值 1.35/1.60 的由来，读错了阈值全部失效。"""
    from hand_mouse_control import measure_curl

    assert measure_curl(hand_points(curl=2.0)) == pytest.approx(2.0, abs=0.01)
    assert measure_curl(hand_points(curl=1.0)) == pytest.approx(1.0, abs=0.01)


def test_curl_survives_the_player_standing_further_away():
    """所有距离都从手腕起算，整只手缩小时比值不变。"""
    from hand_mouse_control import measure_curl

    near = measure_curl(hand_points(curl=1.8, reach=0.20))
    far = measure_curl(hand_points(curl=1.8, reach=0.05))
    assert near == pytest.approx(far, abs=0.01)


def test_a_closed_fist_engages_from_finger_joints():
    ctl = controller(hand="right")
    state = ctl.update(pose(spread=0.5), now=1.0, hand_points=hand_points(curl=1.0))
    assert state["engaged"] is True
    assert state["grip_source"] == "hand"
    assert state["curl"] == pytest.approx(1.0, abs=0.01)


def test_finger_joints_outrank_the_fingertip_spread():
    """姿态模型那三个指尖说这是拳头，手指关节说没有——听手指的。

    这正是加手部模型的理由：姿态模型只有三个指尖，读错的时候鼠标会一直卡在
    按下状态。
    """
    ctl = controller(hand="right")
    state = ctl.update(pose(spread=0.1), now=1.0, hand_points=hand_points(curl=2.0))
    assert state["engaged"] is False
    assert state["grip_source"] == "hand"


def test_without_hand_points_the_spread_still_drives_the_gate():
    """没有手部模型的设备照旧能用，只是粗一些。"""
    ctl = controller(hand="right")
    state = ctl.update(pose(spread=0.1), now=1.0)
    assert state["engaged"] is True
    assert state["grip_source"] == "pose"
    assert state["curl"] is None


def test_a_hand_outside_the_picture_is_not_believed():
    """和姿态那条路同一条规矩：出了画面的点是外推的，不是看见的。"""
    from hand_mouse_control import measure_curl

    assert measure_curl(hand_points(curl=1.0, wrist=(-0.2, 0.5))) is None


def test_curl_keeps_its_hysteresis():
    """中间地带维持原状，否则鼠标会在移动中途松手。"""
    ctl = controller(hand="right")
    ctl.update(pose(spread=0.5), now=1.0, hand_points=hand_points(curl=1.0))
    assert ctl.engaged is True
    between = ctl.update(pose(spread=0.5), now=1.1, hand_points=hand_points(curl=1.45))
    assert between["engaged"] is True
    opened = ctl.update(pose(spread=0.5), now=1.2, hand_points=hand_points(curl=1.9))
    assert opened["engaged"] is False


def test_a_thresholds_pair_without_a_gap_is_refused():
    with pytest.raises(ValueError):
        merge_config(None, {"curl_close": 1.6, "curl_open": 1.5})
