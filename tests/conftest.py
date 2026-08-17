"""Shared test fixtures.

The suite is isolated from the surrounding environment: ``PIHOME_*`` variables are
stripped and each test runs in an empty working directory, so neither a developer's
exported variables nor a local ``.env`` can influence a result.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from pihome_hub.app import create_app
from pihome_hub.config import Settings, get_settings
from pihome_hub.relays import MockRelayBackend, RelayConfig, RelayService
from pihome_hub.storage import prepare_database

#: Long enough to pass validation, and obviously synthetic.
VALID_KEY = "7f3a91c4e8b2d65097143bce8a2f5d0b6c47e19238af5d6c"
#: The sensor key, distinct from the relay key so scope confusion is detectable.
VALID_SENSOR_KEY = VALID_KEY[::-1]

RELAY_HEADERS = {"X-API-Key": VALID_KEY}
SENSOR_HEADERS = {"X-API-Key": VALID_SENSOR_KEY}


@pytest.fixture
def anyio_backend() -> str:
    """Run ``@pytest.mark.anyio`` tests on asyncio only.

    anyio's plugin comes in with Starlette, so async tests need no extra dependency;
    the service itself only ever runs under uvicorn's asyncio loop, so testing the
    trio backend as well would prove nothing.
    """
    return "asyncio"


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.startswith("PIHOME_"):
            monkeypatch.delenv(name, raising=False)
    # Not a PIHOME_ variable, but systemd exports it and the database path falls
    # back to it. A developer running the suite from inside a unit would otherwise
    # have tests writing to that unit's real state directory.
    monkeypatch.delenv("STATE_DIRECTORY", raising=False)
    monkeypatch.chdir(tmp_path)
    # get_settings() memoises; without this a single resolution would leak into
    # every later test in the session.
    get_settings.cache_clear()


def build_settings(**overrides: Any) -> Settings:
    """Construct settings from defaults plus explicit overrides."""
    values: dict[str, Any] = {
        "relay_api_key": VALID_KEY,
        "sensor_api_key": VALID_SENSOR_KEY,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings() -> Settings:
    return build_settings()


def build_relay_service(backend: MockRelayBackend | None = None) -> RelayService:
    """A two-relay service on a mock backend, matching config/relays.example.yaml."""
    return RelayService(
        backend if backend is not None else MockRelayBackend(),
        [
            RelayConfig(id="porch-light", pin=17, label="Porch light"),
            RelayConfig(id="gate-light", pin=27, label="Gate light"),
        ],
    )


@pytest.fixture
def relay_backend() -> MockRelayBackend:
    return MockRelayBackend()


@pytest.fixture
def relay_service(relay_backend: MockRelayBackend) -> RelayService:
    return build_relay_service(relay_backend)


@pytest.fixture
def app(settings: Settings, relay_service: RelayService) -> FastAPI:
    # The schema is created here for the same reason __main__ creates it before
    # uvicorn starts: create_app() touches no files, so something has to.
    prepare_database(settings.database_path)
    return create_app(settings, relay_service=relay_service)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def registered_routes(app: FastAPI) -> list[tuple[str, str]]:
    """Every ``(method, path)`` the application serves.

    FastAPI does not flatten included routers into ``APIRoute`` objects on
    ``app.routes`` — it stores one wrapper per ``include_router`` call. Filtering
    ``app.routes`` for ``APIRoute`` therefore yields nothing, which would make any
    test built on it pass vacuously. The assertion at the end is the guard: if a
    future FastAPI reorganises this again, the fixture fails instead of quietly
    reporting that the application has no routes.
    """
    found: list[tuple[str, str]] = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            found.extend((method, route.path) for method in route.methods or ())
        elif hasattr(route, "effective_route_contexts"):
            for context in route.effective_route_contexts():
                found.extend((method, context.path) for method in context.methods or ())

    assert found, "route enumeration found nothing — the fixture is out of date"
    return found
