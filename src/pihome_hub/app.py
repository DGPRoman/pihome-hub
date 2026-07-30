"""ASGI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from pihome_hub import __version__
from pihome_hub.api.system import router as system_router
from pihome_hub.api.v1.relays import router as relays_router
from pihome_hub.config import Settings, get_settings
from pihome_hub.logging import configure_logging
from pihome_hub.ratelimit import FailureLimiter
from pihome_hub.relays import RelayService, UnknownRelayError, load_relays
from pihome_hub.relays.factory import create_backend
from pihome_hub.security import authenticate_or_none

logger = logging.getLogger(__name__)

_DESCRIPTION = """\
HTTP control plane for a Raspberry Pi wired to relay-switched circuits.

Relay control lives under `/v1` and requires the `X-API-Key` header. Hardware access
goes through a swappable backend, so the service runs unchanged — against a mock —
on a development machine with no GPIO pins.
"""


def build_relay_service(settings: Settings) -> RelayService:
    """Assemble the relay service described by ``settings``.

    Kept separate from the lifespan so that a misconfiguration surfaces before the
    server starts listening, where it can be reported as a readable message instead
    of a traceback out of an already-running event loop.
    """
    relays = load_relays(settings.relay_config_path)
    return RelayService(create_backend(settings.gpio_backend), relays)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Acquire and release the relay service for the lifetime of the process.

    Only a service this lifespan built is closed by it. A caller that supplied one
    keeps ownership: closing it here would release GPIO pins that the caller — a
    test with two clients over one service, or an embedder — still expects to work,
    and a closed service reads as stale and raises on the next write.
    """
    settings: Settings = app.state.settings

    owned = getattr(app.state, "relays", None) is None
    if owned:
        app.state.relays = build_relay_service(settings)

    service: RelayService = app.state.relays
    logger.info(
        "pihome-hub starting",
        extra={
            "version": __version__,
            "backend": settings.gpio_backend,
            "relays": len(service.configured),
            "docs_enabled": settings.docs_enabled,
        },
    )
    try:
        yield
    finally:
        if owned:
            service.close()
            app.state.relays = None
        logger.info("pihome-hub stopped")


async def _unknown_relay_handler(request: Request, exc: Exception) -> JSONResponse:
    """Map an unknown relay id to 404 rather than letting it become a 500."""
    return JSONResponse(status_code=404, content={"detail": str(exc)})


async def _validation_handler(request: Request, exc: Exception) -> JSONResponse:
    """Answer 401 before 422 when the caller never proved who it was.

    FastAPI reads and parses the request body *before* solving dependencies, so a
    body that is not valid JSON raises here without authentication ever running. That
    made a malformed body a route-existence oracle: 422 for a real path taking a body,
    404 for one that does not — usable with no key and never counted by the limiter.

    Authentication is therefore re-checked at this boundary, and a failure is recorded
    so probing costs the same as any other failed attempt.
    """
    if not request.url.path.startswith("/v1"):
        return await request_validation_exception_handler(
            request, cast(RequestValidationError, exc)
        )

    try:
        authenticate_or_none(request)
    except HTTPException as auth_error:
        return JSONResponse(
            status_code=auth_error.status_code,
            content={"detail": auth_error.detail},
            headers=auth_error.headers,
        )

    return await request_validation_exception_handler(request, cast(RequestValidationError, exc))


def create_app(
    settings: Settings | None = None,
    *,
    relay_service: RelayService | None = None,
) -> FastAPI:
    """Build the ASGI application.

    Both dependencies can be supplied explicitly so tests — and ``__main__`` —
    construct an app without touching the process environment or the filesystem.

    Ownership follows construction: a ``relay_service`` passed in here belongs to the
    caller, who must close it. Omit it and the lifespan builds one and closes it.
    """
    resolved = settings if settings is not None else get_settings()
    configure_logging(resolved.log_level, json_output=resolved.log_json)

    app = FastAPI(
        title="pihome-hub",
        description=_DESCRIPTION,
        version=__version__,
        lifespan=_lifespan,
        # Documentation endpoints are opt-in. A public deployment should not hand
        # out a machine-readable map of its own routes.
        docs_url="/docs" if resolved.docs_enabled else None,
        openapi_url="/openapi.json" if resolved.docs_enabled else None,
        redoc_url=None,
        # Slash redirection happens during routing, before dependencies run, so with
        # it enabled an unauthenticated caller gets a 307 for a real path and a 404
        # for a fake one — a route-existence oracle that sidesteps the API key. It
        # also turns a POST into a follow-the-redirect round trip. Off: one canonical
        # spelling per route.
        redirect_slashes=False,
    )
    app.state.settings = resolved
    app.state.auth_limiter = FailureLimiter(
        max_failures=resolved.auth_max_failures,
        window_seconds=resolved.auth_failure_window_seconds,
    )
    if relay_service is not None:
        app.state.relays = relay_service

    app.add_exception_handler(UnknownRelayError, _unknown_relay_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.include_router(system_router)
    app.include_router(relays_router)

    return app
