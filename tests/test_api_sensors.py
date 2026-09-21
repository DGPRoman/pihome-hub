"""The /v1/sensors surface, and the key split it exists to enforce."""

from __future__ import annotations

import time
from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pihome_hub.app import create_app
from pihome_hub.relays import RelayService
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
    """The backend behind ``wired``, for tests that need to see commands issued.

    A relay already off and driven off again looks identical afterwards, so some
    of what happens at this seam is only visible in the writes.
    """
    return CountingRelayBackend()


@pytest.fixture
def wired(
    tmp_path: Path, relay_writes: CountingRelayBackend
) -> Iterator[tuple[TestClient, RelayService]]:
    """An app with one sensor and one automation rule, on a mock relay backend."""
    sensors = tmp_path / "sensors.yaml"
    sensors.write_text(SENSORS_YAML, encoding="utf-8")
    automation = tmp_path / "automation.yaml"
    automation.write_text(AUTOMATION_YAML, encoding="utf-8")

    relays = build_relay_service(relay_writes)
    settings = build_settings(
        sensor_config_path=sensors,
        automation_config_path=automation,
    )
    with TestClient(create_app(settings, relay_service=relays)) as client:
        yield client, relays


class TestIngestion:
    def test_a_reading_is_accepted(self, wired: tuple[TestClient, RelayService]) -> None:
        client, _ = wired
        response = client.post(
            "/v1/sensors/porch-motion/readings", json={"motion": True}, headers=SENSOR_HEADERS
        )
        assert response.status_code == HTTPStatus.ACCEPTED

    def test_the_response_carries_no_house_state(
        self, wired: tuple[TestClient, RelayService]
    ) -> None:
        """The sensor key does not grant reads, so ingestion must not leak them back."""
        client, _ = wired
        response = client.post(
            "/v1/sensors/porch-motion/readings", json={"motion": True}, headers=SENSOR_HEADERS
        )
        assert response.content == b""

    def test_an_unknown_device_is_a_404(self, wired: tuple[TestClient, RelayService]) -> None:
        client, _ = wired
        response = client.post(
            "/v1/sensors/ghost/readings", json={"motion": True}, headers=SENSOR_HEADERS
        )
        assert response.status_code == HTTPStatus.NOT_FOUND

    def test_an_empty_reading_is_rejected(self, wired: tuple[TestClient, RelayService]) -> None:
        client, _ = wired
        response = client.post("/v1/sensors/porch-motion/readings", json={}, headers=SENSOR_HEADERS)
        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    @pytest.mark.parametrize("bad", ["yes", 1, "true"])
    def test_a_non_boolean_motion_is_rejected(
        self, wired: tuple[TestClient, RelayService], bad: object
    ) -> None:
        client, _ = wired
        response = client.post(
            "/v1/sensors/porch-motion/readings", json={"motion": bad}, headers=SENSOR_HEADERS
        )
        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    def test_several_quantities_in_one_request(
        self, wired: tuple[TestClient, RelayService]
    ) -> None:
        client, _ = wired
        response = client.post(
            "/v1/sensors/porch-motion/readings",
            json={"motion": False, "temperature": 21.5, "humidity": 48},
            headers=SENSOR_HEADERS,
        )
        assert response.status_code == HTTPStatus.ACCEPTED

        body = client.get("/v1/sensors/porch-motion", headers=RELAY_HEADERS).json()
        assert body["temperature"] == 21.5
        assert body["humidity"] == 48.0


class TestReading:
    def test_a_silent_device_is_listed_as_stale(
        self, wired: tuple[TestClient, RelayService]
    ) -> None:
        client, _ = wired
        body = client.get("/v1/sensors", headers=RELAY_HEADERS).json()
        assert body["sensors"] == [
            {
                "id": "porch-motion",
                "label": "Porch motion",
                "stale": True,
                # The window the flag was decided against, so a client can judge a
                # single quantity for itself rather than trusting one flag for the
                # whole device.
                "stale_after_seconds": 300.0,
                "last_seen": None,
                "motion": None,
                "motion_updated_at": None,
                "temperature": None,
                "humidity": None,
                "climate_updated_at": None,
            }
        ]

    def test_a_reported_device_is_not_stale(self, wired: tuple[TestClient, RelayService]) -> None:
        client, _ = wired
        client.post(
            "/v1/sensors/porch-motion/readings", json={"motion": True}, headers=SENSOR_HEADERS
        )

        body = client.get("/v1/sensors/porch-motion", headers=RELAY_HEADERS).json()
        assert body["stale"] is False
        assert body["motion"] is True
        assert body["last_seen"] is not None

    def test_an_unknown_device_is_a_404(self, wired: tuple[TestClient, RelayService]) -> None:
        client, _ = wired
        assert (
            client.get("/v1/sensors/ghost", headers=RELAY_HEADERS).status_code
            == HTTPStatus.NOT_FOUND
        )


