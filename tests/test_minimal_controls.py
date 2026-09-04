from pathlib import Path
import json
import sys
import time
import types

import pytest

from output_backend import OutputManager, XUSB_GAMEPAD_BUTTONS, GAMEPAD_AXES
from control_kernel import ControlKernel
from voice_backend import VoiceService, compact_text
from sherpa_kws_backend import _spoken_command_candidates

ROOT = Path(__file__).resolve().parents[1]


def test_only_full_model_is_registered():
    server = (ROOT / 'server.py').read_text(encoding='utf-8')
    assert 'pose_landmarker_full.task' in server
    assert 'pose_landmarker_lite.task' not in server
    assert 'VERSION = "1.00"' in server


def test_main_ui_stays_compact_and_settings_hold_complex_options():
    page = (ROOT / 'web' / 'index.html').read_text(encoding='utf-8')
    app = (ROOT / 'web' / 'app.js').read_text(encoding='utf-8')
    for required in ['1.00', '开始体感', '站好并校准', '重新识别我的位置', '视角回正', 'Xbox 360 右摇杆', '紧急停止 · F9', 'id="settingsBtn"', '当前游戏的输入映射', '旧版语音别名', '头控', '3D 头姿（推荐）', '查看全部语音指令', '区域不准？直接拖动调整', '当前游戏', 'profileBindingRows']:
        assert required in page
    assert '开始游戏控制' in app and '停止游戏控制' in app
    for removed in ['开始 30 秒性能测试', '静止抖动测试', '实时性能数据', 'Lite / Full 对比结果', 'modelSelect']:
        assert removed not in page
    assert 'settings-mask' in page
    assert 'voice-commands-mask' in page
    assert '上下视角待机' in page
    assert 'font-size:17px' in page


def test_v100_command_catalog_and_head_ui():
    catalog = json.loads((ROOT / 'config' / 'voice_commands_v094.json').read_text(encoding='utf-8'))
    assert catalog['product_version'] == '1.00'
    assert len(catalog['commands']) >= 39
    app = (ROOT / 'web' / 'app.js').read_text(encoding='utf-8')
    server = (ROOT / 'server.py').read_text(encoding='utf-8')
    assert "开始游戏控制" in app and "停止游戏控制" in app
    assert "· Y ${Number(hs.output_y)" not in app
    assert 'for item in VOICE.command_registry.values()' in server


def test_body_relative_zones_use_both_wrists_and_both_feet():
    kernel = (ROOT / 'control_kernel.py').read_text(encoding='utf-8')
    app = (ROOT / 'web' / 'app.js').read_text(encoding='utf-8')
    for name in ['left_wrist','right_wrist','left_ankle','right_ankle','left_foot_index','right_foot_index']:
        assert name in kernel
    for zone in ['leftHandUpper','leftHandLower','rightHandUpper','rightHandLower','leftFoot','rightFoot']:
        assert zone in kernel
    assert '0.36 * torso_px' in kernel
    assert '0.42 * torso_px' in kernel
    assert 'state["inside"] >= 2' in kernel and 'exit_frames = 1 if name == "lookGate" else 2' in kernel
    assert 'set_action_holds' in kernel
    assert 'zone.' in kernel
    assert '/api/kernel/status' in app
    assert 'detectForVideo' not in app


def test_seventh_look_gate_exists_before_and_after_fixed_scene_capture():
    page = (ROOT / 'web' / 'index.html').read_text(encoding='utf-8')
    app = (ROOT / 'web' / 'app.js').read_text(encoding='utf-8')
    kernel_text = (ROOT / 'control_kernel.py').read_text(encoding='utf-8')
    assert 'data-zone="lookGate"' in page
    assert "lookGate:{label:'上下视角'" in app
    assert 'state?.circle' in app and 'state?.rect' in app
    assert 'rects["lookGate"]' in kernel_text
    assert 'body_relative_provisional' in kernel_text


def test_first_start_keeps_scene_capture_but_output_is_not_scene_gated():
    app = (ROOT / 'web' / 'app.js').read_text(encoding='utf-8')
    assert 'ensureInitialSceneLayout' in app
    assert "post('/api/scene/capture'" in app
    assert 'await setOutput(!output.enabled)' in app
    assert 'if(!output.enabled&&!(await ensureInitialSceneLayout()))' not in app
    assert '头控、动作、语音和手机输入仍可用' in app


