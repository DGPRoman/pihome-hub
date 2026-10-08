"""Turning a relay's automation off and back on, over HTTP.

The engine tests show a rule leaving a relay alone. These show the rest of the
contract a client is built against: the field on every relay read, the route that
changes it, what that route does to a light and to a hold, and that the choice is
still there after the hub restarts.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pihome_hub.app import create_app
from pihome_hub.config import Settings
from pihome_hub.relays import RelayService
from pihome_hub.storage import prepare_database
from tests.conftest import (
    RELAY_HEADERS,
    SENSOR_HEADERS,
    CountingRelayBackend,
    build_relay_service,
    build_settings,
)

SENSORS_YAML = """
devices:
  - id: porch-motion
    label: "Porch motion"
    stale_after_seconds: 300
"""

AUTOMATION_YAML = """
rules:
  - id: porch-motion-light
    when:
      device: porch-motion
      motion: true
    then:
      relay: porch-light
      state: on
      hold_seconds: 0.05
"""

#: Must match the rule above.
HOLD_SECONDS = 0.05
PORCH_PIN = 17


@pytest.fixture
def relay_writes() -> CountingRelayBackend:
    return CountingRelayBackend()


@pytest.fixture
def relays(relay_writes: CountingRelayBackend) -> RelayService:
    return build_relay_service(relay_writes)


@pytest.fixture
def wired_settings(tmp_path: Path) -> Settings:
    """One sensor, one rule with a hold, and a prepared database."""
    sensors = tmp_path / "sensors.yaml"
    sensors.write_text(SENSORS_YAML, encoding="utf-8")
    automation = tmp_path / "automation.yaml"
    automation.write_text(AUTOMATION_YAML, encoding="utf-8")

    settings = build_settings(sensor_config_path=sensors, automation_config_path=automation)
    prepare_database(settings.database_path)
    return settings


@pytest.fixture
def client(wired_settings: Settings, relays: RelayService) -> Iterator[TestClient]:
    with TestClient(create_app(wired_settings, relay_service=relays)) as wired:
        yield wired


def set_automatic(client: TestClient, *, automatic: bool) -> dict[str, object]:
    response = client.put(
        "/v1/relays/porch-light/automatic", json={"automatic": automatic}, headers=RELAY_HEADERS
    )
    assert response.status_code == HTTPStatus.OK, response.text
    body: dict[str, object] = response.json()
    return body


def read_relay(client: TestClient) -> dict[str, object]:
    body: dict[str, object] = client.get("/v1/relays/porch-light", headers=RELAY_HEADERS).json()
    return body


def push_motion(client: TestClient, *, motion: bool) -> None:
    response = client.post(
        "/v1/sensors/porch-motion/readings", json={"motion": motion}, headers=SENSOR_HEADERS
    )
    assert response.status_code == HTTPStatus.ACCEPTED


def wait_past_the_hold() -> None:
    """Long enough that a live countdown would have fired several times over."""
    time.sleep(HOLD_SECONDS * 10)


class TestTheField:
    def test_every_relay_starts_automatic(self, client: TestClient) -> None:
        relays = client.get("/v1/relays", headers=RELAY_HEADERS).json()["relays"]

        assert [relay["automatic"] for relay in relays] == [True, True]

    def test_the_collection_reports_a_relay_turned_off(self, client: TestClient) -> None:
        set_automatic(client, automatic=False)

        relays = client.get("/v1/relays", headers=RELAY_HEADERS).json()["relays"]

        assert {relay["id"]: relay["automatic"] for relay in relays} == {
            "porch-light": False,
            "gate-light": True,
        }

    def test_a_relay_write_reports_it_too(self, client: TestClient) -> None:
        """Every RelayState carries it, including the one a switch answers with."""
        set_automatic(client, automatic=False)

        response = client.put("/v1/relays/porch-light", json={"on": True}, headers=RELAY_HEADERS)

        assert response.json()["automatic"] is False


class TestTurningItOff:
    def test_answers_with_the_relay(self, client: TestClient) -> None:
        body = set_automatic(client, automatic=False)

        assert body == {
            "id": "porch-light",
            "label": "Porch light",
            "on": False,
            "automatic": False,
            "hold_expires_at": None,
        }
        assert read_relay(client)["automatic"] is False

    def test_switches_a_lit_relay_off(self, client: TestClient, relays: RelayService) -> None:
        client.put("/v1/relays/porch-light", json={"on": True}, headers=RELAY_HEADERS)

        body = set_automatic(client, automatic=False)

        assert body["on"] is False
        assert relays.state_of("porch-light") is False

    def test_leaves_other_relays_alone(self, client: TestClient, relays: RelayService) -> None:
        client.put("/v1/relays", json={"on": True}, headers=RELAY_HEADERS)

        set_automatic(client, automatic=False)

        assert relays.state_of("gate-light") is True
        assert client.get("/v1/relays/gate-light", headers=RELAY_HEADERS).json()["automatic"]

    def test_releases_the_hold(
        self, client: TestClient, relay_writes: CountingRelayBackend
    ) -> None:
        push_motion(client, motion=True)
        assert read_relay(client)["hold_expires_at"] is not None

        assert set_automatic(client, automatic=False)["hold_expires_at"] is None
        wait_past_the_hold()

        assert read_relay(client)["hold_expires_at"] is None
        assert relay_writes.writes_to(PORCH_PIN) == [True, False], "the hold acted anyway"

    def test_motion_no_longer_switches_the_relay_on(
        self, client: TestClient, relay_writes: CountingRelayBackend
    ) -> None:
        set_automatic(client, automatic=False)

        push_motion(client, motion=True)

        assert read_relay(client)["on"] is False
        assert relay_writes.writes_to(PORCH_PIN) == []

    def test_a_person_can_still_switch_it_by_hand(
        self, client: TestClient, relays: RelayService
    ) -> None:
        set_automatic(client, automatic=False)

        client.put("/v1/relays/porch-light", json={"on": True}, headers=RELAY_HEADERS)
        push_motion(client, motion=True)
        push_motion(client, motion=False)

        assert relays.state_of("porch-light") is True
        assert read_relay(client)["automatic"] is False, "switching by hand is not handing back"

    def test_is_idempotent(self, client: TestClient) -> None:
        assert set_automatic(client, automatic=False) == set_automatic(client, automatic=False)


class TestTurningItBackOn:
    def test_switches_nothing(self, client: TestClient, relay_writes: CountingRelayBackend) -> None:
        set_automatic(client, automatic=False)
        client.put("/v1/relays/porch-light", json={"on": True}, headers=RELAY_HEADERS)

        body = set_automatic(client, automatic=True)

        assert body["automatic"] is True
        assert body["on"] is True
        assert relay_writes.writes_to(PORCH_PIN) == [True]

    def test_the_rules_act_on_the_next_change(
        self, client: TestClient, relays: RelayService
    ) -> None:
        set_automatic(client, automatic=False)
        push_motion(client, motion=True)
        push_motion(client, motion=False)

        set_automatic(client, automatic=True)
        push_motion(client, motion=True)

        assert relays.state_of("porch-light") is True
        assert read_relay(client)["hold_expires_at"] is not None


class TestItSurvivesARestart:
    def test_a_new_app_over_the_same_database_still_has_it_off(
        self, wired_settings: Settings, relays: RelayService
    ) -> None:
        with TestClient(create_app(wired_settings, relay_service=relays)) as before:
            set_automatic(before, automatic=False)

        with TestClient(create_app(wired_settings, relay_service=relays)) as after:
            assert read_relay(after)["automatic"] is False
            push_motion(after, motion=True)
            assert relays.state_of("porch-light") is False, "a rule acted after the restart"

    def test_turning_it_back_on_survives_a_restart_too(
        self, wired_settings: Settings, relays: RelayService
    ) -> None:
        with TestClient(create_app(wired_settings, relay_service=relays)) as before:
            set_automatic(before, automatic=False)
            set_automatic(before, automatic=True)

        with TestClient(create_app(wired_settings, relay_service=relays)) as after:
            assert read_relay(after)["automatic"] is True


class TestTheRequest:
    def test_an_unknown_relay_is_a_404(self, client: TestClient) -> None:
        response = client.put(
            "/v1/relays/ghost-relay/automatic", json={"automatic": False}, headers=RELAY_HEADERS
        )

        assert response.status_code == HTTPStatus.NOT_FOUND
        assert "ghost-relay" in response.json()["detail"]

    def test_missing_body_is_a_422(self, client: TestClient) -> None:
        response = client.put("/v1/relays/porch-light/automatic", headers=RELAY_HEADERS)

        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    def test_unknown_field_is_rejected(self, client: TestClient) -> None:
        response = client.put(
            "/v1/relays/porch-light/automatic",
            json={"automatic": False, "for_minutes": 30},
            headers=RELAY_HEADERS,
        )

        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    @pytest.mark.parametrize("bad", ["false", 0, None, [], {}])
    def test_non_boolean_value_is_rejected(self, client: TestClient, bad: object) -> None:
        response = client.put(
            "/v1/relays/porch-light/automatic", json={"automatic": bad}, headers=RELAY_HEADERS
        )

        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    def test_the_sensor_key_cannot_change_it(self, client: TestClient) -> None:
        """Firmware that could take a light out of the rules could keep it dark."""
        response = client.put(
            "/v1/relays/porch-light/automatic", json={"automatic": False}, headers=SENSOR_HEADERS
        )

        assert response.status_code == HTTPStatus.UNAUTHORIZED
        assert read_relay(client)["automatic"] is True

    def test_reads_do_not_accept_it(self, client: TestClient) -> None:
        response = client.get("/v1/relays/porch-light/automatic", headers=RELAY_HEADERS)

        assert response.status_code == HTTPStatus.METHOD_NOT_ALLOWED


class TestWhenTheHubCannotKeepIt:
    def test_the_change_is_a_503_and_changes_nothing(self, tmp_path: Path) -> None:
        """Never prepared: the choice cannot be stored, so it is not acted on either."""
        relays = build_relay_service()
        relays.turn_on("porch-light")
        settings = build_settings(database_path=tmp_path / "unprepared" / "hub.db")

        with TestClient(create_app(settings, relay_service=relays)) as client:
            response = client.put(
                "/v1/relays/porch-light/automatic",
                json={"automatic": False},
                headers=RELAY_HEADERS,
            )

            assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
            assert read_relay(client)["automatic"] is True
            assert relays.state_of("porch-light") is True

    def test_with_no_engine_running_it_is_a_503(self, relays: RelayService) -> None:
        """The window around shutdown, when the lifespan has closed the engine. A
        client told "not now" can try again; one told 500 is told the hub is broken."""
        relays.turn_on("porch-light")
        settings = build_settings()
        prepare_database(settings.database_path)
        # Outside a `with` block the lifespan never runs, so no engine is built.
        client = TestClient(create_app(settings, relay_service=relays))

        response = client.put(
            "/v1/relays/porch-light/automatic", json={"automatic": False}, headers=RELAY_HEADERS
        )

        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert relays.state_of("porch-light") is True
