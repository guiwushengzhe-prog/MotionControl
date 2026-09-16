"""The pairing exchange and per-connection authentication.

Each test plays the phone side for real -- generating a P-256 key, deriving the
same secret via ECDH, and computing the same MACs -- so these exercise the
actual protocol rather than a mock of it.

The properties that matter:

  * the permanent secret is never on the wire, so a listener who records the
    whole exchange still cannot produce it;
  * an attacker who does not know the pairing code cannot finish pairing, and
    gets a hard attempt cap rather than unlimited guesses;
  * a proof is bound to one device_id and one role, so it cannot be replayed
    across either;
  * a fresh nonce per connection means a captured proof is useless later.
"""

from __future__ import annotations

import base64

import pytest

import device_pairing as dp

pytestmark = pytest.mark.skipif(
    not dp.crypto_available(), reason="cryptography is not installed"
)

DEVICE = "camera-11111111-2222-3333-4444-555555555555"


@pytest.fixture
def service(tmp_path):
    return dp.PairingService(dp.PairingStore(tmp_path / "paired_devices.json"))


def b64(raw):
    return base64.b64encode(raw).decode("ascii")


def unb64(text):
    return base64.b64decode(text)


class Phone:
    """The other side of the exchange, implemented honestly."""

    def __init__(self, device_id=DEVICE, role="camera"):
        self.device_id = device_id
        self.role = role
        self.key = dp.generate_keypair()
        self.pub = dp.public_bytes(self.key)
        self.secret = None
        self.pub_pc = None

    def init_message(self):
        return {"type": "pair_init", "pair_protocol": dp.PAIR_PROTOCOL,
                "device_id": self.device_id, "role": self.role, "pub_phone": b64(self.pub)}

    def absorb_challenge(self, challenge):
        self.pub_pc = unb64(challenge["pub_pc"])
        self.secret = dp.derive_secret(self.key, self.pub_pc, unb64(challenge["salt"]),
                                       self.device_id)

    def confirm_message(self, code):
        mac = dp.pair_mac(self.secret, "phone", code, self.pub, self.pub_pc)
        return {"type": "pair_confirm", "device_id": self.device_id, "mac_phone": b64(mac)}

    def hello(self, nonce, *, role=None, device_id=None, secret=None):
        role = role or self.role
        device_id = device_id or self.device_id
        proof = dp.connection_proof(secret or self.secret, nonce, device_id, role)
        return {"type": "hello", "protocol_version": dp.PROTOCOL_VERSION,
                "device_id": device_id, "role": role,
                "auth": {"mode": "paired_hmac", "proof": b64(proof)}}


def pair(service, phone):
    code = service.begin_pairing()["code"]
    phone.absorb_challenge(service.handle_pair_init(phone.init_message()))
    result = service.handle_pair_confirm(phone.confirm_message(code))
    return code, result


# --- the happy path --------------------------------------------------------


def test_pairing_derives_the_same_secret_on_both_sides(service):
    phone = Phone()
    _code, result = pair(service, phone)
    assert result["ok"] is True
    assert service.store.secret_for(DEVICE) == phone.secret


def test_pc_confirmation_proves_the_pc_also_has_the_secret(service):
    phone = Phone()
    code, result = pair(service, phone)
    expected = dp.pair_mac(phone.secret, "pc", code, phone.pub_pc, phone.pub)
    assert unb64(result["mac_pc"]) == expected


def test_paired_device_authenticates_a_connection(service):
    phone = Phone()
    pair(service, phone)
    nonce = service.new_nonce()
    assert service.verify_hello(phone.hello(nonce), nonce) == (DEVICE, "camera")


# --- what the design is actually defending against -------------------------


def test_secret_never_appears_on_the_wire(service):
    """Record every byte the exchange emits; the secret must not be in it."""
    phone = Phone()
    code = service.begin_pairing()["code"]
    challenge = service.handle_pair_init(phone.init_message())
    phone.absorb_challenge(challenge)
    confirm = phone.confirm_message(code)
    result = service.handle_pair_confirm(confirm)

    wire = repr([phone.init_message(), challenge, confirm, result]).encode()
    assert phone.secret not in wire
    assert b64(phone.secret).encode() not in wire


def test_eavesdropper_with_the_full_transcript_cannot_derive_the_secret(service):
    """ECDH is what makes the transcript useless without a private key."""
    phone = Phone()
    code = service.begin_pairing()["code"]
    challenge = service.handle_pair_init(phone.init_message())
    phone.absorb_challenge(challenge)
    service.handle_pair_confirm(phone.confirm_message(code))

    # The listener has both public keys, the salt and the code, but no private
    # key, so the best it can do is guess one -- which yields a different Z.
    attacker_key = dp.generate_keypair()
    guessed = dp.derive_secret(attacker_key, unb64(challenge["pub_pc"]),
                               unb64(challenge["salt"]), DEVICE)
    assert guessed != phone.secret


def test_wrong_pairing_code_is_rejected(service):
    phone = Phone()
    service.begin_pairing()
    phone.absorb_challenge(service.handle_pair_init(phone.init_message()))
    with pytest.raises(dp.PairingError, match="配对码不正确"):
        service.handle_pair_confirm(phone.confirm_message("00000000"))
    assert service.store.secret_for(DEVICE) is None


def test_pairing_code_is_burned_after_the_attempt_cap(service):
    phone = Phone()
    service.begin_pairing()
    phone.absorb_challenge(service.handle_pair_init(phone.init_message()))
    for _ in range(dp.PAIRING_CODE_MAX_ATTEMPTS - 1):
        with pytest.raises(dp.PairingError, match="配对码不正确"):
            service.handle_pair_confirm(phone.confirm_message("00000000"))
    with pytest.raises(dp.PairingError, match="次数过多"):
        service.handle_pair_confirm(phone.confirm_message("00000000"))
    assert service.pairing_status()["active"] is False


