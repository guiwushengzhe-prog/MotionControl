"""Failures and slow consumers must not corrupt settings or stall audio producers."""

import base64
import json
import queue
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from motioncontrol import voice_backend
from motioncontrol.command_executor import CommandExecutor
from motioncontrol.voice_backend import AUDIO_QUEUE_CAPACITY, VoiceService, _AudioFrame


class Recognizer:
    supported = []
    unsupported = []
    unheard = {}
    mode = "test"

    def __init__(self, accept=None):
        self.calls = []
        self.resets = 0
        self._accept = accept

    def accept(self, data):
        self.calls.append(data)
        return self._accept(data) if self._accept else {"kind": "final", "text": "体感甲令"}

    def reset(self):
        self.resets += 1

    def close(self):
        pass


def service(tmp_path, **options):
    calls = []
    item = VoiceService(tmp_path, lambda action: calls.append(action) or {"executed": True}, **options)
    item.configure([{"phrase": "甲令", "type": "keyboard", "target": "F1"},
                    {"phrase": "乙令", "type": "keyboard", "target": "F2"}], wake_word="体感")
    item.recognizer = Recognizer()
    item.audio_ready = True
    return item, calls


def test_second_settings_replace_failure_restores_both_files_and_active_memory(tmp_path, monkeypatch):
    released = []
    item, _ = service(tmp_path, clear_source=lambda source: released.append(source))
    item.connect_phone_text("phone", "device")
    before = (item.config_path.read_bytes(), item.personal_path.read_bytes(),
              list(item.mappings), item.wake_word, dict(item.command_registry), list(released))
    real_replace = voice_backend.os.replace
    failed = False

    def fail_second(source, target):
        nonlocal failed
        if target == item.personal_path and not failed:
            failed = True
            raise OSError("disk full")
        real_replace(source, target)

    monkeypatch.setattr(voice_backend.os, "replace", fail_second)
    try:
        with pytest.raises(OSError, match="disk full"):
            item.configure([{"phrase": "新令", "type": "keyboard", "target": "F3"}], wake_word="动作")
        assert item.config_path.read_bytes() == before[0]
        assert item.personal_path.read_bytes() == before[1]
        assert item.mappings == before[2]
        assert item.wake_word == before[3]
        assert item.command_registry == before[4]
        assert released == before[5]
        assert item.source_is_active("phone")
        assert not item._voice_journal.exists()
    finally:
        item.close()


def test_interrupted_settings_commit_recovers_before_loading(tmp_path):
    item, _ = service(tmp_path)
    previous = {"mappings": base64.b64encode(item.config_path.read_bytes()).decode(),
                "personal": base64.b64encode(item.personal_path.read_bytes()).decode()}
    original_mappings, original_wake = list(item.mappings), item.wake_word
    item._voice_journal.write_text(json.dumps(previous), encoding="utf-8")
    item.config_path.write_text('{"mappings": []}', encoding="utf-8")
    item.close()
    restored = VoiceService(tmp_path, lambda action: {"executed": True})
    try:
        assert restored.mappings == original_mappings
        assert restored.wake_word == original_wake
        assert not restored._voice_journal.exists()
    finally:
        restored.close()


def test_microphone_callback_never_waits_for_recognition_lock_even_on_overflow(tmp_path):
    item, _ = service(tmp_path)
    item._mic_queue = queue.Queue(maxsize=1)
    item._mic_queue.put(b"full")
    done = threading.Event()
    thread = threading.Thread(target=lambda: (item._mic_callback(b"\0\0", 1, None, "overflow"), done.set()))
    try:
        with item._lock:
            thread.start()
            assert done.wait(0.5), "audio callback waited for the recognition lock"
        assert item._mic_gap.is_set()
        assert item.audio_dropped_frames == 1
    finally:
        thread.join(1.0)
        item.close()


def test_phone_audio_producer_stays_fast_and_bounded_while_recognizer_is_busy(tmp_path):
    item, _ = service(tmp_path)
    entered, resume = threading.Event(), threading.Event()
    item.recognizer = Recognizer(lambda data: (entered.set(), resume.wait(2.0), None)[-1])
    try:
        item.accept_phone_audio("phone", "device", b"\0\0", sequence=0)
        assert entered.wait(1.0)
        done = threading.Event()

        def enqueue():
            for number in range(1, 30):
                item.accept_phone_audio("phone", "device", b"\0\0", sequence=number)
            done.set()

        producer = threading.Thread(target=enqueue)
        producer.start()
        assert done.wait(0.5), "WebSocket producer waited for recognition"
        assert item._phone_queue.qsize() <= AUDIO_QUEUE_CAPACITY
        assert item.audio_dropped_frames > 0
        resume.set()
        producer.join(1.0)
        item._phone_queue.join()
    finally:
        resume.set()
        item.close()


