"""Accounts in the database: creating them, changing them, checking a password.

A connection per operation, like the rest of the storage layer — opening one is
cheap, and a shared connection would need a lock because FastAPI runs synchronous
work in a threadpool and a SQLite connection is not safe across threads.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pihome_hub.accounts.errors import (
    DuplicateUsernameError,
    LastAdminError,
    UnknownUserError,
)
from pihome_hub.accounts.models import Role, User, check_username
from pihome_hub.accounts.passwords import (
    dummy_verify,
    hash_password,
    needs_rehash,
    verify_password,
)
from pihome_hub.storage import connect, writing


def _utc_now() -> datetime:
    return datetime.now(UTC)


class UserStore:
    """The accounts table, behind the operations that keep it consistent."""

    def __init__(self, path: Path, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._path = path
        self._clock = clock

    # -- Reading -------------------------------------------------------------

    def count(self) -> int:
        """How many accounts exist. Zero is the state a fresh installation is in."""
        with connect(self._path) as connection:
            return int(connection.execute("SELECT count(*) FROM users").fetchone()[0])

    def list_users(self) -> list[User]:
        """Every account, ordered the way someone reading a list would expect."""
        with connect(self._path) as connection:
            rows = connection.execute(
                "SELECT id, username, role, disabled, created_at FROM users"
                " ORDER BY username COLLATE NOCASE"
            ).fetchall()
        return [_to_user(row) for row in rows]

    def get(self, username: str) -> User:
        """One account by name, ignoring case. Raises :class:`UnknownUserError`."""
        with connect(self._path) as connection:
            return _require(connection, username)

    # -- Logging in ----------------------------------------------------------

    def authenticate(self, username: str, password: str) -> User | None:
        """The account, if the password is right and the account is usable.

        ``None`` for every failure — no such account, wrong password, disabled
        account. Which of the three it was is not the caller's to publish, and a
        login endpoint that distinguishes them tells anyone who asks which names
        are real.
        """
        with connect(self._path) as connection:
            row = connection.execute(
                "SELECT id, username, role, disabled, created_at, password_hash FROM users"
                " WHERE username = ? COLLATE NOCASE",
                (username,),
            ).fetchone()

        if row is None:
            # The same work a real check would have done. Without it "no such
            # account" answers in microseconds and "wrong password" in hundreds of
            # milliseconds, and the difference is readable from outside.
            dummy_verify(password)
            return None

        if not verify_password(password, row["password_hash"]):
            return None

        # After the password rather than before it: checked first, a disabled
        # account would answer fast and become the same oracle again.
        if row["disabled"]:
            return None

        if needs_rehash(row["password_hash"]):
            # A login is the one moment the plaintext is in hand, so it is the only
            # moment a stronger hash can be computed without asking anyone. A write
            # failure here is not swallowed: it means the state directory has gone
            # read-only, which is worth hearing about now rather than later.
            with writing(self._path) as connection:
                connection.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (hash_password(password), row["id"]),
                )

        return _to_user(row)

    # -- Changing ------------------------------------------------------------

    def create(self, username: str, password: str, role: Role) -> User:
        """Add an account. Raises if the name is taken, invalid, or the password weak."""
        check_username(username)
        # Hashed before the transaction, deliberately: it costs hundreds of
        # milliseconds on the target board, and holding SQLite's write lock for that
        # long would stall every other writer for no reason.
        password_hash = hash_password(password)
        created_at = self._clock()

        with writing(self._path) as connection:
            # Checked rather than caught: the write lock is already held, so this
            # cannot race, and an IntegrityError from anywhere else stays reported as
            # what it is instead of being blamed on a duplicate name.
            if _find(connection, username) is not None:
                raise DuplicateUsernameError(username)

            connection.execute(
                "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                (username, password_hash, role.value, created_at.isoformat()),
            )
            # Read back by name rather than by ``lastrowid``: it costs one indexed
            # lookup and proves the account is reachable by what it was called.
            return _require(connection, username)

    def set_password(self, username: str, password: str) -> None:
        """Replace an account's password."""
        password_hash = hash_password(password)

        with writing(self._path) as connection:
            user = _require(connection, username)
            connection.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user.id)
            )

    def set_role(self, username: str, role: Role) -> User:
        with writing(self._path) as connection:
            user = _require(connection, username)
            if role is not Role.ADMIN:
                _refuse_if_last_admin(connection, user)
            connection.execute("UPDATE users SET role = ? WHERE id = ?", (role.value, user.id))
            return _reload(connection, user.id)

    def set_disabled(self, username: str, disabled: bool) -> User:
        """Disable or re-enable an account, keeping its password and its history."""
        with writing(self._path) as connection:
            user = _require(connection, username)
            if disabled:
                _refuse_if_last_admin(connection, user)
            connection.execute(
                "UPDATE users SET disabled = ? WHERE id = ?", (int(disabled), user.id)
            )
            return _reload(connection, user.id)

    def delete(self, username: str) -> None:
        with writing(self._path) as connection:
            user = _require(connection, username)
            _refuse_if_last_admin(connection, user)
            connection.execute("DELETE FROM users WHERE id = ?", (user.id,))


def _refuse_if_last_admin(connection: sqlite3.Connection, user: User) -> None:
    """Refuse a change that would leave nobody able to administer the service.

    Counted inside the caller's transaction, which is why that transaction is
    immediate: the read and the write have to be one indivisible step.
    """
    if user.role is not Role.ADMIN or user.disabled:
        return

    others = connection.execute(
        "SELECT count(*) FROM users WHERE role = 'admin' AND disabled = 0 AND id != ?",
        (user.id,),
    ).fetchone()[0]

    if others == 0:
        raise LastAdminError(user.username)


def _find(connection: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    # Annotated rather than returned directly: sqlite3.Cursor.fetchone() is typed as
    # Any, and this is the one place the row type is established for everything below.
    row: sqlite3.Row | None = connection.execute(
        "SELECT id, username, role, disabled, created_at FROM users"
        " WHERE username = ? COLLATE NOCASE",
        (username,),
    ).fetchone()
    return row


def _require(connection: sqlite3.Connection, username: str) -> User:
    row = _find(connection, username)
    if row is None:
        raise UnknownUserError(username)
    return _to_user(row)


def _reload(connection: sqlite3.Connection, user_id: int) -> User:
    """Read a row back after writing it, so the caller sees what was actually stored."""
    row = connection.execute(
        "SELECT id, username, role, disabled, created_at FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    return _to_user(row)


def _to_user(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        username=row["username"],
        role=Role(row["role"]),
        disabled=bool(row["disabled"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )
