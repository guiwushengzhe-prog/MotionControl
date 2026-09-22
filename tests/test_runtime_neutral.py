"""运行参考只在安全事件后建立，动作过程中不追头。"""
import math
import pytest
from motioncontrol.head_control import HeadController, HeadEstimate, MULTI2D_SCALE, MULTI2D_USE


def ready(tmp_path):
    c=HeadController(tmp_path/'head.json')
    c.config.update(horizontal_algorithm='gesture_v188', invert_x=False)
    c.calibrated=True
    c.center_yaw=0.0
    c.center_pitch=0.0
    c.noise_yaw=0.05
    c.frozen22_calibration_valid=True
    c.frozen22_cal_sigma_deg=.05
    c.noise_world_rigid_yaw=.05
    c.center_multi2d_proxy=(0.,)*7
    c.noise_multi2d_proxy=(0.,)*7
    return c


def sample(c,t,y,p=0.):
    c.control_yaw=y
    c.raw=HeadEstimate(True,yaw=y,pitch=p,yaw_proxy=y*.01)
    c.frozen22_yaw_median=y
    c.current_world_rigid_yaw=y
    v=[0.]*7
    for i,sc in zip(MULTI2D_USE,MULTI2D_SCALE): v[i]=sc*y
    c.current_multi2d_proxy=tuple(v)
    return c._capture_runtime_neutral(t)


def test_snapshot_atomic_and_calibration_immutable(tmp_path):
    c=ready(tmp_path); c._arm_runtime_neutral()
    for i in range(13): assert not sample(c,1+i*.04,5.)
    assert sample(c,1.52,5.)
    assert c.center_yaw==0 and c.center_multi2d_proxy==(0.,)*7
    assert c._runtime_neutral['main']==c._runtime_neutral['world']==c._runtime_neutral['frozen']==5.
    assert c._runtime_neutral['proxy']==.05
    assert c._runtime_neutral['multi']==c.current_multi2d_proxy
    assert c._horizontal_evidence_scale(.4,1.6,'gesture_v188')==0


@pytest.mark.parametrize('direction',[-1,1])
def test_slow_turn_after_snapshot_keeps_reference_and_votes(tmp_path,direction):
    c=ready(tmp_path); c._arm_runtime_neutral()
    for i in range(15): sample(c,1+i*.04,5.)
    for i in range(80):
        sample(c,2+i*.04,5+direction*.05*i)
        scale=c._horizontal_evidence_scale(direction*.4,2+i*.04,'gesture_v188')
    assert c._runtime_neutral['main']==5 and c._runtime_neutral_epoch==1
    assert scale==1
    # 偏头保持也没有资格更新参考。
    for i in range(50): sample(c,6+i*.04,9.)
    assert c._runtime_neutral['main']==5


def test_initial_motion_and_gaps_do_not_capture(tmp_path):
    c=ready(tmp_path); c._arm_runtime_neutral()
    for i in range(50): assert not sample(c,1+i*.04,i*.04)
    for i in range(20): assert not sample(c,4+i*.2,5.)
    assert c._runtime_neutral_pending


def test_successful_calibration_supplies_reference_before_optional_refresh(tmp_path):
    c = ready(tmp_path)
    c.center_yaw_samples = [5.] * 32
    c.center_pitch_samples = [2.] * 32
    c.center_yaw_proxy_samples = [.05] * 32
    c._finish_center(True)
    assert c.calibrated
    assert c._runtime_neutral['main'] == c.center_yaw == 5.
    assert c._runtime_neutral['proxy'] == c.center_yaw_proxy == .05
    assert c._runtime_neutral_pending


def test_pitch_only_still_blocked(tmp_path):
    c=ready(tmp_path); c._arm_runtime_neutral()
    for i in range(15): sample(c,1+i*.04,5.)
    for i in range(10):
        sample(c,2+i*.04,7.,p=8.)
        c.current_world_rigid_yaw=5.
        assert c._horizontal_evidence_scale(.4,2+i*.04,'gesture_v188')==0
    assert c.v188_pitch_guard_active


def test_runtime_update_zero_before_snapshot_and_return_rearms(tmp_path, monkeypatch):
    c=ready(tmp_path)
    class Estimator:
        def estimate(self,pose,*args): return HeadEstimate(True,yaw=pose['yaw'],pitch=0.,yaw_proxy=0.)
    c.estimator=Estimator()
    monkeypatch.setattr(c._head_point_filter,'apply',lambda pose,*args:pose)
    monkeypatch.setattr(c._yaw_filter,'apply',lambda v,*args:v)
    monkeypatch.setattr(c,'_horizontal_evidence_scale',lambda *args:1.)
    c._arm_runtime_neutral()
    for i in range(15):
        assert c.update({'yaw':5.},640,480,now=1+i*.04)[0]==0
    assert c._runtime_neutral['main']==5.
    outputs=[]
    for i in range(30): outputs.append(c.update({'yaw':5+i*.3},640,480,now=2+i*.04)[0])
    assert any(v>0 for v in outputs)
    for i in range(20): c.update({'yaw':13.7},640,480,now=3.2+i*.04)
    assert c._runtime_neutral_epoch==1
    # 回中锁解除是独立事件；仅在该事件后允许第二次快照。
    c._x_intent_v153._return_latched=True
    c._x_intent_v153._return_from_direction=1
    for i in range(45):
        assert c.update({'yaw':6.},640,480,now=4+i*.04)[0]==0
    assert c._runtime_neutral_epoch==2
    assert c._runtime_neutral['main']==6.


