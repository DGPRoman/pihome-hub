"""ASGI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from pihome_hub import __version__
from pihome_hub.api.system import router as system_router
from pihome_hub.api.v1.relays import router as relays_router
from pihome_hub.config import Settings, get_settings
from pihome_hub.logging import configure_logging
from pihome_hub.ratelimit import FailureLimiter
from pihome_hub.relays import RelayService, UnknownRelayError, load_relays
from pihome_hub.relays.factory import create_backend

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
    """Own the relay service for the lifetime of the process.

    Whatever service the app holds is closed on the way out, so GPIO pins are
    never left claimed after shutdown.
    """
    settings: Settings = app.state.settings

    if not hasattr(app.state, "relays"):
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
        service.close()
        logger.info("pihome-hub stopped")


async def _unknown_relay_handler(request: Request, exc: Exception) -> JSONResponse:
    """Map an unknown relay id to 404 rather than letting it become a 500."""
    return JSONResponse(status_code=404, content={"detail": str(exc)})


def create_app(
    settings: Settings | None = None,
    *,
    relay_service: RelayService | None = None,
) -> FastAPI:
    """Build the ASGI application.

    Both dependencies can be supplied explicitly so tests — and ``__main__`` —
    construct an app without touching the process environment or the filesystem.
    A service passed here is still closed on shutdown by the lifespan.
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
    app.include_router(system_router)
    app.include_router(relays_router)

    return app
