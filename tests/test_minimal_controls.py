from pathlib import Path
import json
import sys
import time
import types

import pytest

from motioncontrol.output_backend import OutputManager, XUSB_GAMEPAD_BUTTONS, GAMEPAD_AXES
from motioncontrol.control_kernel import ControlKernel
from motioncontrol.voice_backend import VoiceService, compact_text
from motioncontrol.sherpa_kws_backend import _spoken_command_candidates
from conftest import apply_layout

ROOT = Path(__file__).resolve().parents[1]


def test_only_full_model_is_registered():
    server = (ROOT / 'server.py').read_text(encoding='utf-8')
    assert 'pose_landmarker_full.task' in server
    assert 'pose_landmarker_lite.task' not in server
    # 版本号在 motioncontrol/version.py，server.py 只负责把它 import 进来。写死
    # 一个数字在这里，等于给"改版本号"这件事加了一个会红的测试。
    assert "from motioncontrol.version import VERSION" in server


def test_main_ui_stays_compact_and_settings_hold_complex_options():
    page = (ROOT / 'web' / 'index.html').read_text(encoding='utf-8')
    app = (ROOT / 'web' / 'app.js').read_text(encoding='utf-8')
    # 三个标签的名字就是这一版的信息架构：开始 = 现在要玩，本游戏 = 换游戏会变的，
    # 通用设置 = 换游戏不用动的。改名字等于改架构，所以钉在这里。
    for required in ['2.0', '开始', '本游戏', '通用设置', '站好并校准', '恢复跟随', '重设正前方', '紧急停止 · F9', '通用口令', '三维头姿（推荐）', '挪动区域', 'profileBindingRows']:
        assert required in page
    assert '开始游戏控制' in app and '暂停游戏控制' in app
    for removed in ['开始 30 秒性能测试', '静止抖动测试', '实时性能数据', 'Lite / Full 对比结果', 'modelSelect']:
        assert removed not in page
    for removed in ['hidden-compat', 'id="cameraBtn"', 'id="outputBtn"', 'id="sceneEditor"', 'id="fixedZonesMask"', 'id="sceneTools"', 'saveProfileBindingsBtn', '<style>']:
        assert removed not in page
    assert '<dialog' in page
    assert '上下视角待机' in page
    css = (ROOT / 'web' / 'app.css').read_text(encoding='utf-8')
    assert '[hidden] { display: none !important; }' in css
    assert 'position: sticky' in css


def test_v2_command_catalog_and_head_ui():
    catalog = json.loads((ROOT / 'config' / 'voice_commands_v094.json').read_text(encoding='utf-8'))
    # product_version 已经删掉：代码里从来没人读它，只有测试在核对，于是它的全部
    # 作用就是多一个发版时要记得改、改漏了也没人知道的数字。
    assert 'product_version' not in catalog
    # 内置口令只留系统功能和每个游戏的 12 句。按游戏键的那些和通用口令是同一件事，
    # 留着只会同名打架——见 tests/test_voice_phrase_exclusive.py。
    assert sum(item['id'].startswith('game.profile_slot_') for item in catalog['commands']) == 12
    app = (ROOT / 'web' / 'app.js').read_text(encoding='utf-8')
    server = (ROOT / 'server.py').read_text(encoding='utf-8')
    assert "开始游戏控制" in app and "暂停游戏控制" in app
    assert "· Y ${Number(hs.output_y)" not in app
    assert 'for item in VOICE.command_registry.values()' in server


def test_body_relative_zones_use_both_wrists_and_both_feet():
    kernel = (ROOT / "motioncontrol" / 'control_kernel.py').read_text(encoding='utf-8')
    app = (ROOT / 'web' / 'app.js').read_text(encoding='utf-8')
    for name in ['left_wrist','right_wrist','left_ankle','right_ankle','left_foot_index','right_foot_index']:
        assert name in kernel
    for zone in ['leftHandUpper','leftHandLower','rightHandUpper','rightHandLower','leftFoot','rightFoot']:
        assert zone in kernel
    # Hand regions span the upper corner: bottom edge measured down from the
    # hips (false-trigger boundary), inner edge inset from the head.  The
    # default ratios live in zone_fit; a body fit replaces them per player.
    from motioncontrol.zone_fit import DEFAULT_ZONE_FIT
    assert DEFAULT_ZONE_FIT["leftHand"] == {"bottom": 0.40, "inset": 0.45}
    assert DEFAULT_ZONE_FIT["leftFoot"]["half_w"] == 0.50 and '0.52 * torso_px' in kernel
    assert 'fit[name]["bottom"] * torso_px / ih' in kernel and 'fit[name]["inset"] * torso_px / iw' in kernel
    assert 'headJump' in kernel and 'headJump' in app
    assert 'exit_frames = 1 if name == "lookGate" else 2' in kernel
    assert 'set_action_holds' in kernel
    assert 'zone.' in kernel
    assert '/api/kernel/status' in app
    assert 'detectForVideo' not in app


