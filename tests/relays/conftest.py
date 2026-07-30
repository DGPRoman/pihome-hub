"""Shared fixtures for relay tests."""

from __future__ import annotations

import pytest

from pihome_hub.relays import MockRelayBackend, RelayConfig, RelayService


@pytest.fixture
def porch() -> RelayConfig:
    return RelayConfig(id="porch-light", pin=17, label="Porch light")


@pytest.fixture
def gate() -> RelayConfig:
    return RelayConfig(id="gate-light", pin=27, label="Gate light")


@pytest.fixture
def backend() -> MockRelayBackend:
    return MockRelayBackend()


@pytest.fixture
def service(backend: MockRelayBackend, porch: RelayConfig, gate: RelayConfig) -> RelayService:
    return RelayService(backend, [porch, gate])
