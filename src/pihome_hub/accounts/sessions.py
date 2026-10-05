"""Login sessions: what a browser holds, and what the database keeps instead.

The token is never stored, only its SHA-256 — see :mod:`pihome_hub.accounts.tokens`
for why that hash and not a slow one.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict

from pihome_hub.accounts.models import Role, User
from pihome_hub.accounts.tokens import fingerprint, new_token, timestamp
from pihome_hub.storage import connect, writing

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
        token = new_token()
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
                (fingerprint(token), user.id, timestamp(now), timestamp(expires_at)),
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
                (fingerprint(token),),
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
                "DELETE FROM sessions WHERE token_hash = ?", (fingerprint(token),)
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
        now = timestamp(self._clock())
        with connect(self._path) as connection:
            row = connection.execute(
                "SELECT count(*) FROM sessions WHERE expires_at > ?", (now,)
            ).fetchone()
        return int(row[0])


def _delete_expired(connection: sqlite3.Connection, now: datetime) -> int:
    cursor = connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (timestamp(now),))
    return cursor.rowcount
