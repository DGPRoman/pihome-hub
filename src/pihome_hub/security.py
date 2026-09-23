"""Authentication: three API keys, and a session cookie.

The keys cover distinct roles — relay control, sensor ingestion, and devices saying
where they are — and keeping them separate means a key recovered from sensor
firmware cannot be replayed to switch relays or to repoint a device. They are for
firmware and scripts: a device that is provisioned once and then left alone has
nobody to type a password.

Sessions are for people. A browser holds an opaque token in an HttpOnly cookie and
the account behind it is re-read on every request, so a role change or a disabled
account takes effect at once rather than whenever the session runs out.
"""

from __future__ import annotations

import ipaddress
import logging
import secrets
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Annotated, Final

from fastapi import Depends, Header, HTTPException, Request, status

from pihome_hub.accounts import Role, Session, SessionStore
from pihome_hub.config import Settings
from pihome_hub.ratelimit import FailureLimiter

logger = logging.getLogger(__name__)

API_KEY_HEADER = "X-API-Key"

#: Returned for both a missing and an incorrect key. Distinguishing the two would
#: tell a prober whether the header name is right, which is free information.
_UNAUTHORIZED_DETAIL = "Invalid or missing API key"

#: Source label used when a request arrives with no identifiable peer address.
_UNKNOWN_CLIENT = "unknown"

#: Name of the session cookie. Prefixed so it cannot collide with a cookie set by
#: something else served from the same host.
SESSION_COOKIE = "pihome_session"

#: Returned when a request carries no usable session. One answer for absent, expired,
#: unknown, and belonging-to-a-disabled-account, because the difference is not the
#: caller's to learn.
_NO_SESSION_DETAIL = "Not authenticated"


class Scope(StrEnum):
    """The role a key grants."""

    RELAY = "relay"
    SENSOR = "sensor"
    #: Devices announcing where they are and what key to ask them with. Its own
    #: scope because an announcement is a credential handover that lands in a table
    #: this hub then makes authenticated requests from — neither of the other two
    #: keys should carry that, and a key extracted from sensor firmware should not
    #: be able to repoint the device in a PC case.
    DEVICE = "device"


