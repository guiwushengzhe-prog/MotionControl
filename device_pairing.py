"""Device pairing and per-connection authentication for /ws/input.

Why this exists: ControlKernel.handle_sensor() feeds output.set_sensor_state()
directly, so a sensor_frame arriving on /ws/input is real gamepad input.  The
8765/8766 split stops a LAN device from *reading* the account and config APIs,
but it does nothing about a LAN device *writing* forged input.  This module is
the other half.

The key exchange is ECDH, not a server-generated secret handed back over the
wire.  The threat model is "do not trust other devices on this network", and
mailing the permanent 32-byte key across that same network during pairing would
contradict it outright: anyone listening at that moment owns every later HMAC.
People run this in dorms, on campus, on shared Wi-Fi, so "the network is
trustworthy during pairing" is not a premise worth building on.

    phone -> pair_init      {device_id, role, pub_phone}
    pc    -> pair_challenge {pub_pc, salt}
    both  :  Z = ECDH(P-256);  secret = HKDF-SHA256(Z, salt, info|device_id)
    phone -> pair_confirm   {mac_phone = HMAC(secret, phone|code|pubs...)}
    pc    -> pair_result    {mac_pc    = HMAC(secret, pc   |code|pubs...)}

A passive listener sees two public keys, a salt and two MACs, and cannot derive
Z.  An active man in the middle has to forge mac_phone without ever seeing the
pairing code, so it can only be guessed -- hence the short window and the hard
attempt cap.  The permanent secret never appears on the network at all.

P-256 rather than X25519 because the phone side is a WebView on minSdk 24:
WebCrypto's X25519 is recent, its P-256 ECDH is everywhere.  The wire format is
the uncompressed point, which is exactly what WebCrypto exportKey raw emits.

This is authentication, not confidentiality.  Pose, sensor, recognised speech
and scene JPEGs still cross the LAN in the clear, because ws:// is still ws://.
Fixing that means WSS or message encryption, and is a separate change; the
pair_protocol field is the version marker that lets it happen later without
redoing this exchange.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import secrets
import sys
import threading
import time
from hashlib import sha256
from pathlib import Path

PAIR_PROTOCOL = "ecdh_p256_v1"
PROTOCOL_VERSION = 2

PAIRING_CODE_DIGITS = 8
PAIRING_CODE_TTL_S = 120.0
PAIRING_CODE_MAX_ATTEMPTS = 5

VALID_ROLES = ("camera", "sensor")

_HKDF_INFO_PREFIX = b"motioncontrol-pairing-v1|"


class PairingError(ValueError):
    """A pairing or authentication step failed.  The message is user-facing."""


class PairingUnavailable(RuntimeError):
    """The crypto backend is missing, so pairing cannot run at all."""


def _backend():
    """Import the crypto backend lazily so a missing wheel degrades loudly.

    Hand-rolling ECDH was the alternative and is not worth it: this key
    protects every later authentication.
    """
    try:
        from cryptography.hazmat.primitives import constant_time, hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise PairingUnavailable(
            "设备配对需要 cryptography 库，当前 Python 环境没有安装。"
        ) from exc
    return ec, HKDF, hashes, serialization, constant_time


def crypto_available() -> bool:
    try:
        _backend()
        return True
    except PairingUnavailable:
        return False


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text, *, label: str, expect: int | None = None) -> bytes:
    if not isinstance(text, str):
        raise PairingError(f"{label} must be a base64 string")
    try:
        raw = base64.b64decode(text, validate=True)
    except Exception as exc:
        raise PairingError(f"{label} is not valid base64") from exc
    if expect is not None and len(raw) != expect:
        raise PairingError(f"{label} must be {expect} bytes, got {len(raw)}")
    return raw


# ---------------------------------------------------------------------------
# storage


def default_pairing_path() -> Path:
    from user_paths import user_path

    return user_path("paired_devices")


def _dpapi(func_name: str, blob: bytes, description: str | None) -> bytes:
    import ctypes
    import ctypes.wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(blob, len(blob))
    src = BLOB(len(blob), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    func = getattr(ctypes.windll.crypt32, func_name)
    if func_name == "CryptProtectData":
        ok = func(ctypes.byref(src), description, None, None, None, 0, ctypes.byref(out))
    else:
        ok = func(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out))
    if not ok:
        raise OSError(f"{func_name} failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def protect(raw: bytes) -> bytes:
    """DPAPI-encrypt for the current user, so a copied file is useless elsewhere.

    The PC has to keep the secret itself -- HMAC verification needs the key, not
    a hash of it -- so encrypting at rest is not optional.  Off Windows this is
    a passthrough purely so the logic stays testable; the app ships on Windows.
    """
    if sys.platform != "win32":
        return raw
    return _dpapi("CryptProtectData", raw, "MotionControl pairing")


def unprotect(blob: bytes) -> bytes:
    if sys.platform != "win32":
        return blob
    return _dpapi("CryptUnprotectData", blob, None)


class PairingStore:
    """Paired devices on disk, encrypted at rest."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_pairing_path()
        self._lock = threading.RLock()
        self._devices: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        try:
            blob = self.path.read_bytes()
        except FileNotFoundError:
            return {}
        try:
            data = json.loads(unprotect(blob).decode("utf-8"))
        except Exception:
            # A file we cannot decrypt belongs to another user or another
            # machine.  Treat that as "nothing paired" rather than refusing to
            # start: the user simply pairs again.
            return {}
        devices = data.get("devices")
        return devices if isinstance(devices, dict) else {}

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": 1, "devices": self._devices},
                             ensure_ascii=False).encode("utf-8")
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_bytes(protect(payload))
        os.replace(temporary, self.path)

    def secret_for(self, device_id: str) -> bytes | None:
        with self._lock:
            entry = self._devices.get(str(device_id))
            if not entry:
                return None
            try:
                return base64.b64decode(entry["secret"])
            except Exception:
                return None

    def remember(self, device_id: str, secret: bytes, name: str = "") -> None:
        with self._lock:
            self._devices[str(device_id)] = {
                "secret": _b64(secret),
                "name": str(name or "")[:64],
                "paired_at": time.time(),
            }
            self._save_locked()

    def forget(self, device_id: str) -> bool:
        with self._lock:
            removed = self._devices.pop(str(device_id), None) is not None
            if removed:
                self._save_locked()
            return removed

    def devices(self) -> list[dict]:
        with self._lock:
            return [
                {"device_id": ident, "name": entry.get("name", ""),
                 "paired_at": entry.get("paired_at")}
                for ident, entry in sorted(self._devices.items())
            ]

    def is_empty(self) -> bool:
        with self._lock:
            return not self._devices


