"""Authentication behaviour of the v1 API."""

from __future__ import annotations

import re
from http import HTTPStatus
from typing import Final

import pytest
from fastapi.testclient import TestClient

from pihome_hub.app import create_app
from pihome_hub.relays import RelayService
from tests.conftest import RELAY_HEADERS, SENSOR_HEADERS, VALID_KEY, build_settings

#: Routes under /v1 that deliberately take no credential, each with the reason it is
#: allowed to. Written as a mapping so a hole cannot be punched without stating why,
#: and checked below against the routes that actually exist so one cannot go stale.
_ANONYMOUS_BY_DESIGN: Final = {
    ("POST", "/v1/session"): (
        "the way in. Requiring a credential of it would leave the door reachable only "
        "by callers who already have another way through"
    ),
    ("DELETE", "/v1/session"): (
        "logging out clears an HttpOnly cookie, and the server is the only thing that "
        "can clear one. Answering 401 to an expired session would strand it in the "
        "browser until its max-age ran out. It reads nothing and, with no cookie to "
        "act on, changes nothing"
    ),
}


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

        One exemption: ``POST /v1/session`` is the way in, so requiring a credential
        of it would leave the door reachable only by callers already through it. It is
        named here rather than pattern-matched, so a second anonymous route is a
        decision somebody has to write down.
        """
        versioned = [
            (m, p)
            for m, p in registered_routes
            if p.startswith("/v1") and (m, p) not in _ANONYMOUS_BY_DESIGN
        ]
        assert versioned, "no /v1 routes found to check"

        unguarded = []
        for method, path in versioned:
            # Substitute every placeholder, not just the ones that exist today. An
            # unsubstituted "{device_id}" happens to match its own route as a literal
            # segment, so a narrower substitution would still pass — by coincidence
            # rather than by covering the route.
            concrete = re.sub(r"\{[^}]+\}", "some-id", path)
            response = client.request(method, concrete, json={"on": True})
            if response.status_code != HTTPStatus.UNAUTHORIZED:
                unguarded.append(f"{method} {path} -> {response.status_code}")

        assert not unguarded, f"reachable without a key: {unguarded}"

    def test_every_exemption_is_still_a_route(
        self, registered_routes: list[tuple[str, str]]
    ) -> None:
        """A renamed or deleted route must not leave an unexplained hole in the sweep."""
        assert set(_ANONYMOUS_BY_DESIGN) <= set(registered_routes)

    def test_logging_out_without_a_session_changes_nothing(self, client: TestClient) -> None:
        """What makes the second exemption safe rather than merely convenient."""
        response = client.delete("/v1/session")

        assert response.status_code == HTTPStatus.NO_CONTENT
        assert not response.content

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
