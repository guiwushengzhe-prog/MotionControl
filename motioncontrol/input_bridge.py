from __future__ import annotations

import base64
import hashlib
import ipaddress
import inspect
import json
import math
import socket
import struct
import threading
import time
from collections import deque
from collections.abc import Iterable
from urllib.parse import parse_qs, urlparse

from motioncontrol import local_endpoints


WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE_BYTES = 1024 * 1024
# MediaPipe image landmarks are nominally normalized, but x/y can be just
# outside [0, 1] when a person touches the image edge.  Keep a generous
# finite guard against corrupted payloads without clamping coordinates here;
# the kernel needs the original geometry and display code can clip later.
LANDMARK_COORDINATE_ABS_LIMIT = 10.0
POSE_SOURCE_PREFIX = "mobile_pose:"
SENSOR_SOURCE_PREFIX = "mobile_sensor:"
MOUSE_SOURCE_PREFIX = "mobile_mouse:"
VOICE_SOURCE_PREFIX = "mobile_voice:"
VOICE_AUDIO_MAX_BYTES = 256 * 1024
POSE_MAX_AGE_MS = 500.0

# Compact mobile camera protocols.  Phones still run MediaPipe locally and the
# bridge expands packed landmarks back to the canonical 33-point pose.  Keep
# mc25-v1 readable for already-installed phones; mc27-v2 adds the two inner-eye
# points required by the audited frozen22 11-point face signature.
MOBILE_POSE_FEATURE_INDICES = (
    0,   # nose
    2, 3, 5, 6,  # eyes + outer eyes used by scene/head control
    7, 8, 9, 10,  # ears + mouth
    11, 12, 13, 14, 15, 16,  # shoulders/elbows/wrists
    23, 24, 25, 26, 27, 28,  # hips/knees/ankles
    29, 30, 31, 32,  # heels/foot indices
)
MOBILE_POSE_FEATURE_LAYOUT = "mc25-v1"
MOBILE_POSE_FEATURE_INDICES_V2 = (
    0, 1, 2, 3, 4, 5, 6,  # nose + complete MediaPipe eye landmarks
    7, 8, 9, 10,  # ears + mouth
    11, 12, 13, 14, 15, 16,  # shoulders/elbows/wrists
    23, 24, 25, 26, 27, 28,  # hips/knees/ankles
    29, 30, 31, 32,  # heels/foot indices
)
MOBILE_POSE_FEATURE_LAYOUT_V2 = "mc27-v2"
# mc33-v3 sends the whole skeleton.  The compact layouts above skip indices
# 17-22 -- pinky, index and thumb on both hands -- which is exactly the data a
# fist needs: the pose model has no finger joints, so "closed" has to be read
# from how far those three tips sit from the wrist.  Hand steering is therefore
# only available on this layout.  It costs six more points per frame than
# mc27-v2; the compact layouts stay for phones that have not updated.
MOBILE_POSE_FEATURE_INDICES_V3 = tuple(range(33))
MOBILE_POSE_FEATURE_LAYOUT_V3 = "mc33-v3"
MOBILE_POSE_FEATURE_LAYOUTS = {
    MOBILE_POSE_FEATURE_LAYOUT: MOBILE_POSE_FEATURE_INDICES,
    MOBILE_POSE_FEATURE_LAYOUT_V2: MOBILE_POSE_FEATURE_INDICES_V2,
    MOBILE_POSE_FEATURE_LAYOUT_V3: MOBILE_POSE_FEATURE_INDICES_V3,
}

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
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class WebSocketPeer:
    """Small RFC 6455 text-frame peer for the local input bridge."""

    def __init__(self, handler, *, desktop: bool = False) -> None:
        self.handler = handler
        self.stream = handler.rfile
        self.connection = handler.connection
        # 收到但还没拼成一帧的字节。直接从 socket 读、自己攒，不走 handler.rfile——
        # 原因见 _read。
        #
        # 为什么可以不管 rfile 里可能预读的字节：RFC 6455 规定客户端必须等到服务端
        # 的 101 回应之后才能发数据帧，浏览器（包括手机 App 里的 WebView）都照做。
        # 所以解析完握手请求的那一刻，rfile 的缓冲里除了请求本身什么都没有。
        self._inbox = bytearray()
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

    # 一帧读到一半对方停住了，给它多久。两帧之间对方可以一直不说话，那不算。
    MID_FRAME_PATIENCE_S = 10.0

    def _read(self, size: int, *, frame_start: bool = False) -> bytes:
        """凑够 size 个字节。

        ## 为什么不用 handler.rfile

        这个连接设了 1 秒超时（保护发送：手机不读了，电脑最多卡 1 秒）。可 rfile
        底下那层 socket.SocketIO 有个脾气：**超时过一次，以后每次读都直接报
        "cannot read from timed out object"**。于是手机只要安静超过 1 秒——画面里
        没人时它就一帧都不发——电脑这边第一次超时之后，下一次读就当成连接坏了、
        主动断开。手机收到断开，显示「未连接电脑」，1.5 秒后重连，还是没人，又断。
        在用户眼里就是"人一离开画面就断线"，而这两件事本来毫无关系。

        裸 socket 没有这个脾气，超时之后照样能读。

        ## 超时怎么算

        在两帧之间（一个字节都还没收到）超时，就是对方暂时没话说：原样抛出
        socket.timeout，外面的循环接着等。一帧读到一半超时，说明对方真卡住了，
        等够 MID_FRAME_PATIENCE_S 才放弃，已经收到的字节一个不丢。
        """
        waited = 0.0
        while len(self._inbox) < size:
            try:
                chunk = self.connection.recv(65536)
            except socket.timeout:
                if frame_start and not self._inbox:
                    raise
                waited += self.connection.gettimeout() or 1.0
                if waited >= self.MID_FRAME_PATIENCE_S:
                    raise ConnectionError("websocket frame stalled mid-way")
                continue
            if not chunk:
                raise ConnectionError("websocket closed")
            self._inbox += chunk
        data = bytes(self._inbox[:size])
        del self._inbox[:size]
        return data

    def recv(self) -> tuple[int, bytes]:
        header = self._read(2, frame_start=True)
        first, second = header
        if not first & 0x80:
            raise WebSocketProtocolError("fragmented websocket frames are not supported")
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read(8))[0]
        if length > MAX_MESSAGE_BYTES:
            raise WebSocketProtocolError("websocket message is too large")
        if not masked:
            raise WebSocketProtocolError("client websocket frame must be masked")
        mask = self._read(4)
        payload = bytearray(self._read(length))
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


