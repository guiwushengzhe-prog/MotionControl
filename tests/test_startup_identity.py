import json
from pathlib import Path

from motioncontrol.game_profiles import GameProfileStore
from motioncontrol.startup_identity import source_revision, startup_snapshot

ROOT = Path(__file__).resolve().parents[1]


def test_loaded_code_revision_changes_even_when_version_does_not(tmp_path):
    (tmp_path / 'server.py').write_text('version = "test-version"')
    module = tmp_path / 'motioncontrol' / 'code.py'
    module.parent.mkdir()
    module.write_text('behavior = 1')
    first = source_revision(tmp_path)
    module.write_text('behavior = 2')
    assert source_revision(tmp_path) != first


def test_startup_snapshot_uses_complete_custom_game_configuration(isolated_user_data):
    store = GameProfileStore(ROOT)
    game = store.add_custom_game('和平精英')
    store.select(game['id'])
    store.set_overrides({'zone.leftHand': {'action': {'type': 'keyboard', 'target': 'K'}}})
    state = startup_snapshot(ROOT)
    assert state['selected_id'] == game['id']
    assert state['configuration_revision'] == store.revision()
    assert state['user_data_root'] == str(isolated_user_data.resolve())
    assert state['source_revision'] == source_revision(ROOT)


def test_batch_entry_forwards_the_same_startup_options():
    assert 'START.ps1" %*' in (ROOT / 'START.bat').read_text(encoding='utf-8')