# ---------------------------------------------------------------------------
# crypto primitives


def derive_secret(private_key, peer_public_raw: bytes, salt: bytes, device_id: str) -> bytes:
    ec, HKDF, hashes, _serialization, _ct = _backend()
    try:
        peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), peer_public_raw)
    except Exception as exc:
        raise PairingError("对方公钥无效") from exc
    shared = private_key.exchange(ec.ECDH(), peer)
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=_HKDF_INFO_PREFIX + device_id.encode("utf-8"),
    ).derive(shared)


def public_bytes(private_key) -> bytes:
    """Uncompressed point, matching WebCrypto exportKey raw for P-256."""
    _ec, _HKDF, _hashes, serialization, _ct = _backend()
    return private_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )


def generate_keypair():
    ec, _HKDF, _hashes, _serialization, _ct = _backend()
    return ec.generate_private_key(ec.SECP256R1())


def _mac(secret: bytes, *parts: str) -> bytes:
    message = "|".join(parts).encode("utf-8")
    return hmac.new(secret, message, sha256).digest()


def pair_mac(secret: bytes, side: str, code: str, pub_a: bytes, pub_b: bytes) -> bytes:
    """Bind the confirmation to the code and to both public keys.

    Including both keys is what stops a man in the middle from relaying one
    side's confirmation to the other; including the code is what forces them to
    guess it.
    """
    return _mac(secret, side, code, _b64(pub_a), _b64(pub_b))


