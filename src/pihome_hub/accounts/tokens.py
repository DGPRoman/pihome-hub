"""Bearer tokens kept as verifiers: what sessions and invitations both hand out.

A token is returned to its holder exactly once and never stored. What is stored is
its SHA-256, so a stolen database yields nothing replayable — the rows hold
verifiers, not credentials — and lookup is by that hash, so the value compared is
already public knowledge if the file is lost.

SHA-256 rather than the scrypt passwords use, and the difference is what the input
is. A password is chosen by a person, so it is guessable and hashing it has to be
deliberately slow. A token is 256 bits out of ``secrets``, so there is nothing to
guess — and a slow hash would spend 16 MiB and a few hundred milliseconds on every
authenticated request.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime
from typing import Final

#: Bytes from ``secrets`` behind each token — 256 bits, which is not searchable.
TOKEN_BYTES: Final = 32


def new_token() -> str:
    """A fresh token, URL-safe so that it survives a cookie, a JSON body and a link."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def fingerprint(token: str) -> str:
    """What goes in the table. Not reversible, and not useful if it leaks."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def timestamp(moment: datetime) -> str:
    """How a time is written to a column that then gets compared.

    Converted to UTC first, and that is the load-bearing part. SQLite compares these
    as text, and text order matches time order only while every row carries the same
    offset — within one it does, since a value with no fractional seconds sorts
    before one with, and '.' is above '+' in exactly the direction the times run.
    A clock handed in on a different offset would quietly break both the sweep and
    the expiry check, so the conversion happens here rather than being assumed.
    """
    return moment.astimezone(UTC).isoformat()