def test_phone_audio_callbacks_and_actions_keep_fifo_order(tmp_path):
    item, calls = service(tmp_path)
    item.recognizer = Recognizer(lambda data: {"kind": "final", "text": "体感甲令" if data == b"\1\0" else "体感乙令"})
    results = []
    try:
        item.accept_phone_audio("phone", "device", b"\1\0", sequence=10,
                                result_callback=lambda event, result: results.append(result))
        item.accept_phone_audio("phone", "device", b"\2\0", sequence=11,
                                result_callback=lambda event, result: results.append(result))
        item._phone_queue.join()
        assert [action["target"] for action in calls] == ["F1", "F2"]
        assert [result["command"] for result in results] == ["甲令", "乙令"]
    finally:
        item.close()


def test_phone_audio_sequence_gap_resets_before_next_chunk(tmp_path):
    item, _ = service(tmp_path)
    try:
        item.accept_phone_audio("phone", "device", b"\1\0", sequence=10)
        item._phone_queue.join()
        before = item.recognizer.resets
        item.accept_phone_audio("phone", "device", b"\2\0", sequence=12)
        item._phone_queue.join()
        assert item.recognizer.resets == before + 1
        assert item.audio_stream_resets == 1
    finally:
        item.close()


def test_stale_capture_and_queued_audio_never_reach_recognition(tmp_path):
    item, _ = service(tmp_path)
    try:
        _, _, result = item.accept_phone_audio("phone", "device", b"\0\0", captured_at_ms=(time.time() - 2) * 1000)
        assert result["reason"] == "audio_frame_stale"
        frame = _AudioFrame("phone", "device", b"\0\0", time.monotonic() - 2,
                            item._audio_generation, 10)
        assert item._consume_audio(frame, "phone") is None
        assert item.recognizer.calls == []
    finally:
        item.close()


def test_result_that_becomes_stale_during_recognition_does_not_execute(tmp_path, monkeypatch):
    item, calls = service(tmp_path)
    clock = [100.0]
    monkeypatch.setattr(voice_backend, "time", SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 1000.0))

    def recognize(data):
        clock[0] += 1.0
        return {"kind": "final", "text": "体感甲令"}

    item.recognizer = Recognizer(recognize)
    try:
        frame = _AudioFrame("phone", "device", b"\0\0", 100.0, item._audio_generation, 10)
        assert item._consume_audio(frame, "phone") == (None, None)
        assert calls == []
        assert item.audio_stream_resets == 1
    finally:
        item.close()


def test_system_actions_use_injected_dispatcher_with_source_identity(tmp_path):
    submitted = []
    item, _ = service(tmp_path, on_system_command=lambda action: submitted.append(action) or {"queued": True})
    item.configure([{"phrase": "先令", "type": "system", "target": "OUTPUT.START"},
                    {"phrase": "后令", "type": "system", "target": "OUTPUT.STOP"}])
    try:
        item.accept_phone_text("phone", "device", "体感先令")
        item.accept_phone_text("phone", "device", "体感后令")
        assert [action["target"] for action in submitted] == ["OUTPUT.START", "OUTPUT.STOP"]
        assert all(action["voice_source_id"] == "phone" for action in submitted)
        assert item.last_executed is None
    finally:
        item.close()


def test_running_microphone_continues_after_configuration_rebuild(tmp_path, monkeypatch):
    item, calls = service(tmp_path)
    streams = []

    class Stream:
        def __init__(self, **options):
            self.callback = options["callback"]
            streams.append(self)

        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(
        RawInputStream=Stream, query_devices=lambda *args: {"name": "test microphone"},
        check_input_settings=lambda **options: None))
    try:
        item.start_local_microphone()
        item.configure([{"phrase": "甲令", "type": "keyboard", "target": "F3"}])
        item.recognizer = Recognizer()
        item.audio_ready = True
        streams[0].callback(b"\0\0", 1, SimpleNamespace(currentTime=20.0, inputBufferAdcTime=19.9), "")
        item._mic_queue.join()
        assert [action["target"] for action in calls] == ["F3"]
    finally:
        item.close()


