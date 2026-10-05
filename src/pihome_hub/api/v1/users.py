"""Account administration, for an admin in a browser.

Deliberately less than ``pihome-hub-admin`` can do. Over HTTP an admin manages the
``operator`` and ``viewer`` accounts and nothing above them: an admin account is not
changed, disabled or deleted here, and no account is raised to ``admin``. A session
taken over in a browser therefore cannot make a second admin or lock the real one
out — that needs the console on the hub, which is where the first admin came from.

It also means the last-admin rule in the account store is never reached from here.
It is still there for the console, and still the last word if this ever changes.

An account made here has no password anybody holds. The way in is an invitation: a
one-time token, valid for fifteen minutes, which the admin passes on as a link or a
code and which the person redeems at ``POST /v1/session``. A new phone, or a session
that ran out, is another invitation to the same account.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Final

from fastapi import APIRouter, HTTPException, Request, status

from pihome_hub.accounts import (
    DuplicateUsernameError,
    InvalidUsernameError,
    InvitationStore,
    Role,
    Session,
    User,
    UserStore,
)
from pihome_hub.api.v1.schemas import (
    InvitationResponse,
    NewUserRequest,
    UserChangeRequest,
    UserCollection,
    UserResponse,
)
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


#: Why a disabled account is refused an invitation.
_DISABLED_DETAIL: Final = "The account is disabled. Enable it before inviting anyone to it"


def _users(request: Request) -> UserStore:
    users: UserStore = request.app.state.users
    return users


def _invitations(request: Request) -> InvitationStore:
    invitations: InvitationStore = request.app.state.invitations
    return invitations


def _describe(user: User, invitation_expires_at: datetime | None) -> UserResponse:
    return UserResponse(
        username=user.username,
        role=user.role,
        disabled=user.disabled,
        created_at=user.created_at,
        invitation_expires_at=invitation_expires_at,
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
    pending = _invitations(request).pending()
    return UserCollection(
        users=[_describe(user, pending.get(user.id)) for user in _users(request).list_users()]
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Create an operator or viewer account to invite somebody to",
    responses={
        409: {"description": "The name is taken, ignoring case"},
        422: {"description": "The name or the role is not one this hub accepts"},
    },
)
def create_user(
    request: Request, new: NewUserRequest, admin: Session = AdminSessionRequired
) -> UserResponse:
    """Make the account with no password anybody holds. Invite somebody to it next."""
    try:
        user = _users(request).create_without_password(new.username, new.role)
    except InvalidUsernameError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except DuplicateUsernameError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    logger.info(
        "account created",
        extra={"username": user.username, "role": user.role.value, "by": admin.user.username},
    )
    return _describe(user, None)


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
    pending = _invitations(request).pending()

    logger.info(
        "account changed",
        extra={
            "username": updated.username,
            "by": admin.user.username,
            "role": updated.role.value,
            "disabled": updated.disabled,
        },
    )
    return _describe(updated, pending.get(updated.id))


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


@router.post(
    "/{username}/invitation",
    status_code=status.HTTP_201_CREATED,
    summary="Invite somebody to an operator or viewer account",
    responses={
        404: {"description": "No such account"},
        409: {"description": "The account is disabled"},
    },
)
def invite(
    request: Request, username: str, admin: Session = AdminSessionRequired
) -> InvitationResponse:
    """Issue a one-time token for the account, replacing any it already had.

    The token is in the body because it has to reach a person, and nowhere else: it
    is not logged here or anywhere, and only its hash is kept.
    """
    target = _manageable(_users(request), username)
    if target.disabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_DISABLED_DETAIL)

    token, expires_at = _invitations(request).issue(target)

    logger.info(
        "invitation issued",
        extra={
            "username": target.username,
            "by": admin.user.username,
            "expires_at": expires_at.isoformat(),
        },
    )
    return InvitationResponse(token=token, expires_at=expires_at)


@router.delete(
    "/{username}/invitation",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Withdraw an account's invitation",
    responses={404: {"description": "No such account"}},
)
def revoke_invitation(
    request: Request, username: str, admin: Session = AdminSessionRequired
) -> None:
    """Withdraw it. ``204`` whether or not there was one: either way there is none now."""
    target = _manageable(_users(request), username)
    if _invitations(request).revoke(target):
        logger.info(
            "invitation revoked", extra={"username": target.username, "by": admin.user.username}
        )
