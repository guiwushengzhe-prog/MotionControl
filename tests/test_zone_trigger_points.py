"""独立点任选其一，用户连成的一组点必须同时进入，保存与回放不丢失。"""

import pytest

from motioncontrol.control_kernel import ControlKernel, MP_NAMES, RUNTIME_BODY_ZONES
from motioncontrol.intent_library import make_replay_kernel
from motioncontrol_shared.pose_points import POSE_CONNECTIONS, POSE_POINT_LABELS
from motioncontrol_shared.profile_schema import normalize_overrides
from test_game_profiles_v097 import make_store
from test_minimal_controls import KernelOutput, _standing_pose, _zone_feeder


@pytest.fixture
def setup_zone(monkeypatch):
    kernel = ControlKernel(KernelOutput())
    feed = _zone_feeder(kernel, monkeypatch)
    feed(_standing_pose(), 10)
    kernel.set_frozen_zones({name: {'x1': .80, 'x2': .95, 'y1': .10, 'y2': .25}
                             for name in RUNTIME_BODY_ZONES})
    yield kernel, feed
    kernel.close()


def set_choices(kernel, zone, points, segments=()):
    kernel.configure_bindings({'zones': {zone: {
        'action': {'type': 'keyboard', 'target': 'Q'},
        'trigger_points': points, 'trigger_segments': list(segments),
    }}})


def pose_with(**points):
    pose = _standing_pose()
    for name, inside in points.items():
        pose[name] = {'x': .88 if inside else .70, 'y': .17 if inside else .35, 'score': .95}
    return pose


@pytest.mark.parametrize('mode', ['simple', 'smart'])
@pytest.mark.parametrize('zone', list(RUNTIME_BODY_ZONES))
def test_any_selected_point_can_trigger_any_zone(setup_zone, mode, zone):
    kernel, feed = setup_zone
    kernel.configure_zone_trigger_mode(mode)
    set_choices(kernel, zone, ['left_wrist', 'right_wrist'])
    for point in ('left_wrist', 'right_wrist'):
        feed(pose_with(**{point: True}))
        assert kernel.zone_state[zone]['pressed']
        assert kernel.zone_state[zone]['trigger_point'] == point
        feed(pose_with(left_wrist=False, right_wrist=False), 3)
        assert not kernel.zone_state[zone]['pressed']


def test_single_selection_empty_selection_and_low_quality_never_use_unselected_points(setup_zone):
    kernel, feed = setup_zone
    kernel.configure_zone_trigger_mode('simple')
    set_choices(kernel, 'leftHand', ['left_index'])
    feed(pose_with(left_index=False, right_wrist=True), 3)
    assert not kernel.zone_state['leftHand']['pressed']
    # 食指直接使用骨骼点；手腕仍保留旧版跟踪器的失踪恢复规则。
    low_quality = pose_with(left_index=True)
    low_quality['left_index']['score'] = .1
    feed(low_quality, 3)
    assert not kernel.zone_state['leftHand']['pressed']
    feed(pose_with(left_index=True))
    assert kernel.zone_state['leftHand']['pressed']
    set_choices(kernel, 'leftHand', [])
    assert not kernel.zone_state['leftHand']['pressed']
    feed(pose_with(left_index=True, right_wrist=True), 3)
    assert not kernel.zone_state['leftHand']['pressed']


@pytest.mark.parametrize('mode', ['simple', 'smart'])
def test_segment_requires_both_endpoints_in_the_same_frame(setup_zone, mode):
    kernel, feed = setup_zone
    kernel.configure_zone_trigger_mode(mode)
    set_choices(kernel, 'leftHand', [], [['left_wrist', 'left_index']])
    # 线穿过区域或两端先后进去，都不等于两个端点同时进去。
    for pose in (pose_with(left_wrist=True, left_index=False),
                 pose_with(left_wrist=False, left_index=True)):
        feed(pose, 3)
        assert not kernel.zone_state['leftHand']['pressed']
    crossing = pose_with(left_wrist=False, left_index=False)
    crossing['left_wrist'].update(x=.7, y=.17)
    crossing['left_index'].update(x=1., y=.17)
    feed(crossing, 3)
    assert not kernel.zone_state['leftHand']['pressed']
    feed(pose_with(left_wrist=True, left_index=True))
    assert kernel.zone_state['leftHand']['pressed']
    missing = pose_with(left_wrist=True)
    missing.pop('left_index', None)
    feed(missing, 3)
    assert not kernel.zone_state['leftHand']['pressed']


def test_individual_points_and_segments_are_alternatives(setup_zone):
    kernel, feed = setup_zone
    kernel.configure_zone_trigger_mode('simple')
    set_choices(kernel, 'leftHand', ['right_wrist'], [['left_wrist', 'left_index']])
    feed(pose_with(right_wrist=True, left_wrist=False, left_index=False))
    assert kernel.zone_state['leftHand']['pressed']
    feed(pose_with(right_wrist=False, left_wrist=False, left_index=False), 3)
    feed(pose_with(right_wrist=False, left_wrist=True, left_index=True))
    assert kernel.zone_state['leftHand']['pressed']