def test_seventh_look_gate_follows_and_freezes_like_the_other_zones():
    page = (ROOT / 'web' / 'index.html').read_text(encoding='utf-8')
    app = (ROOT / 'web' / 'app.js').read_text(encoding='utf-8')
    kernel_text = (ROOT / "motioncontrol" / 'control_kernel.py').read_text(encoding='utf-8')
    assert 'data-zone="lookGate"' in page
    assert "lookGate:{label:'上下视角'" in app
    assert 'state?.rect' in app
    assert 'rects["lookGate"]' in kernel_text
    assert '"lookGate"' in kernel_text.split('FROZEN_ZONE_IDS = ')[1].split('\n')[0]


def test_first_start_no_longer_records_a_reference_scene():
    """参考场景删了：开始游戏不再拍参考照片、不再问要不要切成固定区域。"""
    app = (ROOT / 'web' / 'app.js').read_text(encoding='utf-8')
    assert 'ensureInitialSceneLayout' not in app
    assert '/api/scene/' not in app
    assert 'await setOutput(!output.enabled)' in app
    assert 'inputStatus.handheld_connected' in app


def test_four_motion_rules_and_settings_exist():
    app = (ROOT / 'web' / 'app.js').read_text(encoding='utf-8')
    kernel = (ROOT / "motioncontrol" / 'control_kernel.py').read_text(encoding='utf-8')
    server = (ROOT / 'server.py').read_text(encoding='utf-8')
    for action in ['march','calf_back']:
        assert action in kernel and action in server
    # 下蹲、双手举过头的识别规则跟着动作从云端下载，写在官方动作文件里。
    official = ROOT / 'cloud' / 'official_poses'
    squat = (official / 'squat.json').read_text(encoding='utf-8')
    hands_up = (official / 'hands_up.json').read_text(encoding='utf-8')
    assert '["angle", "left_hip", "left_knee", "left_ankle"], 145' in squat
    assert '["<", ["y", "left_wrist"], ["-", ["y", "nose"], ["*", 0.06, ["torso"]]]]' in hands_up
    assert 'CALF_LIFT_ANKLE_RISE' in kernel and 'CALF_LIFT_KNEE_SHARE' in kernel
    assert 'active_until' in kernel
    assert '/api/motion/config' in server
    assert '/api/motion/state' in server
    assert 'user_path("motion_mappings")' in server


def test_head_calibration_uses_default_until_atomic_success():
    app = (ROOT / 'web' / 'app.js').read_text(encoding='utf-8')
    kernel = (ROOT / "motioncontrol" / 'control_kernel.py').read_text(encoding='utf-8')
    head_control = (ROOT / "motioncontrol" / 'head_control.py').read_text(encoding='utf-8')
    assert 'head-control-v4.3-reference-video-tuned' in head_control
    assert 'CENTER_PREPARE_S = 1.00' in (ROOT / "motioncontrol" / 'head_control.py').read_text(encoding='utf-8')
    assert 'CENTER_WALL_LIMIT_S = 6.00' in (ROOT / "motioncontrol" / 'head_control.py').read_text(encoding='utf-8')
    assert 'cancel_calibration' in kernel
    assert '站好并校准' in (ROOT / 'web' / 'index.html').read_text(encoding='utf-8')
    assert 'HeadController' in kernel


def test_xbox_masks_include_abxy_and_side_buttons():
    assert XUSB_GAMEPAD_BUTTONS['A'] == 0x1000
    assert XUSB_GAMEPAD_BUTTONS['B'] == 0x2000
    assert XUSB_GAMEPAD_BUTTONS['X'] == 0x4000
    assert XUSB_GAMEPAD_BUTTONS['Y'] == 0x8000
    assert XUSB_GAMEPAD_BUTTONS['LB'] == 0x0100
    assert XUSB_GAMEPAD_BUTTONS['RB'] == 0x0200
    assert GAMEPAD_AXES['LS_UP'] == (0.0, 1.0)


class FakePad:
    def __init__(self):
        self.buttons = ()
        self.history = []
        self.stick = (0,0)
        self.left_stick = (0,0)
        self.closed = False
    def set_buttons(self, names):
        self.buttons = tuple(names)
        self.history.append(self.buttons)
    def set_right_stick(self, x, y=0): self.stick = (x,y)
    def set_left_stick(self, x, y=0): self.left_stick = (x,y)
    def reset(self): self.buttons = (); self.stick = (0,0); self.left_stick = (0,0); self.history.append(())
    def close(self): self.closed = True


class FakeMouse:
    available = True
    last_error = None
    def move(self, dx, dy=0): return True


class FakeKeyboard:
    available = True
    last_error = None
    def __init__(self): self.combos=[]; self.released=False; self.pressed=set(); self.history=[]
    def set_key(self,key,pressed):
        key=str(key).upper(); self.history.append((key,pressed))
        if pressed:self.pressed.add(key)
        else:self.pressed.discard(key)
    def tap_combo(self, combo, hold_seconds=.06): self.combos.append(combo)
    def release_all(self): self.released=True; self.pressed.clear()


def test_output_manager_can_hold_body_zone_buttons_even_in_mouse_head_mode(tmp_path):
    out = OutputManager(tmp_path, mouse=FakeMouse(), keyboard=FakeKeyboard())
    pad = FakePad(); out._pad = pad
    try:
        out.set_config(mode='mouse', enabled=True)
        status = out.set_buttons(['A','LB'])
        assert pad.buttons == ('A','LB')
        assert status['buttons'] == ['A','LB']
        out.emergency_stop()
        assert pad.buttons == ()
    finally:
        out.close()