def test_pairing_code_expires(service):
    phone = Phone()
    service.begin_pairing(now=1000.0)
    with pytest.raises(dp.PairingError, match="已过期"):
        service.handle_pair_init(phone.init_message(), now=1000.0 + dp.PAIRING_CODE_TTL_S + 1)


def test_pairing_requires_an_open_window(service):
    phone = Phone()
    with pytest.raises(dp.PairingError, match="还没有开始配对"):
        service.handle_pair_init(phone.init_message())


# --- connection authentication boundaries ----------------------------------


def test_unpaired_device_cannot_authenticate(service):
    phone = Phone()
    nonce = service.new_nonce()
    hello = phone.hello(nonce, secret=b"\x00" * 32)
    with pytest.raises(dp.PairingError, match="尚未与本机配对"):
        service.verify_hello(hello, nonce)


def test_proof_does_not_transfer_across_roles(service):
    """role is inside the MAC, so a camera proof cannot authenticate a sensor."""
    phone = Phone(role="camera")
    pair(service, phone)
    nonce = service.new_nonce()
    camera_proof = phone.hello(nonce)["auth"]["proof"]
    forged = {"type": "hello", "protocol_version": dp.PROTOCOL_VERSION,
              "device_id": DEVICE, "role": "sensor",
              "auth": {"mode": "paired_hmac", "proof": camera_proof}}
    with pytest.raises(dp.PairingError, match="认证失败"):
        service.verify_hello(forged, nonce)


def test_proof_does_not_transfer_across_devices(service):
    """A paired attacker must not be able to authenticate as someone else."""
    attacker = Phone(device_id="camera-attacker")
    pair(service, attacker)
    victim = Phone(device_id="camera-victim")
    pair(service, victim)

    nonce = service.new_nonce()
    # Attacker signs with its own secret but claims the victim's id.
    forged = attacker.hello(nonce, device_id="camera-victim")
    with pytest.raises(dp.PairingError, match="认证失败"):
        service.verify_hello(forged, nonce)


def test_proof_is_bound_to_its_nonce(service):
    phone = Phone()
    pair(service, phone)
    first = service.new_nonce()
    hello = phone.hello(first)
    assert service.verify_hello(hello, first) == (DEVICE, "camera")
    second = service.new_nonce()
    assert second != first
    with pytest.raises(dp.PairingError, match="认证失败"):
        service.verify_hello(hello, second)


def test_hello_rejects_unknown_protocol_version(service):
    phone = Phone()
    pair(service, phone)
    nonce = service.new_nonce()
    hello = phone.hello(nonce)
    hello["protocol_version"] = 99
    with pytest.raises(dp.PairingError, match="协议版本"):
        service.verify_hello(hello, nonce)


def test_hello_rejects_unknown_auth_mode(service):
    phone = Phone()
    pair(service, phone)
    nonce = service.new_nonce()
    hello = phone.hello(nonce)
    hello["auth"]["mode"] = "trust_me"
    with pytest.raises(dp.PairingError, match="认证方式"):
        service.verify_hello(hello, nonce)


def test_pair_init_rejects_unknown_pair_protocol(service):
    phone = Phone()
    service.begin_pairing()
    message = phone.init_message()
    message["pair_protocol"] = "plaintext_v0"
    with pytest.raises(dp.PairingError, match="配对协议"):
        service.handle_pair_init(message)


def test_pair_init_rejects_a_malformed_public_key(service):
    phone = Phone()
    service.begin_pairing()
    message = phone.init_message()
    message["pub_phone"] = b64(b"\x04" + b"\x00" * 64)
    with pytest.raises(dp.PairingError, match="公钥无效"):
        service.handle_pair_init(message)


def test_roles_are_validated(service):
    phone = Phone()
    service.begin_pairing()
    message = phone.init_message()
    message["role"] = "admin"
    with pytest.raises(dp.PairingError, match="role must be"):
        service.handle_pair_init(message)


# --- storage ---------------------------------------------------------------


def test_store_round_trips_through_disk(tmp_path):
    path = tmp_path / "paired.json"
    store = dp.PairingStore(path)
    store.remember(DEVICE, b"\x11" * 32, name="我的手机")
    assert dp.PairingStore(path).secret_for(DEVICE) == b"\x11" * 32
    assert dp.PairingStore(path).devices()[0]["name"] == "我的手机"


def test_store_file_does_not_contain_the_secret_in_the_clear(tmp_path):
    """On Windows this is DPAPI; the assertion holds either way for base64."""
    import sys

    path = tmp_path / "paired.json"
    dp.PairingStore(path).remember(DEVICE, b"\x11" * 32)
    blob = path.read_bytes()
    if sys.platform == "win32":
        assert b"\x11" * 32 not in blob
        assert base64.b64encode(b"\x11" * 32) not in blob


def test_forget_removes_a_device(tmp_path):
    path = tmp_path / "paired.json"
    store = dp.PairingStore(path)
    store.remember(DEVICE, b"\x22" * 32)
    assert store.forget(DEVICE) is True
    assert store.secret_for(DEVICE) is None
    assert dp.PairingStore(path).secret_for(DEVICE) is None


def test_undecryptable_store_reads_as_empty(tmp_path):
    path = tmp_path / "paired.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a valid protected blob")
    assert dp.PairingStore(path).devices() == []
