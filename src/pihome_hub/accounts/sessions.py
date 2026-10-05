"""Login sessions: what a browser or a phone holds, and what the database keeps instead.

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

#: How long a session lasts from the moment it is opened or last renewed. Thirty days
#: so a tablet on a kitchen wall is not asked again every week.
DEFAULT_SESSION_LIFETIME_SECONDS: Final = 30 * 24 * 60 * 60

#: How long a session goes unrenewed before it may be renewed again. Renewing on
#: every request would be a database write on every authenticated request, on an SD
#: card with finite write cycles. Once a day is one write per device per day, and it
#: still keeps a phone that is opened daily signed in for as long as it is used.
RENEWAL_INTERVAL: Final = timedelta(days=1)


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
    #: When the expiry was last moved: the moment the session was opened, until it
    #: is first renewed.
    renewed_at: datetime


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
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, renewed_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    fingerprint(token),
                    user.id,
                    timestamp(now),
                    timestamp(expires_at),
                    timestamp(now),
                ),
            )

        return token, Session(user=user, created_at=now, expires_at=expires_at, renewed_at=now)

    def resolve(self, token: str) -> Session | None:
        """The session this token names, if it is still usable.

        ``None`` for unknown, expired, and belonging-to-a-disabled-account alike.
        The account is re-read every time rather than trusted from the moment of
        login, so disabling someone takes effect on their next request instead of
        whenever their session happens to run out.
        """
        with connect(self._path) as connection:
            row: sqlite3.Row | None = connection.execute(
                "SELECT s.created_at, s.expires_at, s.renewed_at, u.id AS user_id,"
                " u.username, u.role, u.disabled, u.created_at AS user_created_at"
                " FROM sessions s JOIN users u ON u.id = s.user_id"
                " WHERE s.token_hash = ?",
                (fingerprint(token),),
            ).fetchone()

        if row is None or row["disabled"]:
            return None

        expires_at = datetime.fromisoformat(row["expires_at"])
        if expires_at <= self._clock():
            # Left in place rather than deleted here. This is the read path, it runs
            # on every authenticated request, and a write per request is what
            # RENEWAL_INTERVAL above exists to avoid. create() sweeps instead.
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
            # The migration that added the column filled it for every row, so this
            # fallback is for a row written some other way, not for an expected case.
            renewed_at=datetime.fromisoformat(row["renewed_at"] or row["created_at"]),
        )

    def renew(self, token: str) -> Session | None:
        """Move the session's expiry to a lifetime from now, at most once a day.

        ``None`` when the token names no usable session — unknown, expired, or its
        account disabled — and nothing is revived. A session renewed or opened less
        than :data:`RENEWAL_INTERVAL` ago comes back as it is, without a write. The
        token is unchanged either way, so whoever holds it has nothing to replace.

        Whether a request may renew at all is not decided here: that is a question
        about where it came from, which the caller knows and this store does not.
        """
        session = self.resolve(token)
        if session is None:
            return None

        now = self._clock()
        if now - session.renewed_at < RENEWAL_INTERVAL:
            return session

        expires_at = now + self._lifetime
        with writing(self._path) as connection:
            # Still unexpired at the moment of writing, so a session that ran out
            # between the read above and this cannot be brought back by it.
            cursor = connection.execute(
                "UPDATE sessions SET expires_at = ?, renewed_at = ?"
                " WHERE token_hash = ? AND expires_at > ?",
                (timestamp(expires_at), timestamp(now), fingerprint(token), timestamp(now)),
            )
            if cursor.rowcount == 0:
                return None

        return session.model_copy(update={"expires_at": expires_at, "renewed_at": now})

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
