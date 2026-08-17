"""What an account is, and what a role means."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict

from pihome_hub.accounts.errors import InvalidUsernameError

MIN_USERNAME_LENGTH: Final = 2
MAX_USERNAME_LENGTH: Final = 32

#: Letters, digits, and dot, underscore or hyphen between them. Narrow on purpose:
#: a username is typed at a prompt, printed in a table and passed to a shell, and a
#: name holding a space, a control character or a right-to-left mark is a name that
#: does not read the same everywhere it appears.
_USERNAME_PATTERN: Final = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9._-]*[a-zA-Z0-9])?$")


class Role(StrEnum):
    """What an account may do.

    Three, because two would force a choice between "cannot switch anything" and
    "can also delete accounts", and the household case wants the middle:

    * ``admin`` — everything, including creating and removing accounts.
    * ``operator`` — switch relays and read everything. The everyday account.
    * ``viewer`` — read only. Sees what the house is doing, changes nothing.

    The values are the strings stored in the database, which the ``users`` table
    constrains as well; a role this enum does not know cannot reach a row.
    """

    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


class User(BaseModel):
    """An account, as everything outside this package sees it.

    No password hash. It is read inside the store and never leaves it, so nothing
    holding a :class:`User` can leak a value it was never given — including the
    route that will one day serialise this straight to a client.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    username: str
    role: Role
    disabled: bool
    created_at: datetime


def check_username(username: str) -> str:
    """Return ``username`` if it is one this service will accept, or raise.

    Applied at the door rather than on :class:`User`, which is built from rows that
    are already stored: a rule tightened later must not make an existing account
    unreadable, only unrepeatable.
    """
    if not MIN_USERNAME_LENGTH <= len(username) <= MAX_USERNAME_LENGTH:
        msg = (
            f"username must be between {MIN_USERNAME_LENGTH} and {MAX_USERNAME_LENGTH} "
            f"characters, got {len(username)}"
        )
        raise InvalidUsernameError(msg)

    if not _USERNAME_PATTERN.match(username):
        msg = (
            f"username {username!r} must be letters and digits, optionally separated by "
            "'.', '_' or '-'"
        )
        raise InvalidUsernameError(msg)

    return username
