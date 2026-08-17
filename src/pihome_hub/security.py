"""Authentication: two API keys, and a session cookie.

The keys cover distinct roles — relay control and sensor ingestion — and keeping
them separate means a key recovered from sensor firmware cannot be replayed to
switch relays. They are for firmware and scripts: a device that is provisioned once
and then left alone has nobody to type a password.

Sessions are for people. A browser holds an opaque token in an HttpOnly cookie and
the account behind it is re-read on every request, so a role change or a disabled
account takes effect at once rather than whenever the session runs out.
"""

from __future__ import annotations

import ipaddress
import logging
import secrets
from enum import StrEnum
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from pihome_hub.accounts import Session, SessionStore
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


def _expected_key(settings: Settings, scope: Scope) -> str:
    if scope is Scope.RELAY:
        return settings.relay_api_key.get_secret_value()
    return settings.sensor_api_key.get_secret_value()


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
    supplied = presented or ""
    if not secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        limiter.record_failure(client)
        logger.warning(
            "authentication failed",
            extra={
                "client": client,
                "scope": scope.value,
                "path": request.url.path,
                "key_present": presented is not None,
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

    Either key therefore satisfies it, and the check cannot be used to widen a
    scope. Failures are counted in their own bucket so that a device with buggy
    firmware cannot exhaust the allowance protecting the relay key.
    """
    settings: Settings = request.app.state.settings
    limiter: FailureLimiter = request.app.state.auth_limiter
    presented = (request.headers.get(API_KEY_HEADER) or "").encode("utf-8")

    for scope in Scope:
        expected = _expected_key(settings, scope).encode("utf-8")
        if secrets.compare_digest(presented, expected):
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


RelayKeyRequired = Depends(require_relay_key)
SensorKeyRequired = Depends(require_sensor_key)


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