def test_motion_holds_can_drive_keyboard_gamepad_and_left_stick(tmp_path):
    keyboard=FakeKeyboard(); out=OutputManager(tmp_path, mouse=FakeMouse(), keyboard=keyboard); pad=FakePad(); out._pad=pad
    try:
        out.set_config(enabled=True)
        out.set_holds([
            {'id':'march','type':'gamepad_axis','target':'LS_UP'},
            {'id':'squat','type':'keyboard','target':'W'},
            {'id':'hands_up','type':'gamepad','target':'Y'},
        ])
        assert pad.left_stick == (0.0,1.0)
        assert 'W' in keyboard.pressed
        assert 'Y' in pad.buttons
        out.set_holds([])
        assert pad.left_stick == (0.0,0.0)
        assert 'W' not in keyboard.pressed
        assert 'Y' not in pad.buttons
    finally:
        out.close()


def test_voice_gamepad_pulse_does_not_cancel_zone_button(tmp_path):
    out = OutputManager(tmp_path, mouse=FakeMouse(), keyboard=FakeKeyboard())
    pad = FakePad(); out._pad = pad
    try:
        out.set_config(enabled=True)
        out.set_buttons(['B'])
        out.tap_gamepad('A', duration=.04)
        assert ('A','B') in pad.history
        assert pad.buttons == ('B',)
    finally:
        out.close()


def test_voice_keyboard_combo_uses_same_output_gate(tmp_path):
    keyboard = FakeKeyboard()
    out = OutputManager(tmp_path, mouse=FakeMouse(), keyboard=keyboard)
    try:
        result = out.execute_action({'type':'keyboard','target':'CTRL+S'})
        assert result['executed'] is False
        out.set_config(enabled=True)
        result = out.execute_action({'type':'keyboard','target':'CTRL+S'})
        assert result['executed'] is True
        assert ('CTRL', True) in keyboard.history and ('S', True) in keyboard.history
        assert keyboard.pressed == set()
    finally:
        out.close()


def test_kws_keywords_are_built_directly_from_custom_phrases():
    pairs = _spoken_command_candidates('体感', [
        {'phrase': '地图', 'synonyms': []},
        {'phrase': '闪避', 'synonyms': ['躲避']},
    ], ['体感紧急停止'])
    spoken = [p[0] for p in pairs]
    assert '地图' in spoken
    assert '闪避' in spoken
    assert '躲避' in spoken
    assert '紧急停止' in spoken
    # Wake word is handled by the first KWS stage, not in command candidates
    assert '体感' not in spoken


def test_voice_mapping_is_the_vocab_and_persists_without_model(tmp_path):
    service = VoiceService(tmp_path, lambda action: {'executed': True})
    status = service.configure([
        {'phrase':'地图','type':'keyboard','target':'M'},
        {'phrase':'闪避','type':'gamepad','target':'B'},
        {'phrase':'保存','type':'keyboard','target':'ctrl+s'},
    ])
    assert [m['phrase'] for m in status['mappings']] == ['地图','闪避','保存']
    assert status['mappings'][2]['target'] == 'CTRL+S'
    saved = json.loads(service.config_path.read_text(encoding='utf-8'))
    assert len(saved['mappings']) == 3
    assert compact_text('打 开 地 图') == '打开地图'


def test_voice_exact_final_dispatches_custom_mapping(tmp_path):
    calls=[]
    service = VoiceService(tmp_path, lambda action: calls.append(action) or {'executed': True})
    service.mappings=[{'phrase':'打开地图','type':'keyboard','target':'M'}]
    service._match_and_execute('打开 地图')
    deadline=time.time()+1
    while not calls and time.time()<deadline: time.sleep(.01)
    assert calls == [{'type':'keyboard','target':'M','behavior':'tap','phrase':'体感打开地图','source':'voice'}]


def test_voice_counts_each_command_heard_but_not_the_bare_wake_word(tmp_path):
    """新手教学靠这个数知道「刚又说了一句」：同一句说两遍 last_command 不变，数会变。"""
    service = VoiceService(tmp_path, lambda action: {'executed': True})
    service.mappings = [{'phrase': '打开地图', 'type': 'keyboard', 'target': 'M'}]
    assert service.status()['commands_heard'] == 0
    service._match_and_execute('打开 地图')
    service._match_and_execute('打开 地图')
    assert service.status()['commands_heard'] == 2
    service._match_and_execute('随便说点什么')
    assert service.status()['commands_heard'] == 2
    service._match_and_execute(service.wake_word, enforce_wake=True)
    assert service.status()['commands_heard'] == 2


def test_voice_api_uses_local_mic_or_phone_command_not_browser_audio():
    server = (ROOT / 'server.py').read_text(encoding='utf-8')
    assert '/api/voice/status' in server
    assert '/api/voice/config' in server
    assert '/api/voice/audio' in server
    assert 'browser voice endpoint disabled' in server
    assert '/ws/input voice_command(command_id)' in server
    app = (ROOT / 'web' / 'app.js').read_text(encoding='utf-8')
    assert 'getUserMedia' not in app
    assert 'postBinary' not in app
    assert 'createScriptProcessor' not in app


