"""Logging in and out.

Three routes on one path, which is what the resource is: ``POST`` opens a session,
``GET`` reports the one you have, ``DELETE`` ends it.

The token goes into an HttpOnly cookie and into no response body. A browser holding
it cannot read it from JavaScript, so a cross-site scripting bug somewhere in the
web client cannot exfiltrate a credential that outlives the page.
"""

from __future__ import annotations

import logging
from typing import Final, Literal

from fastapi import APIRouter, HTTPException, Request, Response, status

from pihome_hub.accounts import Session, SessionStore, UserStore
from pihome_hub.api.v1.schemas import LoginRequest, SessionResponse
from pihome_hub.config import Settings
from pihome_hub.ratelimit import FailureLimiter
from pihome_hub.security import (
    SESSION_COOKIE,
    SessionRequired,
    guard_login_attempt,
    login_bucket,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/session", tags=["session"])

#: The cookie is scoped to the whole application, not to this path: it has to travel
#: with the relay and sensor requests it authenticates, which live elsewhere.
_COOKIE_PATH: Final = "/"

#: ``Strict`` rather than ``Lax``. This is the credential for routes that switch mains
#: circuits, and Lax would still send it on a top-level navigation another site
#: initiated. Strict is the whole of the cross-site request forgery defence here —
#: see SECURITY.md for what that does and does not cover.
_COOKIE_SAMESITE: Final[Literal["strict"]] = "strict"


def _describe(session: Session) -> SessionResponse:
    return SessionResponse(
        username=session.user.username,
        role=session.user.role,
        expires_at=session.expires_at,
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Log in",
    responses={
        401: {"description": "Wrong username or password, or the account is disabled"},
        429: {"description": "Too many failed attempts from this client"},
    },
)
def log_in(request: Request, credentials: LoginRequest, response: Response) -> SessionResponse:
    """Exchange a username and password for a session cookie.

    The only route under ``/v1`` that takes no credential of its own, which is what
    makes it the way in. It answers ``401`` to every kind of wrong — no such account,
    wrong password, account disabled — because which one it was is not the caller's
    to learn.
    """
    guard_login_attempt(request)

    settings: Settings = request.app.state.settings
    users: UserStore = request.app.state.users
    sessions: SessionStore = request.app.state.sessions
    limiter: FailureLimiter = request.app.state.auth_limiter
    bucket = login_bucket(request)

    user = users.authenticate(credentials.username, credentials.password)
    if user is None:
        limiter.record_failure(bucket)
        logger.warning(
            "login failed",
            extra={
                "client": bucket,
                # The name that was offered, never the password. This goes to the
                # journal, which an operator reads and a log shipper may forward.
                "username": credentials.username,
                "recent_failures": limiter.failure_count(bucket),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    limiter.reset(bucket)
    token, session = sessions.create(user)

    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_lifetime_seconds,
        path=_COOKIE_PATH,
        httponly=True,
        samesite=_COOKIE_SAMESITE,
        secure=settings.session_cookie_secure,
    )
    logger.info(
        "logged in",
        extra={
            "username": user.username,
            "role": user.role.value,
            "expires_at": session.expires_at.isoformat(),
        },
    )
    return _describe(session)


@router.get(
    "",
    summary="Report the current session",
    responses={401: {"description": "No usable session"}},
)
def read_session(session: Session = SessionRequired) -> SessionResponse:
    """Who the caller is, according to the cookie they sent.

    What a web client calls on load to decide between the dashboard and the login
    form, and the reason it answers from the database rather than from the cookie:
    an account disabled since login is reported as no session at all.
    """
    return _describe(session)


@router.delete(
    "",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Log out",
)
def log_out(request: Request, response: Response) -> None:
    """End the session this request carries, and clear the cookie either way.

    ``204`` even when there was no session to end. Logging out is a statement about
    where the caller wants to be, not a claim about where it was, and answering
    ``401`` would leave a client that has already forgotten its token unable to tell
    the browser to drop it.
    """
    settings: Settings = request.app.state.settings
    sessions: SessionStore = request.app.state.sessions

    token = request.cookies.get(SESSION_COOKIE)
    if token:
        ended = sessions.destroy(token)
        logger.info("logged out", extra={"session_existed": ended})

    # Every attribute that identified the cookie has to be repeated, or the browser
    # treats this as a different cookie and leaves the original in place.
    response.delete_cookie(
        SESSION_COOKIE,
        path=_COOKIE_PATH,
        httponly=True,
        samesite=_COOKIE_SAMESITE,
        secure=settings.session_cookie_secure,
    )
