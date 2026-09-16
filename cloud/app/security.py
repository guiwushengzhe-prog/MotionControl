"""Passwords, tokens and the small amount of hashing around them.

Argon2id rather than bcrypt or passlib. bcrypt silently truncates at 72 bytes,
which a long passphrase runs into; passlib has not had a release since 2020 and
breaks against bcrypt 4.x. ``argon2-cffi`` is maintained, and ``PasswordHasher``
already carries ``check_needs_rehash``, which is what lets the cost parameters
be raised later without asking anyone to change their password.

Session tokens and invite codes are random, so they are stored as a plain
SHA-256. Argon2 is for making *guesses* expensive and there is nothing to guess
in 256 bits of ``secrets.token_urlsafe``; running it on every request would
just add tens of milliseconds to every authenticated call.

The one place a plain digest is wrong is the client IP. IPv4 is 2^32 values --
a few seconds of brute force -- so an unkeyed hash of an address is simply a
slower way of storing the address. That one gets an HMAC under the server key.
"""

from __future__ import annotations

import hmac
import secrets
from hashlib import sha256

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from .settings import get_settings

_settings = get_settings()

_hasher = PasswordHasher(
    time_cost=_settings.argon2_time_cost,
    memory_cost=_settings.argon2_memory_cost,
    parallelism=_settings.argon2_parallelism,
)

MIN_PASSWORD_CHARS = 10
MAX_PASSWORD_BYTES = 1024


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


def new_token() -> str:
    """A session or invite secret. 32 bytes of urandom, URL-safe."""
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    """What gets stored for a random secret."""
    return sha256(token.encode("utf-8")).hexdigest()


def keyed_digest(value: str) -> str:
    """For low-entropy values such as an IP address, where a bare hash reverses."""
    return hmac.new(_settings.secret_key.encode("utf-8"),
                    value.encode("utf-8"), sha256).hexdigest()


def normalize_email(email: str) -> str:
    """Lower-case and trim. Nothing cleverer.

    Provider-specific folding -- dropping gmail dots, cutting at "+" -- is
    tempting and wrong: it is not the mail system's own rule for every domain,
    so it would merge two addresses that really are different people.
    """
    return str(email or "").strip().lower()


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
