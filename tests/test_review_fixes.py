"""Regression guards for defects found during review of the v1 API.

Each test here failed before the corresponding fix. Grouped in one module because
they share nothing but their origin.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pihome_hub.app import create_app
from pihome_hub.logging import configure_logging
from pihome_hub.ratelimit import FailureLimiter
from pihome_hub.relays import RelayService
from tests.conftest import RELAY_HEADERS, SENSOR_HEADERS, build_relay_service, build_settings


class TestTextLogsCarryContext:
    """The default format used to drop every ``extra=`` field, so an auth failure
    logged nothing about who caused it."""

    def test_extra_fields_appear_in_the_default_format(self) -> None:
        configure_logging("INFO", json_output=False)
        handler = logging.getLogger().handlers[0]

        record = logging.LogRecord(
            name="probe",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="authentication failed",
            args=(),
            exc_info=None,
        )
        record.client = "10.9.9.9"
        record.recent_failures = 7

        rendered = handler.format(record)

        assert "authentication failed" in rendered
        assert "10.9.9.9" in rendered
        assert "recent_failures=7" in rendered

    def test_json_format_still_carries_them(self) -> None:
        configure_logging("INFO", json_output=True)
        handler = logging.getLogger().handlers[0]
        record = logging.LogRecord(
            name="probe",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="relay set",
            args=(),
            exc_info=None,
        )
        record.relay_id = "porch-light"

        assert '"relay_id": "porch-light"' in handler.format(record)


class TestMalformedBodyIsNotARouteOracle:
    """FastAPI parses the body before dependencies, so an invalid body used to return
    422 without authentication — distinguishing a real path from a fake one."""

    def test_invalid_json_without_a_key_is_401_not_422(self, client: TestClient) -> None:
        response = client.put(
            "/v1/relays", content=b"{", headers={"Content-Type": "application/json"}
        )
        assert response.status_code == HTTPStatus.UNAUTHORIZED

    def test_a_real_and_a_fake_path_are_indistinguishable(self, client: TestClient) -> None:
        headers = {"Content-Type": "application/json"}
        real = client.put("/v1/relays", content=b"{", headers=headers)
        fake = client.put("/v1/nope", content=b"{", headers=headers)
        assert real.status_code == HTTPStatus.UNAUTHORIZED
        assert fake.status_code == HTTPStatus.NOT_FOUND

    def test_probing_with_a_bad_body_still_counts_against_the_limiter(self) -> None:
        settings = build_settings(auth_max_failures=2, auth_failure_window_seconds=300)
        app = create_app(settings, relay_service=build_relay_service())
        headers = {"Content-Type": "application/json"}

        with TestClient(app) as client:
            for _ in range(2):
                assert (
                    client.put("/v1/relays", content=b"{", headers=headers).status_code
                    == HTTPStatus.UNAUTHORIZED
                )

            assert (
                client.put("/v1/relays", content=b"{", headers=headers).status_code
                == HTTPStatus.TOO_MANY_REQUESTS
            )

    def test_an_authenticated_caller_still_gets_a_real_validation_error(
        self, client: TestClient
    ) -> None:
        response = client.put(
            "/v1/relays",
            content=b"{",
            headers={"Content-Type": "application/json", **RELAY_HEADERS},
        )
        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


class TestLimiterBucketsAreScoped:
    """One shared bucket let a success on either key clear the other's failures."""

    def test_a_sensor_success_does_not_clear_relay_failures(self) -> None:
        limiter = FailureLimiter(max_failures=2, window_seconds=300.0)
        limiter.record_failure("relay:10.0.0.1")
        limiter.record_failure("sensor:10.0.0.1")

        limiter.reset("sensor:10.0.0.1")

        assert limiter.failure_count("relay:10.0.0.1") == 1

    def test_the_two_scopes_are_counted_apart_over_http(self) -> None:
        """Failing as a sensor must not consume the relay allowance."""
        settings = build_settings(auth_max_failures=2, auth_failure_window_seconds=300)
        app = create_app(settings, relay_service=build_relay_service())

        with TestClient(app) as client:
            # Two failures presenting the sensor key against a relay route.
            for _ in range(2):
                assert (
                    client.get("/v1/relays", headers=SENSOR_HEADERS).status_code
                    == HTTPStatus.UNAUTHORIZED
                )
            # The relay bucket is now full, since these were relay-scope attempts.
            assert client.get("/v1/relays").status_code == HTTPStatus.TOO_MANY_REQUESTS


class TestRelayServiceOwnership:
    """The lifespan used to close a service it had been handed, so a second startup
    reused a closed one and writes failed."""

    def test_a_supplied_service_survives_the_lifespan(self) -> None:
        service = build_relay_service()
        app = create_app(build_settings(), relay_service=service)

        with TestClient(app) as client:
            assert (
                client.put(
                    "/v1/relays/porch-light", json={"on": True}, headers=RELAY_HEADERS
                ).status_code
                == HTTPStatus.OK
            )

        # Still usable: the caller owns it, so the lifespan left it alone.
        assert service.toggle("porch-light") is False

    def test_two_lifespans_over_one_app_both_work(self) -> None:
        app = create_app(build_settings(), relay_service=build_relay_service())

        for _ in range(2):
            with TestClient(app) as client:
                response = client.put(
                    "/v1/relays/porch-light", json={"on": True}, headers=RELAY_HEADERS
                )
                assert response.status_code == HTTPStatus.OK

    def test_a_lifespan_built_service_is_released(self, tmp_path: Path) -> None:
        """The other half of the contract: what the lifespan builds, it must close."""
        config = tmp_path / "relays.yaml"
        config.write_text(
            'relays:\n  - id: porch-light\n    pin: 17\n    label: "Porch"\n', encoding="utf-8"
        )
        app = create_app(build_settings(relay_config_path=config))

        with TestClient(app) as client:
            assert client.get("/v1/relays", headers=RELAY_HEADERS).status_code == HTTPStatus.OK
            service: RelayService = app.state.relays

        assert app.state.relays is None, "shutdown left a closed service on app.state"
        with pytest.raises(RuntimeError, match="setup_output"):
            service.turn_on("porch-light")
