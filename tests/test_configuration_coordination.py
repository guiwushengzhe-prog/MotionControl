import base64
import copy
import importlib.util
import json
import threading
from pathlib import Path

import pytest

from motioncontrol import config_transaction
from motioncontrol.control_kernel import ControlKernel
from motioncontrol.view_control import configure_view_control
from motioncontrol_shared.profile_schema import flatten_bindings
from test_control_kernel import FakeOutput

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def kernel():
    made = ControlKernel(FakeOutput(), persist=True)
    yield made
    made.close()


def test_view_axes_commit_together_and_survive_restart(kernel):
    configure_view_control(kernel, "head_turn", "right")
    assert kernel.head_controller.config["enabled"]
    assert kernel.head_controller.config["horizontal_algorithm"] == "gesture_v188"
    assert kernel.hand_mouse_controller.config["horizontal_hand"] == "off"
    assert kernel.hand_mouse_controller.config["vertical_hand"] == "right"
    assert not kernel.vertical_look["enabled"]
    restored = ControlKernel(FakeOutput(), persist=True)
    try:
        assert restored.head_controller.config == kernel.head_controller.config
        assert restored.hand_mouse_controller.config == kernel.hand_mouse_controller.config
        assert not restored.vertical_look["enabled"]
    finally:
        restored.close()


def test_failed_second_view_file_restores_files_and_live_controllers(kernel, monkeypatch):
    configure_view_control(kernel, "left", "head")
    general = kernel._general_settings_path()
    head = Path(kernel.head_controller.profile_path)
    previous_files = {p: p.read_bytes() for p in (general, head)}
    previous_head = kernel.head_controller
    previous_hand = kernel.hand_mouse_controller
    previous_look = copy.deepcopy(kernel.vertical_look)
    original = config_transaction.atomic_bytes

    def fail_second(path, content):
        if Path(path) == head:
            raise OSError("disk full")
        original(path, content)

    monkeypatch.setattr(config_transaction, "atomic_bytes", fail_second)
    with pytest.raises(OSError, match="disk full"):
        configure_view_control(kernel, "head_turn", "right")
    assert kernel.head_controller is previous_head
    assert kernel.hand_mouse_controller is previous_hand
    assert kernel.vertical_look == previous_look
    assert {p: p.read_bytes() for p in previous_files} == previous_files
    assert not (general.parent / ".view-control.journal").exists()


def test_invalid_view_source_has_no_side_effects(kernel):
    head, hand = kernel.head_controller, kernel.hand_mouse_controller
    with pytest.raises(ValueError):
        configure_view_control(kernel, "left", "invalid")
    assert kernel.head_controller is head
    assert kernel.hand_mouse_controller is hand
    assert not kernel._general_settings_path().exists()


def test_startup_journal_restores_interrupted_files_and_removes_new_files(tmp_path):
    old, new = tmp_path / "old.json", tmp_path / "new.json"
    old.write_bytes(b"partial update")
    new.write_bytes(b"new update")
    journal = tmp_path / ".transaction.json"
    journal.write_text(json.dumps({"schema": 1, "files": [
        {"path": "old.json", "content": base64.b64encode(b"original").decode()},
        {"path": "new.json", "content": None},
    ]}))
    assert config_transaction.recover(journal)
    assert old.read_bytes() == b"original"
    assert not new.exists()
    assert not journal.exists()


@pytest.fixture
def application():
    # Import under the per-test user-data directory. No server sockets or
    # capture devices are opened; all background services are closed below.
    spec = importlib.util.spec_from_file_location("coordination_test_server", ROOT / "server.py")
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    yield app
    app.SYSTEM_COMMANDS.close()
    app.INPUT_BRIDGE.close()
    app.VOICE.close()
    app.RUNTIME.close()
    app.OUTPUT.close()


def game_bundle():
    return {"game_id": "generic-xbox", "overrides": {
        "zone.leftHand": {"action": {"type": "gamepad", "target": "Y", "behavior": "hold"}},
        "voice.game.profile_slot_1": {"phrase": "换方案", "action": {"type": "gamepad", "target": "B", "behavior": "tap"}},
    }, "motions": []}


def test_install_game_bundle_updates_both_consumers(application):
    app = application
    app._install_game_bundle(game_bundle())
    bindings = app.PROFILES.effective_profile()["bindings"]
    assert app.KERNEL.control_bindings == flatten_bindings(bindings)
    assert app.VOICE._profile_bindings == bindings
    assert bindings["voice"]["game.profile_slot_1"]["phrase"] == "换方案"
    assert app.CONFIG_REVISION == 1


def test_failed_bundle_restores_disk_memory_and_both_consumers(application, monkeypatch):
    app = application
    app._install_game_bundle(game_bundle())
    paths = [app.PROFILES.selection_path, app.PROFILES.custom_games_path, app.MOTION_CONFIG_FILE]
    files = {p: p.read_bytes() if p.exists() else None for p in paths}
    selection = copy.deepcopy(app.PROFILES._selection)
    bindings = copy.deepcopy(app.KERNEL.control_bindings)
    voice = copy.deepcopy(app.VOICE._profile_bindings)

    def fail(items):
        raise OSError("disk full")

    monkeypatch.setattr(app, "save_motion_config", fail)
    changed = game_bundle()
    changed["overrides"]["zone.leftHand"]["action"]["target"] = "A"
    with pytest.raises(OSError, match="disk full"):
        app._install_game_bundle(changed)
    assert app.PROFILES._selection == selection
    assert app.KERNEL.control_bindings == bindings
    assert app.VOICE._profile_bindings == voice
    assert {p: p.read_bytes() if p.exists() else None for p in paths} == files
    assert app.CONFIG_REVISION == 1