def _validate_pose_features(message: dict) -> None:
    if message.get("type") != "pose_features_v1" or message.get("role") != "camera":
        raise ValueError("pose_features_v1 requires role=camera")
    layout = message.get("layout")
    indices = MOBILE_POSE_FEATURE_LAYOUTS.get(layout)
    if indices is None:
        supported = ", ".join(MOBILE_POSE_FEATURE_LAYOUTS)
        raise ValueError(f"pose_features_v1 layout must be one of: {supported}")
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
    if not isinstance(message.get("preview_mirrored"), bool) or not isinstance(message.get("coordinates_mirrored"), bool):
        raise ValueError("mirror flags must be boolean")
    points = message.get("points")
    if not isinstance(points, list) or len(points) not in {0, len(indices)}:
        raise ValueError(f"points must contain 0 or {len(indices)} packed landmarks for {layout}")
    for point in points:
        if not isinstance(point, list) or len(point) != 4 or not all(_is_number(v) for v in point):
            raise ValueError("each packed point must be [x,y,z,visibility]")
        x, y, z, visibility = map(float, point)
        if max(abs(x), abs(y), abs(z)) > LANDMARK_COORDINATE_ABS_LIMIT:
            raise ValueError("packed pose coordinate exceeds safety limit")
        if not 0.0 <= visibility <= 1.0:
            raise ValueError("packed pose visibility must be in [0,1]")
    world_points = message.get("world_points")
    if world_points is not None:
        if layout not in {MOBILE_POSE_FEATURE_LAYOUT_V2, MOBILE_POSE_FEATURE_LAYOUT_V3}:
            raise ValueError("world_points requires mc27-v2 or mc33-v3")
        if not isinstance(world_points, list) or len(world_points) != 33:
            raise ValueError("world_points must contain exactly 33 packed landmarks")
        for point in world_points:
            if not isinstance(point, list) or len(point) != 4 or not all(_is_number(v) for v in point):
                raise ValueError("each packed world point must be [x,y,z,visibility]")
            x, y, z, visibility = map(float, point)
            if max(abs(x), abs(y), abs(z)) > LANDMARK_COORDINATE_ABS_LIMIT:
                raise ValueError("packed world coordinate exceeds safety limit")
            if not 0.0 <= visibility <= 1.0:
                raise ValueError("packed world visibility must be in [0,1]")
    _validate_packed_hands(message.get("hands"))
    if not _is_number(message.get("inference_ms", 0)) or float(message.get("inference_ms", 0)) < 0:
        raise ValueError("inference_ms must be >= 0")


def _validate_packed_hands(hands: object) -> None:
    """Check the optional hand block a camera device may attach to a frame.

    The device runs the hand model only while the desktop has asked for it, so
    absent is the normal case and must stay cheap; a device that never gained
    the feature keeps working unchanged.  Handedness is the side the desktop
    asked the device to look at, not a guess made from the picture -- the
    device crops around the wrist it was told to watch, so there is nothing to
    guess and nothing to mix up.
    """
    if hands is None:
        return
    if not isinstance(hands, list) or len(hands) > 2:
        raise ValueError("hands must contain 0 to 2 items")
    for hand in hands:
        if not isinstance(hand, dict) or hand.get("handedness") not in {"Left", "Right"}:
            raise ValueError("handedness must be Left or Right")
        points = hand.get("points")
        if not isinstance(points, list) or len(points) != 21:
            raise ValueError("each hand must contain exactly 21 packed landmarks")
        for point in points:
            if not isinstance(point, list) or len(point) != 4 or not all(_is_number(v) for v in point):
                raise ValueError("each packed hand point must be [x,y,z,score]")
            x, y, z, score = map(float, point)
            if max(abs(x), abs(y), abs(z)) > LANDMARK_COORDINATE_ABS_LIMIT:
                raise ValueError("packed hand coordinate exceeds safety limit")
            if not 0.0 <= score <= 1.0:
                raise ValueError("packed hand score must be in [0,1]")


def _expand_pose_features(message: dict) -> dict:
    """Expand a supported compact layout into canonical pose_frame_v2.

    This conversion happens only inside the PC process.  Omitted landmarks are
    present with zero visibility so existing renderers and algorithms can keep
    consuming a 33-landmark frame unchanged.
    """
    _validate_pose_features(message)
    indices = MOBILE_POSE_FEATURE_LAYOUTS[message["layout"]]
    packed = message.get("points") or []
    world_points = message.get("world_points")
    world_pose = None
    if world_points is not None:
        world_pose = [
            {
                "x": float(values[0]), "y": float(values[1]),
                "z": float(values[2]), "visibility": float(values[3]),
            }
            for values in world_points
        ]
    poses = []
    if packed:
        full = [{"x": 0.0, "y": 0.0, "z": 0.0, "visibility": 0.0} for _ in range(33)]
        for index, values in zip(indices, packed):
            full[index] = {
                "x": float(values[0]), "y": float(values[1]),
                "z": float(values[2]), "visibility": float(values[3]),
            }
        poses = [{"detection_id": None, "pose": full, "world_pose": world_pose}]
    hands = [
        {
            "handedness": hand["handedness"],
            "landmarks": [
                {"x": float(v[0]), "y": float(v[1]), "z": float(v[2]), "visibility": float(v[3])}
                for v in hand["points"]
            ],
        }
        for hand in (message.get("hands") or [])
    ]
    return {
        "type": "pose_frame_v2",
        "role": "camera",
        "device_id": message["device_id"],
        "sequence": int(message["sequence"]),
        "captured_at_ms": float(message["captured_at_ms"]),
        "sent_at_ms": float(message.get("sent_at_ms", message["captured_at_ms"])),
        "width": int(message["width"]),
        "height": int(message["height"]),
        "camera_facing": message["camera_facing"],
        "camera_id": str(message.get("camera_id", "logical")),
        "orientation_degrees": 0,
        "preview_mirrored": bool(message["preview_mirrored"]),
        "coordinates_mirrored": bool(message["coordinates_mirrored"]),
        "actual_model": str(message.get("actual_model", "")),
        "delegate": str(message.get("delegate", "")),
        "inference_side": int(message.get("inference_side") or 0),
        "voice_state": str(message.get("voice_state", "not_connected")),
        "poses": poses,
        "hands": hands,
        "inference_ms": float(message.get("inference_ms", 0.0)),
        "wire_protocol": "pose_features_v1",
    }


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