@pytest.mark.parametrize('direction', [-1, 1])
def test_noisy_refresh_keeps_existing_reference_and_new_turn_cancels_it(tmp_path, monkeypatch, direction):
    c = ready(tmp_path)
    c._arm_runtime_neutral()
    for i in range(15):
        sample(c, 1 + i * .04, 5.)
    snapshot = c._runtime_neutral.copy()

    class Estimator:
        def estimate(self, pose, *args):
            return HeadEstimate(True, yaw=pose['yaw'], pitch=pose['pitch'], yaw_proxy=0.)

    c.estimator = Estimator()
    monkeypatch.setattr(c._head_point_filter, 'apply', lambda pose, *args: pose)
    monkeypatch.setattr(c._yaw_filter, 'apply', lambda value, *args: value)
    monkeypatch.setattr(c, '_horizontal_evidence_scale', lambda *args: 1.)
    c._arm_runtime_neutral()
    # Auxiliary jitter prevents a fresh snapshot while the head stays neutral.
    for i in range(30):
        assert c.update({'yaw': 5., 'pitch': (-1.) ** i}, 640, 480, now=2 + i * .04)[0] == 0
    assert c._runtime_neutral_pending
    outputs = [c.update({'yaw': 5 + direction * i * .3, 'pitch': (-1.) ** i},
                        640, 480, now=3.2 + i * .04)[0] for i in range(30)]
    assert any(direction * value > 0 for value in outputs)
    assert not c._runtime_neutral_pending
    # Stopping after that new outward turn must not silently move the centre.
    for i in range(30):
        c.update({'yaw': 5 + direction * 29 * .3, 'pitch': 0.}, 640, 480, now=4.4 + i * .04)
    assert c._runtime_neutral == snapshot
    assert c._runtime_neutral_epoch == 1


@pytest.mark.parametrize('direction', [-1, 1])
def test_settled_return_rebases_before_new_turn_towards_old_center(tmp_path, monkeypatch, direction):
    c = ready(tmp_path)
    c._arm_runtime_neutral()
    for i in range(15):
        sample(c, 1 + i * .04, 5.)

    class Estimator:
        def estimate(self, pose, *args):
            return HeadEstimate(True, yaw=pose['yaw'], pitch=0., yaw_proxy=0.)

    c.estimator = Estimator()
    monkeypatch.setattr(c._head_point_filter, 'apply', lambda pose, *args: pose)
    monkeypatch.setattr(c._yaw_filter, 'apply', lambda value, *args: value)
    monkeypatch.setattr(c, '_horizontal_evidence_scale', lambda *args: 1.)
    neutral = 5. + direction * 12.
    c._x_intent_v153._return_latched = True
    c._x_intent_v153._return_from_direction = direction
    for i in range(15):
        assert c.update({'yaw': neutral}, 640, 480, now=2 + i * .04)[0] == 0
    assert c._runtime_neutral['main'] == neutral
    assert not c._runtime_neutral_pending
    assert c._runtime_neutral_epoch == 2
    # Only travel 3 degrees back towards the old centre, never reaching it.
    outputs = [c.update({'yaw': neutral - direction * i * .1}, 640, 480,
                        now=2.6 + i * .04)[0] for i in range(31)]
    assert any(-direction * value > 0 for value in outputs)
    assert c.center_yaw == 0.


def test_return_rebase_uses_joint_medians_even_with_noisy_auxiliary(tmp_path):
    c = ready(tmp_path)
    for i in range(10):
        c.control_yaw = 6.
        c.raw = HeadEstimate(True, yaw=6., pitch=0., yaw_proxy=.06)
        c.frozen22_yaw_median = 6.
        c.current_world_rigid_yaw = 6. + (-1.) ** i
        c.current_multi2d_proxy = tuple(scale * 6 for scale in (1.,) * 7)
        c._capture_runtime_neutral(1 + i * .04)
    assert c._rebase_runtime_neutral_after_return(1.36)
    assert c._runtime_neutral['main'] == c._runtime_neutral['frozen'] == 6.
    assert c._runtime_neutral['world'] == 6.
    assert c._runtime_neutral['proxy'] == .06
    assert c._runtime_neutral['multi'] == (6.,) * 7
