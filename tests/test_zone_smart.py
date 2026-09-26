"""区域「智能」判定：进框先判断是故意伸进来的，还是做动作时扫过。

用的是合成的骨架，证明的是逻辑：没冲突进去就按、只有一只手在动马上按、两只手一起
往上走不按、动作已经在做一直到手出框都不按、系统功能要稳住……真人动作下误按多少、
慢多少，要拿「录我的动作」录下来的数据在 tools/eval_zone_arbiter.py 里量。
"""

from __future__ import annotations

import json

import pytest

from motioncontrol.control_kernel import ControlKernel
from motioncontrol.zone_arbiter import (
    SYSTEM_HOLD_S, T_MAX_S, Kinematics, Snippet, SnippetBank, sweep_score,
)
from motioncontrol.zone_fit import body_frame
from test_minimal_controls import KernelOutput, _standing_pose, _zone_feeder

FPS = 30
HANDS_UP = [{"id": "hands_up", "enabled": True, "type": "gamepad", "target": "Y"}]


def with_elbows(pose, left=(.40, .52), right=(.60, .52)):
    pose["left_elbow"] = {"x": left[0], "y": left[1], "score": .95}
    pose["right_elbow"] = {"x": right[0], "y": right[1], "score": .95}
    return pose


def standing():
    return with_elbows(_standing_pose())


def into(kernel, pose, zone, depth):
    """把这只手的手腕放进手区，越过下沿 depth、越过里沿更多（量身那把尺）。"""
    frame = body_frame(pose, 640, 480)
    rect = kernel.zone_rects[zone]
    direction = frame["left_dir"] if zone == "leftHand" else frame["right_dir"]
    inner = rect["x1"] if direction > 0 else rect["x2"]
    wrist = "left_wrist" if zone == "leftHand" else "right_wrist"
    pose[wrist] = {"x": inner + direction * (depth + .15) * frame["ux"],
                   "y": rect["y2"] - depth * frame["uy"], "score": .95}
    return pose


@pytest.fixture()
def kernel():
    made = ControlKernel(KernelOutput())
    yield made
    made.close()


def start(kernel, monkeypatch):
    feed = _zone_feeder(kernel, monkeypatch)
    feed(standing(), 10)
    return feed


# ---------- 没有冲突：和进去就按一样快 ----------

def test_smart_is_the_default(kernel):
    assert kernel.status()["zone_trigger_mode"] == "smart"


def test_an_old_guarded_setting_becomes_smart(tmp_path):
    from motioncontrol.user_paths import user_path
    path = user_path("general_settings")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"zone_trigger_mode": "guarded"}), encoding="utf-8")
    made = ControlKernel(KernelOutput())
    try:
        assert made.zone_trigger_mode == "smart"
        made.configure_zone_trigger_mode("guarded")  # 老界面发来的也认
        assert made.zone_trigger_mode == "smart"
    finally:
        made.close()


def test_without_a_conflicting_motion_the_first_frame_inside_presses(kernel, monkeypatch):
    feed = start(kernel, monkeypatch)
    feed(into(kernel, standing(), "leftHand", .12), 1)
    assert kernel.zone_state["leftHand"]["pressed"]
    assert kernel.status()["zone_overlaps"] == {}


def test_sitting_right_on_the_edge_does_not_flicker(kernel, monkeypatch):
    """人停在框边上，骨架一抖就进进出出：进去不到 ENTRY_MARGIN 不按。"""
    feed = start(kernel, monkeypatch)
    feed(into(kernel, standing(), "leftHand", .005), 10)
    assert not kernel.zone_state["leftHand"]["pressed"]
    feed(into(kernel, standing(), "leftHand", .05), 1)
    assert kernel.zone_state["leftHand"]["pressed"]


def test_waving_taps_once_per_entry(kernel, monkeypatch):
    feed = start(kernel, monkeypatch)
    presses = 0
    for _ in range(3):
        feed(into(kernel, standing(), "leftHand", .12), 2)
        presses += kernel.zone_state["leftHand"]["pressed"]
        feed(standing(), 2)
        assert not kernel.zone_state["leftHand"]["pressed"]
    assert presses == 3