def test_four_motion_rules_and_settings_exist():
    app = (ROOT / 'web' / 'app.js').read_text(encoding='utf-8')
    kernel = (ROOT / 'control_kernel.py').read_text(encoding='utf-8')
    server = (ROOT / 'server.py').read_text(encoding='utf-8')
    for action in ['march','calf_back','squat','hands_up']:
        assert action in kernel and action in server
    assert 'left_angle < 135' in kernel
    assert 'left_angle < 115' in kernel and 'right_angle < 115' in kernel
    assert 'pose_map["left_wrist"]["y"] < pose_map["nose"]["y"] - 0.06 * torso' in kernel
    assert 'active_until' in kernel
    assert '/api/motion/config' in server
    assert '/api/motion/state' in server
    assert 'motion_mappings.json' in server


def test_head_calibration_uses_default_until_atomic_success():
    app = (ROOT / 'web' / 'app.js').read_text(encoding='utf-8')
    kernel = (ROOT / 'control_kernel.py').read_text(encoding='utf-8')
    head_control = (ROOT / 'head_control.py').read_text(encoding='utf-8')
    assert 'head-control-v4.3-reference-video-tuned' in head_control
    assert 'CENTER_PREPARE_S = 1.00' in (ROOT / 'head_control.py').read_text(encoding='utf-8')
    assert 'CENTER_WALL_LIMIT_S = 6.00' in (ROOT / 'head_control.py').read_text(encoding='utf-8')
    assert 'cancel_calibration' in kernel
    assert '开始校准' in (ROOT / 'web' / 'index.html').read_text(encoding='utf-8')
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
    saved = json.loads((tmp_path/'config'/'voice_mappings.json').read_text(encoding='utf-8'))
    assert len(saved['mappings']) == 3
    assert compact_text('打 开 地 图') == '打开地图'


def test_voice_exact_final_dispatches_custom_mapping(tmp_path):
    calls=[]
    service = VoiceService(tmp_path, lambda action: calls.append(action) or {'executed': True})
    service.mappings=[{'phrase':'打开地图','type':'keyboard','target':'M'}]
    service._match_and_execute('打开 地图')
    deadline=time.time()+1
    while not calls and time.time()<deadline: time.sleep(.01)
    assert calls == [{'type':'keyboard','target':'M','source':'voice'}]


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
    backend = (ROOT / 'output_backend.py').read_text(encoding='utf-8')
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
    assert service.mappings == [{'phrase':'地图','type':'keyboard','target':'M'}]
    assert (root / 'config' / 'voice_mappings.json').is_file()


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
    assert 'bytes_received' not in status and 'rms' not in status
    status, result = service.accept_phone_text('mobile_voice:phone-a', 'phone-a', '体感 攻击')
    deadline = time.time() + 1
    while not calls and time.time() < deadline:
        time.sleep(.01)
    assert result['matched'] is True
    assert calls == [{'type': 'keyboard', 'target': 'J', 'source': 'voice:mobile_voice:phone-a'}]
    service.disconnect('mobile_voice:phone-a')
    assert 'voice:mobile_voice:phone-a' in cleared
    assert service.status()['connected'] is False


def test_voice_text_bridge_accepts_only_active_phone_body_source_and_releases_on_switch():
    from input_bridge import InputBridge

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
        assert voice.accepted == [('mobile_voice:phone-a', 'phone-a', '体感 攻击', .9)]
        bridge.set_body_mode('computer')
        assert voice.disconnected == ['mobile_voice:phone-a']
        bridge.handle_message(peer, message)
        assert len(voice.accepted) == 1
        sensor_message = dict(message, role='sensor')
        bridge.set_body_mode('phone')
        bridge.handle_message(peer, sensor_message)
        assert len(voice.accepted) == 1
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
        monkeypatch.setattr('control_kernel.time.monotonic', lambda: clock[0])
        def feed(pose, count):
            for _ in range(count):
                clock[0] += 0.10
                kernel.handle_pose_map('camera', pose, width=640, height=480)

        _stub_head_controller(kernel, pitch=-.9)
        kernel.configure_scene_layout({
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
        kernel.configure_scene_layout({
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