def _validate_voice_audio(message: dict) -> bytes:
    """Validate and decode one phone PCM audio frame.

    The wire format is deliberately explicit so a camera frame cannot be
    mistaken for audio: 16 kHz, mono, little-endian signed 16-bit PCM and a
    strict base64 payload.  A frame is kept below the WebSocket message limit
    and the recognizer's own input limit.
    """
    if message.get("type") != "voice_audio" or message.get("role") != "camera":
        raise ValueError("voice_audio requires role=camera")
    if not isinstance(message.get("device_id"), str) or not message["device_id"].strip():
        raise ValueError("device_id must be a non-empty string")
    if not _is_int(message.get("sequence")) or message["sequence"] < 0:
        raise ValueError("sequence must be an integer >= 0")
    if not _is_number(message.get("captured_at_ms")):
        raise ValueError("captured_at_ms must be a number")
    sample_rate = message.get("sample_rate", message.get("sampleRate"))
    if not _is_int(sample_rate) or sample_rate != 16_000:
        raise ValueError("voice_audio sample_rate must be 16000")
    channels = message.get("channels", message.get("channel_count", 1))
    if not _is_int(channels) or channels != 1:
        raise ValueError("voice_audio channels must be 1")
    encoding = str(message.get("encoding", message.get("format", ""))).strip().lower()
    if encoding != "pcm16le":
        raise ValueError("voice_audio encoding must be pcm16le")
    encoded = message.get("audio_base64", message.get("pcm_base64"))
    if not isinstance(encoded, str) or not encoded.strip():
        raise ValueError("voice_audio 缺少 base64 音频")
    try:
        pcm16 = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise ValueError("voice_audio base64 无效") from None
    if not pcm16 or len(pcm16) % 2:
        raise ValueError("voice_audio 必须是非空偶数字节 PCM16")
    if len(pcm16) > VOICE_AUDIO_MAX_BYTES:
        raise ValueError("voice_audio 单帧过大")
    return pcm16


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


def _validate_mouse_frame(message: dict) -> tuple[set[str], int, int]:
    if message.get("type") != "mouse_frame" or message.get("role") != "mouse":
        raise ValueError("鼠标数据的模式必须为 mouse")
    if not isinstance(message.get("device_id"), str) or not message["device_id"].strip():
        raise ValueError("鼠标设备编号不能为空")
    if not _is_int(message.get("sequence")) or message["sequence"] < 0:
        raise ValueError("鼠标数据序号必须为不小于 0 的整数")
    if not _is_number(message.get("captured_at_ms")):
        raise ValueError("鼠标采集时间必须为有限数值")
    if "sent_at_ms" in message and not _is_number(message["sent_at_ms"]):
        raise ValueError("鼠标发送时间必须为有限数值")
    if not all(_is_int(message.get(axis)) and abs(message[axis]) <= 32767 for axis in ("dx", "dy")):
        raise ValueError("鼠标横向和纵向位移必须为 -32767 到 32767 的整数")
    buttons = message.get("buttons")
    if not _is_int(buttons) or buttons not in {0, 1, 2, 3}:
        raise ValueError("鼠标按键状态必须为 0 到 3 的整数")
    names = {name for mask, name in ((1, "LEFT"), (2, "RIGHT")) if buttons & mask}
    return names, message["dx"], message["dy"]


def _local_addresses() -> list[str]:
    """Addresses the phone can reach this PC at, best link first.

    This used to resolve the machine's own hostname.  Measured on a Windows
    machine with a phone tethered over USB, that returned only the USB address
    and hid the Wi-Fi one -- and it never saw the virtual adapters at all, so
    it could not have filtered them either.  local_endpoints asks Windows the
    same way Windows asks itself.
    """
    return local_endpoints.addresses()