class TestKeySeparation:
    """The whole point of two keys: neither can do the other's job."""

    def test_the_relay_key_cannot_push_readings(
        self, wired: tuple[TestClient, RelayService]
    ) -> None:
        client, _ = wired
        response = client.post(
            "/v1/sensors/porch-motion/readings", json={"motion": True}, headers=RELAY_HEADERS
        )
        assert response.status_code == HTTPStatus.UNAUTHORIZED

    def test_the_sensor_key_cannot_read_the_house(
        self, wired: tuple[TestClient, RelayService]
    ) -> None:
        client, _ = wired
        assert (
            client.get("/v1/sensors", headers=SENSOR_HEADERS).status_code == HTTPStatus.UNAUTHORIZED
        )

    def test_the_sensor_key_cannot_drive_relays_directly(
        self, wired: tuple[TestClient, RelayService]
    ) -> None:
        client, _ = wired
        assert (
            client.post("/v1/relays/porch-light/toggle", headers=SENSOR_HEADERS).status_code
            == HTTPStatus.UNAUTHORIZED
        )

    def test_ingestion_needs_a_key(self, wired: tuple[TestClient, RelayService]) -> None:
        client, _ = wired
        response = client.post("/v1/sensors/porch-motion/readings", json={"motion": True})
        assert response.status_code == HTTPStatus.UNAUTHORIZED

    def test_a_rejected_reading_does_not_reach_the_store(
        self, wired: tuple[TestClient, RelayService]
    ) -> None:
        client, _ = wired
        client.post("/v1/sensors/porch-motion/readings", json={"motion": True})

        body = client.get("/v1/sensors/porch-motion", headers=RELAY_HEADERS).json()
        assert body["motion"] is None


class TestAutomationOverHttp:
    def test_a_motion_reading_switches_the_relay(
        self, wired: tuple[TestClient, RelayService]
    ) -> None:
        client, relays = wired
        assert relays.state_of("porch-light") is False

        client.post(
            "/v1/sensors/porch-motion/readings", json={"motion": True}, headers=SENSOR_HEADERS
        )

        assert relays.state_of("porch-light") is True

    def test_the_relay_reverts_once_the_hold_expires(
        self, wired: tuple[TestClient, RelayService]
    ) -> None:
        """The timer is an asyncio task on the server's loop; it must survive the
        request that scheduled it having already returned."""
        client, relays = wired
        client.post(
            "/v1/sensors/porch-motion/readings", json={"motion": True}, headers=SENSOR_HEADERS
        )
        assert relays.state_of("porch-light") is True

        deadline = time.monotonic() + 2.0
        while relays.state_of("porch-light") and time.monotonic() < deadline:
            time.sleep(0.01)

        assert relays.state_of("porch-light") is False

    def test_motion_false_does_not_fire_the_rule(
        self, wired: tuple[TestClient, RelayService]
    ) -> None:
        client, relays = wired
        client.post(
            "/v1/sensors/porch-motion/readings", json={"motion": False}, headers=SENSOR_HEADERS
        )
        assert relays.state_of("porch-light") is False

    def test_a_climate_only_reading_does_not_fire_the_rule(
        self, wired: tuple[TestClient, RelayService]
    ) -> None:
        client, relays = wired
        client.post(
            "/v1/sensors/porch-motion/readings",
            json={"temperature": 20.0},
            headers=SENSOR_HEADERS,
        )
        assert relays.state_of("porch-light") is False


def push_motion(client: TestClient, *, motion: bool) -> None:
    response = client.post(
        "/v1/sensors/porch-motion/readings", json={"motion": motion}, headers=SENSOR_HEADERS
    )
    assert response.status_code == HTTPStatus.ACCEPTED


def set_relay(client: TestClient, *, on: bool) -> dict[str, object]:
    response = client.put("/v1/relays/porch-light", json={"on": on}, headers=RELAY_HEADERS)
    assert response.status_code == HTTPStatus.OK
    body: dict[str, object] = response.json()
    return body


def read_relay(client: TestClient) -> dict[str, object]:
    body: dict[str, object] = client.get("/v1/relays/porch-light", headers=RELAY_HEADERS).json()
    return body


def wait_past_the_hold() -> None:
    """Long enough that a live countdown would have fired several times over."""
    time.sleep(HOLD_SECONDS * 10)


