from pathlib import Path

from sherpa_kws_backend import _spoken_command_candidates, find_sherpa_kws_model


def test_kws_excludes_one_character_commands_but_keeps_safe_aliases():
    mappings = [
        {'phrase': '上', 'synonyms': ['向上', '往上']},
        {'phrase': '截图', 'synonyms': ['记录场景']},
        {'phrase': '加速', 'synonyms': []},
    ]
    pairs = _spoken_command_candidates('体感', mappings, ['体感紧急停止'])
    assert ('上', '上') not in pairs
    assert ('向上', '向上') in pairs
    assert ('截图', '截图') in pairs
    assert ('加速', '加速') in pairs
    assert ('紧急停止', '体感紧急停止') in pairs


def test_kws_model_path_prefers_explicit_config(tmp_path: Path):
    root = tmp_path / 'app'
    configured = tmp_path / 'speech-model'
    configured.mkdir(parents=True)
    for name in [
        'encoder-epoch-13-avg-2-chunk-16-left-64.onnx',
        'decoder-epoch-13-avg-2-chunk-16-left-64.onnx',
        'joiner-epoch-13-avg-2-chunk-16-left-64.onnx',
        'tokens.txt', 'en.phone',
    ]:
        (configured / name).write_bytes(b'x')
    (root / 'config').mkdir(parents=True)
    (root / 'config' / 'sherpa_kws_model_path.txt').write_text(str(configured), encoding='utf-8')
    assert find_sherpa_kws_model(root) == configured.resolve()
