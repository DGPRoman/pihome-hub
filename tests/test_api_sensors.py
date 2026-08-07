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
from tests.conftest import RELAY_HEADERS, SENSOR_HEADERS, build_relay_service, build_settings

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


@pytest.fixture
def wired(tmp_path: Path) -> Iterator[tuple[TestClient, RelayService]]:
    """An app with one sensor and one automation rule, on a mock relay backend."""
    sensors = tmp_path / "sensors.yaml"
    sensors.write_text(SENSORS_YAML, encoding="utf-8")
    automation = tmp_path / "automation.yaml"
    automation.write_text(AUTOMATION_YAML, encoding="utf-8")

    relays = build_relay_service()
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