def connection_proof(secret: bytes, nonce: str, device_id: str, role: str,
                     protocol_version: int = PROTOCOL_VERSION) -> bytes:
    """Per-connection proof.  role is inside, so a proof cannot cross roles."""
    return _mac(secret, nonce, device_id, str(protocol_version), role)


def constant_time_equal(left: bytes, right: bytes) -> bool:
    try:
        _ec, _HKDF, _hashes, _serialization, constant_time = _backend()
    except PairingUnavailable:
        return hmac.compare_digest(left, right)
    return constant_time.bytes_eq(left, right)


# ---------------------------------------------------------------------------
# pairing session


class PairingSession:
    """One live pairing code and the half-finished exchanges under it."""

    def __init__(self, code: str, *, ttl: float = PAIRING_CODE_TTL_S,
                 max_attempts: int = PAIRING_CODE_MAX_ATTEMPTS,
                 now: float | None = None) -> None:
        self.code = code
        self.expires_at = (now if now is not None else time.monotonic()) + ttl
        self.attempts_left = max_attempts
        self.pending: dict[str, dict] = {}

    def expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.monotonic()) >= self.expires_at

    def remaining_s(self, now: float | None = None) -> float:
        return max(0.0, self.expires_at - (now if now is not None else time.monotonic()))


def generate_pairing_code(digits: int = PAIRING_CODE_DIGITS) -> str:
    return str(secrets.randbelow(10 ** digits)).zfill(digits)


