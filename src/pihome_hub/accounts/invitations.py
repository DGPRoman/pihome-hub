"""One-time invitations: how a person joins an account without a password.

An admin issues one for an account and the hub returns a token, once. Whoever holds
it can exchange it for a session, once, within fifteen minutes. For an account made
by :meth:`~pihome_hub.accounts.UserStore.create_without_password` that token is the
whole credential, so everything here is about it not working twice, not working
late, and not opening an account it should never open.

The token is kept as its SHA-256 only — see :mod:`pihome_hub.accounts.tokens`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from pihome_hub.accounts.models import Role, User
from pihome_hub.accounts.tokens import fingerprint, new_token, timestamp
from pihome_hub.storage import connect, writing

#: How long an invitation works for. Fixed rather than configurable: it is long
#: enough to scan a code off a screen or open a link that has just arrived, and every
#: minute past that is a minute a forwarded or photographed link still opens the
#: account.
INVITATION_LIFETIME: Final = timedelta(minutes=15)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class InvitationStore:
    """Outstanding invitations, in the same SQLite file the accounts are in."""

    def __init__(self, path: Path, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._path = path
        self._clock = clock

    def issue(self, user: User) -> tuple[str, datetime]:
        """Make a token for ``user``. Returns the token, then when it stops working.

        Replaces any invitation the account already had, so the link an admin was
        last shown is the only one that opens it. Which accounts may be invited is
        the caller's decision; :meth:`redeem` is what refuses the ones that may not
        be opened this way, whatever was decided here.
        """
        token = new_token()
        now = self._clock()
        expires_at = now + INVITATION_LIFETIME

        with writing(self._path) as connection:
            # Issuing is the natural moment to sweep, for the reason sessions give:
            # it is already a write, it is rare, and it needs nobody to own a timer.
            _delete_expired(connection, now)
            connection.execute("DELETE FROM invitations WHERE user_id = ?", (user.id,))
            connection.execute(
                "INSERT INTO invitations (token_hash, user_id, created_at, expires_at)"
                " VALUES (?, ?, ?, ?)",
                (fingerprint(token), user.id, timestamp(now), timestamp(expires_at)),
            )

        return token, expires_at

    def revoke(self, user: User) -> bool:
        """Withdraw ``user``'s invitation. ``True`` if there was one."""
        with writing(self._path) as connection:
            cursor = connection.execute("DELETE FROM invitations WHERE user_id = ?", (user.id,))
            return cursor.rowcount > 0

    def redeem(self, token: str) -> User | None:
        """The account this token opens, spending the token. ``None`` if it opens none.

        One answer for a token that is unknown, expired, already used or revoked, and
        for one naming an account that has been disabled or made an admin since it
        was issued. Which of those it was is not the presenter's to learn.

        Found and deleted inside one immediate transaction, so two requests presenting
        the same token cannot both be told yes: the second waits for the write lock
        and then finds no row. A token that was found is spent whatever the outcome —
        one that named an account it may not open is not kept for another try.
        """
        now = self._clock()
        hashed = fingerprint(token)

        with writing(self._path) as connection:
            row: sqlite3.Row | None = connection.execute(
                "SELECT i.expires_at, u.id AS user_id, u.username, u.role, u.disabled,"
                " u.created_at AS user_created_at"
                " FROM invitations i JOIN users u ON u.id = i.user_id"
                " WHERE i.token_hash = ?",
                (hashed,),
            ).fetchone()
            if row is None:
                return None
            connection.execute("DELETE FROM invitations WHERE token_hash = ?", (hashed,))

        if datetime.fromisoformat(row["expires_at"]) <= now or row["disabled"]:
            return None

        role = Role(row["role"])
        if role is Role.ADMIN:
            # Never issued for one over HTTP, so this is an account raised at the
            # console in the fifteen minutes since. An admin logs in with a password.
            return None

        return User(
            id=row["user_id"],
            username=row["username"],
            role=role,
            disabled=False,
            created_at=datetime.fromisoformat(row["user_created_at"]),
        )

    def pending(self) -> dict[int, datetime]:
        """When each outstanding invitation stops working, by account id.

        Expired rows are left out whether or not they have been swept yet: an
        invitation that no longer works is not outstanding.
        """
        with connect(self._path) as connection:
            rows = connection.execute(
                "SELECT user_id, expires_at FROM invitations WHERE expires_at > ?",
                (timestamp(self._clock()),),
            ).fetchall()
        return {row["user_id"]: datetime.fromisoformat(row["expires_at"]) for row in rows}


def _delete_expired(connection: sqlite3.Connection, now: datetime) -> int:
    cursor = connection.execute("DELETE FROM invitations WHERE expires_at <= ?", (timestamp(now),))
    return cursor.rowcount