def test_the_hand_steering_the_mouse_presses_nothing(kernel, monkeypatch):
    feed = start(kernel, monkeypatch)
    monkeypatch.setattr(kernel.hand_mouse_controller, "owns_hand", lambda hand: hand == "left")
    feed(into(kernel, standing(), "leftHand", .12), 5)
    assert not kernel.zone_state["leftHand"]["pressed"]


# ---------- 有冲突：看身体别的地方 ----------

def test_one_hand_reaching_in_still_presses_at_once_when_a_motion_could_sweep_it(kernel, monkeypatch):
    kernel.configure_motions(HANDS_UP)
    feed = start(kernel, monkeypatch)
    overlaps = kernel.status()["zone_overlaps"]
    assert overlaps["leftHand"]["yields"] and overlaps["leftHand"]["triggers"] == ["motion.hands_up"]
    feed(into(kernel, standing(), "leftHand", .12), 1)
    assert kernel.zone_state["leftHand"]["pressed"], "只有这一只手在动：第一帧就按，不用等"
    assert kernel.zone_state["leftHand"]["reason"] == "quiet"


def test_raising_both_hands_sweeps_past_without_pressing(kernel, monkeypatch):
    kernel.configure_motions(HANDS_UP)
    feed = start(kernel, monkeypatch)
    # 两手从两侧往上抬：先扫过两边的手区，再举到头顶。
    path = [((.36, .60), (.64, .60), (.40, .52), (.60, .52)),
            ((.28, .46), (.72, .46), (.35, .45), (.65, .45)),
            ((.26, .34), (.74, .34), (.34, .38), (.66, .38)),
            ((.32, .24), (.68, .24), (.36, .31), (.64, .31)),
            ((.42, .18), (.58, .18), (.39, .30), (.61, .30))]
    pressed, phases = [], set()
    for lw, rw, le, re in path:
        pose = with_elbows(_standing_pose(left_wrist=lw, right_wrist=rw), left=le, right=re)
        for _ in range(3):
            feed(pose)
            pressed += [z for z in ("leftHand", "rightHand") if kernel.zone_state[z]["pressed"]]
            phases.add(kernel.status()["zones"]["leftHand"]["phase"])
    top = path[-1]
    for _ in range(12):
        feed(with_elbows(_standing_pose(left_wrist=top[0], right_wrist=top[1]), left=top[2], right=top[3]))
        pressed += [z for z in ("leftHand", "rightHand") if kernel.zone_state[z]["pressed"]]
    assert "hands_up" in kernel.motion_active
    assert not pressed, f"举双手时误按了 {sorted(set(pressed))}"
    assert "pending" in phases, "扫过的那几帧界面上是「判断中」"
    assert "swept" in phases, "没按就出去了，框闪一下红"


def test_both_hands_put_into_their_zones_and_held_still_press_soon(kernel, monkeypatch):
    """两只手一起伸进去是像举双手，但停住了就是故意的：停住就按，不等到最后。"""
    kernel.configure_motions(HANDS_UP)
    feed = start(kernel, monkeypatch)
    pose = into(kernel, into(kernel, standing(), "leftHand", .12), "rightHand", .12)
    feed(pose, 1)
    assert not kernel.zone_state["leftHand"]["pressed"], "两只手一起动：先看一看"
    frames = 1
    while not kernel.zone_state["leftHand"]["pressed"] and frames < 30:
        feed(pose)
        frames += 1
    assert kernel.zone_state["leftHand"]["pressed"] and kernel.zone_state["rightHand"]["pressed"]
    assert frames / FPS <= T_MAX_S + 1e-6


def test_a_motion_already_under_way_blocks_the_zone_until_the_hand_leaves(kernel, monkeypatch):
    kernel.configure_motions(HANDS_UP)
    feed = start(kernel, monkeypatch)
    kernel.motion_raw["hands_up"] = True  # 规则已经成立
    pose = into(kernel, standing(), "leftHand", .12)
    feed(pose, 1)
    assert kernel.zone_state["leftHand"]["phase"] == "swept"
    kernel.motion_raw["hands_up"] = False
    kernel.motion_active.discard("hands_up")
    feed(pose, 20)
    assert not kernel.zone_state["leftHand"]["pressed"], "判定扫过之后，手不出框就一直不按"
    feed(standing(), 2)
    feed(into(kernel, standing(), "leftHand", .12), 1)
    assert kernel.zone_state["leftHand"]["pressed"], "出去再进来就是新的一下"