def test_output_api_is_loopback_only_and_has_buttons_route():
    server = (ROOT / 'server.py').read_text(encoding='utf-8')
    assert '/api/output/buttons' in server
    assert 'game output is loopback-only' in server
    backend = (ROOT / "motioncontrol" / 'output_backend.py').read_text(encoding='utf-8')
    assert 'self.enabled = False' in backend
    assert 'F8 toggles output; F9 always performs an emergency stop' in backend


def test_game_overlay_uses_same_body_relative_zones():
    app = (ROOT / 'web' / 'app.js').read_text(encoding='utf-8')
    assert 'documentPictureInPicture.requestWindow' in app
    assert 'renderOverlay' in app
    assert 'renderKernelZones' in app
    assert 'runtime?.kernel' in app
    assert 'requestAnimationFrame' not in app
    assert 'getUserMedia({video' not in app


def test_previous_vosk_model_path_is_pinned():
    path = (ROOT / 'config' / 'vosk_model_path.txt').read_text(encoding='utf-8').strip()
    assert path == r'models/vosk-model-small-cn-0.22'
    page = (ROOT / 'web' / 'index.html').read_text(encoding='utf-8')
    assert 'voiceModelPath' in page


def test_voice_mappings_migrate_from_v071_sibling(tmp_path):
    old = tmp_path / 'MotionControl-v0.7.1-voice-mapping' / 'config'
    old.mkdir(parents=True)
    (old / 'voice_mappings.json').write_text(
        json.dumps({'mappings':[{'phrase':'地图','type':'keyboard','target':'M'}]}, ensure_ascii=False),
        encoding='utf-8',
    )
    root = tmp_path / 'MotionControl-v0.7.2-overlay'
    root.mkdir()
    service = VoiceService(root, lambda action: {'executed': True})
    # normalize_action now records the behavior explicitly; voice defaults to tap.
    assert service.mappings == [{'phrase':'地图','type':'keyboard','target':'M','behavior':'tap'}]
    assert service.config_path.is_file()
    assert not (root / 'config' / 'voice_mappings.json').exists()


def test_voice_parser_requires_wake_word_for_phone_and_clears_source(tmp_path):
    calls = []
    cleared = []
    service = VoiceService(
        tmp_path,
        lambda action: calls.append(action) or {'executed': True},
        clear_source=lambda source: cleared.append(source) or {},
    )
    service.mappings = [{'phrase': '攻击', 'type': 'keyboard', 'target': 'J'}]
    status, result = service.accept_phone_text('mobile_voice:phone-a', 'phone-a', '攻击')
    assert result['reason'] == 'wake_word_required'
    assert not calls
    # 手机现在也能直接送声音过来（voice_audio），状态里就有了音量那几项。只送文字的
    # 时候它们得是 0：不能让界面以为收到过声音。
    assert status.get('bytes_received', 0) == 0 and status.get('rms', 0) == 0
    status, result = service.accept_phone_text('mobile_voice:phone-a', 'phone-a', '体感 攻击')
    deadline = time.time() + 1
    while not calls and time.time() < deadline:
        time.sleep(.01)
    assert result['matched'] is True
    assert calls == [{'type': 'keyboard', 'target': 'J', 'behavior': 'tap', 'phrase': '体感攻击',
                      'source': 'voice:mobile_voice:phone-a'}]
    service.disconnect('mobile_voice:phone-a')
    assert 'voice:mobile_voice:phone-a' in cleared
    assert service.status()['connected'] is False


def test_phone_voice_follows_the_audio_source_not_the_camera():
    """手机认好的语音看「音频来源」，不看摄像头来源。

    两个是分开选的：电脑摄像头 + 手机麦克风是正常的搭法，这时手机上说的话要算数；
    换摄像头也不该把手机麦克风断掉。音频来源改回电脑麦克风，手机说的就不算了。
    """
    from motioncontrol.input_bridge import InputBridge

    class FakeVoice:
        def __init__(self):
            self.accepted = []
            self.disconnected = []

        def accept_phone_text(self, source_id, device_id, text, confidence=None):
            self.accepted.append((source_id, device_id, text, confidence))
            return {'source_kind': 'phone'}, {'matched': True}

        def disconnect(self, source_id=None):
            self.disconnected.append(source_id)
            return {}

    class FakeOutput:
        def clear_source(self, source):
            pass

    class FakeKernel:
        def clear_source(self, source):
            pass

    class Peer:
        def __init__(self):
            self.source_ids = set()
            self.desktop = False
            self.accepted_inputs = 0
            self.errors = []

        def send_json(self, message):
            self.errors.append(message)

        def close(self):
            pass

    voice = FakeVoice()
    bridge = InputBridge(FakeOutput(), FakeKernel(), voice=voice)
    peer = Peer()
    message = {
        'type': 'voice_text', 'role': 'camera', 'device_id': 'phone-a',
        'sequence': 1, 'captured_at_ms': 1234, 'text': '体感 攻击', 'confidence': .9,
    }
    try:
        bridge.set_body_mode('phone')
        bridge.handle_message(peer, message)
        assert voice.accepted == [], '音频来源默认是电脑麦克风'
        assert any('电脑麦克风' in str(item.get('message')) for item in peer.errors), '要告诉手机为什么不算'
        bridge.set_audio_mode('phone')
        bridge.handle_message(peer, message)
        assert voice.accepted == [('mobile_voice:phone-a', 'phone-a', '体感 攻击', .9)]
        bridge.set_body_mode('computer')
        assert voice.disconnected == [], '换摄像头不断手机麦克风'
        bridge.handle_message(peer, message)
        assert len(voice.accepted) == 2, '电脑摄像头 + 手机麦克风'
        bridge.handle_message(peer, dict(message, role='sensor'))
        assert len(voice.accepted) == 2, '手柄那条连接送来的不算'
        bridge.set_audio_mode('computer')
        bridge.handle_message(peer, message)
        assert len(voice.accepted) == 2
    finally:
        bridge.close()


