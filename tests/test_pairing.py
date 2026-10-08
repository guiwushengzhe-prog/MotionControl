"""手机配对：钥匙藏在二维码里，扫过一次码的手机自动带上；没配对的只能连、不能发输入。"""

from __future__ import annotations

import json
import socket
import struct
import threading
from types import SimpleNamespace

from motioncontrol.connection_code import connection_code
from motioncontrol.input_bridge import PAIRING_REQUIRED_MESSAGE, InputBridge
from motioncontrol.pairing import PairingKey


class FakePeer:
    def __init__(self, *, paired=True):
        self.desktop = False
        self.paired = paired
        self.source_ids = set()
        self.accepted_inputs = 0
        self.messages = []
        self.closed = False

    def send_json(self, message):
        self.messages.append(message)

    def close(self):
        self.closed = True


class FakeOutput:
    def __init__(self):
        self.sensor_calls = []
        self.mouse_calls = []

    def set_sensor_state(self, source, buttons, **kwargs):
        self.sensor_calls.append(source)

    def set_mouse_state(self, *args, **kwargs):
        self.mouse_calls.append(args)

    def clear_source(self, source):
        pass


def sensor_frame():
    return {
        "type": "sensor_frame", "role": "sensor", "device_id": "phone-1", "sequence": 1,
        "captured_at_ms": 2000, "player_slot": 0,
        "quaternion": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
        "rotation_rate": {"x": 0.0, "y": 0.0, "z": 0.0},
        "acceleration": {"x": 0.0, "y": 0.0, "z": 9.8}, "recenter": False,
        "touches": [{"control": "A", "pressed": True}],
    }


def test_the_key_is_made_once_and_kept(tmp_path):
    path = tmp_path / "user" / "pairing_key.txt"
    first = PairingKey(path).key()
    assert len(first) >= 22 and path.read_text(encoding="utf-8") == first
    # 重启之后还是同一把：扫过码的手机不用再扫。
    assert PairingKey(path).key() == first


def test_only_the_exact_key_is_accepted(tmp_path):
    pairing = PairingKey(tmp_path / "pairing_key.txt")
    key = pairing.key()
    assert pairing.accepts(key)
    for wrong in ("", None, 123, key[:-1], key + "x", "a" * len(key), key.upper() if key.upper() != key else "B" * 22):
        assert not pairing.accepts(wrong), wrong


def test_a_damaged_key_file_is_replaced_not_trusted(tmp_path):
    path = tmp_path / "pairing_key.txt"
    path.write_text("short", encoding="utf-8")
    pairing = PairingKey(path)
    assert pairing.key() != "short" and not pairing.accepts("short")


def test_the_qr_code_carries_the_key(tmp_path):
    key = PairingKey(tmp_path / "pairing_key.txt").key()
    data = connection_code("0123456789ab", "电脑", [], 8765, key)
    assert data["payload"]["key"] == key
    assert json.loads(data["text"])["key"] == key


def test_an_unpaired_phone_is_told_once_and_its_input_does_nothing():
    output = FakeOutput()
    bridge = InputBridge(output)
    try:
        peer = FakePeer(paired=False)
        for _ in range(5):
            bridge.handle_message(peer, sensor_frame())
        assert output.sensor_calls == []
        errors = [m for m in peer.messages if m.get("type") == "error"]
        assert errors == [{"type": "error", "code": "pairing_required", "message": PAIRING_REQUIRED_MESSAGE}]
        assert bridge.status()["unpaired_phone"] is True
        assert bridge.status()["handheld_connected"] is False
        # 对时照常回：手机靠它判断连接是活的，不算输入。
        bridge.handle_message(peer, {"type": "clock_sync", "client_sent_ms": 1})
        assert peer.messages[-1]["type"] == "clock_sync"
        # 游戏控制开关同样不能碰。
        bridge.handle_message(peer, {"type": "game_output_control", "enabled": True})
        assert len([m for m in peer.messages if m.get("type") == "error"]) == 1
    finally:
        bridge.close()


