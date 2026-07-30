"""The /v1/relays REST surface."""

from __future__ import annotations

from collections.abc import Iterator
from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient

from pihome_hub.app import create_app
from pihome_hub.relays import MockRelayBackend, RelayConfig, RelayService
from tests.conftest import RELAY_HEADERS, build_settings


@pytest.fixture
def api(client: TestClient) -> TestClient:
    """A client that sends a valid relay key on every request."""
    client.headers.update(RELAY_HEADERS)
    return client


@pytest.fixture
def client_with_relay_named_toggle() -> Iterator[TestClient]:
    """A client whose app has a relay literally named `toggle`."""
    app = create_app(
        build_settings(),
        relay_service=RelayService(
            MockRelayBackend(),
            [
                RelayConfig(id="toggle", pin=17, label="Awkwardly named"),
                RelayConfig(id="gate-light", pin=27, label="Gate light"),
            ],
        ),
    )
    with TestClient(app, headers=RELAY_HEADERS) as scoped:
        yield scoped


class TestListRelays:
    def test_returns_every_configured_relay(self, api: TestClient) -> None:
        body = api.get("/v1/relays").json()
        assert body == {
            "relays": [
                {"id": "porch-light", "label": "Porch light", "on": False},
                {"id": "gate-light", "label": "Gate light", "on": False},
            ]
        }

    def test_preserves_configuration_order(self, api: TestClient) -> None:
        ids = [relay["id"] for relay in api.get("/v1/relays").json()["relays"]]
        assert ids == ["porch-light", "gate-light"]


class TestReadOneRelay:
    def test_returns_the_relay(self, api: TestClient) -> None:
        body = api.get("/v1/relays/porch-light").json()
        assert body == {"id": "porch-light", "label": "Porch light", "on": False}

    def test_unknown_relay_is_a_404(self, api: TestClient) -> None:
        response = api.get("/v1/relays/ghost-relay")
        assert response.status_code == HTTPStatus.NOT_FOUND
        assert "ghost-relay" in response.json()["detail"]


class TestSetOneRelay:
    def test_energises_the_circuit(self, api: TestClient, relay_backend: MockRelayBackend) -> None:
        body = api.put("/v1/relays/porch-light", json={"on": True}).json()
        assert body["on"] is True
        assert relay_backend.is_on(17) is True

    def test_de_energises_the_circuit(
        self, api: TestClient, relay_backend: MockRelayBackend
    ) -> None:
        api.put("/v1/relays/porch-light", json={"on": True})
        body = api.put("/v1/relays/porch-light", json={"on": False}).json()
        assert body["on"] is False
        assert relay_backend.is_on(17) is False

    def test_is_idempotent(self, api: TestClient) -> None:
        first = api.put("/v1/relays/porch-light", json={"on": True}).json()
        second = api.put("/v1/relays/porch-light", json={"on": True}).json()
        assert first == second

    def test_leaves_other_relays_alone(self, api: TestClient) -> None:
        api.put("/v1/relays/porch-light", json={"on": True})
        assert api.get("/v1/relays/gate-light").json()["on"] is False

    def test_unknown_relay_is_a_404(self, api: TestClient) -> None:
        assert (
            api.put("/v1/relays/ghost-relay", json={"on": True}).status_code == HTTPStatus.NOT_FOUND
        )

    def test_missing_body_is_a_422(self, api: TestClient) -> None:
        assert api.put("/v1/relays/porch-light").status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    def test_unknown_field_is_rejected(self, api: TestClient) -> None:
        response = api.put("/v1/relays/porch-light", json={"on": True, "surprise": 1})
        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    @pytest.mark.parametrize("bad", ["yes", 2, None, [], {}])
    def test_non_boolean_state_is_rejected(self, api: TestClient, bad: object) -> None:
        response = api.put("/v1/relays/porch-light", json={"on": bad})
        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


class TestToggleOneRelay:
    def test_inverts_the_relay(self, api: TestClient) -> None:
        assert api.post("/v1/relays/porch-light/toggle").json()["on"] is True
        assert api.post("/v1/relays/porch-light/toggle").json()["on"] is False

    def test_unknown_relay_is_a_404(self, api: TestClient) -> None:
        assert api.post("/v1/relays/ghost-relay/toggle").status_code == HTTPStatus.NOT_FOUND


class TestGroupOperations:
    def test_set_all_on(self, api: TestClient) -> None:
        body = api.put("/v1/relays", json={"on": True}).json()
        assert all(relay["on"] for relay in body["relays"])

    def test_set_all_off(self, api: TestClient) -> None:
        api.put("/v1/relays", json={"on": True})
        body = api.put("/v1/relays", json={"on": False}).json()
        assert not any(relay["on"] for relay in body["relays"])

    def test_toggle_all_inverts_each_relay_independently(
        self, api: TestClient, relay_service: RelayService
    ) -> None:
        api.put("/v1/relays/porch-light", json={"on": True})

        body = api.post("/v1/relays/toggle").json()

        states = {relay["id"]: relay["on"] for relay in body["relays"]}
        assert states == {"porch-light": False, "gate-light": True}

    def test_toggle_is_not_shadowed_by_the_relay_id_route(self, api: TestClient) -> None:
        """`/v1/relays/toggle` must reach the group route, not be read as a relay id."""
        assert api.post("/v1/relays/toggle").status_code == HTTPStatus.OK


class TestMethodDiscipline:
    def test_toggle_rejects_get(self, api: TestClient) -> None:
        assert api.get("/v1/relays/porch-light/toggle").status_code == HTTPStatus.METHOD_NOT_ALLOWED

    def test_reads_do_not_accept_post(self, api: TestClient) -> None:
        assert api.post("/v1/relays").status_code == HTTPStatus.METHOD_NOT_ALLOWED


class TestPathHandling:
    def test_a_relay_may_be_named_toggle_without_shadowing_the_group_route(
        self, client_with_relay_named_toggle: TestClient
    ) -> None:
        """`toggle` is a legal relay id; the group route must still be reachable."""
        client = client_with_relay_named_toggle
        assert client.get("/v1/relays/toggle").json()["id"] == "toggle"
        assert client.post("/v1/relays/toggle/toggle").json()["id"] == "toggle"
        assert "relays" in client.post("/v1/relays/toggle").json()

    def test_trailing_slash_is_not_an_unauthenticated_route_oracle(
        self, client: TestClient
    ) -> None:
        """Slash redirection would 307 a real path and 404 a fake one, before auth runs."""
        real = client.get("/v1/relays/", follow_redirects=False)
        fake = client.get("/v1/nonexistent/", follow_redirects=False)
        assert real.status_code == fake.status_code == HTTPStatus.NOT_FOUND