def test_a_press_is_taken_back_when_the_motion_shows_up_right_after(kernel, monkeypatch):
    kernel.configure_motions(HANDS_UP)
    feed = start(kernel, monkeypatch)
    pose = into(kernel, standing(), "leftHand", .12)
    feed(pose, 1)
    assert kernel.zone_state["leftHand"]["pressed"]
    kernel.motion_raw["hands_up"] = True
    feed(pose, 1)
    assert not kernel.zone_state["leftHand"]["pressed"]
    assert kernel.zone_state["leftHand"]["reason"] == "recalled"


def test_recorded_rates_win_over_what_the_action_file_says(kernel, monkeypatch):
    kernel.configure_motions(HANDS_UP)
    kernel.configure_zone_learning({"motion.hands_up": {"leftHand": {"hits": 0, "reps": 5},
                                                        "rightHand": {"hits": 4, "reps": 5}}}, None)
    overlaps = kernel.status()["zone_overlaps"]
    assert "leftHand" not in overlaps, "录的时候一次都没扫过左手框"
    assert overlaps["rightHand"]["rates"]["motion.hands_up"] == {"hits": 4, "reps": 5, "source": "recorded"}


# ---------- 要跳的框、做动作时也要按 ----------

def test_the_jump_zone_presses_along_with_a_jumping_motion_by_default(kernel):
    kernel.configure_motions([{"id": "jumping_jack", "enabled": True, "type": "gamepad", "target": "Y"}])
    overlaps = kernel.status()["zone_overlaps"]
    assert overlaps["headJump"]["with_motion"] is True and overlaps["headJump"]["yields"] is False
    assert overlaps["leftHand"]["with_motion"] is False and overlaps["leftHand"]["yields"] is True


def test_with_motion_can_be_switched_per_zone(kernel):
    kernel.configure_motions(HANDS_UP)
    kernel.configure_bindings({"zones": {
        "leftHand": {"action": {"type": "gamepad", "target": "X"}, "with_motion": True},
        "headJump": {"action": {"type": "gamepad", "target": "A"}, "with_motion": False},
    }})
    kernel.configure_motions([{"id": "jumping_jack", "enabled": True, "type": "gamepad", "target": "Y"}])
    overlaps = kernel.status()["zone_overlaps"]
    assert overlaps["leftHand"]["yields"] is False
    assert overlaps["headJump"]["yields"] is True


def test_simple_mode_never_yields(kernel):
    kernel.configure_motions(HANDS_UP)
    kernel.configure_zone_trigger_mode("simple")
    assert kernel.status()["zone_overlaps"]["leftHand"]["yields"] is False


# ---------- 系统功能：要稳住 ----------

def test_a_system_function_zone_needs_a_steady_hold(kernel, monkeypatch):
    calls = []
    kernel.configure_system_action_handler(lambda target, trigger: calls.append(target))
    kernel.configure_bindings({"zones": {"leftHand": {"action": {"type": "system", "target": "OUTPUT.TOGGLE"}}}})
    feed = start(kernel, monkeypatch)
    pose = into(kernel, standing(), "leftHand", .12)
    feed(pose, int(0.3 * FPS))
    assert not kernel.zone_state["leftHand"]["pressed"]
    zone = kernel.status()["zones"]["leftHand"]
    assert zone["phase"] == "pending" and 0 < zone["progress"] < 1
    feed(pose, int(SYSTEM_HOLD_S * FPS) + 2)
    assert kernel.zone_state["leftHand"]["pressed"]


def test_a_system_function_zone_waits_while_any_motion_is_going(kernel, monkeypatch):
    kernel.configure_motions([{"id": "squat", "enabled": True, "type": "gamepad", "target": "Y"}])
    kernel.configure_bindings({"zones": {"leftHand": {"action": {"type": "system", "target": "ZONES.FREEZE"}}}})
    feed = start(kernel, monkeypatch)
    kernel.motion_raw["squat"] = True
    feed(into(kernel, standing(), "leftHand", .12), int(SYSTEM_HOLD_S * FPS) + 5)
    assert not kernel.zone_state["leftHand"]["pressed"]
    assert not kernel.zones_frozen


# ---------- 疑似误按，提示去录 ----------

