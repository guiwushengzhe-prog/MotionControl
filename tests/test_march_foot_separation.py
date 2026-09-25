import copy

import pytest

from motioncontrol.control_kernel import ControlKernel
from test_minimal_controls import KernelOutput, _standing_pose, _zone_feeder


def lifted(side, *, knee=.10, ankle=.08, outward=0.0, stance=.0):
    pose = _standing_pose(left_ankle=(.46 - stance, .95), right_ankle=(.54 + stance, .95))
    pose['left_knee']['x'] -= stance * .5
    pose['right_knee']['x'] += stance * .5
    pose[side + '_knee']['y'] -= knee * .24
    pose[side + '_ankle']['y'] -= ankle * .24
    pose[side + '_ankle']['x'] += outward * (-1 if side == 'left' else 1)
    return pose


def with_elbows(pose, *, left=(.40, .55), right=(.60, .55)):
    pose['left_elbow'] = {'x': left[0], 'y': left[1], 'score': .95}
    pose['right_elbow'] = {'x': right[0], 'y': right[1], 'score': .95}
    return pose


@pytest.mark.parametrize('stance', [0.0, .10])
def test_small_alternating_march_is_detected_without_foot_buttons(monkeypatch, stance):
    """单脚第一步只记节奏，左右交替后才开始前进。"""
    kernel = ControlKernel(KernelOutput())
    try:
        feed = _zone_feeder(kernel, monkeypatch)
        rest = lifted('left', knee=0, ankle=0, stance=stance)
        feed(rest, 20)
        for index, side in enumerate(('left', 'right', 'left', 'right')):
            feed(lifted(side, stance=stance), 5)
            assert ('march' in kernel.motion_active) == (index > 0)
            assert not kernel.zone_state['leftFoot']['pressed']
            assert not kernel.zone_state['rightFoot']['pressed']
            feed(rest, 4)
        feed(rest, 35)
        assert 'march' not in kernel.motion_active
    finally:
        kernel.close()


@pytest.mark.parametrize('side', ['left', 'right'])
@pytest.mark.parametrize('mirrored', [False, True])
def test_shallow_side_kick_enters_visible_follow_zone(monkeypatch, side, mirrored):
    kernel = ControlKernel(KernelOutput())
    try:
        feed = _zone_feeder(kernel, monkeypatch)
        rest, kick = _standing_pose(), lifted(side, outward=.07)
        if mirrored:
            for pose in (rest, kick):
                for point in pose.values():
                    point['x'] = 1.0 - point['x']
        feed(rest, 20)
        feed(kick, 5)
        assert kernel.zone_state[side + 'Foot']['pressed']
        assert kernel._point_in_rect(kick[side + '_ankle'], kernel.zone_rects[side + 'Foot'])
        assert 'march' not in kernel.motion_active
        feed(rest, 4)
        assert not kernel.zone_state[side + 'Foot']['pressed']
    finally:
        kernel.close()


def test_side_kick_does_not_move_the_neutral_foot_anchor(monkeypatch):
    """抬脚前的横向过渡不能把侧踢的中性位置一起推走。"""
    kernel = ControlKernel(KernelOutput())
    try:
        feed = _zone_feeder(kernel, monkeypatch)
        rest = _standing_pose()
        feed(rest, 20)
        neutral = kernel.foot_base['left']

        # 脚还没离地，但已经向侧面滑出；这是录像里曾让 neutral 追随目标的过渡段。
        for outward in (.08, .16, .24):
            feed(lifted('left', knee=0, ankle=0, outward=outward), 1)
        assert kernel.foot_base['left'] == pytest.approx(neutral)

        feed(lifted('left', outward=.24), 5)
        assert kernel.zone_state['leftFoot']['pressed']
    finally:
        kernel.close()


def test_noise_and_common_body_rise_are_not_marching(monkeypatch):
    """晃一下、整个人起伏、一帧跳点和同一条腿反复抬起都不能启动踏步。"""
    kernel = ControlKernel(KernelOutput())
    try:
        feed = _zone_feeder(kernel, monkeypatch)
        feed(_standing_pose(), 15)
        for side in ('left', 'right') * 3:
            feed(lifted(side, knee=.04, ankle=.03), 4)
            assert 'march' not in kernel.motion_active
        for _ in range(3):
            feed(lifted('left'), 5)
            feed(_standing_pose(), 4)
            assert 'march' not in kernel.motion_active
        for dy in (-.03, -.05, 0.0):
            feed(_standing_pose(dy=dy), 4)
            assert 'march' not in kernel.motion_active
        # 单帧大跳点不能成为有效的另一侧步。
        feed(lifted('right'), 1)
        feed(_standing_pose(), 4)
        assert 'march' not in kernel.motion_active
    finally:
        kernel.close()


