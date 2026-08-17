"""Login sessions: what a browser holds, and what the database keeps instead.

The token is never stored. What is stored is its SHA-256, so a stolen database
yields nothing replayable — the rows hold verifiers, not credentials. Lookup is by
that hash, so the value compared is already public knowledge if the file is lost.

SHA-256 rather than the scrypt the module next door uses for passwords, and the
difference is what the input is. A password is chosen by a person, so it is
guessable and hashing it has to be deliberately slow. A token is 256 bits out of
``secrets``, so there is nothing to guess — and a slow hash would spend 16 MiB and
a few hundred milliseconds on every authenticated request.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict

from pihome_hub.accounts.models import Role, User
from pihome_hub.storage import connect, writing

#: Bytes from ``secrets`` behind each token — 256 bits, which is not searchable.
_TOKEN_BYTES: Final = 32

#: How long a session lasts from the moment it is opened. Thirty days so a tablet on
#: a kitchen wall is not asked again every week, and absolute rather than sliding:
#: renewing on use would mean a database write on every authenticated request, and
#: this runs on an SD card with finite write cycles.
DEFAULT_SESSION_LIFETIME_SECONDS: Final = 30 * 24 * 60 * 60


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Session(BaseModel):
    """A live session and whose it is.

    Carries no token: the value is returned once, by :meth:`SessionStore.create`,
    and is not recoverable from anything stored afterwards.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    user: User
    created_at: datetime
    expires_at: datetime


class SessionStore:
    """Open sessions, in the same SQLite file the accounts are in."""

    def __init__(
        self,
        path: Path,
        *,
        lifetime: timedelta = timedelta(seconds=DEFAULT_SESSION_LIFETIME_SECONDS),
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._path = path
        self._lifetime = lifetime
        self._clock = clock

    def create(self, user: User) -> tuple[str, Session]:
        """Open a session for ``user``. Returns the token, then the session.

        The token is handed back exactly once. Only its hash is kept, so a caller
        that loses it has to open a new session rather than look the old one up.
        """
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        now = self._clock()
        expires_at = now + self._lifetime

        with writing(self._path) as connection:
            # Opening a session is the natural moment to sweep: it is already a
            # write, it is rare, and it bounds the table without a background task
            # or a timer that has to be owned by somebody.
            _delete_expired(connection, now)
            connection.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at)"
                " VALUES (?, ?, ?, ?)",
                (_fingerprint(token), user.id, _timestamp(now), _timestamp(expires_at)),
            )

        return token, Session(user=user, created_at=now, expires_at=expires_at)

    def resolve(self, token: str) -> Session | None:
        """The session this token names, if it is still usable.

        ``None`` for unknown, expired, and belonging-to-a-disabled-account alike.
        The account is re-read every time rather than trusted from the moment of
        login, so disabling someone takes effect on their next request instead of
        whenever their session happens to run out.
        """
        with connect(self._path) as connection:
            row: sqlite3.Row | None = connection.execute(
                "SELECT s.created_at, s.expires_at, u.id AS user_id, u.username,"
                " u.role, u.disabled, u.created_at AS user_created_at"
                " FROM sessions s JOIN users u ON u.id = s.user_id"
                " WHERE s.token_hash = ?",
                (_fingerprint(token),),
            ).fetchone()

        if row is None or row["disabled"]:
            return None

        expires_at = datetime.fromisoformat(row["expires_at"])
        if expires_at <= self._clock():
            # Left in place rather than deleted here. This is the read path, it runs
            # on every authenticated request, and a write per request is what the
            # absolute lifetime above exists to avoid. create() sweeps instead.
            return None

        return Session(
            user=User(
                id=row["user_id"],
                username=row["username"],
                role=Role(row["role"]),
                disabled=False,
                created_at=datetime.fromisoformat(row["user_created_at"]),
            ),
            created_at=datetime.fromisoformat(row["created_at"]),
            expires_at=expires_at,
        )

    def destroy(self, token: str) -> bool:
        """End one session. ``True`` if there was one to end.

        A token that was never valid and one that has just been used to log out are
        both answered the same way by the caller, so this reports what happened
        without deciding what to say about it.
        """
        with writing(self._path) as connection:
            cursor = connection.execute(
                "DELETE FROM sessions WHERE token_hash = ?", (_fingerprint(token),)
            )
            return cursor.rowcount > 0

    def destroy_all_for(self, user: User) -> int:
        """End every session an account has, returning how many there were.

        For a password change. A new password that left the old sessions running
        would not be a way to lock anyone out, which is most of the reason to
        change one.
        """
        with writing(self._path) as connection:
            cursor = connection.execute("DELETE FROM sessions WHERE user_id = ?", (user.id,))
            return cursor.rowcount

    def count(self) -> int:
        """Live sessions, expired ones excluded whether or not they are still rows."""
        now = _timestamp(self._clock())
        with connect(self._path) as connection:
            row = connection.execute(
                "SELECT count(*) FROM sessions WHERE expires_at > ?", (now,)
            ).fetchone()
        return int(row[0])


def _fingerprint(token: str) -> str:
    """What goes in the table. Not reversible, and not useful if it leaks."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _timestamp(moment: datetime) -> str:
    """How a time is written to a column that then gets compared.

    Converted to UTC first, and that is the load-bearing part. SQLite compares these
    as text, and text order matches time order only while every row carries the same
    offset — within one it does, since a value with no fractional seconds sorts
    before one with, and '.' is above '+' in exactly the direction the times run.
    A clock handed in on a different offset would quietly break both the sweep and
    the expiry check, so the conversion happens here rather than being assumed.
    """
    return moment.astimezone(UTC).isoformat()


def _delete_expired(connection: sqlite3.Connection, now: datetime) -> int:
    cursor = connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (_timestamp(now),))
    return cursor.rowcount
