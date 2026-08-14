"""Password hashing, on scrypt from the standard library.

No dependency, for the one thing in this project that must not be left to fall
behind: ``hashlib.scrypt`` is OpenSSL's implementation, and it is already installed
on every machine that can run the service. The alternative is a package to keep
current on a Pi that is provisioned once and then left alone for months.

A stored hash records the parameters it was made with, so the work factor can be
raised later without locking anybody out of an account.
"""

from __future__ import annotations

import base64
import binascii
import functools
import hashlib
import secrets
import threading
import unicodedata
from typing import Final, NamedTuple

from pihome_hub.accounts.errors import InvalidPasswordHashError, WeakPasswordError

#: Names the format as well as the algorithm. A second one would be a second value
#: here rather than a guess about what the fields mean.
_ALGORITHM: Final = "scrypt"

#: Algorithm, parameters, salt, key.
_FIELDS: Final = 4

#: Work factors. The binding constraint is memory rather than time: scrypt holds
#: ``128 * r * n`` bytes — 16 MiB at these settings — for the whole of one hash, and
#: the target is a Pi Zero 2 W with 512 MB for the entire system. 2**14 costs about
#: 30 ms on a development machine and a few hundred on that board, which is on the
#: right side of noticeable for a login.
_N: Final = 2**14
_R: Final = 8
_P: Final = 1

_SALT_BYTES: Final = 16
_KEY_BYTES: Final = 32

#: Passed to OpenSSL explicitly. Left unset it applies its own 32 MiB default and
#: refuses anything above 2**14 with "memory limit exceeded" — a message that names
#: neither the parameter that caused it nor the limit it hit. Stated here so that
#: raising the work factor fails against a number in this file, and so that a
#: hand-edited hash demanding gigabytes is refused rather than attempted.
_MAX_MEMORY: Final = 64 * 1024 * 1024

#: How many hashes may run at once. scrypt releases the GIL, so on the four cores of
#: a Pi Zero 2 W several run genuinely in parallel — each holding its 16 MiB while it
#: does. Unbounded, a burst of logins is uvicorn's forty threadpool workers holding
#: 640 MiB on a 512 MB board, which the kernel settles by killing the service. The
#: bound is here rather than at the call site because the memory cost belongs to this
#: module: a caller that has to remember to limit itself is a caller that will not.
_MAX_CONCURRENT: Final = 4
_slots: Final = threading.BoundedSemaphore(_MAX_CONCURRENT)

#: Length is the only property of a password worth requiring. Composition rules —
#: a digit, a symbol, a capital — are what produce ``Password1!`` on every account.
MIN_PASSWORD_LENGTH: Final = 12

#: Refused rather than hashed. scrypt's own cost does not depend on how long the
#: input is, but the pass over it does, and nothing anyone types needs this much room.
MAX_PASSWORD_LENGTH: Final = 1024


class _Parameters(NamedTuple):
    n: int
    r: int
    p: int


_CURRENT: Final = _Parameters(_N, _R, _P)


def hash_password(password: str) -> str:
    """Hash ``password`` for storage, returning a self-describing string.

    The strength check lives here rather than at the call site deliberately: this is
    the one point every password in the system passes through, and a rule enforced
    anywhere else is a rule the next caller can skip without noticing.
    """
    _check_strength(password)
    salt = secrets.token_bytes(_SALT_BYTES)
    return _encode(_CURRENT, salt, _derive(password, salt, _CURRENT, _KEY_BYTES))


def verify_password(password: str, encoded: str) -> bool:
    """Is ``password`` the one ``encoded`` was made from?

    Hashed with the parameters recorded in ``encoded``, not the current ones, so a
    raised work factor does not invalidate every account at once.

    Deliberately no minimum-length check: an account whose password predates a raised
    minimum must still be able to log in and change it.
    """
    parameters, salt, expected = _decode(encoded)

    # Not `_check_strength`: this is the only half of it that protects the service
    # rather than the account, and an over-long guess is answered without paying for it.
    if len(password) > MAX_PASSWORD_LENGTH:
        return False

    derived = _derive(password, salt, parameters, len(expected))
    return secrets.compare_digest(derived, expected)


def needs_rehash(encoded: str) -> bool:
    """Was ``encoded`` made with anything weaker than this build now uses?

    Lets an account be upgraded at its next login — the one moment the plaintext is
    briefly in hand and a stronger hash can be computed without asking anyone.
    """
    parameters, salt, key = _decode(encoded)
    return (parameters, len(salt), len(key)) != (_CURRENT, _SALT_BYTES, _KEY_BYTES)


def dummy_verify(password: str) -> bool:
    """Spend what a verification would have cost, and answer no.

    For the case where there is no account to check against, so that a caller can
    write ``return dummy_verify(password)`` and take the same path. Without it "no
    such user" answers in microseconds while "wrong password" takes hundreds of
    milliseconds, and the difference tells anyone who can time a request which
    usernames exist.
    """
    return verify_password(password, _dummy_hash())