class KernelOutput:
    enabled = True

    def __init__(self):
        self.applied = []

    def apply(self, x, y):
        self.applied.append((float(x), float(y)))

    def set_buttons(self, *args, **kwargs):
        pass

    def set_holds(self, *args, **kwargs):
        pass

    def set_sensor_state(self, *args, **kwargs):
        pass

    def clear_source(self, *args, **kwargs):
        pass


def _head_only_pose(*, left_wrist_y=.5, right_wrist_y=.5, left_wrist_x=.2, right_wrist_x=.7, right_shoulder_y=.32):
    return {
        'left_wrist': {'x': left_wrist_x, 'y': left_wrist_y, 'score': .95},
        'right_wrist': {'x': right_wrist_x, 'y': right_wrist_y, 'score': .95},
        'right_shoulder': {'x': .62, 'y': right_shoulder_y, 'score': .95},
    }


def _stub_head_controller(kernel, pitch=.8):
    kernel.head_controller.update = lambda *args: (.2, pitch)
    kernel.head_controller.status = lambda now: {
        'algorithm': 'pnp', 'calibrated': True, 'enabled': True,
        'normalized_x': .2, 'normalized_y': pitch, 'output_x': .2,
        'output_y': pitch,
    }


def test_pure_head_pitch_never_reaches_final_mouse_y():
    output = KernelOutput()
    kernel = ControlKernel(output)
    try:
        _stub_head_controller(kernel, pitch=.9)
        with kernel._lock:
            kernel._update_head_locked(_head_only_pose(), time.monotonic())
        assert output.applied[-1][0] == .2
        assert output.applied[-1][1] == 0.0
        assert kernel.head['output_y'] == 0.0
    finally:
        kernel.close()


def test_look_gate_captures_stable_body_relative_anchor_without_freezing_horizontal_head(monkeypatch):
    output = KernelOutput()
    kernel = ControlKernel(output)
    try:
        clock = [0.0]
        monkeypatch.setattr('motioncontrol.control_kernel.time.monotonic', lambda: clock[0])
        def feed(pose, count):
            for _ in range(count):
                clock[0] += 0.10
                kernel.handle_pose_map('camera', pose, width=640, height=480)

        _stub_head_controller(kernel, pitch=-.9)
        apply_layout(kernel, {
            'zones': {'lookGate': {'cx': .2, 'cy': .2, 'r': .15}},
            'vertical_look': {'enabled': True, 'point': 'right_wrist', 'range_y': .18, 'deadzone': .08},
        })
        neutral = _head_only_pose(left_wrist_y=.2, right_wrist_y=.5, right_shoulder_y=.32)
        feed(neutral, 8)
        assert kernel.vertical_gate_active is True
        assert kernel.vertical_wrist_anchor_rel_y is not None
        assert abs(kernel.vertical_wrist_anchor_rel_y - .18) < 1e-6
        # The left-hand gate authorizes only Y; yaw remains independent so
        # simultaneous horizontal + vertical control is possible.
        assert output.applied[-1][0] == pytest.approx(.2)
        assert output.applied[-1][1] == 0.0

        moved = _head_only_pose(left_wrist_y=.2, right_wrist_y=.62, right_shoulder_y=.32)
        feed(moved, 3)
        assert output.applied[-1][0] == pytest.approx(.2)
        assert output.applied[-1][1] > 0.0

        before = output.applied[-1][1]
        horizontal = _head_only_pose(left_wrist_y=.2, right_wrist_y=.62, right_wrist_x=.95, right_shoulder_y=.32)
        feed(horizontal, 1)
        assert abs(output.applied[-1][1] - before) < .20

        # Body bobbing: wrist and shoulder move together, so relative Y returns
        # toward neutral instead of following absolute image coordinates.
        bobbed = _head_only_pose(left_wrist_y=.2, right_wrist_y=.60, right_shoulder_y=.42)
        feed(bobbed, 8)
        assert abs(output.applied[-1][1]) < .08

        outside = _head_only_pose(left_wrist_x=.9, left_wrist_y=.2, right_wrist_y=.60, right_shoulder_y=.42)
        feed(outside, 1)
        assert kernel.vertical_gate_active is False
        assert kernel.vertical_wrist_anchor_rel_y is None
        assert output.applied[-1][1] == 0.0
    finally:
        kernel.close()