class PairingService:
    """Server side of both the pairing exchange and connection authentication."""

    def __init__(self, store: PairingStore | None = None, *,
                 require_paired_devices: bool = False) -> None:
        self.store = store if store is not None else PairingStore()
        self.require_paired_devices = bool(require_paired_devices)
        self._lock = threading.RLock()
        self._session: PairingSession | None = None

    # -- pairing window ----------------------------------------------------

    def begin_pairing(self, *, now: float | None = None) -> dict:
        """Open a pairing window and return the code for the UI to display."""
        if not crypto_available():
            raise PairingUnavailable("设备配对需要 cryptography 库，当前环境没有安装。")
        with self._lock:
            session = PairingSession(generate_pairing_code(), now=now)
            self._session = session
            return {"code": session.code, "expires_in_s": round(session.remaining_s(now), 1),
                    "pair_protocol": PAIR_PROTOCOL}

    def cancel_pairing(self) -> None:
        with self._lock:
            self._session = None

    def pairing_status(self, *, now: float | None = None) -> dict:
        with self._lock:
            session = self._session
            if session is None or session.expired(now):
                self._session = None if session is not None and session.expired(now) else self._session
                return {"active": False, "pair_protocol": PAIR_PROTOCOL,
                        "require_paired_devices": self.require_paired_devices,
                        "paired_count": len(self.store.devices())}
            return {"active": True, "code": session.code,
                    "expires_in_s": round(session.remaining_s(now), 1),
                    "attempts_left": session.attempts_left,
                    "pair_protocol": PAIR_PROTOCOL,
                    "require_paired_devices": self.require_paired_devices,
                    "paired_count": len(self.store.devices())}

    def _active_session(self, now: float | None = None) -> PairingSession:
        session = self._session
        if session is None:
            raise PairingError("电脑端还没有开始配对，请先在设置里点击“配对新设备”")
        if session.expired(now):
            self._session = None
            raise PairingError("配对码已过期，请在电脑上重新生成")
        if session.attempts_left <= 0:
            self._session = None
            raise PairingError("配对码尝试次数过多，已作废，请在电脑上重新生成")
        return session

    # -- exchange ----------------------------------------------------------

    def handle_pair_init(self, message: dict, *, now: float | None = None) -> dict:
        device_id = _require_device_id(message)
        role = _require_role(message)
        if message.get("pair_protocol") != PAIR_PROTOCOL:
            raise PairingError(f"不支持的配对协议：{message.get('pair_protocol')!r}")
        pub_phone = _unb64(message.get("pub_phone"), label="pub_phone", expect=65)
        with self._lock:
            session = self._active_session(now)
            private_key = generate_keypair()
            pub_pc = public_bytes(private_key)
            salt = secrets.token_bytes(32)
            secret = derive_secret(private_key, pub_phone, salt, device_id)
            session.pending[device_id] = {
                "secret": secret, "pub_phone": pub_phone, "pub_pc": pub_pc, "role": role,
            }
            return {"type": "pair_challenge", "pair_protocol": PAIR_PROTOCOL,
                    "pub_pc": _b64(pub_pc), "salt": _b64(salt)}

    def handle_pair_confirm(self, message: dict, *, now: float | None = None) -> dict:
        device_id = _require_device_id(message)
        mac_phone = _unb64(message.get("mac_phone"), label="mac_phone", expect=32)
        name = str(message.get("device_name") or "")[:64]
        with self._lock:
            session = self._active_session(now)
            pending = session.pending.get(device_id)
            if pending is None:
                raise PairingError("请先发送 pair_init")
            expected = pair_mac(pending["secret"], "phone", session.code,
                                pending["pub_phone"], pending["pub_pc"])
            if not constant_time_equal(mac_phone, expected):
                # A wrong MAC means a wrong code, which is the only thing an
                # active attacker can be getting wrong.  Burn an attempt.
                session.attempts_left -= 1
                if session.attempts_left <= 0:
                    self._session = None
                    raise PairingError("配对码错误次数过多，已作废，请在电脑上重新生成")
                raise PairingError(f"配对码不正确，还可以尝试 {session.attempts_left} 次")
            self.store.remember(device_id, pending["secret"], name)
            mac_pc = pair_mac(pending["secret"], "pc", session.code,
                              pending["pub_pc"], pending["pub_phone"])
            session.pending.pop(device_id, None)
            self._session = None
            return {"type": "pair_result", "ok": True, "mac_pc": _b64(mac_pc),
                    "device_id": device_id}

    # -- connection authentication ----------------------------------------

    def new_nonce(self) -> str:
        return secrets.token_hex(32)

    def verify_hello(self, message: dict, nonce: str) -> tuple[str, str]:
        """Check a hello and return the identity this connection may now claim."""
        device_id = _require_device_id(message)
        role = _require_role(message)
        version = message.get("protocol_version")
        if version != PROTOCOL_VERSION:
            raise PairingError(f"不支持的协议版本：{version!r}")
        auth = message.get("auth")
        if not isinstance(auth, dict):
            raise PairingError("hello 缺少 auth")
        if auth.get("mode") != "paired_hmac":
            raise PairingError(f"不支持的认证方式：{auth.get('mode')!r}")
        proof = _unb64(auth.get("proof"), label="proof", expect=32)
        secret = self.store.secret_for(device_id)
        if secret is None:
            raise PairingError("该设备尚未与本机配对，请在电脑上点击“配对新设备”")
        expected = connection_proof(secret, nonce, device_id, role, version)
        if not constant_time_equal(proof, expected):
            raise PairingError("设备认证失败")
        return device_id, role


def _require_device_id(message: dict) -> str:
    device_id = message.get("device_id")
    if not isinstance(device_id, str) or not device_id.strip():
        raise PairingError("device_id must be a non-empty string")
    return device_id.strip()


def _require_role(message: dict) -> str:
    role = message.get("role")
    if role not in VALID_ROLES:
        raise PairingError(f"role must be one of {VALID_ROLES}")
    return role
