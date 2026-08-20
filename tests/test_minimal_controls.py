from pathlib import Path
import json
import sys
import time
import types

from output_backend import OutputManager, XUSB_GAMEPAD_BUTTONS, GAMEPAD_AXES
from voice_backend import VoiceService, VoskCommandRecognizer, compact_text

ROOT = Path(__file__).resolve().parents[1]


def test_only_full_model_is_registered():
    server = (ROOT / 'server.py').read_text(encoding='utf-8')
    assert 'pose_landmarker_full.task' in server
    assert 'pose_landmarker_lite.task' not in server
    assert 'VERSION = "0.7.6"' in server


def test_main_ui_stays_compact_and_settings_hold_complex_options():
    page = (ROOT / 'web' / 'index.html').read_text(encoding='utf-8')
    for required in ['身体相对区域会跟着人移动', '自动头控校准', '当前姿势设为中心', 'Xbox 360 右摇杆', '开启输出 F8', '紧急停止 F9', 'id="settingsBtn"', '四个动作与按键', '语音映射', '头控微调']:
        assert required in page
    for removed in ['开始 30 秒性能测试', '静止抖动测试', '实时性能数据', 'Lite / Full 对比结果', 'modelSelect']:
        assert removed not in page
    assert 'settings-mask' in page


def test_body_relative_zones_use_both_wrists_and_both_feet():
    kernel = (ROOT / 'control_kernel.py').read_text(encoding='utf-8')
    app = (ROOT / 'web' / 'app.js').read_text(encoding='utf-8')
    for name in ['left_wrist','right_wrist','left_ankle','right_ankle','left_foot_index','right_foot_index']:
        assert name in kernel
    for zone in ['leftHandUpper','leftHandLower','rightHandUpper','rightHandLower','leftFoot','rightFoot']:
        assert zone in kernel
    assert '0.36 * torso_px' in kernel
    assert '0.42 * torso_px' in kernel
    assert 'state["inside"] >= 2' in kernel and 'state["outside"] >= 2' in kernel
    assert 'set_buttons, keys, source="zones"' in kernel
    assert '/api/kernel/status' in app
    assert 'detectForVideo' not in app


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
    assert '当前使用：默认参数' in kernel
    assert 'CALIBRATION_TOTAL_DURATION_S = 7.5' in kernel
    assert 'cancel_calibration' in kernel
    assert 'setCurrentCenter' in app
    assert '"calibrated": True' in kernel
    assert '_torso_length' in kernel


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


def test_vosk_grammar_is_built_directly_from_custom_phrases(tmp_path, monkeypatch):
    calls=[]
    class FakeModel:
        def __init__(self, path): self.path=path
        def vosk_model_find_word(self, value): return 1 if value in {'地图','闪避'} else -1
    class FakeRecognizer:
        def __init__(self, *args): calls.append(args)
        def AcceptWaveform(self, pcm): return False
        def PartialResult(self): return '{"partial":""}'
    fake=types.SimpleNamespace(Model=FakeModel,KaldiRecognizer=FakeRecognizer,SetLogLevel=lambda value:None)
    monkeypatch.setitem(sys.modules,'vosk',fake)
    model=tmp_path/'vosk-model-small-cn-0.22'; model.mkdir()
    rec=VoskCommandRecognizer(model,['地图','闪避'])
    assert rec.mode == 'grammar'
    grammar=json.loads(calls[0][2])
    assert grammar == ['地图','闪避','[unk]']


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


def test_voice_api_uses_local_mic_or_phone_text_not_browser_audio():
    server = (ROOT / 'server.py').read_text(encoding='utf-8')
    assert '/api/voice/status' in server
    assert '/api/voice/config' in server
    assert '/api/voice/audio' in server
    assert 'browser voice endpoint disabled' in server
    assert '/ws/input voice_text' in server
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