class InputBridge:
    """Receives MotionBridge input and fans mobile poses to desktop consumers."""

    def __init__(self, output, kernel=None, voice=None, *, on_output_control=None) -> None:
        self.output = output
        self._on_output_control = on_output_control
        self.kernel = kernel
        self._voice_service = voice
        self._control_config_provider = None
        self._lock = threading.RLock()
        self._peers: set[WebSocketPeer] = set()
        self._source_peers: dict[str, WebSocketPeer] = {}
        self._pose_sources: dict[str, dict] = {}
        self._sensor_sources: dict[str, dict] = {}
        self._mouse_sources: dict[str, dict] = {}
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
        self._last_trigger_held: list[dict] = []
        # The phone is the usual body source whether or not a kernel is wired in.
        self._body_mode = "phone"
        self._body_enabled = True
        # Sequence ownership belongs to a websocket session, not merely a
        # device id: reconnects may legitimately restart their sequence at zero.
        self._pose_sessions: dict[WebSocketPeer, dict] = {}
        self._sensor_sessions: dict[tuple[WebSocketPeer, str], dict] = {}
        self._superseded_sensor_sessions: set[tuple[WebSocketPeer, str]] = set()
        self._mouse_sessions: dict[tuple[WebSocketPeer, str], dict] = {}
        self._superseded_mouse_sessions: set[tuple[WebSocketPeer, str]] = set()
        self._voice_audio_sequences: dict[tuple[WebSocketPeer, str], int] = {}
        self._superseded_voice_sessions: set[tuple[WebSocketPeer, str]] = set()
        self._voice_owner_generations: dict[str, int] = {}
        self._pending_voice_clears: list[tuple[object, str, int | None]] = []
        self._superseded_pose_peers: set[WebSocketPeer] = set()
        self._clock_synced_peers: set[WebSocketPeer] = set()
        self._voice_clock_trusted_peers: set[WebSocketPeer] = set()
        self._pose_rejected = {"disabled": 0, "owner": 0, "sequence": 0, "stale": 0}
        self._sensor_rejected = {"owner": 0, "sequence": 0, "stale": 0}
        self._mouse_rejected = {"owner": 0, "sequence": 0, "stale": 0}
        remembered_audio = "computer"
        if kernel is not None:
            try:
                remembered_audio = kernel.general_setting("audio_source", "computer")
            except (AttributeError, TypeError):
                remembered_audio = "computer"
        remembered_audio = str(remembered_audio or "").strip().lower()
        if remembered_audio not in {"computer", "phone"}:
            remembered_audio = "computer"
        # audio_mode and audio_source are kept as the same persisted choice;
        # the duplicate names make the bridge status compatible with callers
        # that describe this setting as either a mode or a source.
        self._audio_mode = remembered_audio
        self._audio_source = remembered_audio
        # 最近一次因为"来源选的是电脑"而丢掉手机画面的时刻。界面靠它把这件事说
        # 出来：两边都显示正常、什么都不动，是最难查的一种坏法。
        self._phone_ignored_at = 0.0
        self._pose_frames_with_people = 0
        self._host = "0.0.0.0"
        self._port = 8765
        self._stop = threading.Event()
        self._watchdog = threading.Thread(target=self._watch_loop, name="motion-input-watchdog", daemon=True)
        self.configure_voice(voice)
        self._watchdog.start()

    def configure_voice(self, voice) -> None:
        with self._lock:
            self._voice_service = voice
            self._voice_generation_methods: set[str] = set()
            try:
                self._voice_audio_async = "result_callback" in inspect.signature(voice.accept_phone_audio).parameters
            except (AttributeError, TypeError, ValueError):
                self._voice_audio_async = False
            if callable(getattr(voice, "phone_source_generation", None)):
                for method in ("accept_phone_audio", "accept_phone_text", "accept_phone_command"):
                    try:
                        if "expected_generation" in inspect.signature(getattr(voice, method)).parameters:
                            self._voice_generation_methods.add(method)
                    except (AttributeError, TypeError, ValueError):
                        pass

    def _voice_submission_locked(self, method: str, source_id: str) -> tuple[object, dict]:
        """Capture admission before dropping the bridge lock for voice work."""
        voice = self._voice_service
        kwargs = {}
        if method in self._voice_generation_methods:
            kwargs["expected_generation"] = voice.phone_source_generation(source_id)
        return voice, kwargs

    @property
    def audio_source(self) -> str:
        with self._lock:
            return self._audio_source

    @property
    def audio_mode(self) -> str:
        with self._lock:
            return self._audio_mode

    def configure_scene_snapshot_handler(self, handler) -> None:
        with self._lock:
            self._scene_snapshot_handler = handler
    def configure_control_config_provider(self, provider) -> None:
        """Provide the current PC-authoritative game/Zone configuration to phones."""
        with self._lock:
            self._control_config_provider = provider

    def _control_config_snapshot(self) -> dict | None:
        with self._lock:
            provider = self._control_config_provider
        if provider is None:
            return None
        try:
            payload = provider()
            return dict(payload) if isinstance(payload, dict) else None
        except Exception:
            return None

    def broadcast_control_config(self, payload: dict | None = None) -> dict:
        """Push profile + fixed Zone data to connected phone clients.

        The PC remains authoritative; phones cache this only for display/UX and
        never re-decide mappings locally.
        """
        message = dict(payload) if isinstance(payload, dict) else self._control_config_snapshot()
        if not message:
            return {"sent": 0}
        message.setdefault("type", "control_config_v1")
        with self._lock:
            peers = [peer for peer in self._peers if not peer.desktop]
        sent = 0
        for peer in peers:
            try:
                peer.send_json(message)
                sent += 1
            except (ConnectionError, OSError):
                self.disconnect(peer)
        return {"sent": sent}

    def broadcast_trigger_state(self, payload: dict) -> dict:
        """把"现在按着什么、刚才触发了什么"推给手机。

        和 broadcast_control_config 走同一条路，区别只在频率：配置一天变几次，
        这个在玩的时候一秒可能变几次。所以**只在集合变化时**才会走到这里——
        内核那边已经挡过一道，这里不再节流。
        """
        message = dict(payload)
        message["type"] = "trigger_state_v1"
        with self._lock:
            self._last_trigger_held = [
                dict(item) for item in message.get("held", []) if isinstance(item, dict)
            ]
            peers = [peer for peer in self._peers if not peer.desktop]
        sent = 0
        for peer in peers:
            try:
                peer.send_json(message)
                sent += 1
            except (ConnectionError, OSError):
                self.disconnect(peer)
        return {"sent": sent}

    def _game_output_state(self, *, ok: bool = True, error: str | None = None) -> dict:
        try:
            enabled = bool(self.output.status().get("enabled", False))
        except Exception:
            enabled = bool(getattr(self.output, "enabled", False))
        message = {"type": "game_output_state_v1", "enabled": enabled, "ok": bool(ok)}
        if error:
            message["error"] = str(error)
        return message

    def broadcast_game_output_state(self, *, exclude=None) -> dict:
        """Push actual output state to every connected phone."""
        message = self._game_output_state()
        with self._lock:
            peers = [
                peer for peer in self._peers
                if peer is not exclude and not peer.desktop
            ]
        sent = 0
        for peer in peers:
            try:
                peer.send_json(message)
                sent += 1
            except (ConnectionError, OSError):
                self.disconnect(peer)
        return {"sent": sent, "enabled": message["enabled"]}

    def _send_game_output_state(self, peer, *, ok: bool = True, error: str | None = None) -> None:
        try:
            peer.send_json(self._game_output_state(ok=ok, error=error))
        except (ConnectionError, OSError):
            self.disconnect(peer)

    def _handle_game_output_control(self, peer, message: dict) -> None:
        enabled = message.get("enabled")
        if not isinstance(enabled, bool):
            self._send_game_output_state(peer, ok=False, error="enabled 必须是布尔值")
            return
        try:
            if self._on_output_control is not None:
                self._on_output_control(enabled)
            else:
                self.output.set_config(enabled=enabled)
        except Exception as exc:
            self._send_game_output_state(peer, ok=False, error=str(exc))
            return
        self.broadcast_game_output_state(exclude=peer)
        self._send_game_output_state(peer)

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
        for source_id in cleared:
            self._broadcast_pose_state(source_id, False, "source_switch")

    def set_body_enabled(self, enabled: bool) -> None:
        """Explicit stop wins over the automatic phone fallback.

        The same admission lock covers pose submission to the kernel, so no
        previously admitted mobile frame can resume output after this returns.
        """
        cleared: list[str] = []
        with self._lock:
            self._body_enabled = bool(enabled)
            if not self._body_enabled:
                for source_id in list(self._pose_sources):
                    owner, active = self._clear_source_locked(source_id)
                    if owner is not None:
                        owner.source_ids.discard(source_id)
                    if active:
                        cleared.append(source_id)
        for source_id in cleared:
            self._broadcast_pose_state(source_id, False, "body_stopped")

    def set_audio_mode(self, mode: str) -> None:
        """Select the independent audio source and remember it for restart."""
        mode = str(mode).strip().lower()
        if mode not in {"computer", "phone"}:
            raise ValueError("audio mode must be computer or phone")
        with self._lock:
            self._audio_mode = mode
            self._audio_source = mode
            if mode != "phone":
                for source_id in list(self._voice_sources):
                    owner, _ = self._clear_source_locked(source_id)
                    if owner is not None:
                        owner.source_ids.discard(source_id)
        self._flush_voice_clears()
        if self.kernel is not None:
            try:
                self.kernel.remember_general_setting("audio_source", mode)
            except (AttributeError, TypeError, OSError):
                pass

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
        self._flush_voice_clears()
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

    def usb_tether_status(self) -> dict:
        """Whether a USB tether is up, and whether it took the PC's internet.

        Worth reporting rather than silently fixing: the fix is an interface
        metric, which needs administrator rights the app does not have and
        should not want.  Telling the player what happened, with a script that
        asks for elevation once, beats either a silent surprise on the phone
        bill or an app that demands elevation to drive a gamepad.
        """
        carrier = local_endpoints.tether_carries_internet()
        tether = next((item for item in local_endpoints.endpoints()
                       if item["kind"] == local_endpoints.KIND_USB), None)
        return {
            "present": tether is not None,
            "address": tether["address"] if tether else None,
            "adapter": tether["adapter"] if tether else None,
            "carries_internet": carrier is not None,
        }

    def server_candidates(self) -> list[dict]:
        """Every address the phone should try, best link first.

        Handing the phone the whole list is what stops a changed address from
        meaning "连不上": it tries them all and keeps whichever answers.  The
        kind travels with each one so a cable can win on merit rather than on
        the player knowing to pick it.
        """
        return [
            {"host": item["address"], "port": self._port, "kind": item["kind"]}
            for item in local_endpoints.endpoints()
        ]

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
                    "delegate": state.get("delegate"),
                    "inference_side": state.get("inference_side"),
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
            mouse_sources = [
                {
                    "source_id": source_id,
                    "source_kind": "mobile_mouse",
                    "device_id": state["device_id"],
                    "age_ms": round(max(0.0, (now - state["received_at"]) * 1000)),
                    "connected": source_id in self._source_peers,
                    "buttons": state["buttons"],
                }
                for source_id, state in self._mouse_sources.items()
            ]
            latest_pose = self._latest_pose
            active_pose_source = self._active_pose_source
            audio_mode = self._audio_mode
            audio_source = self._audio_source
            body_enabled = self._body_enabled
            body_mode = self._body_mode
            pose_rejected = dict(self._pose_rejected)
            sensor_rejected = dict(self._sensor_rejected)
            mouse_rejected = dict(self._mouse_rejected)
            host = self._host
            port = self._port
        pose_age = round(max(0.0, (now - latest_pose["received_at"]) * 1000)) if latest_pose else None
        return {
            "server_host": host,
            "server_port": port,
            "lan_ipv4": self.lan_ipv4(),
            "usb_tether": self.usb_tether_status(),
            # 手机在传，但来源选的是电脑摄像头，所以它的画面正在被丢掉。
            "phone_ignored": bool(self._phone_ignored_at
                                  and now - self._phone_ignored_at < 2.0),
            "body_mode": body_mode,
            "body_enabled": body_enabled,
            "pose_rejected": pose_rejected,
            "sensor_rejected": sensor_rejected,
            "mouse_rejected": mouse_rejected,
            "audio_mode": audio_mode,
            "audio_source": audio_source,
            "phone_ws_urls": self.phone_ws_urls(),
            "mobile_pose_connected": any(item["connected"] for item in pose_sources),
            "mobile_pose_age_ms": pose_age,
            "mobile_pose_source_id": active_pose_source,
            "mobile_pose_sources": pose_sources,
            "handheld_connected": any(item["connected"] for item in sensor_sources + mouse_sources),
            "handheld_sources": sensor_sources + mouse_sources,
            "mobile_voice_connected": any(item["connected"] for item in voice_sources),
            "mobile_voice_sources": voice_sources,
            "mobile_voice_source_id": self._active_voice_source,
        }

    def register(self, peer: WebSocketPeer) -> None:
        with self._lock:
            self._peers.add(peer)
            cached = self._latest_pose
            active_source = self._active_pose_source
        if not peer.desktop:
            config = self._control_config_snapshot()
            if config:
                try:
                    peer.send_json(config)
                except (ConnectionError, OSError):
                    self.disconnect(peer)
                    return
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
            kernel = self.kernel
            held = [dict(item) for item in self._last_trigger_held]
            # The handheld socket sends sensor or mouse frames; it has no
            # camera picture to draw zones on.
            sensor_only = bool(peer.source_ids) and all(
                source.startswith((SENSOR_SOURCE_PREFIX, MOUSE_SOURCE_PREFIX)) for source in peer.source_ids)
        runtime_zones = {}
        snapshot = getattr(kernel, "runtime_zones", None)
        if callable(snapshot):
            try:
                runtime_zones = snapshot()
            except Exception:
                pass
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
            # The phone already consumes zones on trigger_state_v1. Reuse that
            # message at the existing 15-input acknowledgement cadence so
            # player-relative rectangles keep following the body without a
            # per-frame broadcast. Fixed configuration still arrives through
            # control_config_v1.zones.
            if not sensor_only:
                peer.send_json({
                    "type": "trigger_state_v1",
                    "held": held,
                    "fired": [],
                    "zones": runtime_zones,
                    "at": round(time.monotonic(), 3),
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

    def _local_camera_live(self) -> bool:
        """Is the PC's own camera actually producing poses right now?"""
        kernel = self.kernel
        source = getattr(kernel, "active_body_source", None)
        if not source or str(source).startswith(POSE_SOURCE_PREFIX):
            return False
        last = float(getattr(kernel, "body_last_at", 0.0) or 0.0)
        return last > 0.0 and (time.monotonic() - last) < 1.0

    def _accept_pose_sequence_locked(self, peer, source_id: str, message: dict) -> bool:
        session = self._pose_sessions.get(peer)
        if session is None or session["source_id"] != source_id:
            session = {"source_id": source_id, "sequence": -1, "clock_trusted": False}
            self._pose_sessions[peer] = session
        return self._accept_frame_sequence_locked(peer, session, message, self._pose_rejected)

    def _accept_frame_sequence_locked(self, peer, session: dict, message: dict, rejected: dict) -> bool:
        sequence = int(message["sequence"])
        if sequence <= session["sequence"]:
            rejected["sequence"] += 1
            return False
        # Record seen sequences even if age validation below rejects them.
        session["sequence"] = sequence
        captured = float(message["captured_at_ms"])
        sent = message.get("sent_at_ms")
        if _is_number(sent):
            sent = float(sent)
            # This relative age is meaningful without synchronized clocks.
            if sent - captured > POSE_MAX_AGE_MS:
                rejected["stale"] += 1
                return False
        wall_ms = time.time() * 1000.0
        reference = sent if _is_number(sent) else captured
        if peer in self._clock_synced_peers and abs(wall_ms - reference) <= 1000.0:
            session["clock_trusted"] = True
        # Legacy clocks may be monotonic, unsynchronized, or synthetic replay
        # timestamps. Do not compare those with our wall clock until validated.
        if session["clock_trusted"]:
            if reference - wall_ms > 1000.0:
                session["clock_trusted"] = False
            elif wall_ms - captured > POSE_MAX_AGE_MS:
                rejected["stale"] += 1
                return False
        return True

    def _handle_pose(self, peer: WebSocketPeer, message: dict) -> None:
        _validate_pose_frame(message)
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
            if getattr(peer, "_closed", False):
                return
            if not self._body_enabled:
                self._pose_rejected["disabled"] += 1
                return
            # A selected but unavailable computer camera may still fall back
            # to a phone. Explicit stop above always rejects that fallback.
            if self.kernel is not None and self._body_mode != "phone" and self._local_camera_live():
                self._phone_ignored_at = received_at
                return
            if peer in self._superseded_pose_peers:
                self._pose_rejected["owner"] += 1
                return
            if self._active_pose_source and self._active_pose_source != source_id:
                # The first healthy camera owns a 300 ms lease. New devices
                # cannot alternate control frame by frame while it is active.
                if self._latest_pose and received_at - self._latest_pose["received_at"] <= 0.30:
                    self._pose_rejected["owner"] += 1
                    return
            if not self._accept_pose_sequence_locked(peer, source_id, message):
                return
            if self._active_pose_source and self._active_pose_source != source_id:
                old_source = self._active_pose_source
                old_owner, _ = self._clear_source_locked(old_source)
                if old_owner is not None:
                    old_owner.source_ids.discard(old_source)
                switched_from = old_source
            old_owner = self._source_peers.get(source_id)
            if old_owner is not None and old_owner is not peer:
                # A new websocket for the same device is a reconnect. Retire
                # the previous socket so it cannot reclaim this device later.
                old_owner.source_ids.discard(source_id)
                self._superseded_pose_peers.add(old_owner)
                self._reset_pose_metrics_locked()
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
                "delegate": str(message.get("delegate", "")),
                "inference_side": int(message.get("inference_side") or 0),
                "preview_mirrored": bool(message["preview_mirrored"]),
                "coordinates_mirrored": bool(message["coordinates_mirrored"]),
            }
            self._pose_sources[source_id] = state
            if pose_count:
                self._pose_frames_with_people += 1
            self._latest_pose = {"message": forwarded, "received_at": received_at, "source_id": source_id, "pose_count": pose_count}
            if self.kernel is not None:
                self.kernel.handle_pose_message(source_id, forwarded, return_status=False)
        if switched_from:
            self._broadcast_pose_state(switched_from, False, "source_switch")
        if activated:
            self._broadcast_pose_state(source_id, True, "source_active")
        self._broadcast_pose(forwarded)
        self._accept_input(peer)

    def _handle_pose_features(self, peer: WebSocketPeer, message: dict) -> None:
        # Keep exactly one downstream control path: compact phone payloads are
        # expanded locally and then pass through the existing pose handler.
        self._handle_pose(peer, _expand_pose_features(message))

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
        with self._lock:
            if getattr(peer, "_closed", False):
                return
            session_key = (peer, source_id)
            if session_key in self._superseded_sensor_sessions:
                self._sensor_rejected["owner"] += 1
                return
            session = self._sensor_sessions.setdefault(session_key, {"sequence": -1, "clock_trusted": False})
            if not self._accept_frame_sequence_locked(peer, session, message, self._sensor_rejected):
                return
            old_owner = self._source_peers.get(source_id)
            if old_owner is not None and old_owner is not peer:
                old_owner.source_ids.discard(source_id)
                self._superseded_sensor_sessions.add((old_owner, source_id))
            if self.kernel is not None:
                self.kernel.handle_sensor(
                    output_source, buttons,
                    left_trigger=left_trigger, right_trigger=right_trigger,
                    stick_x=stick_x, stick_y=stick_y, quaternion=quaternion,
                    rotation_rate=rotation_rate, acceleration=acceleration,
                    recenter=recenter, return_status=False,
                )
            else:
                self.output.set_sensor_state(
                    output_source, buttons,
                    left_trigger=left_trigger, right_trigger=right_trigger,
                    stick_x=stick_x, stick_y=stick_y,
                )
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

    def _handle_mouse(self, peer: WebSocketPeer, message: dict) -> None:
        buttons, dx, dy = _validate_mouse_frame(message)
        device_id = message["device_id"].strip()
        source_id = MOUSE_SOURCE_PREFIX + device_id
        with self._lock:
            if getattr(peer, "_closed", False):
                return
            session_key = (peer, source_id)
            if session_key in self._superseded_mouse_sessions:
                self._mouse_rejected["owner"] += 1
                return
            session = self._mouse_sessions.setdefault(session_key, {"sequence": -1, "clock_trusted": False})
            if not self._accept_frame_sequence_locked(peer, session, message, self._mouse_rejected):
                return
            old_owner = self._source_peers.get(source_id)
            if old_owner is not None and old_owner is not peer:
                old_owner.source_ids.discard(source_id)
            # 断流保护可能已经移除了按键来源，但旧连接仍不能抢回新连接。
            for owner, session_source in self._mouse_sessions:
                if owner is not peer and session_source == source_id:
                    self._superseded_mouse_sessions.add((owner, source_id))
            self._source_peers[source_id] = peer
            peer.source_ids.add(source_id)
            self._mouse_sources[source_id] = {
                "device_id": device_id,
                "received_at": time.monotonic(),
                "buttons": message["buttons"],
            }
            self.output.set_mouse_state(source_id, buttons, dx=dx, dy=dy)
        self._accept_input(peer)

    def _claim_voice_source_locked(self, peer, source_id: str) -> bool:
        if getattr(peer, "_closed", False):
            return False
        if (peer, source_id) in self._superseded_voice_sessions:
            return False
        if self._active_voice_source and self._active_voice_source != source_id:
            old_source = self._active_voice_source
            old_owner, _ = self._clear_source_locked(old_source)
            if old_owner is not None:
                old_owner.source_ids.discard(old_source)
        old_owner = self._source_peers.get(source_id)
        if old_owner is not None and old_owner is not peer:
            self._clear_source_locked(source_id)
            old_owner.source_ids.discard(source_id)
            self._superseded_voice_sessions.add((old_owner, source_id))
        if old_owner is not peer:
            self._voice_owner_generations[source_id] = self._voice_owner_generations.get(source_id, 0) + 1
        self._active_voice_source = source_id
        self._source_peers[source_id] = peer
        peer.source_ids.add(source_id)
        return True

    def _handle_voice_audio(self, peer: WebSocketPeer, message: dict) -> None:
        pcm16 = _validate_voice_audio(message)
        device_id = message["device_id"].strip()
        source_id = VOICE_SOURCE_PREFIX + device_id
        received_at = time.monotonic()
        with self._lock:
            # The phone may keep its microphone stream alive while the user
            # changes the desktop selector. Do not let those stale frames
            # silently take over a computer microphone selection.
            if self._audio_mode != "phone":
                return
            if (peer, source_id) in self._superseded_voice_sessions:
                return
            sequence_key = (peer, source_id)
            sequence = int(message["sequence"])
            if sequence <= self._voice_audio_sequences.get(sequence_key, -1):
                return
            self._voice_audio_sequences[sequence_key] = sequence
            if not self._claim_voice_source_locked(peer, source_id):
                return
            state = self._voice_sources.setdefault(source_id, {"device_id": device_id})
            state.update({
                "device_id": device_id,
                "received_at": received_at,
                "sequence": int(message["sequence"]),
            })
            captured_at_ms = None
            captured = float(message["captured_at_ms"])
            wall_ms = time.time() * 1000.0
            if peer in self._clock_synced_peers and abs(wall_ms - captured) <= 1000.0:
                self._voice_clock_trusted_peers.add(peer)
            if peer in self._voice_clock_trusted_peers:
                if captured - wall_ms > 1000.0:
                    self._voice_clock_trusted_peers.discard(peer)
                else:
                    captured_at_ms = captured
            owner_generation = self._voice_owner_generations[source_id]
            voice, submission_kwargs = self._voice_submission_locked("accept_phone_audio", source_id)
        self._flush_voice_clears()
        if voice is None:
            self._send_error(peer, "本地语音解析器未配置")
            return

        def deliver(event, result) -> None:
            if not (event or result):
                return
            with self._lock:
                if (self._source_peers.get(source_id) is not peer
                        or self._active_voice_source != source_id or self._audio_mode != "phone"
                        or self._voice_owner_generations.get(source_id) != owner_generation):
                    return
            event = event if isinstance(event, dict) else {}
            payload = {
                "type": "voice_result", "device_id": device_id,
                "sequence": int(message["sequence"]),
                "kind": str(event.get("kind", "match" if result else "status")),
                "partial": str(event.get("text", "")) if event.get("kind") == "partial" else "",
                "final": str(event.get("text", "")) if event.get("kind") == "final" else "",
            }
            if result is not None:
                payload["matched"] = bool(result.get("matched", False))
                payload["result"] = dict(result)
            try:
                peer.send_json(payload)
            except (ConnectionError, OSError):
                self.disconnect(peer)

        try:
            if self._voice_audio_async:
                _, event, result = voice.accept_phone_audio(
                    source_id, device_id, pcm16, sequence=int(message["sequence"]),
                    captured_at_ms=captured_at_ms, result_callback=deliver, **submission_kwargs,
                )
            else:
                _, event, result = voice.accept_phone_audio(source_id, device_id, pcm16, **submission_kwargs)
            # Only recognition events and command matches go back to the phone;
            # ordinary PCM frames therefore do not create a response flood.
            deliver(event, result)
            self._accept_input(peer)
        except (ValueError, RuntimeError) as exc:
            self._send_error(peer, str(exc))

    def _phone_voice_refused(self, peer: WebSocketPeer) -> bool:
        """手机认好的语音（voice_text / voice_command）只在音频来源选了手机麦克风时才收。

        音频来源和摄像头来源是分开选的，「电脑摄像头 + 手机麦克风」也是正常的搭法。
        这里以前看的是身体来源，还要求说话的手机就是当摄像头的那台——这么搭的时候
        手机上说什么都被丢掉。现在和手机直接送声音（voice_audio）看同一个开关。
        """
        if self.kernel is not None and self._audio_mode != "phone":
            self._send_error(peer, "音频来源选的是电脑麦克风，手机上说的话不生效")
            return True
        return False

    def _handle_voice_text(self, peer: WebSocketPeer, message: dict) -> None:
        _validate_voice_text(message)
        if self._phone_voice_refused(peer):
            return
        device_id = message["device_id"].strip()
        source_id = VOICE_SOURCE_PREFIX + device_id
        with self._lock:
            if self.kernel is not None and self._audio_mode != "phone":
                return
            if not self._claim_voice_source_locked(peer, source_id):
                return
            self._voice_sources[source_id] = {"device_id": device_id, "received_at": time.monotonic()}
            voice, submission_kwargs = self._voice_submission_locked("accept_phone_text", source_id)
        self._flush_voice_clears()
        if voice is None:
            self._send_error(peer, "本地语音解析器未配置")
            return
        try:
            _, result = voice.accept_phone_text(
                source_id, device_id, message["text"],
                float(message["confidence"]) if message.get("confidence") is not None else None,
                **submission_kwargs,
            )
            if result and not result.get("matched", False):
                self._send_error(peer, str(result.get("reason", "语音命令未匹配")))
            self._accept_input(peer)
        except (ValueError, RuntimeError) as exc:
            self._send_error(peer, str(exc))

    def _handle_voice_command(self, peer: WebSocketPeer, message: dict) -> None:
        """v0.9.6: phone normally sends only stable command_id; phrase is optional compatibility metadata."""
        if self._phone_voice_refused(peer):
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
            if self.kernel is not None and self._audio_mode != "phone":
                return
            if not self._claim_voice_source_locked(peer, source_id):
                return
            self._voice_sources[source_id] = {"device_id": device_id, "received_at": time.monotonic()}
            voice, submission_kwargs = self._voice_submission_locked("accept_phone_command", source_id)
        self._flush_voice_clears()
        if voice is None:
            self._send_error(peer, "本地语音解析器未配置")
            return
        try:
            _, result = voice.accept_phone_command(source_id, device_id, command_id, phrase, **submission_kwargs)
            if result and not result.get("matched", False):
                self._send_error(peer, str(result.get("reason", "语音命令未匹配")))
            self._accept_input(peer)
        except (ValueError, RuntimeError) as exc:
            self._send_error(peer, str(exc))

    def handle_message(self, peer: WebSocketPeer, message: dict) -> None:
        try:
            if not isinstance(message, dict):
                raise ValueError("message must be a JSON object")
            message_type = message.get("type")
            if message_type == "clock_sync":
                if not _is_number(message.get("client_sent_ms")):
                    raise ValueError("client_sent_ms must be a number")
                with self._lock:
                    self._clock_synced_peers.add(peer)
                    self._voice_clock_trusted_peers.discard(peer)
                    session = self._pose_sessions.get(peer)
                    if session is not None:
                        session["clock_trusted"] = False
                    for sessions in (self._sensor_sessions, self._mouse_sessions):
                        for (owner, _source), session in sessions.items():
                            if owner is peer:
                                session["clock_trusted"] = False
                peer.send_json({"type": "clock_sync", "client_sent_ms": message["client_sent_ms"], "server_ms": round(time.time() * 1000)})
            elif message_type == "pose_frame_v2":
                self._handle_pose(peer, message)
            elif message_type == "pose_features_v1":
                self._handle_pose_features(peer, message)
            elif message_type == "sensor_frame":
                self._handle_sensor(peer, message)
            elif message_type == "mouse_frame":
                self._handle_mouse(peer, message)
            elif message_type == "voice_audio":
                self._handle_voice_audio(peer, message)
            elif message_type == "voice_text":
                self._handle_voice_text(peer, message)
            elif message_type == "voice_command":
                self._handle_voice_command(peer, message)
            elif message_type == "game_output_control":
                self._handle_game_output_control(peer, message)
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
        mouse_source = source_id in self._mouse_sources
        self._mouse_sources.pop(source_id, None)
        voice_source = source_id in self._voice_sources
        self._voice_sources.pop(source_id, None)
        if voice_source:
            self._voice_owner_generations[source_id] = self._voice_owner_generations.get(source_id, 0) + 1
        try:
            if voice_source and self._voice_service is not None:
                voice = self._voice_service
                invalidate = getattr(voice, "invalidate_phone_source", None)
                token = invalidate(source_id) if callable(invalidate) else None
                self._pending_voice_clears.append((voice, source_id, token))
                # Release latches before waiting for a busy recognizer. A late
                # result is invalidated by the generation token above.
                self.output.clear_source(f"voice:{source_id}")
            elif mouse_source:
                self.output.clear_source(source_id)
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

    def _flush_voice_clears(self) -> None:
        """Recognizer cleanup may block; it must not hold pose admission's lock."""
        with self._lock:
            pending, self._pending_voice_clears = self._pending_voice_clears, []
        for voice, source_id, token in pending:
            try:
                if token is None:
                    voice.disconnect(source_id)
                else:
                    voice.disconnect(source_id, expected_generation=token)
            except (AttributeError, RuntimeError, OSError):
                pass

    def disconnect(self, peer: WebSocketPeer) -> None:
        cleared_pose_sources: list[str] = []
        with self._lock:
            self._peers.discard(peer)
            self._pose_sessions.pop(peer, None)
            self._superseded_pose_peers.discard(peer)
            self._clock_synced_peers.discard(peer)
            self._voice_clock_trusted_peers.discard(peer)
            for sessions in (self._sensor_sessions, self._mouse_sessions, self._voice_audio_sequences):
                for key in [key for key in sessions if key[0] is peer]:
                    sessions.pop(key, None)
            self._superseded_sensor_sessions = {key for key in self._superseded_sensor_sessions if key[0] is not peer}
            self._superseded_mouse_sessions = {key for key in self._superseded_mouse_sessions if key[0] is not peer}
            self._superseded_voice_sessions = {key for key in self._superseded_voice_sessions if key[0] is not peer}
            for source_id in list(peer.source_ids):
                if self._source_peers.get(source_id) is peer:
                    _, was_active_pose = self._clear_source_locked(source_id)
                    if was_active_pose:
                        cleared_pose_sources.append(source_id)
            peer.source_ids.clear()
        self._flush_voice_clears()
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
                    for sources in (self._sensor_sources, self._mouse_sources)
                    for source_id, state in sources.items()
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
            self._flush_voice_clears()
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
        # Without this the phone's output button would sit on "waiting" until
        # someone happened to toggle output on the desktop.
        if not peer.desktop:
            self._send_game_output_state(peer)
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
            self._body_enabled = False
            peers = list(self._peers | set(self._source_peers.values()))
        for peer in peers:
            self.disconnect(peer)
        if self._watchdog is not threading.current_thread():
            self._watchdog.join(timeout=1.0)