def test_optional_vertical_gate_exclusivity_pauses_only_horizontal_output():
    output = KernelOutput()
    kernel = ControlKernel(output)
    try:
        _stub_head_controller(kernel, pitch=0.0)
        apply_layout(kernel, {
            'zones': {'lookGate': {'cx': .2, 'cy': .2, 'r': .15}},
            'vertical_look': {'enabled': True, 'point': 'right_wrist', 'range_y': .18, 'deadzone': .08},
        })
        kernel.configure_head(vertical_exclusive=True)
        inside = _head_only_pose(left_wrist_x=.2, left_wrist_y=.2)
        for _ in range(3):
            state = kernel.handle_pose_map('camera', inside, width=640, height=480)
        assert state['vertical_gate_active'] is True
        assert state['head']['horizontal_paused_by_vertical_gate'] is True
        assert output.applied[-1][0] == 0.0

        outside = _head_only_pose(left_wrist_x=.9, left_wrist_y=.2)
        state = kernel.handle_pose_map('camera', outside, width=640, height=480)
        assert state['vertical_gate_active'] is False
        assert state['head']['horizontal_paused_by_vertical_gate'] is False
        assert output.applied[-1][0] == pytest.approx(.2)
    finally:
        kernel.close()


# --- provisional body-relative zone geometry -------------------------------
# The provisional (body_relative) zones had no geometry coverage at all: the
# only prior assertion was that the "headJump" id existed.  These cases pin the
# two properties the user actually depends on -- a small jump must be able to
# enter the head zone, and resting arms must never enter the hand zones.

def _standing_pose(dy=0.0, dx=0.0, left_wrist=None, right_wrist=None,
                   left_ankle=None, right_ankle=None):
    """One upright frame.  dy<0 lifts the whole body, as a real jump does."""
    def pt(x, y):
        return {'x': x + dx, 'y': y + dy, 'score': .95}
    return {
        'nose': pt(.50, .30),
        'left_ear': pt(.47, .31), 'right_ear': pt(.53, .31),
        'left_shoulder': pt(.42, .40), 'right_shoulder': pt(.58, .40),
        'left_hip': pt(.45, .64), 'right_hip': pt(.55, .64),
        'left_knee': pt(.46, .80), 'right_knee': pt(.54, .80),
        # Feet stand on the floor line unless a case overrides them.
        'left_ankle': pt(*(left_ankle or (.46, .95))),
        'right_ankle': pt(*(right_ankle or (.54, .95))),
        # Arms hang naturally at hip height unless a case overrides them.
        'left_wrist': pt(*(left_wrist or (.40, .66))),
        'right_wrist': pt(*(right_wrist or (.60, .66))),
    }


def _squat_pose(depth):
    """Lower the upper body while both ankles stay on the floor."""
    pose = _standing_pose()
    for name in ('nose', 'left_ear', 'right_ear', 'left_shoulder', 'right_shoulder',
                 'left_wrist', 'right_wrist'):
        pose[name]['y'] += depth
    for name in ('left_hip', 'right_hip'):
        pose[name]['y'] += .65 * depth
    for name in ('left_knee', 'right_knee'):
        pose[name]['y'] += .20 * depth
    pose['left_knee']['x'] -= .50 * depth
    pose['right_knee']['x'] += .50 * depth
    return pose


