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


@pytest.mark.parametrize('stance', [0.0, .10])
def test_small_alternating_march_is_detected_without_foot_buttons(monkeypatch, stance):
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


def test_noise_one_sided_lifts_and_common_body_rise_are_not_marching(monkeypatch):
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


def test_crossed_foot_and_tracking_loss_do_not_create_a_march_pair(monkeypatch):
    kernel = ControlKernel(KernelOutput())
    try:
        feed = _zone_feeder(kernel, monkeypatch)
        feed(_standing_pose(), 20)
        feed(lifted('left', outward=-.15), 5)
        assert not kernel.zone_state['leftFoot']['pressed']
        feed({}, 2)
        feed(lifted('right'), 5)
        assert 'march' not in kernel.motion_active
        feed(_standing_pose(), 5)
        feed(lifted('left'), 5)
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