def normalise_client(host: str) -> str:
    """Reduce a peer address to the unit an attacker cannot cheaply multiply.

    A single IPv6 address is worthless as an identity: a routed ``/64`` holds about
    1.8e19 of them at no cost, so counting per address would hand out a fresh
    allowance for every guess. Allocations are therefore collapsed to their ``/64``.

    IPv4 stays per address, deliberately: addresses are scarce and heavily shared
    behind NAT, so widening to a prefix would punish unrelated households.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Not an IP at all — a hostname, or a label for a Unix socket peer.
        return host

    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


def client_key(request: Request) -> str:
    """Identify the peer for rate-limiting purposes.

    Derived from the address that opened the connection. No ``X-Forwarded-For``
    handling: trusting that header without knowing which proxy sits in front would let
    any caller forge its own identity and sidestep the limiter entirely. Behind a
    reverse proxy, rate limiting belongs in the proxy — see SECURITY.md.
    """
    if request.client is None:
        return _UNKNOWN_CLIENT
    return normalise_client(request.client.host)


def _expected_key(settings: Settings, scope: Scope) -> str | None:
    """The key that satisfies ``scope``, or ``None`` if none is configured.

    Only the device key can be absent, and a scope with no key configured
    authenticates nobody: there is no value to compare against, so every request
    in that scope is refused. That is what makes the feature opt-in rather than
    open — a deployment that never set one has a route nothing can get through,
    not a route with no lock on it.
    """
    if scope is Scope.RELAY:
        return settings.relay_api_key.get_secret_value()
    if scope is Scope.SENSOR:
        return settings.sensor_api_key.get_secret_value()
    if settings.device_api_key is None:
        return None
    return settings.device_api_key.get_secret_value()


def _authenticate(request: Request, scope: Scope, presented: str | None) -> None:
    settings: Settings = request.app.state.settings
    limiter: FailureLimiter = request.app.state.auth_limiter
    # Bucketed per scope as well as per peer. Sharing one bucket would let a caller
    # holding either key clear the other's failure count on every success, so a
    # leaked sensor key would double as a rate-limit eraser for the relay key.
    client = f"{scope.value}:{client_key(request)}"

    if limiter.is_blocked(client):
        logger.warning(
            "authentication attempt rejected: too many recent failures",
            extra={"client": client, "scope": scope.value, "path": request.url.path},
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed authentication attempts",
        )

    expected = _expected_key(settings, scope)
    # compare_digest over bytes: it takes constant time for equal-length inputs,
    # and encoding first avoids the TypeError str comparison raises on non-ASCII.
    supplied = (presented or "").encode("utf-8")
    accepted = expected is not None and secrets.compare_digest(supplied, expected.encode("utf-8"))
    if not accepted:
        limiter.record_failure(client)
        logger.warning(
            "authentication failed",
            extra={
                "client": client,
                "scope": scope.value,
                "path": request.url.path,
                "key_present": presented is not None,
                # Distinguishes "wrong key" from "this deployment configured none",
                # which is the difference between a device to fix and a hub to
                # finish setting up. In the log, where the operator is — the
                # response says the same thing either way.
                "key_configured": expected is not None,
                "recent_failures": limiter.failure_count(client),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_UNAUTHORIZED_DETAIL,
            headers={"WWW-Authenticate": API_KEY_HEADER},
        )

    limiter.reset(client)


def authenticate_any_scope(request: Request) -> None:
    """Require *some* valid key, outside the dependency system.

    Needed because FastAPI parses the request body before solving dependencies, so a
    malformed body never reaches the route's own dependency. The question here is
    narrower than the route's: not "may you do this?" — the route will still decide
    that — but "are you anonymous?", since answering 422 to an anonymous caller
    reveals which paths exist and what they accept.

    Either key therefore satisfies it, and so does any session — the question is
    only whether somebody is there, and a logged-in caller plainly is. Without that
    branch a browser with a session but no key would be told 401 for a body it got
    wrong, which is both untrue and the opposite of useful. The check cannot be
    used to widen a scope either way: the route's own dependency still decides.

    Failures are counted in their own bucket so that a device with buggy firmware
    cannot exhaust the allowance protecting the relay key.
    """
    if current_session(request) is not None:
        return

    settings: Settings = request.app.state.settings
    limiter: FailureLimiter = request.app.state.auth_limiter
    presented = (request.headers.get(API_KEY_HEADER) or "").encode("utf-8")

    for scope in Scope:
        expected = _expected_key(settings, scope)
        if expected is not None and secrets.compare_digest(presented, expected.encode("utf-8")):
            return

    bucket = f"probe:{client_key(request)}"
    if limiter.is_blocked(bucket):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed authentication attempts",
        )

    limiter.record_failure(bucket)
    logger.warning(
        "unauthenticated request with an unparseable body",
        extra={
            "client": client_key(request),
            "path": request.url.path,
            "recent_failures": limiter.failure_count(bucket),
        },
    )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=_UNAUTHORIZED_DETAIL,
        headers={"WWW-Authenticate": API_KEY_HEADER},
    )


def require_relay_key(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias=API_KEY_HEADER)] = None,
) -> None:
    """Dependency guarding relay control routes."""
    _authenticate(request, Scope.RELAY, x_api_key)


def require_sensor_key(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias=API_KEY_HEADER)] = None,
) -> None:
    """Dependency guarding sensor ingestion routes."""
    _authenticate(request, Scope.SENSOR, x_api_key)


def require_device_key(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias=API_KEY_HEADER)] = None,
) -> None:
    """Dependency guarding the announcement route.

    Refuses everything while no device key is configured, which is the state every
    deployment that has no devices is in.
    """
    _authenticate(request, Scope.DEVICE, x_api_key)


RelayKeyRequired = Depends(require_relay_key)
SensorKeyRequired = Depends(require_sensor_key)
DeviceKeyRequired = Depends(require_device_key)


def current_session(request: Request) -> Session | None:
    """The session this request carries, if it carries a usable one.

    Never raises, so a route can offer more to a caller who is logged in without
    refusing one who is not.
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None

    sessions: SessionStore = request.app.state.sessions
    return sessions.resolve(token)


def require_session(request: Request) -> Session:
    """Dependency for a route that needs to know who is asking.

    No ``WWW-Authenticate`` header. There is no registered scheme for cookies, and
    inventing one would only prompt some clients to show a basic-auth dialog that
    cannot possibly work here.
    """
    session = current_session(request)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_NO_SESSION_DETAIL,
        )
    return session


SessionRequired = Depends(require_session)

#: Roles in order of what they may do. Compared by rank rather than by set
#: membership so that adding a role between two existing ones is one line here and
#: nothing at the call sites.
_RANK: Final[Mapping[Role, int]] = {
    Role.VIEWER: 0,
    Role.OPERATOR: 1,
    Role.ADMIN: 2,
}

#: Returned when a caller is known and not entitled. Deliberately different from
#: _NO_SESSION_DETAIL: re-authenticating fixes that one and cannot fix this one.
_FORBIDDEN_DETAIL = "This account is not allowed to do that"