class TestOperatorVersusAutomation:
    """Where the two intents collide.

    The engine tests drive relays through the engine; the relay tests go over HTTP
    and never involve a rule. Neither reaches the case that matters — an operator
    writing to a relay that a rule is already holding — so both could pass while
    the house quietly ignored whoever was standing in it.
    """

    def test_an_operator_write_survives_the_hold(
        self, wired: tuple[TestClient, RelayService]
    ) -> None:
        """Motion fires the rule, the operator says "stay on", the hold expires.

        The hold reverts to the inverse of what the rule applied. Saying "on" to a
        light a rule has just switched on is not a no-op: it is the only way to
        tell the house to keep it, and it used to be undone a few seconds later.
        """
        client, relays = wired
        push_motion(client, motion=True)
        assert relays.state_of("porch-light") is True

        set_relay(client, on=True)
        wait_past_the_hold()

        assert relays.state_of("porch-light") is True, "the hold undid the operator"

    def test_an_operator_write_clears_the_pending_hold(
        self, wired: tuple[TestClient, RelayService]
    ) -> None:
        """Visible over the API, which is where a client has to see it."""
        client, _ = wired
        push_motion(client, motion=True)
        assert read_relay(client)["hold_expires_at"] is not None

        assert set_relay(client, on=True)["hold_expires_at"] is None
        assert read_relay(client)["hold_expires_at"] is None

    def test_an_operator_switching_it_off_is_not_reverted_either(
        self, wired: tuple[TestClient, RelayService], relay_writes: CountingRelayBackend
    ) -> None:
        """The other direction, where the state cannot tell the two apart.

        The revert would drive the relay off — which is where the operator already
        put it. Only the command distinguishes the house obeying them from the
        house reaching the same place by overruling them, and the difference shows
        the moment the rule's inverse is the opposite value.
        """
        client, relays = wired
        push_motion(client, motion=True)
        set_relay(client, on=False)

        wait_past_the_hold()

        assert relays.state_of("porch-light") is False
        assert relay_writes.writes_to(PORCH_PIN) == [True, False], "the hold acted anyway"

    def test_an_unchanged_reading_after_an_operator_write_changes_nothing(
        self, wired: tuple[TestClient, RelayService], relay_writes: CountingRelayBackend
    ) -> None:
        """The reporting interval, end to end.

        The sensor has seen no change and says so again. Before edge detection this
        drove the light straight back on, which meant no manual switch survived
        longer than one heartbeat.
        """
        client, relays = wired
        push_motion(client, motion=True)
        set_relay(client, on=False)

        push_motion(client, motion=True)

        assert relays.state_of("porch-light") is False
        assert relay_writes.writes_to(PORCH_PIN) == [True, False]

    def test_a_rule_still_fires_after_an_operator_write(
        self, wired: tuple[TestClient, RelayService]
    ) -> None:
        """The reverse order. An operator's write settles the past, not the future:
        motion arriving afterwards is new information and the rule must act on it."""
        client, relays = wired
        set_relay(client, on=False)

        push_motion(client, motion=True)

        assert relays.state_of("porch-light") is True
        assert read_relay(client)["hold_expires_at"] is not None

    def test_a_rule_firing_after_an_operator_write_still_reverts(
        self, wired: tuple[TestClient, RelayService]
    ) -> None:
        """...and the hold it schedules is a real one, not a cancelled leftover."""
        client, relays = wired
        set_relay(client, on=True)

        push_motion(client, motion=True)
        deadline = time.monotonic() + 2.0
        while relays.state_of("porch-light") and time.monotonic() < deadline:
            time.sleep(0.01)

        assert relays.state_of("porch-light") is False

    def test_a_write_to_every_relay_clears_the_hold_too(
        self, wired: tuple[TestClient, RelayService]
    ) -> None:
        """The group routes are a separate way of saying the same thing, and an
        operator reaching for "all on" means it no less than one who names a relay."""
        client, relays = wired
        push_motion(client, motion=True)

        response = client.put("/v1/relays", json={"on": True}, headers=RELAY_HEADERS)
        assert response.status_code == HTTPStatus.OK
        wait_past_the_hold()

        assert relays.state_of("porch-light") is True, "the hold undid a group write"

    def test_a_toggle_clears_the_hold_too(self, wired: tuple[TestClient, RelayService]) -> None:
        """Twice, deliberately.

        One toggle always lands on the opposite of what the rule applied, so the
        revert's guard alone would save it and this would pass with the release
        gone. Two toggles put the relay back where the rule left it, and then only
        the release stands between the operator and a countdown they never saw.
        """
        client, relays = wired
        push_motion(client, motion=True)

        for _ in range(2):
            response = client.post("/v1/relays/porch-light/toggle", headers=RELAY_HEADERS)
            assert response.status_code == HTTPStatus.OK
        assert relays.state_of("porch-light") is True

        wait_past_the_hold()

        assert relays.state_of("porch-light") is True, "the hold undid the operator"

    def test_a_group_toggle_clears_the_hold_too(
        self, wired: tuple[TestClient, RelayService]
    ) -> None:
        client, _ = wired
        push_motion(client, motion=True)
        assert read_relay(client)["hold_expires_at"] is not None

        client.post("/v1/relays/toggle", headers=RELAY_HEADERS)

        assert read_relay(client)["hold_expires_at"] is None