def test_fixed_circle_stays_put_requires_outward_contact_and_accepts_foot_segment(monkeypatch):
    kernel = ControlKernel(KernelOutput())
    try:
        feed = _zone_feeder(kernel, monkeypatch)
        zones = {'leftFoot': {'shape': 'circle', 'cx': .38, 'cy': .90, 'r': .012}}
        kernel.configure_scene_layout({'zones': zones})
        original = copy.deepcopy(kernel.fixed_zones)
        feed(_standing_pose(), 20)
        # 脚踝和脚尖都在圆外，但脚段实际穿过圆。
        kick = lifted('left', outward=.06, ankle=.05 / .24)
        kick['left_foot_index'] = {'x': .36, 'y': .90, 'score': .95}
        feed(kick, 4)
        assert kernel.zone_state['leftFoot']['pressed']
        assert not kernel._point_in_circle(kick['left_ankle'], zones['leftFoot'])
        assert not kernel._point_in_circle(kick['left_foot_index'], zones['leftFoot'])
        feed(_standing_pose(), 4)
        # 向外踢但未触圈时，不能越过可见边界触发。
        feed(lifted('left', outward=.10, ankle=.08), 4)
        assert not kernel.zone_state['leftFoot']['pressed']
        assert kernel.fixed_zones == original
    finally:
        kernel.close()


def test_wide_stance_march_inside_fixed_circle_does_not_count_as_side_kick(monkeypatch):
    kernel = ControlKernel(KernelOutput())
    try:
        feed = _zone_feeder(kernel, monkeypatch)
        kernel.configure_scene_layout({'zones': {'leftFoot': {'shape': 'circle', 'cx': .36, 'cy': .93, 'r': .035}}})
        feed(lifted('left', knee=0, ankle=0, stance=.10), 20)
        for side in ('left', 'right', 'left'):
            feed(lifted(side, stance=.10), 5)
            assert not kernel.zone_state['leftFoot']['pressed']
        assert 'march' in kernel.motion_active
    finally:
        kernel.close()


def test_a_crossed_foot_does_not_press_the_foot_zone(monkeypatch):
    kernel = ControlKernel(KernelOutput())
    try:
        feed = _zone_feeder(kernel, monkeypatch)
        feed(_standing_pose(), 20)
        feed(lifted('left', outward=-.15), 5)
        assert not kernel.zone_state['leftFoot']['pressed']
        feed({}, 2)
        feed(_standing_pose(), 50)
        assert 'march' not in kernel.motion_active, '停下来之后不该还在走'
        feed(lifted('right'), 5)
        assert 'march' not in kernel.motion_active, '只有一只脚完成动作不能启动踏步'
        feed(_standing_pose(), 4)
        feed(lifted('left'), 5)
        assert 'march' in kernel.motion_active
    finally:
        kernel.close()


@pytest.mark.parametrize('side', ['left', 'right'])
def test_lifting_the_lower_leg_back_is_calf_lift_not_a_step(monkeypatch, side):
    """小腿向后抬起：脚踝抬到膝盖那么高，膝盖没怎么动。正面看腿几乎是直的，
    以前按膝角小于 115° 判定，这样做几乎触发不了。"""
    kernel = ControlKernel(KernelOutput())
    try:
        # 小腿后抬绑了键，踏步才要等脚抬到最高再定是哪一个。
        kernel.configure_motions([{'id': 'calf_back', 'enabled': True, 'type': 'gamepad', 'target': 'B'},
                                  {'id': 'march', 'enabled': True, 'type': 'gamepad', 'target': 'LS_UP'}])
        feed = _zone_feeder(kernel, monkeypatch)
        feed(_standing_pose(), 20)
        for rise in (.10, .30, .50):
            feed(lifted(side, knee=.02, ankle=rise), 1)
        feed(lifted(side, knee=.02, ankle=.60), 6)
        assert 'calf_back' in kernel.motion_active
        assert 'march' not in kernel.motion_active, '往上抬的途中不能先算成一步'
        feed(_standing_pose(), 6)
        assert 'calf_back' not in kernel.motion_active
        assert 'march' not in kernel.motion_active, '放下来的时候也不能算成一步'
    finally:
        kernel.close()