def test_old_microphone_stream_cannot_feed_a_restarted_session(tmp_path, monkeypatch):
    item, _ = service(tmp_path)
    callbacks = []

    class Stream:
        def __init__(self, **options):
            callbacks.append(options["callback"])

        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(
        RawInputStream=Stream, query_devices=lambda *args: {"name": "test microphone"},
        check_input_settings=lambda **options: None))
    try:
        item.start_local_microphone()
        item.start_local_microphone()
        callbacks[0](b"\0\0", 1, None, "")
        assert item._mic_queue.qsize() == 0
        assert item.recognizer.calls == []
        callbacks[1](b"\0\0", 1, None, "")
        item._mic_queue.join()
        assert item.recognizer.calls == [b"\0\0"]
    finally:
        item.close()


def test_fast_phone_invalidation_discards_inflight_recognition_without_waiting(tmp_path):
    item, calls = service(tmp_path)
    entered, resume, invalidated = threading.Event(), threading.Event(), threading.Event()
    item.recognizer = Recognizer(lambda data: (entered.set(), resume.wait(2.0),
                                              {"kind": "final", "text": "体感甲令"})[-1])
    try:
        item.accept_phone_audio("phone", "device", b"\0\0", sequence=0)
        assert entered.wait(1.0)
        thread = threading.Thread(target=lambda: (item.invalidate_phone_source("phone"), invalidated.set()))
        thread.start()
        assert invalidated.wait(0.5), "source invalidation waited for recognition"
        resume.set()
        thread.join(1.0)
        item._phone_queue.join()
        assert calls == []
        assert not item.source_is_active("phone")
    finally:
        resume.set()
        item.close()


def test_deferred_old_cleanup_does_not_disconnect_reconnected_same_device(tmp_path):
    item, _ = service(tmp_path)
    try:
        item.connect_phone_text("phone", "device")
        original_generation = item._audio_generation
        token = item.invalidate_phone_source("phone")
        item.connect_phone_text("phone", "device")
        item.disconnect("phone", expected_generation=token)
        assert item.source_is_active("phone")
        assert not item.source_is_active("phone", expected_generation=original_generation)
    finally:
        item.close()


def test_pending_settings_write_does_not_publish_candidate_to_status_readers(tmp_path, monkeypatch):
    item, _ = service(tmp_path)
    before = item.status()
    entered, resume, finished = threading.Event(), threading.Event(), threading.Event()
    stage = item._stage_bytes

    def slow_stage(path, content):
        if path == item.config_path:
            entered.set()
            assert resume.wait(2.0)
        return stage(path, content)

    monkeypatch.setattr(item, "_stage_bytes", slow_stage)
    thread = threading.Thread(target=lambda: (item.configure(
        [{"phrase": "新令", "type": "keyboard", "target": "F3"}], wake_word="动作"), finished.set()))
    try:
        thread.start()
        assert entered.wait(1.0)
        pending = item.status()
        assert pending["mappings"] == before["mappings"]
        assert pending["wake_word"] == before["wake_word"]
        resume.set()
        assert finished.wait(1.0)
        assert item.wake_word == "动作"
    finally:
        resume.set()
        thread.join(2.0)
        item.close()


@pytest.mark.parametrize("kind", ["audio", "text", "command"])
def test_delayed_phone_submission_cannot_renew_an_invalidated_admission(tmp_path, kind):
    item, calls = service(tmp_path)
    item.connect_phone_text("phone", "device")
    admitted = item.phone_source_generation("phone")
    result = []
    entered = threading.Event()

    def submit():
        entered.set()
        if kind == "audio":
            result.append(item.accept_phone_audio("phone", "device", b"\0\0", expected_generation=admitted)[2])
        elif kind == "text":
            result.append(item.accept_phone_text("phone", "device", "体感甲令", expected_generation=admitted)[1])
        else:
            result.append(item.accept_phone_command("phone", "device", "system.output_start", expected_generation=admitted)[1])

    try:
        # Text/commands may already have left the bridge while waiting for
        # recognition; audio admission also compares the captured token.
        with item._lock:
            if kind == "audio":
                item.invalidate_phone_source("phone")
            thread = threading.Thread(target=submit)
            thread.start()
            assert entered.wait(1.0)
            if kind != "audio":
                item.invalidate_phone_source("phone")
        thread.join(1.0)
        assert not thread.is_alive()
        assert result == [{"matched": False, "reason": "voice_source_generation_changed"}]
        assert not item.source_is_active("phone")
        assert "phone" in item._invalidated_phone_sources
        assert calls == []
        assert item._phone_queue.empty()
    finally:
        item.close()


