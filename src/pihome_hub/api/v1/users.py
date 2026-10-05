"""Account administration, for an admin in a browser.

Deliberately less than ``pihome-hub-admin`` can do. Over HTTP an admin manages the
``operator`` and ``viewer`` accounts and nothing above them: an admin account is not
changed, disabled or deleted here, and no account is raised to ``admin``. A session
taken over in a browser therefore cannot make a second admin or lock the real one
out — that needs the console on the hub, which is where the first admin came from.

It also means the last-admin rule in the account store is never reached from here.
It is still there for the console, and still the last word if this ever changes.
"""

from __future__ import annotations

import logging
from typing import Final

from fastapi import APIRouter, HTTPException, Request, status

from pihome_hub.accounts import Role, Session, User, UserStore
from pihome_hub.api.v1.schemas import UserChangeRequest, UserCollection, UserResponse
from pihome_hub.security import AdminSessionRequired

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/users",
    tags=["users"],
    # On the router as well as on each route, so a route added later without it is
    # still guarded. FastAPI resolves the dependency once per request either way.
    dependencies=[AdminSessionRequired],
    responses={
        401: {"description": "No usable session — an API key is not accepted here"},
        403: {"description": "Not an admin, or a write without the CSRF header"},
    },
)

#: Why an admin account is refused as the target of a route here.
_CONSOLE_ONLY_DETAIL: Final = (
    "Admin accounts are managed with pihome-hub-admin on the hub, not over HTTP"
)


def _users(request: Request) -> UserStore:
    users: UserStore = request.app.state.users
    return users


def _describe(user: User) -> UserResponse:
    return UserResponse(
        username=user.username,
        role=user.role,
        disabled=user.disabled,
        created_at=user.created_at,
    )


def _manageable(users: UserStore, username: str) -> User:
    """The account, if it is one this API may change. Raises otherwise.

    Checked before the store's own transaction rather than inside it. The window
    between is one an admin at the console would have to hit by promoting this very
    account in the same instant, and the result would be an admin changing an admin
    — which the same person may do at that console anyway.
    """
    user = users.get(username)
    if user.role is Role.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_CONSOLE_ONLY_DETAIL)
    return user


@router.get("", summary="List every account")
def list_users(request: Request) -> UserCollection:
    return UserCollection(users=[_describe(user) for user in _users(request).list_users()])


@router.patch(
    "/{username}",
    summary="Change an operator or viewer account",
    responses={404: {"description": "No such account"}},
)
def update_user(
    request: Request,
    username: str,
    change: UserChangeRequest,
    admin: Session = AdminSessionRequired,
) -> UserResponse:
    """Move an account between ``operator`` and ``viewer``, or block or unblock it.

    Takes effect on that account's next request, not when its session runs out:
    resolving a session re-reads the account it belongs to.
    """
    users = _users(request)
    target = _manageable(users, username)
    updated = users.update(target.username, role=change.role, disabled=change.disabled)

    logger.info(
        "account changed",
        extra={
            "username": updated.username,
            "by": admin.user.username,
            "role": updated.role.value,
            "disabled": updated.disabled,
        },
    )
    return _describe(updated)


@router.delete(
    "/{username}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an operator or viewer account",
    responses={404: {"description": "No such account"}},
)
def delete_user(request: Request, username: str, admin: Session = AdminSessionRequired) -> None:
    """Remove the account. Its sessions go with it, so it is logged out at once."""
    users = _users(request)
    target = _manageable(users, username)
    users.delete(target.username)

    logger.info("account deleted", extra={"username": target.username, "by": admin.user.username})