def test_a_paired_phone_works_as_before():
    output = FakeOutput()
    bridge = InputBridge(output)
    try:
        peer = FakePeer(paired=True)
        bridge.handle_message(peer, sensor_frame())
        assert output.sensor_calls
        assert not [m for m in peer.messages if m.get("code") == "pairing_required"]
        assert bridge.status()["unpaired_phone"] is False
    finally:
        bridge.close()


# ---- 真 socket：整个连接流程 ------------------------------------------------

def masked_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    mask = b"\x11\x22\x33\x44"
    body = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    if len(payload) < 126:
        head = bytes([0x80 | opcode, 0x80 | len(payload)])
    else:
        head = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack("!H", len(payload))
    return head + mask + body


def read_frames(sock: socket.socket) -> list[dict]:
    sock.settimeout(2.0)
    data = b""
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    except socket.timeout:
        pass
    _, _, body = data.partition(b"\r\n\r\n")
    messages = []
    while len(body) >= 2:
        opcode, length = body[0] & 0x0F, body[1] & 0x7F
        offset = 2
        if length == 126:
            length = struct.unpack("!H", body[2:4])[0]
            offset = 4
        elif length == 127:
            length = struct.unpack("!Q", body[2:10])[0]
            offset = 10
        payload, body = body[offset:offset + length], body[offset + length:]
        if opcode == 0x1:
            messages.append(json.loads(payload))
    return messages


def serve(bridge: InputBridge, query: str, *, paired: bool, frames: list[bytes]):
    server, client = socket.socketpair()
    lines = []
    handler = SimpleNamespace(
        connection=server, rfile=server.makefile("rb"), wfile=server.makefile("wb"),
        headers={"Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="}, close_connection=False,
        send_response=lambda code, message=None: lines.append(f"HTTP/1.1 {code} {message}\r\n"),
        send_header=lambda name, value: lines.append(f"{name}: {value}\r\n"),
        end_headers=lambda: (handler.wfile.write(("".join(lines) + "\r\n").encode()), handler.wfile.flush()),
        send_error=lambda *args: None,
    )
    thread = threading.Thread(target=bridge.serve_websocket, args=(handler, query), kwargs={"paired": paired}, daemon=True)
    thread.start()
    for frame in frames:
        client.sendall(frame)
    client.sendall(masked_frame(b"", opcode=0x8))
    thread.join(timeout=5)
    messages = read_frames(client)
    for sock in (server, client):
        sock.close()
    return messages


def test_over_a_real_connection_unpaired_gets_config_and_the_notice_but_no_control():
    output = FakeOutput()
    bridge = InputBridge(output)
    bridge.configure_control_config_provider(lambda: {"type": "control_config_v1", "zones": []})
    try:
        messages = serve(bridge, "client=desktop", paired=False,
                         frames=[masked_frame(json.dumps(sensor_frame()).encode())])
        kinds = [m.get("type") for m in messages]
        # 旧版手机网页靠这份配置触发热更，更新后才认得钥匙，所以照发。
        assert "control_config_v1" in kinds
        assert {"type": "error", "code": "pairing_required", "message": PAIRING_REQUIRED_MESSAGE} in messages
        # 不给游戏控制开关的状态，也不当电脑界面（那一路会收到骨骼流）。
        assert "game_output_state_v1" not in kinds
        assert output.sensor_calls == []
    finally:
        bridge.close()


def test_over_a_real_connection_paired_input_reaches_the_output():
    output = FakeOutput()
    bridge = InputBridge(output)
    try:
        messages = serve(bridge, "key=whatever", paired=True,
                         frames=[masked_frame(json.dumps(sensor_frame()).encode())])
        assert not [m for m in messages if m.get("code") == "pairing_required"]
        assert output.sensor_calls
    finally:
        bridge.close()
