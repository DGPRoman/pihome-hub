"""Authentication behaviour of the v1 API."""

from __future__ import annotations

from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient

from pihome_hub.app import create_app
from pihome_hub.relays import RelayService
from tests.conftest import RELAY_HEADERS, SENSOR_HEADERS, VALID_KEY, build_settings


class TestKeyRequired:
    def test_no_key_is_rejected(self, client: TestClient) -> None:
        assert client.get("/v1/relays").status_code == HTTPStatus.UNAUTHORIZED

    def test_wrong_key_is_rejected(self, client: TestClient) -> None:
        response = client.get(
            "/v1/relays", headers={"X-API-Key": "wrong-but-long-enough-key-value"}
        )
        assert response.status_code == HTTPStatus.UNAUTHORIZED

    def test_correct_key_is_accepted(self, client: TestClient) -> None:
        assert client.get("/v1/relays", headers=RELAY_HEADERS).status_code == HTTPStatus.OK

    def test_every_versioned_route_is_guarded(
        self, client: TestClient, registered_routes: list[tuple[str, str]]
    ) -> None:
        """Driven by route enumeration, not a hand-written list.

        A new ``/v1`` route added without the auth dependency fails here, which a
        hardcoded list of requests would never have noticed.
        """
        versioned = [(m, p) for m, p in registered_routes if p.startswith("/v1")]
        assert versioned, "no /v1 routes found to check"

        unguarded = []
        for method, path in versioned:
            concrete = path.replace("{relay_id}", "porch-light")
            response = client.request(method, concrete, json={"on": True})
            if response.status_code != HTTPStatus.UNAUTHORIZED:
                unguarded.append(f"{method} {path} -> {response.status_code}")

        assert not unguarded, f"reachable without a key: {unguarded}"

    def test_a_rejected_request_does_not_touch_the_hardware(
        self, client: TestClient, relay_service: RelayService
    ) -> None:
        client.put("/v1/relays/porch-light", json={"on": True})
        assert relay_service.status()["porch-light"] is False


class TestScopeSeparation:
    def test_the_sensor_key_cannot_control_relays(self, client: TestClient) -> None:
        """A key extracted from sensor firmware must not switch relays."""
        response = client.post("/v1/relays/porch-light/toggle", headers=SENSOR_HEADERS)
        assert response.status_code == HTTPStatus.UNAUTHORIZED


class TestResponseShape:
    def test_the_failure_reason_is_not_disclosed(self, client: TestClient) -> None:
        """A missing key and a wrong key must be indistinguishable to a prober."""
        missing = client.get("/v1/relays")
        wrong = client.get("/v1/relays", headers={"X-API-Key": "nope"})
        assert missing.status_code == wrong.status_code
        assert missing.json() == wrong.json()

    def test_the_error_body_does_not_echo_the_expected_key(self, client: TestClient) -> None:
        response = client.get("/v1/relays", headers={"X-API-Key": "nope"})
        assert VALID_KEY not in response.text

    def test_the_error_body_does_not_echo_the_supplied_key(self, client: TestClient) -> None:
        """Reflecting input into an error message invites log-poisoning."""
        response = client.get("/v1/relays", headers={"X-API-Key": "supplied-secret-value"})
        assert "supplied-secret-value" not in response.text


class TestBruteForceProtection:
    @pytest.fixture
    def strict_client(self, relay_service: RelayService) -> TestClient:
        settings = build_settings(auth_max_failures=3, auth_failure_window_seconds=300)
        return TestClient(create_app(settings, relay_service=relay_service))

    def test_repeated_failures_are_eventually_refused(self, strict_client: TestClient) -> None:
        with strict_client as client:
            for _ in range(3):
                assert client.get("/v1/relays").status_code == HTTPStatus.UNAUTHORIZED

            assert client.get("/v1/relays").status_code == HTTPStatus.TOO_MANY_REQUESTS

    def test_a_blocked_client_is_refused_even_with_the_correct_key(
        self, strict_client: TestClient
    ) -> None:
        """Otherwise the limiter would be trivially bypassed by guessing correctly."""
        with strict_client as client:
            for _ in range(3):
                client.get("/v1/relays")

            response = client.get("/v1/relays", headers=RELAY_HEADERS)
            assert response.status_code == HTTPStatus.TOO_MANY_REQUESTS

    def test_success_clears_the_failure_count(self, strict_client: TestClient) -> None:
        with strict_client as client:
            for _ in range(2):
                client.get("/v1/relays")

            assert client.get("/v1/relays", headers=RELAY_HEADERS).status_code == HTTPStatus.OK

            # The allowance is restored, so two more failures are tolerated.
            for _ in range(2):
                assert client.get("/v1/relays").status_code == HTTPStatus.UNAUTHORIZED

    def test_health_stays_reachable_for_a_blocked_client(self, strict_client: TestClient) -> None:
        """Monitoring must not be collateral damage of a brute-force attempt."""
        with strict_client as client:
            for _ in range(4):
                client.get("/v1/relays")

            assert client.get("/health").status_code == HTTPStatus.OK
