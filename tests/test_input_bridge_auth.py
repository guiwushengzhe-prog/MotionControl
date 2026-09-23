"""Identity enforcement on /ws/input, at the bridge level.

device_pairing is tested on its own; this covers the part that decides whether
a frame is allowed to act: handle_message must reject a frame whose device_id
or role does not match what the connection actually proved, and must do it for
every frame type that can reach the game, not just the obvious one.

This matters because handle_sensor() writes straight to the gamepad.  A LAN
device that authenticates once and then relabels its frames would otherwise be
able to drive another device's input, which would make pairing decorative.
"""

from __future__ import annotations

import pytest

from motioncontrol import device_pairing as dp
from motioncontrol.input_bridge import InputBridge

DEVICE = "camera-aaaa"
OTHER = "camera-bbbb"


class FakePeer:
    """Mirrors the fields of WebSocketPeer that handle_message touches."""

    def __init__(self, desktop=False):
        self.desktop = desktop
        self.source_ids = set()
        self.accepted_inputs = 0
        self.sent = []
        self.closed = False
        self.auth_nonce = None
        self.authenticated_device_id = None
        self.authenticated_role = None

    def send_json(self, message):
        self.sent.append(message)

    def close(self):
        self.closed = True


class FakeOutput:
    def __init__(self):
        self.enabled = False

    def status(self):
        return {"enabled": self.enabled}

    def set_config(self, *, enabled=None, **_kwargs):
        if enabled is not None:
            self.enabled = bool(enabled)
        return self.status()

    def clear_source(self, source):
        pass


def bridge(tmp_path, *, require=False):
    pairing = dp.PairingService(
        dp.PairingStore(tmp_path / "paired.json"), require_paired_devices=require
    )
    output = FakeOutput()
    result = InputBridge(output, None, pairing=pairing)
    result.test_output = output
    return result, pairing


def authenticate(bridge_obj, pairing, peer, device_id=DEVICE, role="camera"):
    """Pair a device and bring `peer` to the authenticated state."""
    pairing.store.remember(device_id, b"\x5a" * 32)
    peer.auth_nonce = pairing.new_nonce()
    proof = dp.connection_proof(b"\x5a" * 32, peer.auth_nonce, device_id, role)
    import base64

    bridge_obj.handle_message(peer, {
        "type": "hello", "protocol_version": dp.PROTOCOL_VERSION,
        "device_id": device_id, "role": role,
        "auth": {"mode": "paired_hmac", "proof": base64.b64encode(proof).decode()},
    })


def sensor_frame(device_id=DEVICE, role="sensor"):
    return {"type": "sensor_frame", "role": role, "device_id": device_id,
            "sequence": 1, "captured_at_ms": 0, "player_slot": 0, "touches": []}


pytestmark = pytest.mark.skipif(
    not dp.crypto_available(), reason="cryptography is not installed"
)


def test_hello_binds_the_connection_identity(tmp_path):
    b, pairing = bridge(tmp_path)
    peer = FakePeer()
    authenticate(b, pairing, peer)
    assert peer.authenticated_device_id == DEVICE
    assert peer.authenticated_role == "camera"
    assert any(message["type"] == "hello_ack" for message in peer.sent)
    assert peer.sent[-1] == {
        "type": "game_output_state_v1", "enabled": False, "ok": True,
    }


def test_nonce_is_single_use(tmp_path):
    """A captured hello cannot be replayed on the same connection."""
    b, pairing = bridge(tmp_path)
    peer = FakePeer()
    authenticate(b, pairing, peer)
    assert peer.auth_nonce is None


def test_frame_claiming_another_device_is_rejected_and_disconnected(tmp_path):
    b, pairing = bridge(tmp_path)
    peer = FakePeer()
    authenticate(b, pairing, peer, role="sensor")
    b.handle_message(peer, sensor_frame(device_id=OTHER))
    assert peer.closed, "a connection claiming another identity must be dropped"
    assert any("device_id" in str(m.get("message", "")) for m in peer.sent)