def test_incomplete_knee_to_elbow_attempt_is_not_a_step(monkeypatch):
    """肘还差一点但明显在靠近时，不能在脚踝峰值处补出踏步。"""
    kernel = ControlKernel(KernelOutput())
    try:
        kernel.configure_motions([
            {'id': 'cross_knee_elbow', 'enabled': True, 'type': 'gamepad', 'target': 'X'},
            {'id': 'march', 'enabled': True, 'type': 'gamepad_axis', 'target': 'LS_UP'},
        ])
        feed = _zone_feeder(kernel, monkeypatch)
        feed(with_elbows(_standing_pose()), 20)
        for ankle in (.10, .22, .30, .29):
            # 左膝已抬起；右肘距离略超过真正碰到的 0.95，但仍在候选余量内。
            feed(with_elbows(lifted('left', knee=.30, ankle=ankle), right=(.60, .55)))
        assert 'cross_knee_elbow' not in kernel.motion_active
        assert 'march' not in kernel.motion_active
    finally:
        kernel.close()


def test_completed_knee_to_elbow_attempt_still_wins_over_march(monkeypatch):
    """真正碰到对侧肘时仍认提膝碰肘，并且不产生踏步。"""
    kernel = ControlKernel(KernelOutput())
    try:
        kernel.configure_motions([
            {'id': 'cross_knee_elbow', 'enabled': True, 'type': 'gamepad', 'target': 'X'},
            {'id': 'march', 'enabled': True, 'type': 'gamepad_axis', 'target': 'LS_UP'},
        ])
        feed = _zone_feeder(kernel, monkeypatch)
        feed(with_elbows(_standing_pose()), 20)
        for ankle in (.10, .22, .30, .29):
            pose = lifted('left', knee=.30, ankle=ankle)
            # 左膝 (.46, .728) 与右肘重合，满足真正碰肘条件。
            feed(with_elbows(pose, right=(.46, .728)))
        assert 'cross_knee_elbow' in kernel.motion_active
        assert 'march' not in kernel.motion_active
    finally:
        kernel.close()


def test_a_real_step_ankle_rises_less_than_calf_lift(monkeypatch):
    """真人录像：踏步脚踝抬 0.07~0.29 个躯干，小腿后抬 0.57 以上。"""
    kernel = ControlKernel(KernelOutput())
    try:
        kernel.configure_motions([{'id': 'calf_back', 'enabled': True, 'type': 'gamepad', 'target': 'B'}])
        feed = _zone_feeder(kernel, monkeypatch)
        feed(_standing_pose(), 20)
        for rise in (.10, .22, .25, .18):
            feed(lifted('left', knee=.04, ankle=rise), 1)
        assert 'march' not in kernel.motion_active
        feed(_standing_pose(), 4)
        for rise in (.10, .22, .25, .18):
            feed(lifted('right', knee=.04, ankle=rise), 1)
        assert 'march' in kernel.motion_active
        assert 'calf_back' not in kernel.motion_active
    finally:
        kernel.close()


def test_a_knee_lift_is_a_step_not_calf_lift(monkeypatch):
    """踏步是膝盖带着脚一起上来。"""
    kernel = ControlKernel(KernelOutput())
    try:
        feed = _zone_feeder(kernel, monkeypatch)
        feed(_standing_pose(), 20)
        feed(lifted('left', knee=.30, ankle=.30), 6)
        assert 'march' not in kernel.motion_active
        assert 'calf_back' not in kernel.motion_active
        feed(_standing_pose(), 4)
        feed(lifted('right', knee=.30, ankle=.30), 6)
        assert 'march' in kernel.motion_active
        assert 'calf_back' not in kernel.motion_active
    finally:
        kernel.close()


