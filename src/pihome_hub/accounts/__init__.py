"""Accounts domain: who may log in, and how their password is checked."""

from __future__ import annotations

from pihome_hub.accounts.errors import (
    AccountError,
    InvalidPasswordHashError,
    WeakPasswordError,
)
from pihome_hub.accounts.passwords import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    dummy_verify,
    hash_password,
    needs_rehash,
    verify_password,
)

__all__ = [
    "MAX_PASSWORD_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "AccountError",
    "InvalidPasswordHashError",
    "WeakPasswordError",
    "dummy_verify",
    "hash_password",
    "needs_rehash",
    "verify_password",
]
