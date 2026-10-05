"""Logging in and out.

Three routes on one path, which is what the resource is: ``POST`` opens a session,
``GET`` reports the one you have — and renews it, from the home network — and
``DELETE`` ends it.

The token goes into an HttpOnly cookie and into no response body. A browser holding
it cannot read it from JavaScript, so a cross-site scripting bug somewhere in the
web client cannot exfiltrate a credential that outlives the page.
"""

from __future__ import annotations

import logging
from typing import Final, Literal

from fastapi import APIRouter, HTTPException, Request, Response, status

from pihome_hub.accounts import InvitationStore, Session, SessionStore, UserStore
from pihome_hub.api.v1.schemas import InvitationLoginRequest, LoginRequest, SessionResponse
from pihome_hub.config import Settings
from pihome_hub.ratelimit import FailureLimiter
from pihome_hub.security import (
    SESSION_COOKIE,
    SessionRequired,
    comes_from,
    guard_login_attempt,
    login_bucket,
    require_csrf_header,
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


def _set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    """Hand the browser its token, for a lifetime from now.

    Shared by logging in and renewing, so the two cannot come to disagree about any
    attribute — a cookie set with different ones is a second cookie, not an update.
    """
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_lifetime_seconds,
        path=_COOKIE_PATH,
        httponly=True,
        samesite=_COOKIE_SAMESITE,
        secure=settings.session_cookie_secure,
    )


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
        401: {
            "description": "Wrong username or password, a disabled account, or an "
            "invitation that opens nothing"
        },
        403: {"description": "An invitation presented without the CSRF header"},
        429: {"description": "Too many failed attempts from this client"},
    },
)
def log_in(
    request: Request,
    credentials: LoginRequest | InvitationLoginRequest,
    response: Response,
) -> SessionResponse:
    """Exchange a username and password, or an invitation, for a session cookie.

    The only route under ``/v1`` that takes no credential of its own, which is what
    makes it the way in. It answers ``401`` to every kind of wrong — no such account,
    wrong password, account disabled — because which one it was is not the caller's
    to learn.

    An invitation is the other way in, and the only one for an account made to be
    joined that way. It is presented here rather than at a route of its own so that
    the set of routes reachable without authentication does not grow. It must also
    carry ``X-Pihome-CSRF``. A JSON body from another origin already needs a preflight
    this service will not answer; the header makes that a stated rule rather than a
    consequence of how bodies are parsed, which matters more here than for a
    password, since a forged redemption would put a browser into an account of the
    forger's choosing without the person noticing. Checked before the token is
    looked at, so a refused attempt does not spend it. A password login is not asked
    for the header: adding a required one to an existing route breaks every client
    already written against it.
    """
    guard_login_attempt(request)

    settings: Settings = request.app.state.settings
    sessions: SessionStore = request.app.state.sessions
    limiter: FailureLimiter = request.app.state.auth_limiter
    bucket = login_bucket(request)

    if isinstance(credentials, InvitationLoginRequest):
        require_csrf_header(request, None)
        invitations: InvitationStore = request.app.state.invitations
        user = invitations.redeem(credentials.invitation)
        method = "invitation"
        if user is None:
            limiter.record_failure(bucket)
            # Nothing that was presented is logged: the token is the credential.
            logger.warning(
                "login failed",
                extra={
                    "client": bucket,
                    "method": method,
                    "recent_failures": limiter.failure_count(bucket),
                },
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="The invitation is not valid. Ask whoever sent it for a new one",
            )
    else:
        users: UserStore = request.app.state.users
        user = users.authenticate(credentials.username, credentials.password)
        method = "password"
        if user is None:
            limiter.record_failure(bucket)
            logger.warning(
                "login failed",
                extra={
                    "client": bucket,
                    "method": method,
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

    _set_session_cookie(response, token, settings)
    logger.info(
        "logged in",
        extra={
            "username": user.username,
            "role": user.role.value,
            "method": method,
            "expires_at": session.expires_at.isoformat(),
        },
    )
    return _describe(session)


@router.get(
    "",
    summary="Report the current session",
    responses={401: {"description": "No usable session"}},
)
def read_session(
    request: Request,
    response: Response,
    session: Session = SessionRequired,
) -> SessionResponse:
    """Who the caller is, according to the cookie they sent.

    What a client calls when it starts and when it comes back to the foreground, to
    decide between the house and the way in — and the reason it answers from the
    database rather than from the cookie: an account disabled since login is reported
    as no session at all.

    It is also where a session is renewed, so that a phone opened every day stays
    signed in without an admin issuing a new invitation each month. Only from the
    home network, so a token copied off a device cannot be kept alive from anywhere
    else; and at most once a day, so this costs a write per device per day rather
    than one per request. Otherwise this writes nothing, as it did before renewal.
    """
    settings: Settings = request.app.state.settings
    if not comes_from(request, settings.session_renewal_networks):
        return _describe(session)

    sessions: SessionStore = request.app.state.sessions
    # Present: SessionRequired has just resolved a session from it.
    token = request.cookies[SESSION_COOKIE]
    renewed = sessions.renew(token)
    if renewed is None or renewed.renewed_at == session.renewed_at:
        # Renewed within the last day, or ended between the two reads. Either way
        # the session already resolved is the answer, and there is nothing to send.
        return _describe(session)

    _set_session_cookie(response, token, settings)
    logger.info(
        "session renewed",
        extra={"username": renewed.user.username, "expires_at": renewed.expires_at.isoformat()},
    )
    return _describe(renewed)


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
