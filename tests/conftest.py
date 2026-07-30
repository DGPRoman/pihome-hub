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

#: Long enough to pass validation, and obviously synthetic.
VALID_KEY = "7f3a91c4e8b2d65097143bce8a2f5d0b6c47e19238af5d6c"


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.startswith("PIHOME_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    # get_settings() memoises; without this a single resolution would leak into
    # every later test in the session.
    get_settings.cache_clear()


def build_settings(**overrides: Any) -> Settings:
    """Construct settings from defaults plus explicit overrides."""
    values: dict[str, Any] = {
        "relay_api_key": VALID_KEY,
        "sensor_api_key": VALID_KEY[::-1],
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings() -> Settings:
    return build_settings()


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def app_paths(app: FastAPI) -> set[str]:
    """Every path the application has registered."""
    return {route.path for route in app.routes if isinstance(route, APIRoute)}
