"""Keep sensor sessions and asynchronous phone results owned by their socket."""

from __future__ import annotations

import base64
import threading

import pytest

from motioncontrol.input_bridge import InputBridge
from test_mobile_input_bridge import FakeOutput, FakePeer, sensor_frame
from test_control_coordination import BridgeKernel
from test_mobile_input_bridge import pose_frame


class AsyncVoice:
    def __init__(self):
        self.frames = []
        self.disconnected = []

    def accept_phone_audio(self, source, device, pcm, *, sequence=None,
                           captured_at_ms=None, result_callback=None):
        self.frames.append((source, sequence, captured_at_ms, result_callback))
        return {}, None, None

    def disconnect(self, source):
        self.disconnected.append(source)


def audio_frame(sequence=0, captured_at_ms=1000):
    return {"type": "voice_audio", "role": "camera", "device_id": "phone",
            "sequence": sequence, "captured_at_ms": captured_at_ms,
            "sample_rate": 16000, "channels": 1, "encoding": "pcm16le",
            "audio_base64": base64.b64encode(b"\0\0" * 1600).decode("ascii")}


@pytest.fixture()
def audio_bridge():
    voice = AsyncVoice()
    bridge = InputBridge(FakeOutput(), voice=voice)
    bridge.set_audio_mode("phone")
    yield bridge, voice
    bridge.close()


def test_current_async_result_keeps_its_originating_sequence(audio_bridge):
    bridge, voice = audio_bridge
    phone = FakePeer()
    bridge._handle_voice_audio(phone, audio_frame(7))
    bridge._handle_voice_audio(phone, audio_frame(8))
    assert phone.messages == [], "PCM admission must not pretend recognition already finished"
    voice.frames[0][3]({"kind": "final", "text": "体感跳跃"}, {"matched": True})
    assert phone.messages[-1]["type"] == "voice_result"
    assert phone.messages[-1]["sequence"] == 7
    assert phone.messages[-1]["final"] == "体感跳跃"


@pytest.mark.parametrize("ending", ["disconnect", "audio_source", "reconnect"])
def test_late_async_result_is_not_delivered_to_a_retired_source(audio_bridge, ending):
    bridge, voice = audio_bridge
    phone = FakePeer()
    bridge._handle_voice_audio(phone, audio_frame(7))
    callback = voice.frames[-1][3]
    if ending == "disconnect":
        bridge.disconnect(phone)
    elif ending == "audio_source":
        bridge.set_audio_mode("computer")
    else:
        bridge._handle_voice_audio(FakePeer(), audio_frame(0))
    callback({"kind": "final", "text": "体感跳跃"}, {"matched": True})
    assert not any(message.get("type") == "voice_result" for message in phone.messages)


def test_late_result_from_same_socket_before_audio_restart_is_discarded(audio_bridge):
    bridge, voice = audio_bridge
    phone = FakePeer()
    bridge._handle_voice_audio(phone, audio_frame(7))
    old_callback = voice.frames[-1][3]
    bridge.set_audio_mode("computer")
    bridge.set_audio_mode("phone")
    bridge._handle_voice_audio(phone, audio_frame(8))
    old_callback({"kind": "final", "text": "旧命令"}, {"matched": True})
    assert not any(message.get("type") == "voice_result" for message in phone.messages)
    voice.frames[-1][3]({"kind": "final", "text": "新命令"}, {"matched": True})
    assert phone.messages[-1]["final"] == "新命令"


def test_legacy_audio_clock_is_not_compared_with_server_epoch(audio_bridge):
    bridge, voice = audio_bridge
    bridge._handle_voice_audio(FakePeer(), audio_frame(0, captured_at_ms=1000))
    assert voice.frames[-1][2] is None