def test_swaying_on_the_spot_is_neither(monkeypatch):
    """站着晃，脚踝最多离地 0.055 个躯干（真人录像）。"""
    kernel = ControlKernel(KernelOutput())
    try:
        feed = _zone_feeder(kernel, monkeypatch)
        feed(_standing_pose(), 20)
        for side in ('left', 'right') * 3:
            feed(lifted(side, knee=0, ankle=.05), 5)
            feed(_standing_pose(), 3)
        assert 'calf_back' not in kernel.motion_active
        assert 'march' not in kernel.motion_active
    finally:
        kernel.close()


def test_a_small_step_counts(monkeypatch):
    """小的交替步脚踝只抬 0.07~0.09，第二步后仍能开始前进。"""
    kernel = ControlKernel(KernelOutput())
    try:
        feed = _zone_feeder(kernel, monkeypatch)
        feed(_standing_pose(), 20)
        feed(lifted('left', knee=0, ankle=.09), 3)
        assert 'march' not in kernel.motion_active
        feed(_standing_pose(), 4)
        feed(lifted('right', knee=0, ankle=.09), 3)
        assert 'march' in kernel.motion_active
    finally:
        kernel.close()


def test_a_jump_does_not_cut_the_walk_in_mid_air(monkeypatch):
    """Both feet leave the ground together, so a jump shows no alternation.

    The walk used to expire 0.70s after the last step regardless, which cut the
    stick in mid-air on any jump longer than that.  A jump is a break in the
    rhythm, not a decision to stop, so a walk already running is held through
    it -- while a standing jump must still start no walk of its own.
    """
    def run(air_seconds, *, walk_first):
        kernel = ControlKernel(KernelOutput())
        try:
            feed = _zone_feeder(kernel, monkeypatch)
            rest = _standing_pose()
            feed(rest, 20)
            if walk_first:
                for side in ('left', 'right', 'left', 'right'):
                    feed(lifted(side), 5)
                    feed(rest, 4)
                assert 'march' in kernel.motion_active
            return [
                'march' in kernel.motion_active
                for _ in range(int(air_seconds * 30))
                if not feed(_standing_pose(dy=-.08))
            ]
        finally:
            kernel.close()

    assert all(run(0.8, walk_first=True)), 'walking must survive a realistic jump'
    assert not any(run(0.5, walk_first=False)), 'a standing jump must not start a walk'


def test_a_pose_bound_to_hold_is_actually_held(monkeypatch):
    """The kernel used to rewrite every pose binding to a single tap.

    Three separate places forced it -- two in the profile layer and this one in
    the dispatcher -- so "hold while the pose lasts" was unreachable no matter
    what was saved.  The recognizer releases a pose exactly like a motion, so a
    held output ends with the pose.
    """
    class Recorder(KernelOutput):
        def __init__(self):
            super().__init__()
            self.holds = []

        def set_action_holds(self, holds, source_group='controls'):
            self.holds = [item.get('id') for item in holds or []]

    output = Recorder()
    kernel = ControlKernel(output)
    try:
        feed = _zone_feeder(kernel, monkeypatch)
        kernel.configure_scene_layout({'zones': {}, 'vertical_look': {'enabled': False}})
        kernel.configure_bindings({'poses': {'hands_cross': {
            'action': {'type': 'gamepad', 'target': ['LB', 'LS_UP'], 'behavior': 'hold'},
        }}})
        feed(_standing_pose(), 10)
        assert 'pose.hands_cross' not in output.holds

        with kernel._lock:
            kernel.pose_active = {'hands_cross'}
            kernel._dispatch_controls_locked(0.0)
        assert 'pose.hands_cross' in output.holds, 'a pose asking to hold must reach the hold path'

        with kernel._lock:
            kernel.pose_active = set()
            kernel._dispatch_controls_locked(0.0)
        assert 'pose.hands_cross' not in output.holds
    finally:
        kernel.close()


# --- 脚的下缘和站立基准（真人录像 pose-20260925-003039 / 020232 里看到的几种情形） ---


def test_pelvis_tilting_during_a_side_kick_is_not_a_step(monkeypatch):
    """左脚往外伸时骨盆一歪，右脚相对自己的胯就"抬高"了。两只脚的下缘其实一样
    高，右脚一直踩在地上，不能算成迈了一步。"""
    kernel = ControlKernel(KernelOutput())
    try:
        feed = _zone_feeder(kernel, monkeypatch)
        feed(_standing_pose(), 20)
        for tilt, outward in ((.010, .02), (.020, .04), (.025, .06), (.025, .08)):
            pose = _standing_pose()
            pose['left_hip']['y'] -= tilt          # 伸脚那一侧的胯往上提
            pose['left_ankle']['x'] -= outward     # 左脚贴地往外滑
            feed(pose, 3)
            assert 'march' not in kernel.motion_active
    finally:
        kernel.close()