def test_cleanup_keeps_admission_for_reconnected_device(tmp_path):
    item, calls = service(tmp_path)
    try:
        item.connect_phone_text("phone", "device")
        token = item.invalidate_phone_source("phone")
        assert item.phone_source_generation("phone") == token
        item.disconnect("phone", expected_generation=token)
        assert item.phone_source_generation("phone") == token
        _, result = item.accept_phone_text("phone", "device", "体感甲令", expected_generation=token)
        assert result["matched"]
        assert item.source_is_active("phone")
        assert [action["target"] for action in calls] == ["F1"]
    finally:
        item.close()


def test_command_source_guard_does_not_wait_for_recognition_and_serializes_invalidation(tmp_path):
    item, _ = service(tmp_path)
    item.connect_phone_text("phone", "device")
    generation = item.phone_source_generation("phone")
    committed, invalidate_entered, invalidated = threading.Event(), threading.Event(), threading.Event()
    events = []

    def commit():
        with item.command_source_guard("phone", generation) as active:
            assert active
            events.append("commit")
            committed.set()
            assert invalidate_entered.wait(1.0)
            assert not invalidated.is_set()

    def invalidate():
        invalidate_entered.set()
        item.invalidate_phone_source("phone")
        events.append("invalidate")
        invalidated.set()

    try:
        with item._lock:
            commit_thread = threading.Thread(target=commit)
            commit_thread.start()
            assert committed.wait(1.0), "source guard acquired the recognizer lock"
            invalidation_thread = threading.Thread(target=invalidate)
            invalidation_thread.start()
            assert invalidated.wait(1.0)
        commit_thread.join(1.0)
        invalidation_thread.join(1.0)
        assert events == ["commit", "invalidate"]
        with item.command_source_guard("phone", generation) as active:
            assert not active
    finally:
        item.close()


def test_game_slot_system_override_is_queued_with_both_generations_and_cancelled(tmp_path):
    entered, release = threading.Event(), threading.Event()
    executed, queued = [], []

    def execute(action):
        if action["target"] == "blocker":
            entered.set()
            assert release.wait(2.0)
        else:
            executed.append(action)

    dispatcher = CommandExecutor(execute)
    dispatcher.submit({"target": "blocker"})
    assert entered.wait(1.0)

    def submit(action):
        result = dispatcher.submit(action)
        with dispatcher._condition:
            queued.extend(dict(item) for item in dispatcher._pending)
        return result

    item, immediate = service(tmp_path, on_system_command=submit)
    item._catalog = [{"id": "game.profile_slot_01", "kind": "keyboard", "default_target": "F1",
                      "phrase": "体感功能一"}]
    item.configure_profile_bindings({"voice": {"game.profile_slot_01": {
        "phrase": "开输出", "action": {"type": "system", "target": "OUTPUT.START", "behavior": "tap"}}}})
    try:
        item.accept_phone_text("phone", "device", "开输出")
        assert immediate == []
        assert len(queued) == 1
        action = queued[0]
        assert action["type"] == "system"
        assert action["target"] == "OUTPUT.START"
        assert action["command_id"] == "game.profile_slot_01"
        assert action["voice_source_id"] == "phone"
        assert action["voice_source_generation"] == item.phone_source_generation("phone")
        assert action["_command_generation"] == 0
        dispatcher.invalidate()
        release.set()
        dispatcher.close()
        assert executed == []
    finally:
        release.set()
        dispatcher.close()
        item.close()


def test_disabled_system_override_and_keyboard_slot_keep_ordinary_dispatch(tmp_path):
    queued = []
    item, immediate = service(tmp_path, on_system_command=lambda action: queued.append(action) or {"queued": True})
    item._catalog = [{"id": "game.profile_slot_01", "kind": "keyboard", "default_target": "F1",
                      "phrase": "体感功能一"}]
    try:
        for binding in ({"disabled": True, "action": {"type": "system", "target": "OUTPUT.START"}},
                        {"action": {"type": "gamepad", "target": "A"}}):
            item.configure_profile_bindings({"voice": {"game.profile_slot_01": {"phrase": "开输出", **binding}}})
            item.accept_phone_text("phone", "device", "开输出")
        assert queued == []
        assert len(immediate) == 2  # The boundary still owns disabled/non-system mapping.
        assert all(action["type"] == "keyboard" for action in immediate)
    finally:
        item.close()