#: Methods that do not change the house, as RFC 9110 defines safe.
_SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})

#: Header a browser must send with a cookie-authenticated write.
#:
#: Its *presence* is the whole check; the value is never read. A page on another
#: origin cannot set a header like this without a CORS preflight, and this service
#: answers no CORS headers at all, so the preflight fails and the request is never
#: sent. That is the standard custom-header defence, and it needs no token store,
#: no per-session secret and nothing that can fall out of step with a session.
#:
#: SameSite=Strict on the cookie already keeps it off any cross-site request, and
#: this is deliberately a second lock on the same door: that one is a defence the
#: *browser* provides, and a client that does not implement SameSite does not get
#: it. SECURITY.md said a CSRF defence belonged in the same change that first let a
#: session switch a relay, which is this one.
CSRF_HEADER = "X-Pihome-CSRF"

#: Returned when a cookie-authenticated write arrives without it.
_CSRF_DETAIL = (
    f"A cookie-authenticated write must carry the {CSRF_HEADER} header. Send it with any value."
)


def require_role(minimum: Role) -> Callable[[Request, str | None], None]:
    """Build a dependency admitting a caller entitled to at least ``minimum``.

    Two kinds of caller reach these routes and they are authorised differently,
    which is the whole substance of this function.

    A **session** is a person, and a person has a role on their account. That is
    what gets compared, and a role below the requirement is refused with 403 —
    not 401. The two are worth distinguishing: one says "log in", the other says
    "you are logged in and this is not yours to do", and only the second is a
    reason to go and find an admin. A client that cannot tell them apart shows a
    login form to somebody who is already logged in.

    An **API key** is not a person. It is one shared secret provisioned into
    firmware and into scripts, with no account behind it and nobody to hold one, so
    it carries no role and cannot be given one without inventing a user that
    nothing ever logs in to. A valid relay key is therefore admitted exactly as it
    always has been. That is a real limit and not a tidy one: while the web client
    still reaches the hub through a proxy that attaches the key, a viewer's browser
    is authorised by the key rather than by their role. Narrowing what the key may
    do is a separate decision with a live deployment behind it — SECURITY.md says
    so, and pihome-hub-web#8 is the half that has to land first.

    The session is checked before the key, so a logged-in caller never touches the
    failure limiter and cannot spend another caller's allowance by arriving without
    a header they do not need.
    """

    def dependency(
        request: Request,
        x_api_key: Annotated[str | None, Header(alias=API_KEY_HEADER)] = None,
    ) -> None:
        session = current_session(request)
        if session is not None:
            if _RANK[session.user.role] < _RANK[minimum]:
                logger.warning(
                    "request refused: role is not sufficient",
                    extra={
                        "username": session.user.username,
                        "role": session.user.role.value,
                        "required": minimum.value,
                        "path": request.url.path,
                    },
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=_FORBIDDEN_DETAIL,
                )

            if request.method not in _SAFE_METHODS and CSRF_HEADER not in request.headers:
                # Only the cookie path. A key is not an ambient credential: a
                # browser will not attach it to a request some other page made, so
                # there is nothing here for a forged request to borrow.
                logger.warning(
                    "cookie-authenticated write refused: no CSRF header",
                    extra={
                        "username": session.user.username,
                        "path": request.url.path,
                        "method": request.method,
                    },
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=_CSRF_DETAIL,
                )
            return
        _authenticate(request, Scope.RELAY, x_api_key)

    return dependency


#: Read the house. The floor, and what every /v1 read route requires.
ViewerRequired = Depends(require_role(Role.VIEWER))

#: Change the house. Every mutating relay route requires this.
OperatorRequired = Depends(require_role(Role.OPERATOR))


def login_bucket(request: Request) -> str:
    """Rate-limit bucket for password attempts.

    Its own bucket, so that guessing passwords cannot exhaust the allowance
    protecting the relay key, or be hidden behind one that a working device keeps
    clearing.

    Keyed on the peer rather than on the username offered, deliberately: per-username
    counting lets anybody who knows a name lock its owner out by guessing at it, which
    trades an online-guessing defence for a denial of service against a real account.
    """
    return f"login:{client_key(request)}"


def guard_login_attempt(request: Request) -> None:
    """Refuse a password attempt from a peer that has had too many go wrong."""
    limiter: FailureLimiter = request.app.state.auth_limiter
    bucket = login_bucket(request)

    if limiter.is_blocked(bucket):
        logger.warning(
            "login rejected: too many recent failures",
            extra={"client": bucket, "path": request.url.path},
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed authentication attempts",
        )
