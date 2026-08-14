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