def test_emergency_releases_output_before_kernel_cleanup(application, monkeypatch):
    app, seen = application, []
    monkeypatch.setattr(app.SYSTEM_COMMANDS, "invalidate", lambda: seen.append("invalidate"))
    monkeypatch.setattr(app.OUTPUT, "emergency_stop", lambda: seen.append("release") or {})
    monkeypatch.setattr(app, "_broadcast_game_output_state", lambda: seen.append("broadcast"))
    monkeypatch.setattr(app.KERNEL, "cancel_calibration", lambda *args: seen.append("cleanup"))
    app.emergency_stop_all()
    assert seen[:2] == ["invalidate", "release"]
    assert seen.index("release") < seen.index("cleanup")


def test_output_command_waiting_for_lock_cannot_resume_after_emergency(application, monkeypatch):
    app = application
    entered = threading.Event()
    original = app.SYSTEM_COMMANDS.is_current
    results = []

    def check(generation):
        result = original(generation)
        entered.set()
        return result

    monkeypatch.setattr(app.SYSTEM_COMMANDS, "is_current", check)
    with app.OUTPUT._lock:
        worker = threading.Thread(target=lambda: results.append(
            app.execute_system_target("OUTPUT.START", command_generation=0)))
        worker.start()
        assert entered.wait(2)
        app.emergency_stop_all()
    worker.join(2)
    assert not worker.is_alive()
    assert not results[0]["executed"]
    assert not app.OUTPUT.enabled


def test_center_command_cannot_commit_after_waiting_across_emergency(application, monkeypatch):
    app = application
    entered, release = threading.Event(), threading.Event()
    original = app.SYSTEM_COMMANDS.is_current
    results, centered = [], []
    first = True

    def check(generation):
        nonlocal first
        result = original(generation)
        if first:
            first = False
            entered.set()
            assert release.wait(2)
        return result

    monkeypatch.setattr(app.SYSTEM_COMMANDS, "is_current", check)
    monkeypatch.setattr(app.KERNEL, "set_current_center", lambda: centered.append(True))
    worker = threading.Thread(target=lambda: results.append(
        app.execute_system_target("HEAD.CENTER", command_generation=0)))
    worker.start()
    assert entered.wait(2)
    app.emergency_stop_all()
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    assert not results[0]["executed"]
    assert centered == []


def test_calibration_cannot_start_after_emergency_during_source_check(application, monkeypatch):
    app = application
    entered, release = threading.Event(), threading.Event()
    results, started = [], []

    def active(source):
        entered.set()
        assert release.wait(2)
        return True

    monkeypatch.setattr(app.VOICE, "source_is_active", active)
    monkeypatch.setattr(app.RUNTIME, "body_mode", "phone")
    monkeypatch.setattr(app.KERNEL, "status", lambda: {"active_body_source": "phone"})
    monkeypatch.setattr(app.RUNTIME, "start_calibration", lambda: started.append(True))
    worker = threading.Thread(target=lambda: results.append(app.execute_voice_action(
        {"type": "system", "target": "HEAD.CALIBRATE", "_command_generation": 0})))
    worker.start()
    assert entered.wait(2)
    app.emergency_stop_all()
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    assert not results[0]["executed"]
    assert started == []


def test_phone_disconnect_during_trigger_note_cannot_enable_output(application, monkeypatch):
    app = application
    entered, release = threading.Event(), threading.Event()
    results = []
    with app.VOICE._lock:
        app.VOICE._activate_locked("phone", "phone", "phone")
    generation = app.VOICE.phone_source_generation("phone")

    def note(action):
        entered.set()
        assert release.wait(2)

    monkeypatch.setattr(app, "_note_voice_trigger", note)
    worker = threading.Thread(target=lambda: results.append(app._execute_queued_command({
        "type": "system", "target": "OUTPUT.START", "_command_generation": 0,
        "voice_source_id": "phone", "voice_source_generation": generation})))
    worker.start()
    assert entered.wait(2)
    app.VOICE.invalidate_phone_source("phone")
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    assert not results[0]["executed"]
    assert not app.OUTPUT.enabled


def test_journal_commit_failure_restores_consumers_before_unlock(application, monkeypatch):
    app = application
    app._install_game_bundle(game_bundle())
    old_kernel = copy.deepcopy(app.KERNEL.control_bindings)
    old_voice = copy.deepcopy(app.VOICE._profile_bindings)
    old_file = app.PROFILES.selection_path.read_bytes()
    journal = app.user_data_root() / ".profile-install.journal"
    original = Path.unlink
    failed = False

    def fail_commit(path, *args, **kwargs):
        nonlocal failed
        if path == journal and not failed:
            failed = True
            assert app.KERNEL._lock._is_owned()
            assert app.VOICE._lock._is_owned()
            raise OSError("journal commit failed")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_commit)
    changed = game_bundle()
    changed["overrides"]["zone.leftHand"]["action"]["target"] = "A"
    with pytest.raises(OSError, match="journal commit failed"):
        app._install_game_bundle(changed)
    assert failed
    assert app.KERNEL.control_bindings == old_kernel
    assert app.VOICE._profile_bindings == old_voice
    assert app.PROFILES.selection_path.read_bytes() == old_file
    assert app.CONFIG_REVISION == 1
    assert not journal.exists()
