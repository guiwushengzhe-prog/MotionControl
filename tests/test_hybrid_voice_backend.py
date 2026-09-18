import time
from pathlib import Path

from motioncontrol.funasr_command_backend import build_hotwords, find_fsmn_vad_model, find_funasr_python, find_seaco_model
from motioncontrol.hybrid_voice_backend import HybridWakeAsrRecognizer, resolve_command_candidate


MAPPINGS = [
    {'phrase': '截图', 'synonyms': ['记录场景'], 'type': 'system', 'target': 'SCENE.CAPTURE_REFERENCE'},
    {'phrase': '加速', 'synonyms': ['快一点'], 'type': 'keyboard', 'target': 'W'},
]


class FakeWake:
    def __init__(self):
        self.fire = True
        self.resets = 0

    def accept(self, pcm16):
        if self.fire:
            self.fire = False
            return True
        return False

    def reset(self):
        self.resets += 1


class FakeAsr:
    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = []
        self.ready = True

    def start_async(self):
        pass

    def transcribe(self, pcm16, *, sample_rate, hotwords):
        self.calls.append((pcm16, sample_rate, list(hotwords)))
        text = self.texts.pop(0) if self.texts else ''
        return {'text': text, 'latency_ms': 23.0}

    def status(self):
        return {'ready': True, 'running': True, 'last_error': None}

    def close(self):
        pass


def pcm(level=1200, samples=1600):
    value = int(level).to_bytes(2, 'little', signed=True)
    return value * samples


def make_recognizer(tmp_path, texts):
    return HybridWakeAsrRecognizer(
        tmp_path, tmp_path, tmp_path, tmp_path, tmp_path,
        '体感', MAPPINGS, ['体感紧急停止'],
        wake_recognizer=FakeWake(), asr_client=FakeAsr(texts),
    )


def wait_event(recognizer, timeout=1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        event = recognizer.accept(pcm(0))
        if event:
            return event
        time.sleep(0.01)
    return None


def test_resolve_seaco_text_is_exact_and_strips_wake_word():
    assert resolve_command_candidate('体感 截图。', '体感', MAPPINGS, ['体感紧急停止']) == '截图'
    assert resolve_command_candidate('记录场景', '体感', MAPPINGS, ['体感紧急停止']) == '截图'
    assert resolve_command_candidate('体感紧急停止', '体感', MAPPINGS, ['体感紧急停止']) == '体感紧急停止'
    assert resolve_command_candidate('紧急停止', '体感', MAPPINGS, ['体感紧急停止']) == '体感紧急停止'
    # Safety: do not guess a different command with fuzzy matching.
    assert resolve_command_candidate('加数', '体感', MAPPINGS, ['体感紧急停止']) is None


def test_hotwords_include_wake_commands_and_synonyms():
    words = build_hotwords('体感', MAPPINGS, ['体感紧急停止'])
    assert words[0] == '体感'
    assert '截图' in words and '记录场景' in words
    assert '加速' in words and '快一点' in words


def test_continuous_wake_command_can_be_resolved_from_probe(tmp_path):
    rec = make_recognizer(tmp_path, ['体感截图'])
    first = rec.accept(pcm())
    assert first == {'kind': 'wake', 'text': '体感'}
    rec._wake_at -= 0.8
    rec.accept(pcm(0))  # starts asynchronous probe
    event = wait_event(rec)
    assert event['kind'] == 'final'
    assert event['text'] == '截图'
    assert event['raw_text'] == '体感截图'


def test_two_stage_wake_then_command_uses_final_decode(tmp_path):
    # Probe sees only wake word, final decode sees the actual second utterance.
    rec = make_recognizer(tmp_path, ['体感', '体感 截图'])
    assert rec.accept(pcm())['kind'] == 'wake'
    rec._wake_at -= 0.8
    rec.accept(pcm(0))
    time.sleep(0.03)
    rec.accept(pcm(0))  # consumes empty probe
    # command speech, then force endpoint silence
    rec.accept(pcm(1600))
    rec._last_speech_at -= 0.7
    rec.accept(pcm(0))
    event = wait_event(rec)
    assert event['kind'] == 'final'
    assert event['text'] == '截图'
    assert len(rec.asr_client.calls) >= 2


def test_unconfigured_asr_text_is_rejected_not_guessed(tmp_path):
    rec = make_recognizer(tmp_path, ['体感天气', '体感天气'])
    assert rec.accept(pcm())['kind'] == 'wake'
    rec._wake_at -= 0.8
    rec.accept(pcm(0))
    time.sleep(0.03)
    rec.accept(pcm(0))  # consume probe; keep waiting because it was not a command
    rec.wake_until = time.monotonic() - 0.1
    rec.accept(pcm(0))  # force final decode at command-window timeout
    event = wait_event(rec)
    assert event['kind'] == 'unmatched'
    assert event['raw_text'] == '体感天气'


def test_funasr_paths_prefer_explicit_config(tmp_path: Path):
    root = tmp_path / 'app'
    cfg = root / 'config'
    cfg.mkdir(parents=True)
    py = tmp_path / 'asr-python.exe'
    py.write_bytes(b'x')
    seaco = tmp_path / 'seaco'
    seaco.mkdir()
    (seaco / 'config.yaml').write_text('x', encoding='utf-8')
    (seaco / 'model.pt').write_bytes(b'x')
    vad = tmp_path / 'vad'
    vad.mkdir()
    (vad / 'config.yaml').write_text('x', encoding='utf-8')
    (vad / 'model.pt').write_bytes(b'x')
    (cfg / 'funasr_python_path.txt').write_text(str(py), encoding='utf-8')
    (cfg / 'funasr_seaco_model_path.txt').write_text(str(seaco), encoding='utf-8')
    (cfg / 'funasr_vad_model_path.txt').write_text(str(vad), encoding='utf-8')
    assert find_funasr_python(root) == py.resolve()
    assert find_seaco_model(root) == seaco.resolve()
    assert find_fsmn_vad_model(root) == vad.resolve()
