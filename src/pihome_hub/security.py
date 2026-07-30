"""API-key authentication.

Two independent keys, each covering a distinct role: relay control and sensor
ingestion. Keeping them separate means a key recovered from sensor firmware
cannot be replayed to switch relays.
"""

from __future__ import annotations

import logging
import secrets
from enum import StrEnum
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from pihome_hub.config import Settings
from pihome_hub.ratelimit import FailureLimiter

logger = logging.getLogger(__name__)

API_KEY_HEADER = "X-API-Key"

#: Returned for both a missing and an incorrect key. Distinguishing the two would
#: tell a prober whether the header name is right, which is free information.
_UNAUTHORIZED_DETAIL = "Invalid or missing API key"

#: Source label used when a request arrives with no identifiable peer address.
_UNKNOWN_CLIENT = "unknown"


class Scope(StrEnum):
    """The role a key grants."""

    RELAY = "relay"
    SENSOR = "sensor"


def client_key(request: Request) -> str:
    """Identify the peer for rate-limiting purposes.

    This is the address of whatever opened the TCP connection. No
    ``X-Forwarded-For`` handling: trusting that header without knowing which proxy
    is in front would let any caller forge its own identity and sidestep the
    limiter. If this service is ever put behind a reverse proxy, the limiting
    belongs in the proxy.
    """
    return request.client.host if request.client else _UNKNOWN_CLIENT


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


def authenticate_or_none(request: Request) -> None:
    """Re-run the relay check outside the dependency system.

    Needed because FastAPI parses the request body before solving dependencies, so a
    malformed body bypasses the dependency entirely. Raises the same
    :class:`HTTPException` the dependency would, or returns if the key is good.
    """
    _authenticate(request, Scope.RELAY, request.headers.get(API_KEY_HEADER))


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
