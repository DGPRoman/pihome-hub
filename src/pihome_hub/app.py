"""ASGI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from pihome_hub import __version__
from pihome_hub.api.system import router as system_router
from pihome_hub.config import Settings, get_settings
from pihome_hub.logging import configure_logging

logger = logging.getLogger(__name__)

_DESCRIPTION = """\
HTTP control plane for a Raspberry Pi wired to relay-switched circuits.

Relay control and sensor ingestion live under `/v1`. Hardware access goes through a
swappable backend, so the service runs unchanged — against a mock — on a development
machine with no GPIO pins.
"""


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the startup and shutdown of hardware-backed resources.

    Wiring the relay service in here is Phase 3's job. The hook exists now so that
    hardware has exactly one place to be acquired and — the part that is easy to
    forget — released, rather than being left claimed when the process exits.
    """
    settings: Settings = app.state.settings
    logger.info(
        "pihome-hub starting",
        extra={"version": __version__, "docs_enabled": settings.docs_enabled},
    )
    try:
        yield
    finally:
        logger.info("pihome-hub stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI application.

    Accepts settings explicitly so tests can construct an app without touching the
    process environment.
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
    )
    app.state.settings = resolved
    app.include_router(system_router)

    return app