def test_synced_audio_clock_is_supplied_for_staleness_validation(audio_bridge, monkeypatch):
    bridge, voice = audio_bridge
    phone = FakePeer()
    monkeypatch.setattr("motioncontrol.input_bridge.time.time", lambda: 100.0)
    bridge.handle_message(phone, {"type": "clock_sync", "client_sent_ms": 10})
    bridge._handle_voice_audio(phone, audio_frame(0, captured_at_ms=99950))
    bridge._handle_voice_audio(phone, audio_frame(1, captured_at_ms=99000))
    assert [frame[2] for frame in voice.frames] == [99950, 99000]


def test_audio_repeated_sequence_does_not_enqueue_a_second_command(audio_bridge):
    bridge, voice = audio_bridge
    phone = FakePeer()
    for sequence in (5, 5, 4, 6):
        bridge._handle_voice_audio(phone, audio_frame(sequence))
    assert [frame[1] for frame in voice.frames] == [5, 6]


def test_sensor_duplicate_and_old_sequence_cannot_restore_a_released_button():
    output = FakeOutput()
    bridge = InputBridge(output)
    phone = FakePeer()
    try:
        bridge._handle_sensor(phone, dict(sensor_frame(), sequence=5))
        bridge._handle_sensor(phone, dict(sensor_frame(), sequence=6, touches=[]))
        bridge._handle_sensor(phone, dict(sensor_frame(), sequence=5))
        bridge._handle_sensor(phone, dict(sensor_frame(), sequence=6))
        assert len(output.sensor_calls) == 2
        assert output.sensor_calls[-1][1] == set()
    finally:
        bridge.close()


def test_sensor_reconnect_resets_sequence_and_old_socket_cannot_reclaim():
    output = FakeOutput()
    bridge = InputBridge(output)
    old, new = FakePeer(), FakePeer()
    try:
        bridge._handle_sensor(old, dict(sensor_frame(), sequence=9))
        bridge._handle_sensor(new, dict(sensor_frame(), sequence=0, touches=[]))
        bridge._handle_sensor(old, dict(sensor_frame(), sequence=10))
        bridge.disconnect(old)
        source = "mobile_sensor:phone-1:0"
        assert len(output.sensor_calls) == 2
        assert bridge._source_peers[source] is new
        assert source not in output.cleared
        bridge._handle_sensor(new, dict(sensor_frame(), sequence=1))
        assert len(output.sensor_calls) == 3
    finally:
        bridge.close()


def test_phone_output_toggle_uses_the_registered_command_boundary():
    output = FakeOutput()
    calls = []

    def toggle(enabled):
        calls.append(enabled)
        output.set_config(enabled=enabled)

    bridge = InputBridge(output, on_output_control=toggle)
    phone = FakePeer()
    try:
        bridge._handle_game_output_control(phone, {"enabled": False})
        assert calls == [False]
        assert phone.messages[-1]["enabled"] is False
    finally:
        bridge.close()


def test_explicit_stop_waits_for_admitted_pose_then_releases_it():
    entered, release, disabled = threading.Event(), threading.Event(), threading.Event()

    class BlockingKernel(BridgeKernel):
        def handle_pose_message(self, *args, **kwargs):
            entered.set()
            assert release.wait(2.0)
            return super().handle_pose_message(*args, **kwargs)

    kernel = BlockingKernel()
    bridge = InputBridge(FakeOutput(), kernel)
    phone = FakePeer()
    incoming = threading.Thread(target=bridge._handle_pose, args=(phone, pose_frame("phone", 0)))

    def stop():
        bridge.set_body_enabled(False)
        disabled.set()

    stopping = threading.Thread(target=stop)
    try:
        incoming.start()
        assert entered.wait(1.0)
        stopping.start()
        assert not disabled.wait(0.02), "Stop must be ordered after the admitted pose commit"
        release.set()
        incoming.join(1.0)
        stopping.join(1.0)
        assert disabled.is_set()
        bridge._handle_pose(phone, pose_frame("phone", 1))
        assert kernel.frames == [("mobile_pose:phone", 0)]
        assert kernel.active_body_source is None
    finally:
        release.set()
        incoming.join(1.0)
        if stopping.ident is not None:
            stopping.join(1.0)
        bridge.close()


