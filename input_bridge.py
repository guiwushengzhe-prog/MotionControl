from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import math
import socket
import struct
import threading
import time
from collections import deque
from collections.abc import Iterable
from urllib.parse import parse_qs, urlparse


WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE_BYTES = 1024 * 1024
# MediaPipe image landmarks are nominally normalized, but x/y can be just
# outside [0, 1] when a person touches the image edge.  Keep a generous
# finite guard against corrupted payloads without clamping coordinates here;
# the kernel needs the original geometry and display code can clip later.
LANDMARK_COORDINATE_ABS_LIMIT = 10.0
POSE_SOURCE_PREFIX = "mobile_pose:"
SENSOR_SOURCE_PREFIX = "mobile_sensor:"
VOICE_SOURCE_PREFIX = "mobile_voice:"

SENSOR_BUTTON_ALIASES = {
    "A": "A",
    "B": "B",
    "X": "X",
    "Y": "Y",
    "LB": "LB",
    "RB": "RB",
    "L": "LB",
    "R": "RB",
    "LT": "LT",
    "RT": "RT",
    "ZL": "LT",
    "ZR": "RT",
    "START": "START",
    "BACK": "BACK",
    "SELECT": "BACK",
}
XUSB_BUTTONS = {"A", "B", "X", "Y", "LB", "RB", "START", "BACK"}


