"""Accounts domain: who may log in, what they may do, and how a password is checked."""

from __future__ import annotations

from pihome_hub.accounts.errors import (
    AccountError,
    DuplicateUsernameError,
    InvalidPasswordHashError,
    InvalidUsernameError,
    LastAdminError,
    UnknownUserError,
    WeakPasswordError,
)
from pihome_hub.accounts.models import (
    MAX_USERNAME_LENGTH,
    MIN_USERNAME_LENGTH,
    Role,
    User,
    check_username,
)
from pihome_hub.accounts.passwords import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    dummy_verify,
    hash_password,
    needs_rehash,
    verify_password,
)
from pihome_hub.accounts.store import UserStore

__all__ = [
    "MAX_PASSWORD_LENGTH",
    "MAX_USERNAME_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "MIN_USERNAME_LENGTH",
    "AccountError",
    "DuplicateUsernameError",
    "InvalidPasswordHashError",
    "InvalidUsernameError",
    "LastAdminError",
    "Role",
    "UnknownUserError",
    "User",
    "UserStore",
    "WeakPasswordError",
    "check_username",
    "dummy_verify",
    "hash_password",
    "needs_rehash",
    "verify_password",
]