def test_slow_voice_disconnect_does_not_hold_pose_admission_lock():
    cleaning, release, pose_done = threading.Event(), threading.Event(), threading.Event()

    class SlowVoice(AsyncVoice):
        def invalidate_phone_source(self, source):
            return 17

        def disconnect(self, source, *, expected_generation=None):
            assert expected_generation == 17
            cleaning.set()
            assert release.wait(2.0)
            super().disconnect(source)

    voice, kernel = SlowVoice(), BridgeKernel()
    output = FakeOutput()
    bridge = InputBridge(output, kernel, voice=voice)
    bridge.set_audio_mode("phone")
    phone, camera = FakePeer(), FakePeer()
    bridge._handle_voice_audio(phone, audio_frame())
    disconnecting = threading.Thread(target=bridge.disconnect, args=(phone,))

    def pose():
        bridge._handle_pose(camera, pose_frame("camera", 0))
        pose_done.set()

    incoming = threading.Thread(target=pose)
    try:
        disconnecting.start()
        assert cleaning.wait(1.0)
        assert "voice:mobile_voice:phone" in output.cleared
        incoming.start()
        assert pose_done.wait(0.5), "Busy speech cleanup must not stop body input"
        assert kernel.frames == [("mobile_pose:camera", 0)]
    finally:
        release.set()
        disconnecting.join(1.0)
        if incoming.ident is not None:
            incoming.join(1.0)
        bridge.close()


@pytest.mark.parametrize("kind", ["voice_audio", "voice_text", "voice_command"])
@pytest.mark.parametrize("ending", ["source_switch", "disconnect"])
def test_voice_admission_is_invalidated_before_delayed_submission(tmp_path, monkeypatch, kind, ending):
    from test_voice_stream_stability import service

    voice, actions = service(tmp_path)
    voice.command_registry["测试口令"] = {
        "id": "test.keyboard", "phrase": "测试口令", "kind": "keyboard", "default_target": "F3",
    }
    bridge = InputBridge(FakeOutput(), BridgeKernel(), voice)
    bridge.set_audio_mode("phone")
    phone = FakePeer()
    claimed, release = threading.Event(), threading.Event()
    original_flush = bridge._flush_voice_clears

    def flush():
        if threading.current_thread() is incoming:
            claimed.set()
            assert release.wait(2.0)
        original_flush()

    monkeypatch.setattr(bridge, "_flush_voice_clears", flush)
    if kind == "voice_audio":
        message = audio_frame(7)
    elif kind == "voice_text":
        message = {"type": kind, "role": "camera", "device_id": "phone", "text": "体感甲令",
                   "sequence": 7, "captured_at_ms": 1000}
    else:
        command = next(iter(voice.command_registry.values()))
        message = {"type": kind, "device_id": "phone", "command_id": command["id"]}
    incoming = threading.Thread(target=bridge.handle_message, args=(phone, message))
    try:
        incoming.start()
        assert claimed.wait(1.0)
        if ending == "source_switch":
            bridge.set_audio_mode("computer")
        else:
            bridge.disconnect(phone)
        release.set()
        incoming.join(1.0)
        assert not incoming.is_alive()
        assert actions == [], "An old socket admission must not renew the stopped voice epoch"
        assert not voice.connected
        assert voice._phone_queue.empty()
        bridge.set_audio_mode("phone")
        bridge.handle_message(FakePeer(), {"type": "voice_text", "role": "camera", "device_id": "phone",
                                          "text": "体感甲令", "sequence": 0, "captured_at_ms": 1000})
        assert len(actions) == 1, "A fresh socket admission must still work after source cleanup"
    finally:
        release.set()
        incoming.join(1.0)
        bridge.close()
        voice.close()
