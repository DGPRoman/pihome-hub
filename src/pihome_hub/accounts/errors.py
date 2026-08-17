"""Exceptions raised by the accounts domain."""

from __future__ import annotations


class AccountError(Exception):
    """Base class for every error this package raises."""


class WeakPasswordError(AccountError):
    """Raised when a password is refused before it is ever hashed.

    The message carries the length that was offered, never the password itself:
    this is reported to an operator and written to a terminal.
    """


class InvalidPasswordHashError(AccountError):
    """Raised when a stored hash is not one this service produced.

    A corrupted row or a hand-edited database, not a wrong password. Treating it as
    a wrong password would report a storage fault as a typo, and the operator would
    go looking for the mistake in the wrong place.
    """


class InvalidUsernameError(AccountError):
    """Raised when a username is not one this service will accept."""


class UnknownUserError(AccountError):
    """Raised when an operation names an account that does not exist."""

    def __init__(self, username: str) -> None:
        super().__init__(f"no account named {username!r}")
        self.username = username


class DuplicateUsernameError(AccountError):
    """Raised when a username is already taken, ignoring case.

    Case-insensitively, because 'Roman' and 'roman' as two accounts is a phishing
    affordance rather than a feature — the database index says the same thing.
    """

    def __init__(self, username: str) -> None:
        super().__init__(f"an account named {username!r} already exists")
        self.username = username


class LastAdminError(AccountError):
    """Raised when a change would leave the service with no way to administer it.

    Deleting, disabling or demoting the only enabled admin is not undoable through
    any interface this service offers: the fix would be editing SQLite by hand on
    the Pi. Refusing costs one explicit second admin, which is what an operator who
    genuinely means it would create anyway.
    """

    def __init__(self, username: str) -> None:
        super().__init__(
            f"{username!r} is the only enabled admin; create another one before "
            "changing this account"
        )
        self.username = username