@pytest.mark.parametrize('mode', ['simple', 'smart'])
def test_user_connected_three_points_require_all_and_cannot_bypass_by_a_member(setup_zone, mode):
    kernel, feed = setup_zone
    kernel.configure_zone_trigger_mode(mode)
    points = ['left_index', 'left_elbow', 'right_index']
    edges = [['left_index', 'left_elbow'], ['left_elbow', 'right_index']]
    set_choices(kernel, 'leftHand', points, edges)
    for omitted in points:
        feed(pose_with(**{point: point != omitted for point in points}), 3)
        assert not kernel.zone_state['leftHand']['pressed']
    feed(pose_with(**dict.fromkeys(points, True)), 3)
    assert kernel.zone_state['leftHand']['pressed']
    # 删掉末端连线后，右食指重新成为独立选择；已有按住状态立即释放。
    set_choices(kernel, 'leftHand', points, edges[:1])
    assert not kernel.zone_state['leftHand']['pressed']
    feed(pose_with(left_index=False, left_elbow=False, right_index=True), 3)
    assert kernel.zone_state['leftHand']['pressed']


def test_disconnected_groups_are_alternatives_and_loops_do_not_split_a_group(setup_zone):
    kernel, feed = setup_zone
    kernel.configure_zone_trigger_mode('simple')
    set_choices(kernel, 'leftHand', [], [['left_index', 'left_elbow'],
        ['left_elbow', 'right_index'], ['right_index', 'left_index'], ['right_elbow', 'right_thumb']])
    assert len(kernel._zone_point_groups_locked('leftHand')) == 2
    feed(pose_with(right_elbow=True, right_thumb=True))
    assert kernel.zone_state['leftHand']['pressed']
    feed(pose_with(right_elbow=False, right_thumb=True, left_index=True, left_elbow=True, right_index=False))
    assert not kernel.zone_state['leftHand']['pressed']


def test_arbitrary_edges_are_canonicalized_without_anatomical_restrictions():
    binding = {'action': {'type': 'keyboard', 'target': 'Q'},
               'trigger_points': [], 'trigger_segments': [['left_ankle', 'nose'], ['nose', 'left_ankle']]}
    normalized = normalize_overrides({'zone.rightFoot': binding})
    assert normalized['zone.rightFoot']['trigger_segments'] == [['nose', 'left_ankle']]


def test_selected_steering_hand_is_excluded_even_in_a_different_zone(setup_zone, monkeypatch):
    kernel, feed = setup_zone
    kernel.configure_zone_trigger_mode('simple')
    monkeypatch.setattr(kernel.hand_mouse_controller, 'owns_hand', lambda side: side == 'left')
    set_choices(kernel, 'rightFoot', ['left_wrist', 'right_wrist'])
    feed(pose_with(left_wrist=True, right_wrist=False), 3)
    assert not kernel.zone_state['rightFoot']['pressed']
    feed(pose_with(left_wrist=False, right_wrist=True))
    assert kernel.zone_state['rightFoot']['pressed']


def test_saved_choices_survive_game_switch_restart_and_cloud_normalization(tmp_path):
    store = make_store(tmp_path)
    choices = {'zone.leftHand': {'action': {'type': 'keyboard', 'target': 'Q'},
                               'trigger_points': ['left_elbow', 'left_elbow'],
                               'trigger_segments': [['left_index', 'left_wrist']]}}
    store.set_overrides(normalize_overrides(choices), 'generic-xbox')
    store.select('demo')
    reloaded = make_store_existing(tmp_path)
    binding = reloaded.select('generic-xbox')['bindings']['zones']['leftHand']
    assert binding['trigger_points'] == ['left_elbow']
    assert binding['trigger_segments'] == [['left_wrist', 'left_index']]
    disabled = {'zone.leftHand': {'disabled': True, 'trigger_points': [],
                                 'trigger_segments': [['left_wrist', 'left_index']]}}
    assert normalize_overrides(disabled) == disabled


def make_store_existing(path):
    from motioncontrol.game_profiles import GameProfileStore
    return GameProfileStore(path)


def test_replay_uses_the_same_point_and_segment_choices(setup_zone):
    kernel, _ = setup_zone
    set_choices(kernel, 'leftHand', ['left_elbow'], [['left_wrist', 'left_index']])
    replay = make_replay_kernel(kernel.replay_snapshot())
    try:
        assert replay._zone_point_groups_locked('leftHand') == kernel._zone_point_groups_locked('leftHand')
    finally:
        replay.close()


@pytest.mark.parametrize('field,value', [
    ('trigger_points', 'left_wrist'), ('trigger_points', ['unknown']),
    ('trigger_segments', [['left_wrist']]), ('trigger_segments', [['nose', 'nose']]),
    ('trigger_segments', [['nose', 'unknown']]),
    ('trigger_segments', [['left_wrist', {}]]),
])
def test_invalid_choices_are_refused(field, value):
    with pytest.raises(ValueError):
        normalize_overrides({'zone.leftHand': {'action': {'type': 'keyboard', 'target': 'Q'}, field: value}})


def test_point_catalog_keeps_the_input_packet_order():
    assert len(MP_NAMES) == 33
    assert MP_NAMES[15:17] == ['left_wrist', 'right_wrist']
    assert MP_NAMES[27:33] == ['left_ankle', 'right_ankle', 'left_heel', 'right_heel',
                             'left_foot_index', 'right_foot_index']
    assert all(a in POSE_POINT_LABELS and b in POSE_POINT_LABELS for a, b in POSE_CONNECTIONS)