def _zone_feeder(kernel, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr('motioncontrol.control_kernel.time.monotonic', lambda: clock[0])

    def feed(pose, count=1, step=1 / 30.0):
        for _ in range(count):
            clock[0] += step
            kernel.handle_pose_map('camera', pose, width=640, height=480)
    return feed


def test_small_jump_can_actually_enter_the_head_zone(monkeypatch):
    """The head zone must not ride up with the body during a jump.

    Anchoring it to the live nose made it follow at ~170ms while a jump lasts
    400-600ms, so the target stayed above the nose for the whole flight and
    could never be entered.
    """
    kernel = ControlKernel(KernelOutput())
    try:
        feed = _zone_feeder(kernel, monkeypatch)
        feed(_standing_pose(), 40)
        assert kernel.zone_state['headJump']['pressed'] is False

        # A jump lifts nose, shoulders and hips together by ~0.35 torso.
        rise = .35 * .24
        for frame in range(6):
            feed(_standing_pose(dy=-rise * (frame + 1) / 6.0))
        feed(_standing_pose(dy=-rise), 4)
        assert kernel.zone_state['headJump']['pressed'] is True
    finally:
        kernel.close()


def test_squat_then_stand_does_not_press_the_following_head_zone(monkeypatch):
    kernel = ControlKernel(KernelOutput())
    try:
        feed = _zone_feeder(kernel, monkeypatch)
        feed(_standing_pose(), 40)
        resting_y = kernel.zone_rects['headJump']['y1']
        pressed_frames = []

        poses = ([_squat_pose(.12 * (frame + 1) / 12) for frame in range(12)]
                 + [_squat_pose(.12)] * 45
                 + [_squat_pose(.12 * (1 - (frame + 1) / 8)) for frame in range(8)]
                 + [_standing_pose()] * 10)
        for frame, pose in enumerate(poses):
            feed(pose)
            if kernel.zone_state['headJump']['pressed']:
                pressed_frames.append(frame)

        assert kernel.zone_rects['headJump']['y1'] > resting_y - .02
        assert not pressed_frames, f'head zone pressed on squat frames {pressed_frames}'
    finally:
        kernel.close()


def _leaning_in_pose(scale, drop):
    """Closer to the camera and lower, as when reaching over to tap the phone."""
    pose = _standing_pose()
    for point in pose.values():
        point['x'] = .5 + (point['x'] - .5) * scale
        point['y'] = .40 + (point['y'] - .40) * scale + drop
    return pose


def test_head_zone_returns_above_the_head_after_leaning_in(monkeypatch):
    """Leaning in to tap the phone used to leave the zone under the chin.

    The lean makes the body larger.  The upright span was an all-time maximum,
    so after sitting back every frame looked like a crouch and the zone never
    followed back up.
    """
    kernel = ControlKernel(KernelOutput())
    try:
        feed = _zone_feeder(kernel, monkeypatch)
        feed(_standing_pose(), 40)
        resting_y = kernel.zone_rects['headJump']['y1']
        feed(_leaning_in_pose(1.25, .10), 60)
        feed(_standing_pose(), 150)

        assert kernel.zone_rects['headJump']['y2'] < _standing_pose()['nose']['y']
        assert kernel.zone_rects['headJump']['y1'] == pytest.approx(resting_y, abs=.01)
    finally:
        kernel.close()


def test_one_stray_hip_estimate_does_not_stop_the_head_zone_following(monkeypatch):
    """With the hips out of frame their estimate jumps about.

    One long frame used to become the upright span for good.  From then on the
    zone would not follow the player down, and stayed out of reach above them.
    """
    kernel = ControlKernel(KernelOutput())
    try:
        feed = _zone_feeder(kernel, monkeypatch)
        feed(_standing_pose(), 40)
        resting_gap = _standing_pose()['nose']['y'] - kernel.zone_rects['headJump']['y2']
        stray = _standing_pose()
        for name in ('left_hip', 'right_hip'):
            stray[name]['y'] += .05
        feed(stray)
        feed(_standing_pose(), 30)
        lower = _standing_pose(dy=.12)
        feed(lower, 300)

        gap = lower['nose']['y'] - kernel.zone_rects['headJump']['y2']
        assert gap == pytest.approx(resting_gap, abs=.01)
    finally:
        kernel.close()


def test_natural_standing_never_enters_the_enlarged_hand_zones(monkeypatch):
    kernel = ControlKernel(KernelOutput())
    try:
        feed = _zone_feeder(kernel, monkeypatch)
        feed(_standing_pose(), 60)
        assert kernel.zone_state['leftHand']['pressed'] is False
        assert kernel.zone_state['rightHand']['pressed'] is False

        # Raising a hand out to the side must still trigger, and the enlarged
        # region means it no longer has to reach head height.
        feed(_standing_pose(left_wrist=(.18, .42)), 4)
        assert kernel.zone_state['leftHand']['pressed'] is True
    finally:
        kernel.close()


def test_head_zone_follows_a_lateral_stance_change_but_not_the_jump(monkeypatch):
    kernel = ControlKernel(KernelOutput())
    try:
        feed = _zone_feeder(kernel, monkeypatch)
        feed(_standing_pose(), 40)
        before = dict(kernel.zone_rects['headJump'])

        # Stepping sideways is a sustained change: the zone must track it.
        feed(_standing_pose(dx=.10), 90)
        after = dict(kernel.zone_rects['headJump'])
        assert after['x1'] - before['x1'] > .07
        assert abs(after['y1'] - before['y1']) < .02
    finally:
        kernel.close()


def test_jump_freeze_releases_after_landing(monkeypatch):
    kernel = ControlKernel(KernelOutput())
    try:
        feed = _zone_feeder(kernel, monkeypatch)
        feed(_standing_pose(), 40)
        resting = dict(kernel.zone_rects['headJump'])

        rise = .35 * .24
        for frame in range(6):
            feed(_standing_pose(dy=-rise * (frame + 1) / 6.0))
        feed(_standing_pose(dy=-rise), 4)
        feed(_standing_pose(), 60)

        assert kernel.zone_state['headJump']['pressed'] is False
        landed = dict(kernel.zone_rects['headJump'])
        assert abs(landed['y1'] - resting['y1']) < .02
    finally:
        kernel.close()


def test_foot_zone_clears_any_stance_width_but_catches_an_ordinary_side_lift(monkeypatch):
    """The floor gap, not the width, is what rejects a planted foot.

    floor_y follows the planted ankle, so a foot resting on the floor is out of
    reach at any stance width.  That lets the region be wide enough to catch a
    normal side lift; a narrower one needed about half a torso of lateral
    travel and missed.
    """
    torso = .24
    for ankle_x in (.46, .40, .36, .32):
        kernel = ControlKernel(KernelOutput())
        try:
            feed = _zone_feeder(kernel, monkeypatch)
            feed(_standing_pose(left_ankle=(ankle_x, .95)), 40)
            assert kernel.zone_state['leftFoot']['pressed'] is False, f'stance {ankle_x}'
        finally:
            kernel.close()

    for name, ankle in {
        'small lift': (.46 - .25 * torso, .95 - .15 * torso),
        'side lift': (.46 - .35 * torso, .95 - .25 * torso),
        'flat step out': (.46 - .50 * torso, .95 - .10 * torso),
    }.items():
        kernel = ControlKernel(KernelOutput())
        try:
            feed = _zone_feeder(kernel, monkeypatch)
            feed(_standing_pose(), 40)
            feed(_standing_pose(left_ankle=ankle), 4)
            assert kernel.zone_state['leftFoot']['pressed'] is True, name
            assert kernel.zone_state['rightFoot']['pressed'] is False, name
        finally:
            kernel.close()


def test_foot_and_hand_zones_do_not_overlap_each_other(monkeypatch):
    kernel = ControlKernel(KernelOutput())
    try:
        feed = _zone_feeder(kernel, monkeypatch)
        feed(_standing_pose(), 40)
        z = kernel.zone_rects
        assert z['leftHand']['y2'] < z['leftFoot']['y1']
        assert z['rightHand']['y2'] < z['rightFoot']['y1']
        assert z['leftFoot']['x2'] < z['rightFoot']['x1']
    finally:
        kernel.close()


def test_adjusting_zones_freezes_them_first_and_can_be_undone():
    """挪动区域先把跟随框定住（一直在动的东西没法拖）；取消回到点之前，恢复跟随随时能点。"""
    page = (ROOT / 'web' / 'index.html').read_text(encoding='utf-8')
    app = (ROOT / 'web' / 'app.js').read_text(encoding='utf-8')
    assert 'id="followZonesBtn"' in page and 'id="zoneMoveHereBtn"' in page
    opener = app[app.index('async function openFrozenZoneEditor()'):]
    assert "post('/api/zones/freeze',{frozen:true})" in opener.split('\n}')[0]
    cancel = app[app.index('async function cancelLiveZones()'):].split('\n}')[0]
    assert "post('/api/zones/freeze',{frozen:false})" in cancel


def test_start_script_detects_wireless_adb_devices_too():
    """A wireless adb serial is an mDNS name and can contain a space.

    Splitting the "adb devices" line on whitespace truncates such a serial and
    the tunnel is silently skipped, so the phone loses its fixed 127.0.0.1
    address after every restart.  The tab between serial and state is the only
    reliable delimiter.
    """
    script = (ROOT / 'START.ps1').read_text(encoding='utf-8')
    assert 'reverse tcp:8765 tcp:8765' in script
    assert "-split \"`t\"" in script
    assert r"'^[^\s]+\s+device" not in script
    assert r"-split '\s+'" not in script


def test_spare_voice_slots_are_editable_not_only_displayable():
    """The spare slots are the single editable voice-command editor."""
    app = (ROOT / 'web' / 'app.js').read_text(encoding='utf-8')
    assert "filter(item=>!item.system_fixed&&!String(item.id||'').startsWith('game.profile_slot_'))" not in app
    assert "slot:String(item.id||'').startsWith('game.profile_slot_')" in app
    assert "title:'本游戏口令'" in app
    assert "className='voice-trigger-phrase'" in app


def test_gamepad_combo_is_picked_not_typed_and_poses_can_hold():
    """A combo used to be a free text field, and a pose could not hold at all.

    Valid names are a fixed set, so typing "LB+LS_UP" only meant a typo would
    surface as a rejected save; the parts are ticked instead, stick directions
    included.  A checkbox sends no input event, so the guard that stops a text
    blur from re-saving must not swallow its change.
    """
    app = (ROOT / 'web' / 'app.js').read_text(encoding='utf-8')
    assert "GAMEPAD_STICK_TARGETS=['LS_UP','LS_DOWN','LS_LEFT','LS_RIGHT']" in app
    assert "picker.className='combo-picker'" in app or "picker.className=\"combo-picker\"" in app
    assert "combo.type='hidden'" in app
    assert "input:not([type=checkbox])" in app
    assert "tapOnly:true" not in app, 'no trigger should be locked out of holding'
    css = (ROOT / 'web' / 'app.css').read_text(encoding='utf-8')
    assert '.combo-picker' in css


def test_mouse_is_the_default_output_mode_everywhere(tmp_path):
    """默认鼠标：桌面上看得见，手柄摇杆要有游戏在前台才看得见。

    三个地方必须说同一件事，否则它们每次启动都要打一架——页面一进来就把自己
    那个默认值推给服务端，推的是另一个值就等于悄悄改了设置。以前反过来（三处
    都是 gamepad）踩过一次：页面默认鼠标、服务端默认手柄，物理手柄合流在第一
    次交互里就被关掉了，而界面上看不出来。
    """
    out = OutputManager(tmp_path, mouse=FakeMouse(), keyboard=FakeKeyboard())
    try:
        assert out.status()['mode'] == 'mouse'
    finally:
        out.close()
    app = (ROOT / 'web' / 'app.js').read_text(encoding='utf-8')
    assert "mode:'mouse'" in app
    page = (ROOT / 'web' / 'index.html').read_text(encoding='utf-8')
    modes = page.split('id="outputMode"', 1)[1].split('</select>', 1)[0]
    assert modes.index('value="mouse"') < modes.index('value="gamepad"'), 'the first option is the one selected'