def test_a_small_step_counts_when_the_camera_sees_the_feet_at_different_heights(monkeypatch):
    """手机斜着放，站着时右脚在画面上就比左脚高一截（录像里 0.05 个躯干）。
    左脚那一步本身抬了 0.08，比的时候要先扣掉这一截，不然只剩 0.03，认不出来。"""
    kernel = ControlKernel(KernelOutput())
    try:
        feed = _zone_feeder(kernel, monkeypatch)
        offset = .05 * .24
        rest = lifted('left', knee=0, ankle=0)
        rest['right_ankle']['y'] -= offset
        feed(rest, 20)
        step = lifted('left', knee=.02, ankle=.08)
        step['right_ankle']['y'] -= offset
        feed(step, 4)
        assert 'march' not in kernel.motion_active
        feed(rest, 4)
        step = lifted('right', knee=.02, ankle=.08)
        step['right_ankle']['y'] -= offset
        feed(step, 4)
        assert 'march' in kernel.motion_active
    finally:
        kernel.close()


def test_the_standing_reference_follows_a_new_stance(monkeypatch):
    """站宽了、挪了位置，站稳一秒左右基准就跟过去。以前只在第一次站好时记一下，
    之后原地抬脚都像是往外伸脚：踏步认不出、脚区反倒被踩到。"""
    kernel = ControlKernel(KernelOutput())
    try:
        feed = _zone_feeder(kernel, monkeypatch)
        feed(_standing_pose(), 20)
        wide = lifted('left', knee=0, ankle=0, stance=.05)
        feed(wide, 60)
        feed(lifted('left', stance=.05), 5)
        assert 'march' not in kernel.motion_active
        feed(wide, 4)
        feed(lifted('right', stance=.05), 5)
        assert 'march' in kernel.motion_active
        assert not kernel.zone_state['leftFoot']['pressed']
    finally:
        kernel.close()


def test_the_foot_zone_sits_on_the_heel_and_toe_line(monkeypatch):
    """地面线按脚跟、脚尖算。脚踝离地还有一截，按脚踝算的话脚区整体偏高。"""
    kernel = ControlKernel(KernelOutput())
    try:
        feed = _zone_feeder(kernel, monkeypatch)
        ankle_only = _standing_pose()
        feed(ankle_only, 5)
        high = kernel.zone_rects['leftFoot']['y2']
        with_feet = _standing_pose()
        for side, x in (('left', .46), ('right', .54)):
            with_feet[side + '_heel'] = {'x': x, 'y': .97, 'score': .95}
            with_feet[side + '_foot_index'] = {'x': x, 'y': .975, 'score': .95}
        kernel.zone_rects = {}
        feed(with_feet, 5)
        assert kernel.zone_rects['leftFoot']['y2'] == pytest.approx(high + .025, abs=1e-6)
    finally:
        kernel.close()


def test_a_low_side_kick_with_the_toe_lifted_presses_the_foot_zone(monkeypatch):
    """录像里左脚往外伸得低：脚踝只比另一只高一点，但整只脚都离地了、往外也
    够远。按脚的下缘比高低，这一下要按下去。"""
    kernel = ControlKernel(KernelOutput())
    try:
        feed = _zone_feeder(kernel, monkeypatch)
        rest = _standing_pose()
        for side, x in (('left', .46), ('right', .54)):
            rest[side + '_heel'] = {'x': x, 'y': .97, 'score': .95}
            rest[side + '_foot_index'] = {'x': x - .01 if side == 'left' else x + .01, 'y': .975, 'score': .95}
        feed(rest, 20)
        kick = copy.deepcopy(rest)
        for part in ('ankle', 'heel', 'foot_index'):
            kick['left_' + part]['x'] -= .16
            kick['left_' + part]['y'] -= .02
        feed(kick, 6)
        assert kernel.zone_state['leftFoot']['pressed']
        assert 'march' not in kernel.motion_active
    finally:
        kernel.close()