def test_frame_claiming_another_role_is_rejected_and_disconnected(tmp_path):
    b, pairing = bridge(tmp_path)
    peer = FakePeer()
    authenticate(b, pairing, peer, role="camera")
    b.handle_message(peer, sensor_frame(device_id=DEVICE, role="sensor"))
    assert peer.closed


def test_enforcement_covers_every_game_reaching_frame_type():
    """Guard against a new frame type being added without identity checks."""
    assert InputBridge.BUSINESS_TYPES == frozenset({
        "pose_frame_v2", "pose_features_v1", "sensor_frame",
        "voice_text", "voice_command", "scene_snapshot", "game_output_control",
    })


def test_unauthenticated_frames_are_refused_when_pairing_is_required(tmp_path):
    b, _pairing = bridge(tmp_path, require=True)
    peer = FakePeer()
    b.handle_message(peer, sensor_frame())
    assert any("配对" in str(m.get("message", "")) for m in peer.sent), peer.sent


def test_unauthenticated_frames_still_work_while_pairing_is_optional(tmp_path):
    """Protocol-1 compatibility: already installed phones keep working."""
    b, _pairing = bridge(tmp_path, require=False)
    peer = FakePeer()
    b.handle_message(peer, sensor_frame())
    # It gets past identity enforcement; whatever the sensor handler does with
    # it afterwards is not this test's business.
    assert not any("配对" in str(m.get("message", "")) for m in peer.sent)


@pytest.mark.parametrize("require_pairing", [True, False])
def test_game_output_control_is_denied_without_auth(tmp_path, require_pairing):
    b, _pairing = bridge(tmp_path, require=require_pairing)
    peer = FakePeer()
    b.handle_message(peer, {
        "type": "game_output_control", "device_id": DEVICE,
        "role": "camera", "enabled": True,
    })
    assert b.test_output.enabled is False
    assert peer.sent[-1] == {
        "type": "game_output_state_v1", "enabled": False, "ok": False,
        "error": "设备尚未通过配对认证，不能控制游戏输出",
    }


@pytest.mark.parametrize("enabled", [True, False])
def test_authenticated_game_output_control_returns_actual_state(tmp_path, enabled):
    b, pairing = bridge(tmp_path)
    peer = FakePeer()
    authenticate(b, pairing, peer, role="camera")
    b.handle_message(peer, {
        "type": "game_output_control", "device_id": DEVICE,
        "role": "camera", "enabled": enabled,
    })
    assert b.test_output.enabled is enabled
    assert peer.sent[-1] == {
        "type": "game_output_state_v1", "enabled": enabled, "ok": True,
    }


def test_authenticated_game_output_control_rejects_non_boolean_enabled(tmp_path):
    b, pairing = bridge(tmp_path)
    peer = FakePeer()
    authenticate(b, pairing, peer, role="camera")
    b.handle_message(peer, {
        "type": "game_output_control", "device_id": DEVICE,
        "role": "camera", "enabled": 1,
    })
    assert b.test_output.enabled is False
    assert peer.sent[-1] == {
        "type": "game_output_state_v1", "enabled": False, "ok": False,
        "error": "enabled 必须是布尔值",
    }


def test_challenge_is_offered_on_connect(tmp_path):
    b, _pairing = bridge(tmp_path)
    peer = FakePeer()
    b._send_challenge(peer)
    challenge = peer.sent[-1]
    assert challenge["type"] == "challenge"
    assert challenge["protocol_version"] == dp.PROTOCOL_VERSION
    assert challenge["pair_protocol"] == dp.PAIR_PROTOCOL
    assert len(challenge["nonce"]) == 64
    assert peer.auth_nonce == challenge["nonce"]


def test_desktop_peers_are_not_challenged(tmp_path):
    """The desktop preview client is same-process, not a paired device."""
    b, _pairing = bridge(tmp_path)
    peer = FakePeer(desktop=True)
    b._send_challenge(peer)
    assert peer.sent == []


def test_each_connection_gets_a_distinct_nonce(tmp_path):
    b, _pairing = bridge(tmp_path)
    first, second = FakePeer(), FakePeer()
    b._send_challenge(first)
    b._send_challenge(second)
    assert first.auth_nonce != second.auth_nonce