def test_repeated_misfires_for_an_unrecorded_motion_suggest_recording_it(kernel, monkeypatch):
    kernel.configure_motions([{"id": "squat", "enabled": True, "type": "gamepad", "target": "Y"}])
    feed = start(kernel, monkeypatch)
    for _ in range(3):
        feed(into(kernel, standing(), "leftHand", .12), 2)
        kernel.motion_raw["squat"] = True
        feed(standing(), 3)
        kernel.motion_raw["squat"] = False
        feed(standing(), 60)
    hint = kernel.status()["zone_misfire_hint"]
    assert hint == {"trigger": "motion.squat", "zones": ["leftHand"], "count": 3}
    kernel.configure_zone_learning({"motion.squat": {}}, None)
    assert kernel.status()["zone_misfire_hint"] is None, "录过了就不再提示"


# ---------- 判断用的零件 ----------

def test_sweep_score_reads_the_rest_of_the_body():
    assert sweep_score(0.1, 3.0) == 0.0, "只有这只手在动"
    assert sweep_score(1.2, 1.2) == 1.0, "两只手一样快地一起动，哪怕不快"
    assert sweep_score(2.5, 3.0) == 1.0, "别的地方在大动作"
    assert sweep_score(0.3, 0.2) < 0.35, "两只手都停住了"


def test_kinematics_measures_speed_in_torsos_per_second():
    kin = Kinematics()
    pose = _standing_pose()
    frame = body_frame(pose, 640, 480)
    kin.update(pose, frame, 0.0)
    moved = _standing_pose(left_wrist=(.40 + .1 * frame["ux"], .66))
    kin.update(moved, body_frame(moved, 640, 480), 0.1)
    assert kin.speed("left_wrist") == pytest.approx(1.0, rel=.05)
    assert kin.speed("right_wrist") == pytest.approx(0.0, abs=1e-6)
    assert kin.speed("hip") == pytest.approx(0.0, abs=1e-6)


def test_snippet_bank_votes_with_the_nearest_recordings():
    bank = SnippetBank()
    for i in range(4):
        bank.add(Snippet("leftHand", "intent", "", f"press{i}", [(0.0, [0.0, 0.0])]))
        bank.add(Snippet("leftHand", "sweep", "motion.hands_up", f"up{i}", [(0.0, [5.0, 5.0])]))
    assert bank.sweep_probability("leftHand", 0.0, [0.1, 0.0], ("motion.hands_up",)) < 0.2
    assert bank.sweep_probability("leftHand", 0.0, [4.9, 5.0], ("motion.hands_up",)) > 0.8
    assert bank.sweep_probability("leftHand", 0.0, [4.9, 5.0], ()) is None, "没绑键的动作不算"
    bank.exclude = "press0"
    assert bank.counts("leftHand", ("motion.hands_up",)) == (3, 4)


# ---------- 头顶区（从原来的让路测试搬过来） ----------

def test_standing_up_from_a_front_facing_squat_is_not_a_jump(monkeypatch):
    """正对镜头下蹲时躯干在画面上几乎不变短，以前靠"躯干变短"判断蹲着，头顶区就
    跟着人往下走，站起来的那一下鼻子穿过它——真人录像里按住了 0.6~0.9 秒。"""
    def squat(depth):
        pose = _standing_pose()
        for name in ("nose", "left_ear", "right_ear", "left_shoulder", "right_shoulder",
                     "left_wrist", "right_wrist", "left_hip", "right_hip"):
            pose[name]["y"] += depth
        for name in ("left_knee", "right_knee"):
            pose[name]["y"] += .20 * depth
        pose["left_knee"]["x"] -= .50 * depth
        pose["right_knee"]["x"] += .50 * depth
        return pose

    kernel = ControlKernel(KernelOutput())
    try:
        feed = _zone_feeder(kernel, monkeypatch)
        feed(_standing_pose(), 40)
        poses = ([squat(.12 * (frame + 1) / 12) for frame in range(12)]
                 + [squat(.12)] * 45
                 + [squat(.12 * (1 - (frame + 1) / 8)) for frame in range(8)]
                 + [_standing_pose()] * 10)
        pressed = []
        for frame, pose in enumerate(poses):
            feed(pose)
            if kernel.zone_state["headJump"]["pressed"]:
                pressed.append(frame)
        assert not pressed, f"站起来按到了跳：第 {pressed} 帧"
    finally:
        kernel.close()