@functools.cache
def _dummy_hash() -> str:
    """A hash of nothing anyone knows, at the parameters in force right now.

    Computed on first use rather than at import: it costs a real hash, and neither
    the admin CLI nor a service that is only serving relays should pay for it.
    """
    return hash_password(secrets.token_urlsafe(32))


def _check_strength(password: str) -> None:
    length = len(password)
    if length < MIN_PASSWORD_LENGTH:
        msg = f"password must be at least {MIN_PASSWORD_LENGTH} characters, got {length}"
        raise WeakPasswordError(msg)
    if length > MAX_PASSWORD_LENGTH:
        msg = f"password must be at most {MAX_PASSWORD_LENGTH} characters, got {length}"
        raise WeakPasswordError(msg)


def _derive(password: str, salt: bytes, parameters: _Parameters, dklen: int) -> bytes:
    # Normalised first, to NFC as RFC 8265 specifies for passwords. Two keyboards can
    # produce the same visible password as different bytes — a letter with its accent
    # composed, or the letter followed by a combining mark — and unnormalised those
    # are two different passwords. Doing it now costs nothing; doing it after the
    # first account exists would lock out whoever typed the other form.
    material = unicodedata.normalize("NFC", password).encode("utf-8")

    with _slots:
        return hashlib.scrypt(
            material,
            salt=salt,
            n=parameters.n,
            r=parameters.r,
            p=parameters.p,
            dklen=dklen,
            maxmem=_MAX_MEMORY,
        )


def _encode(parameters: _Parameters, salt: bytes, key: bytes) -> str:
    """``scrypt$n=16384,r=8,p=1$<salt>$<key>``, both halves base64.

    ``$`` is not in the base64 alphabet, so the fields cannot run into each other.
    """
    return "$".join(
        (
            _ALGORITHM,
            f"n={parameters.n},r={parameters.r},p={parameters.p}",
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(key).decode("ascii"),
        )
    )


def _decode(encoded: str) -> tuple[_Parameters, bytes, bytes]:
    """Read back what :func:`_encode` wrote, or refuse the string.

    Strict on the way in because the input is a database row: everything that is not
    exactly this format is a fault worth reporting, not a value worth guessing at.
    """
    fields = encoded.split("$")
    if len(fields) != _FIELDS:
        msg = f"password hash has {len(fields)} fields, expected {_FIELDS}"
        raise InvalidPasswordHashError(msg)

    algorithm, encoded_parameters, encoded_salt, encoded_key = fields
    if algorithm != _ALGORITHM:
        msg = f"password hash names algorithm {algorithm!r}, which this build cannot read"
        raise InvalidPasswordHashError(msg)

    parameters = _decode_parameters(encoded_parameters)
    # Half the current lengths rather than the current lengths: a build that
    # lengthened either still has to read what this one wrote, and needs_rehash() is
    # what closes the gap. Below half is damage rather than an old format — a
    # truncated key is quick to match by brute force, and nothing ever wrote one.
    salt = _decode_base64(encoded_salt, "salt", _SALT_BYTES // 2)
    key = _decode_base64(encoded_key, "key", _KEY_BYTES // 2)
    return parameters, salt, key


def _decode_parameters(field: str) -> _Parameters:
    values: dict[str, int] = {}
    for item in field.split(","):
        name, separator, value = item.partition("=")
        # isascii() as well as isdigit(): the latter alone accepts digits from other
        # scripts, which int() then happily parses.
        if not separator or not (value.isascii() and value.isdigit()):
            msg = f"password hash has an unreadable parameter {item!r}"
            raise InvalidPasswordHashError(msg)
        values[name] = int(value)

    if values.keys() != {"n", "r", "p"}:
        msg = f"password hash names parameters {sorted(values)}, expected ['n', 'p', 'r']"
        raise InvalidPasswordHashError(msg)

    parameters = _Parameters(values["n"], values["r"], values["p"])
    # OpenSSL's own formula. Checked before the call so that absurd parameters are
    # refused by name rather than by "memory limit exceeded" from inside a library.
    if 128 * parameters.r * (parameters.n + parameters.p + 2) > _MAX_MEMORY:
        msg = f"password hash asks for more memory than {_MAX_MEMORY} bytes: {parameters}"
        raise InvalidPasswordHashError(msg)
    return parameters


def _decode_base64(field: str, name: str, minimum: int) -> bytes:
    try:
        decoded = base64.b64decode(field, validate=True)
    except binascii.Error as exc:
        msg = f"password hash has a {name} that is not base64: {exc}"
        raise InvalidPasswordHashError(msg) from exc

    if len(decoded) < minimum:
        msg = f"password hash has a {len(decoded)}-byte {name}, expected at least {minimum}"
        raise InvalidPasswordHashError(msg)
    return decoded