class WebSocketProtocolError(ValueError):
    pass


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _read_exact(stream, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise ConnectionError("websocket closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class WebSocketPeer:
    """Small RFC 6455 text-frame peer for the local input bridge."""

    def __init__(self, handler, *, desktop: bool = False) -> None:
        self.handler = handler
        self.stream = handler.rfile
        self.connection = handler.connection
        self.desktop = desktop
        self.source_ids: set[str] = set()
        self.accepted_inputs = 0
        self._send_lock = threading.Lock()
        self._closed = False

    def _frame(self, opcode: int, payload: bytes) -> bytes:
        if len(payload) > MAX_MESSAGE_BYTES:
            raise ValueError("websocket message is too large")
        first = 0x80 | (opcode & 0x0F)
        if len(payload) < 126:
            return bytes([first, len(payload)]) + payload
        if len(payload) <= 0xFFFF:
            return bytes([first, 126]) + struct.pack("!H", len(payload)) + payload
        return bytes([first, 127]) + struct.pack("!Q", len(payload)) + payload

    def send_bytes(self, opcode: int, payload: bytes) -> None:
        with self._send_lock:
            if self._closed:
                raise ConnectionError("websocket closed")
            self.handler.wfile.write(self._frame(opcode, payload))
            self.handler.wfile.flush()

    def send_json(self, data: dict) -> None:
        self.send_bytes(0x1, json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    def close(self) -> None:
        with self._send_lock:
            if self._closed:
                return
            self._closed = True
            try:
                self.handler.wfile.write(self._frame(0x8, b""))
                self.handler.wfile.flush()
            except (BrokenPipeError, ConnectionError, OSError):
                pass

    def recv(self) -> tuple[int, bytes]:
        header = _read_exact(self.stream, 2)
        first, second = header
        if not first & 0x80:
            raise WebSocketProtocolError("fragmented websocket frames are not supported")
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", _read_exact(self.stream, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", _read_exact(self.stream, 8))[0]
        if length > MAX_MESSAGE_BYTES:
            raise WebSocketProtocolError("websocket message is too large")
        if not masked:
            raise WebSocketProtocolError("client websocket frame must be masked")
        mask = _read_exact(self.stream, 4)
        payload = bytearray(_read_exact(self.stream, length))
        for index in range(length):
            payload[index] ^= mask[index % 4]
        return opcode, bytes(payload)


def perform_websocket_upgrade(handler, *, desktop: bool = False) -> WebSocketPeer:
    key = handler.headers.get("Sec-WebSocket-Key", "").strip()
    if not key:
        raise WebSocketProtocolError("missing Sec-WebSocket-Key")
    accept = base64.b64encode(hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()).decode("ascii")
    handler.close_connection = True
    handler.send_response(101, "Switching Protocols")
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", accept)
    handler.end_headers()
    handler.connection.settimeout(1.0)
    return WebSocketPeer(handler, desktop=desktop)


def _valid_landmark(point, *, normalized: bool) -> bool:
    if not isinstance(point, dict):
        return False
    if not all(_is_number(point.get(name)) for name in ("x", "y", "z", "visibility")):
        return False
    if any(abs(float(point[name])) > LANDMARK_COORDINATE_ABS_LIMIT for name in ("x", "y", "z")):
        return False
    # Visibility remains a probability.  Image x/y are intentionally not
    # clipped or forced into [0, 1]; edge/out-of-frame landmarks are valid
    # input for relative pose calculations.  World landmarks use the same
    # finite guard but never receive an image-coordinate range check.
    if not 0.0 <= float(point["visibility"]) <= 1.0:
        return False
    return True


def _validate_pose_frame(message: dict) -> None:
    if message.get("type") != "pose_frame_v2" or message.get("role") != "camera":
        raise ValueError("pose_frame_v2 requires role=camera")
    if not isinstance(message.get("device_id"), str) or not message["device_id"].strip():
        raise ValueError("device_id must be a non-empty string")
    if not _is_int(message.get("sequence")) or message["sequence"] < 0:
        raise ValueError("sequence must be an integer >= 0")
    if not _is_number(message.get("captured_at_ms")):
        raise ValueError("captured_at_ms must be a number")
    if not all(_is_int(message.get(name)) and message[name] > 0 for name in ("width", "height")):
        raise ValueError("width and height must be positive integers")
    if message.get("camera_facing") not in {"user", "environment"}:
        raise ValueError("camera_facing must be user or environment")
    if message.get("orientation_degrees") not in {0, 90, 180, 270}:
        raise ValueError("orientation_degrees must be 0, 90, 180, or 270")
    if not isinstance(message.get("preview_mirrored"), bool) or not isinstance(message.get("coordinates_mirrored"), bool):
        raise ValueError("mirror flags must be boolean")
    poses = message.get("poses")
    if not isinstance(poses, list) or len(poses) > 2:
        raise ValueError("poses must contain 0 to 2 items")
    for item in poses:
        if not isinstance(item, dict) or not isinstance(item.get("pose"), list) or len(item["pose"]) != 33:
            raise ValueError("each pose must contain exactly 33 landmarks")
        if not all(_valid_landmark(point, normalized=True) for point in item["pose"]):
            raise ValueError("pose landmarks are invalid")
        world = item.get("world_pose")
        if world is not None and (not isinstance(world, list) or len(world) != 33 or not all(_valid_landmark(point, normalized=False) for point in world)):
            raise ValueError("world_pose must contain exactly 33 valid landmarks")
    hands = message.get("hands", [])
    if not isinstance(hands, list) or len(hands) > 4:
        raise ValueError("hands must contain 0 to 4 items")
    for hand in hands:
        if not isinstance(hand, dict) or hand.get("handedness") not in {"Left", "Right", "Unknown"}:
            raise ValueError("handedness is invalid")
        if not isinstance(hand.get("landmarks"), list) or len(hand["landmarks"]) != 21:
            raise ValueError("each hand must contain exactly 21 landmarks")
        if not all(_valid_landmark(point, normalized=True) for point in hand["landmarks"]):
            raise ValueError("hand landmarks are invalid")
    if not _is_number(message.get("inference_ms", 0)) or float(message.get("inference_ms", 0)) < 0:
        raise ValueError("inference_ms must be >= 0")


def _validate_voice_text(message: dict) -> None:
    if message.get("type") != "voice_text" or message.get("role") != "camera":
        raise ValueError("voice_text requires role=camera")
    if not isinstance(message.get("device_id"), str) or not message["device_id"].strip():
        raise ValueError("device_id must be a non-empty string")
    if not _is_int(message.get("sequence")) or message["sequence"] < 0:
        raise ValueError("sequence must be an integer >= 0")
    if not _is_number(message.get("captured_at_ms")):
        raise ValueError("captured_at_ms must be a number")
    text = message.get("text")
    if not isinstance(text, str) or not text.strip() or len(text.strip()) > 96:
        raise ValueError("voice_text text must contain 1 to 96 characters")
    if message.get("confidence") is not None and not (0.0 <= float(message["confidence"]) <= 1.0):
        raise ValueError("voice_text confidence must be between 0 and 1")


def _canonical_sensor_control(control: str) -> str:
    value = str(control).strip().upper()
    canonical = SENSOR_BUTTON_ALIASES.get(value)
    if canonical is None:
        raise ValueError(f"unsupported handheld control: {control}")
    return canonical


def _validate_sensor_frame(message: dict) -> tuple[set[str], float, float, float, float, int]:
    if message.get("type") != "sensor_frame" or message.get("role") != "sensor":
        raise ValueError("sensor_frame requires role=sensor")
    if not isinstance(message.get("device_id"), str) or not message["device_id"].strip():
        raise ValueError("device_id must be a non-empty string")
    if not _is_int(message.get("sequence")) or message["sequence"] < 0:
        raise ValueError("sequence must be an integer >= 0")
    if not _is_number(message.get("captured_at_ms")):
        raise ValueError("captured_at_ms must be a number")
    player_slot = message.get("player_slot")
    if not _is_int(player_slot) or player_slot not in {0, 1}:
        raise ValueError("player_slot must be 0 or 1")
    for group, names in {
        "quaternion": ("x", "y", "z", "w"),
        "rotation_rate": ("x", "y", "z"),
        "acceleration": ("x", "y", "z"),
    }.items():
        value = message.get(group)
        if not isinstance(value, dict) or not all(_is_number(value.get(name)) for name in names):
            raise ValueError(f"{group} is invalid")
    if not isinstance(message.get("recenter"), bool):
        raise ValueError("recenter must be boolean")
    touches = message.get("touches", [])
    if not isinstance(touches, list):
        raise ValueError("touches must be a list")
    buttons: set[str] = set()
    left_trigger = right_trigger = 0.0
    stick_x = stick_y = 0.0
    for touch in touches:
        if not isinstance(touch, dict):
            raise ValueError("touch must be an object")
        control = str(touch.get("control", "")).strip()
        if control.lower() == "stick":
            if not _is_number(touch.get("x")) or not _is_number(touch.get("y")):
                raise ValueError("stick touch requires numeric x and y")
            stick_x = max(-1.0, min(1.0, float(touch["x"])))
            stick_y = max(-1.0, min(1.0, float(touch["y"])))
            continue
        canonical = _canonical_sensor_control(control)
        value = touch.get("value")
        pressed = touch.get("pressed", value is not None)
        if value is not None and not _is_number(value):
            raise ValueError("touch value must be numeric")
        if not isinstance(pressed, bool):
            raise ValueError("touch pressed must be boolean")
        amount = max(0.0, min(1.0, float(value))) if _is_number(value) else (1.0 if pressed else 0.0)
        if canonical == "LT":
            left_trigger = max(left_trigger, amount)
        elif canonical == "RT":
            right_trigger = max(right_trigger, amount)
        elif pressed or amount > 0.0:
            buttons.add(canonical)
    return buttons, left_trigger, right_trigger, stick_x, stick_y, player_slot


def _local_addresses() -> list[str]:
    values: set[str] = set()

    def add(address: str) -> None:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            return
        # Only advertise usable private LAN addresses.  In particular, do
        # not put loopback/APIPA/VPN-less placeholders into the phone field.
        if parsed.version == 4 and parsed.is_private and not parsed.is_loopback and not parsed.is_link_local:
            values.add(str(parsed))

    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(str(item[4][0]))
    except OSError:
        pass
    try:
        _, _, addresses = socket.gethostbyname_ex(socket.gethostname())
        for address in addresses:
            add(str(address))
    except OSError:
        pass
    return sorted(values)


class InputBridge:
    """Receives MotionBridge input and fans mobile poses to desktop consumers."""

    def __init__(self, output, kernel=None, voice=None) -> None:
        self.output = output
        self.kernel = kernel
        self._voice_service = voice
        self._scene_snapshot_handler = None
        self._lock = threading.RLock()
        self._peers: set[WebSocketPeer] = set()
        self._source_peers: dict[str, WebSocketPeer] = {}
        self._pose_sources: dict[str, dict] = {}
        self._sensor_sources: dict[str, dict] = {}
        self._voice_sources: dict[str, dict] = {}
        self._active_voice_source: str | None = None
        self._latest_pose: dict | None = None
        self._active_pose_source: str | None = None
        self._pose_receive_times: deque[float] = deque(maxlen=120)
        self._pose_inference_times: deque[float] = deque(maxlen=120)
        self._pose_last_sequence: int | None = None
        self._pose_skipped_frames = 0
        self._pose_last_inference_ms: float | None = None
        self._pose_last_resolution = {"width": 0, "height": 0}
        self._pose_last_count = 0
        self._body_mode = "phone" if kernel is None else "computer"
        self._pose_frames_with_people = 0
        self._host = "0.0.0.0"
        self._port = 8765
        self._stop = threading.Event()
        self._watchdog = threading.Thread(target=self._watch_loop, name="motion-input-watchdog", daemon=True)
        self._watchdog.start()

    def configure_voice(self, voice) -> None:
        with self._lock:
            self._voice_service = voice

    def configure_scene_snapshot_handler(self, handler) -> None:
        with self._lock:
            self._scene_snapshot_handler = handler

    def request_scene_snapshot(self, purpose: str) -> dict:
        purpose = str(purpose or "capture").strip().lower()
        if purpose not in {"capture", "rematch"}:
            raise ValueError("scene snapshot purpose must be capture or rematch")
        with self._lock:
            source_id = self._active_pose_source
            peer = self._source_peers.get(source_id) if source_id else None
        if peer is None:
            raise RuntimeError("当前没有活动的手机姿态源")
        peer.send_json({
            "type": "scene_snapshot_request",
            "purpose": purpose,
            "max_width": 960,
            "jpeg_quality": 88,
        })
        return {"ok": True, "pending": True, "purpose": purpose, "source_id": source_id}

    def configure_endpoint(self, host: str, port: int) -> None:
        with self._lock:
            self._host = str(host)
            self._port = int(port)

    def set_body_mode(self, mode: str) -> None:
        """Atomically select which body source is allowed to drive the kernel."""
        mode = str(mode).strip().lower()
        if mode not in {"computer", "phone"}:
            raise ValueError("body mode must be computer or phone")
        cleared: list[str] = []
        with self._lock:
            self._body_mode = mode
            if mode != "phone":
                for source_id in list(self._pose_sources):
                    _, was_active = self._clear_source_locked(source_id)
                    if was_active:
                        cleared.append(source_id)
                for source_id in list(self._voice_sources):
                    owner, _ = self._clear_source_locked(source_id)
                    if owner is not None:
                        owner.source_ids.discard(source_id)
        for source_id in cleared:
            self._broadcast_pose_state(source_id, False, "source_switch")

    def clear_mobile_sources(self) -> None:
        cleared: list[str] = []
        with self._lock:
            for source_id in list(self._pose_sources):
                _, was_active = self._clear_source_locked(source_id)
                if was_active:
                    cleared.append(source_id)
            for source_id in list(self._voice_sources):
                owner, _ = self._clear_source_locked(source_id)
                if owner is not None:
                    owner.source_ids.discard(source_id)
        for source_id in cleared:
            self._broadcast_pose_state(source_id, False, "source_switch")

    def phone_ws_urls(self) -> list[str]:
        addresses = _local_addresses()
        if not addresses:
            addresses = ["<本机局域网地址>"]
        return [f"ws://{address}:{self._port}/ws/input" for address in addresses]

    def lan_ipv4(self) -> str | None:
        """Return the first actual private IPv4 advertised to the phone UI."""
        addresses = _local_addresses()
        return addresses[0] if addresses else None

    @staticmethod
    def _rate(times: deque[float]) -> float | None:
        if len(times) < 2:
            return None
        elapsed = times[-1] - times[0]
        return (len(times) - 1) / elapsed if elapsed > 1e-6 else None

    @staticmethod
    def _p95(values: deque[float]) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * 0.95) - 1))
        return ordered[index]

    @staticmethod
    def _round_or_none(value: float | None, digits: int = 1):
        return round(value, digits) if value is not None and math.isfinite(value) else None

    def _reset_pose_metrics_locked(self) -> None:
        self._pose_receive_times.clear()
        self._pose_inference_times.clear()
        self._pose_last_sequence = None
        self._pose_skipped_frames = 0
        self._pose_last_inference_ms = None
        self._pose_last_resolution = {"width": 0, "height": 0}
        self._pose_last_count = 0

    def performance(self) -> dict:
        """Return phone-source metrics without inventing device-side values."""
        now = time.monotonic()
        with self._lock:
            source_id = self._active_pose_source
            state = self._pose_sources.get(source_id) if source_id else None
            age_ms = round(max(0.0, (now - self._latest_pose["received_at"]) * 1000.0)) if self._latest_pose else None
            inference_values = self._pose_inference_times
            return {
                "source": "phone",
                "model": "MediaPipe Pose Full (phone reported)",
                "camera_resolution": dict(self._pose_last_resolution),
                # The phone protocol currently does not claim capture or
                # inference FPS.  Network receive rate is reported separately.
                "capture_fps": None,
                "inference_fps": None,
                "network_fps": self._round_or_none(self._rate(self._pose_receive_times), 2),
                "inference_avg_ms": self._round_or_none(
                    sum(inference_values) / len(inference_values) if inference_values else None,
                ),
                "inference_p95_ms": self._round_or_none(self._p95(inference_values)),
                "phone_inference_ms": self._round_or_none(self._pose_last_inference_ms),
                "pose_frame_age_ms": age_ms,
                "network_age_ms": age_ms,
                "total_latency_ms": None,
                "dropped_frames": 0,
                "skipped_frames": int(self._pose_skipped_frames),
                "web_render_fps": None,
                "recent_humans": int(self._pose_last_count),
                "preview_ready": False,
                "running": bool(state and source_id in self._source_peers),
                "last_error": None,
            }

    def status(self) -> dict:
        now = time.monotonic()
        with self._lock:
            pose_sources = [
                {
                    "source_id": source_id,
                    "source_kind": "mobile_pose",
                    "device_id": state["device_id"],
                    "age_ms": round(max(0.0, (now - state["received_at"]) * 1000)),
                    "pose_count": state["pose_count"],
                    "sequence": state.get("sequence"),
                    "width": state.get("width"),
                    "height": state.get("height"),
                    "inference_ms": state.get("inference_ms"),
                    "preview_mirrored": state.get("preview_mirrored", False),
                    "coordinates_mirrored": state.get("coordinates_mirrored", False),
                    "connected": source_id in self._source_peers,
                    "active": source_id == self._active_pose_source,
                }
                for source_id, state in self._pose_sources.items()
            ]
            sensor_sources = [
                {
                    "source_id": source_id,
                    "source_kind": "mobile_sensor",
                    "device_id": state["device_id"],
                    "player_slot": state["player_slot"],
                    "age_ms": round(max(0.0, (now - state["received_at"]) * 1000)),
                    "connected": source_id in self._source_peers,
                    "quaternion": state["quaternion"],
                    "rotation_rate": state["rotation_rate"],
                    "acceleration": state["acceleration"],
                    "recenter": state["recenter"],
                }
                for source_id, state in self._sensor_sources.items()
            ]
            voice_sources = [
                {
                    "source_id": source_id,
                    "source_kind": "mobile_voice",
                    "device_id": state["device_id"],
                    "age_ms": round(max(0.0, (now - state["received_at"]) * 1000)),
                    "connected": source_id in self._source_peers,
                    "active": source_id == self._active_voice_source,
                }
                for source_id, state in self._voice_sources.items()
            ]
            latest_pose = self._latest_pose
            active_pose_source = self._active_pose_source
            host = self._host
            port = self._port
        pose_age = round(max(0.0, (now - latest_pose["received_at"]) * 1000)) if latest_pose else None
        return {
            "server_host": host,
            "server_port": port,
            "lan_ipv4": self.lan_ipv4(),
            "body_mode": self._body_mode,
            "phone_ws_urls": self.phone_ws_urls(),
            "mobile_pose_connected": any(item["connected"] for item in pose_sources),
            "mobile_pose_age_ms": pose_age,
            "mobile_pose_source_id": active_pose_source,
            "mobile_pose_sources": pose_sources,
            "handheld_connected": any(item["connected"] for item in sensor_sources),
            "handheld_sources": sensor_sources,
            "mobile_voice_connected": any(item["connected"] for item in voice_sources),
            "mobile_voice_sources": voice_sources,
            "mobile_voice_source_id": self._active_voice_source,
        }

    def register(self, peer: WebSocketPeer) -> None:
        with self._lock:
            self._peers.add(peer)
            cached = self._latest_pose
            active_source = self._active_pose_source
        if peer.desktop and active_source:
            try:
                peer.send_json({
                    "type": "pose_source_state",
                    "source_kind": "mobile_pose",
                    "source_id": active_source,
                    "active": True,
                    "reason": "register",
                })
            except (ConnectionError, OSError):
                self.disconnect(peer)
                return
        if peer.desktop and cached and (time.monotonic() - cached["received_at"]) <= 0.30:
            try:
                peer.send_json(cached["message"])
            except (ConnectionError, OSError):
                self.disconnect(peer)

    def _send_error(self, peer: WebSocketPeer, message: str) -> None:
        try:
            peer.send_json({"type": "error", "message": message})
        except (ConnectionError, OSError):
            self.disconnect(peer)

    def _ack(self, peer: WebSocketPeer) -> None:
        with self._lock:
            latest = self._latest_pose
            pose_count = int(latest["pose_count"]) if latest else 0
            visible = pose_count > 0 and (time.monotonic() - latest["received_at"]) <= 0.30
            players = [{"slot": 0, "signals": {"pose_visible": visible}}]
        try:
            peer.send_json({
                "type": "ack",
                "accepted": True,
                "calibration": None,
                "players": players,
                "pose_visible": visible,
                "pose_count": pose_count,
                "pose_frames_with_people": self._pose_frames_with_people,
            })
        except (ConnectionError, OSError):
            self.disconnect(peer)

    def _accept_input(self, peer: WebSocketPeer) -> None:
        peer.accepted_inputs += 1
        if peer.accepted_inputs % 15 == 0:
            self._ack(peer)

    def _broadcast_pose(self, message: dict) -> None:
        with self._lock:
            peers = [peer for peer in self._peers if peer.desktop]
        for peer in peers:
            try:
                peer.send_json(message)
            except (ConnectionError, OSError):
                self.disconnect(peer)

    def _broadcast_pose_state(self, source_id: str, active: bool, reason: str) -> None:
        message = {
            "type": "pose_source_state",
            "source_kind": "mobile_pose",
            "source_id": source_id,
            "active": bool(active),
            "reason": reason,
        }
        with self._lock:
            peers = [peer for peer in self._peers if peer.desktop]
        for peer in peers:
            try:
                peer.send_json(message)
            except (ConnectionError, OSError):
                self.disconnect(peer)

    def _handle_pose(self, peer: WebSocketPeer, message: dict) -> None:
        _validate_pose_frame(message)
        if self.kernel is not None and self._body_mode != "phone":
            self._accept_input(peer)
            return
        device_id = message["device_id"].strip()
        source_id = POSE_SOURCE_PREFIX + device_id
        received_at = time.monotonic()
        pose_count = len(message["poses"])
        forwarded = dict(message)
        forwarded.update({
            "source_id": source_id,
            "source_kind": "mobile_pose",
            "received_at_ms": round(time.time() * 1000),
        })
        switched_from = None
        activated = False
        with self._lock:
            if self._active_pose_source and self._active_pose_source != source_id:
                old_source = self._active_pose_source
                old_owner, _ = self._clear_source_locked(old_source)
                if old_owner is not None:
                    old_owner.source_ids.discard(old_source)
                old_voice = self._active_voice_source
                if old_voice and old_voice != VOICE_SOURCE_PREFIX + device_id:
                    old_voice_owner, _ = self._clear_source_locked(old_voice)
                    if old_voice_owner is not None:
                        old_voice_owner.source_ids.discard(old_voice)
                switched_from = old_source
            activated = self._active_pose_source != source_id
            if activated:
                self._reset_pose_metrics_locked()
            self._active_pose_source = source_id
            self._source_peers[source_id] = peer
            peer.source_ids.add(source_id)
            sequence = int(message["sequence"])
            previous_sequence = self._pose_last_sequence
            if previous_sequence is not None and sequence > previous_sequence + 1:
                self._pose_skipped_frames += sequence - previous_sequence - 1
            self._pose_last_sequence = sequence
            self._pose_receive_times.append(received_at)
            if "inference_ms" in message:
                self._pose_last_inference_ms = float(message["inference_ms"])
                self._pose_inference_times.append(self._pose_last_inference_ms)
            self._pose_last_resolution = {
                "width": int(message["width"]), "height": int(message["height"]),
            }
            self._pose_last_count = pose_count
            state = {
                "device_id": device_id,
                "received_at": received_at,
                "pose_count": pose_count,
                "sequence": sequence,
                "width": int(message["width"]),
                "height": int(message["height"]),
                "inference_ms": self._pose_last_inference_ms,
                "preview_mirrored": bool(message["preview_mirrored"]),
                "coordinates_mirrored": bool(message["coordinates_mirrored"]),
            }
            self._pose_sources[source_id] = state
            voice_source = VOICE_SOURCE_PREFIX + device_id
            if voice_source in self._voice_sources:
                self._voice_sources[voice_source]["received_at"] = received_at
            if pose_count:
                self._pose_frames_with_people += 1
            self._latest_pose = {"message": forwarded, "received_at": received_at, "source_id": source_id, "pose_count": pose_count}
        if self.kernel is not None:
            self.kernel.handle_pose_message(source_id, forwarded)
        if switched_from:
            self._broadcast_pose_state(switched_from, False, "source_switch")
        if activated:
            self._broadcast_pose_state(source_id, True, "source_active")
        self._broadcast_pose(forwarded)
        self._accept_input(peer)

    def _handle_sensor(self, peer: WebSocketPeer, message: dict) -> None:
        buttons, left_trigger, right_trigger, stick_x, stick_y, player_slot = _validate_sensor_frame(message)
        device_id = message["device_id"].strip()
        source_id = SENSOR_SOURCE_PREFIX + device_id + f":{player_slot}"
        output_source = source_id
        received_at = time.monotonic()
        quaternion = dict(message["quaternion"])
        rotation_rate = dict(message["rotation_rate"])
        acceleration = dict(message["acceleration"])
        recenter = bool(message["recenter"])
        if self.kernel is not None:
            self.kernel.handle_sensor(
                output_source,
                buttons,
                left_trigger=left_trigger,
                right_trigger=right_trigger,
                stick_x=stick_x,
                stick_y=stick_y,
                quaternion=quaternion,
                rotation_rate=rotation_rate,
                acceleration=acceleration,
                recenter=recenter,
            )
        else:
            self.output.set_sensor_state(
                output_source,
                buttons,
                left_trigger=left_trigger,
                right_trigger=right_trigger,
                stick_x=stick_x,
                stick_y=stick_y,
            )
        with self._lock:
            self._source_peers[source_id] = peer
            peer.source_ids.add(source_id)
            self._sensor_sources[source_id] = {
                "device_id": device_id,
                "player_slot": player_slot,
                "received_at": received_at,
                "quaternion": quaternion,
                "rotation_rate": rotation_rate,
                "acceleration": acceleration,
                "recenter": recenter,
            }
        self._accept_input(peer)

    def _handle_voice_text(self, peer: WebSocketPeer, message: dict) -> None:
        _validate_voice_text(message)
        if self.kernel is not None and self._body_mode != "phone":
            self._send_error(peer, "voice_text 仅在手机身体源激活时有效")
            return
        device_id = message["device_id"].strip()
        source_id = VOICE_SOURCE_PREFIX + device_id
        with self._lock:
            active_pose = self._active_pose_source
            if active_pose and active_pose != POSE_SOURCE_PREFIX + device_id:
                self._send_error(peer, "voice_text 不是当前身体源")
                return
            if self._active_voice_source and self._active_voice_source != source_id:
                self._clear_source_locked(self._active_voice_source)
            self._active_voice_source = source_id
            self._source_peers[source_id] = peer
            peer.source_ids.add(source_id)
            self._voice_sources[source_id] = {"device_id": device_id, "received_at": time.monotonic()}
        voice = self._voice_service
        if voice is None:
            self._send_error(peer, "本地语音解析器未配置")
            return
        try:
            _, result = voice.accept_phone_text(
                source_id, device_id, message["text"],
                float(message["confidence"]) if message.get("confidence") is not None else None,
            )
            if result and not result.get("matched", False):
                self._send_error(peer, str(result.get("reason", "语音命令未匹配")))
            self._accept_input(peer)
        except (ValueError, RuntimeError) as exc:
            self._send_error(peer, str(exc))

    def _handle_voice_command(self, peer: WebSocketPeer, message: dict) -> None:
        """v0.9.4: phone sends voice_command with command_id + phrase directly."""
        if self.kernel is not None and self._body_mode != "phone":
            self._send_error(peer, "voice_command 仅在手机身体源激活时有效")
            return
        device_id = str(message.get("device_id", "")).strip()
        if not device_id:
            self._send_error(peer, "voice_command 缺少 device_id")
            return
        command_id = str(message.get("command_id", "")).strip()
        if not command_id:
            self._send_error(peer, "voice_command 缺少 command_id")
            return
        phrase = str(message.get("phrase", "")).strip()
        source_id = VOICE_SOURCE_PREFIX + device_id
        with self._lock:
            active_pose = self._active_pose_source
            if active_pose and active_pose != POSE_SOURCE_PREFIX + device_id:
                self._send_error(peer, "voice_command 不是当前身体源")
                return
            if self._active_voice_source and self._active_voice_source != source_id:
                self._clear_source_locked(self._active_voice_source)
            self._active_voice_source = source_id
            self._source_peers[source_id] = peer
            peer.source_ids.add(source_id)
            self._voice_sources[source_id] = {"device_id": device_id, "received_at": time.monotonic()}
        voice = self._voice_service
        if voice is None:
            self._send_error(peer, "本地语音解析器未配置")
            return
        try:
            _, result = voice.accept_phone_command(source_id, device_id, command_id, phrase)
            if result and not result.get("matched", False):
                self._send_error(peer, str(result.get("reason", "语音命令未匹配")))
            self._accept_input(peer)
        except (ValueError, RuntimeError) as exc:
            self._send_error(peer, str(exc))

    def _handle_scene_snapshot(self, peer: WebSocketPeer, message: dict) -> None:
        if self._body_mode != "phone":
            self._send_error(peer, "scene_snapshot 仅在手机身体源激活时有效")
            return
        device_id = str(message.get("device_id", "")).strip()
        if not device_id:
            self._send_error(peer, "scene_snapshot 缺少 device_id")
            return
        with self._lock:
            active_pose = self._active_pose_source
            owner = self._source_peers.get(active_pose) if active_pose else None
            handler = self._scene_snapshot_handler
        if active_pose != POSE_SOURCE_PREFIX + device_id or owner is not peer:
            self._send_error(peer, "scene_snapshot 不是当前活动身体源")
            return
        purpose = str(message.get("purpose", "capture")).strip().lower()
        if purpose not in {"capture", "rematch"}:
            self._send_error(peer, "scene_snapshot purpose 无效")
            return
        encoded = str(message.get("jpeg_base64", "")).strip()
        if not encoded:
            self._send_error(peer, "scene_snapshot 缺少 jpeg_base64")
            return
        try:
            jpeg = base64.b64decode(encoded, validate=True)
        except Exception:
            self._send_error(peer, "scene_snapshot JPEG base64 无效")
            return
        if not jpeg or len(jpeg) > 700 * 1024:
            self._send_error(peer, "scene_snapshot JPEG 大小必须在 1 到 700KB")
            return
        if handler is None:
            self._send_error(peer, "场景截图处理器未配置")
            return
        try:
            result = handler(jpeg, purpose, device_id) or {}
            peer.send_json({"type": "scene_snapshot_result", "purpose": purpose, **result})
            self._accept_input(peer)
        except Exception as exc:
            self._send_error(peer, f"场景截图处理失败：{exc}")

    def handle_message(self, peer: WebSocketPeer, message: dict) -> None:
        try:
            if not isinstance(message, dict):
                raise ValueError("message must be a JSON object")
            message_type = message.get("type")
            if message_type == "clock_sync":
                if not _is_number(message.get("client_sent_ms")):
                    raise ValueError("client_sent_ms must be a number")
                peer.send_json({"type": "clock_sync", "client_sent_ms": message["client_sent_ms"], "server_ms": round(time.time() * 1000)})
            elif message_type == "pose_frame_v2":
                self._handle_pose(peer, message)
            elif message_type == "sensor_frame":
                self._handle_sensor(peer, message)
            elif message_type == "voice_text":
                self._handle_voice_text(peer, message)
            elif message_type == "voice_command":
                self._handle_voice_command(peer, message)
            elif message_type == "scene_snapshot":
                self._handle_scene_snapshot(peer, message)
            else:
                raise ValueError(f"unknown input type: {message_type}")
        except (ValueError, TypeError) as exc:
            self._send_error(peer, str(exc))
        except (ConnectionError, OSError):
            self.disconnect(peer)

    def _clear_source_locked(self, source_id: str):
        owner = self._source_peers.pop(source_id, None)
        self._pose_sources.pop(source_id, None)
        self._sensor_sources.pop(source_id, None)
        voice_source = source_id in self._voice_sources
        self._voice_sources.pop(source_id, None)
        try:
            if voice_source and self._voice_service is not None:
                self._voice_service.disconnect(source_id)
            elif self.kernel is not None:
                self.kernel.clear_source(source_id)
            else:
                self.output.clear_source(source_id)
        except (AttributeError, RuntimeError, OSError):
            pass
        was_active_pose = self._active_pose_source == source_id
        if was_active_pose:
            self._active_pose_source = None
            self._reset_pose_metrics_locked()
        if self._active_voice_source == source_id:
            self._active_voice_source = None
        if self._latest_pose and self._latest_pose["source_id"] == source_id:
            self._latest_pose = None
        return owner, was_active_pose

    def disconnect(self, peer: WebSocketPeer) -> None:
        cleared_pose_sources: list[str] = []
        with self._lock:
            self._peers.discard(peer)
            for source_id in list(peer.source_ids):
                if self._source_peers.get(source_id) is peer:
                    _, was_active_pose = self._clear_source_locked(source_id)
                    if was_active_pose:
                        cleared_pose_sources.append(source_id)
            peer.source_ids.clear()
        peer.close()
        for source_id in cleared_pose_sources:
            self._broadcast_pose_state(source_id, False, "disconnect")

    def _watch_loop(self) -> None:
        while not self._stop.wait(0.05):
            now = time.monotonic()
            cleared_pose_sources: list[str] = []
            with self._lock:
                if self._latest_pose and now - self._latest_pose["received_at"] > 0.30:
                    source_id = self._latest_pose["source_id"]
                    owner, was_active_pose = self._clear_source_locked(source_id)
                    if owner is not None:
                        owner.source_ids.discard(source_id)
                    if was_active_pose:
                        cleared_pose_sources.append(source_id)
                stale = [
                    source_id
                    for source_id, state in self._sensor_sources.items()
                    if now - state["received_at"] > 0.30 and self._source_peers.get(source_id) is not None
                ]
                for source_id in stale:
                    owner, _ = self._clear_source_locked(source_id)
                    if owner is not None:
                        owner.source_ids.discard(source_id)
                voice_stale = [
                    source_id for source_id, state in self._voice_sources.items()
                    if now - state["received_at"] > 1.5
                ]
                for source_id in voice_stale:
                    owner, _ = self._clear_source_locked(source_id)
                    if owner is not None:
                        owner.source_ids.discard(source_id)
            for source_id in cleared_pose_sources:
                self._broadcast_pose_state(source_id, False, "watchdog")

    def serve_websocket(self, handler, query: str) -> None:
        params = parse_qs(query, keep_blank_values=True)
        desktop = params.get("client", [""])[0].lower() == "desktop"
        try:
            peer = perform_websocket_upgrade(handler, desktop=desktop)
        except (ValueError, OSError) as exc:
            try:
                handler.send_error(400, str(exc))
            except OSError:
                pass
            return
        self.register(peer)
        try:
            while True:
                try:
                    opcode, payload = peer.recv()
                except socket.timeout:
                    continue
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    peer.send_bytes(0xA, payload)
                    continue
                if opcode != 0x1:
                    raise WebSocketProtocolError("only text websocket messages are supported")
                try:
                    message = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    self._send_error(peer, f"invalid JSON: {exc}")
                    continue
                self.handle_message(peer, message)
        except (ConnectionError, OSError, WebSocketProtocolError):
            pass
        finally:
            self.disconnect(peer)

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            peers = list(self._peers)
        for peer in peers:
            self.disconnect(peer)
