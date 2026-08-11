"""The /v1/automation surface."""

from __future__ import annotations

from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pihome_hub.app import create_app
from tests.conftest import RELAY_HEADERS, SENSOR_HEADERS, build_relay_service, build_settings

SENSORS_YAML = """
devices:
  - id: porch-motion
    label: "Porch motion"
    stale_after_seconds: 300
"""

AUTOMATION_YAML = """
location:
  latitude: 50.45
  longitude: 30.52
  timezone: Europe/Kyiv
rules:
  - id: porch-motion-light
    when:
      device: porch-motion
      motion: true
    then:
      relay: porch-light
      state: on
      hold_seconds: 120
  - id: gate-motion-light
    enabled: false
    when:
      device: porch-motion
      motion: true
    then:
      relay: gate-light
      state: off
"""


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """An app with one enabled rule, one disabled rule, and a location."""
    sensors = tmp_path / "sensors.yaml"
    sensors.write_text(SENSORS_YAML, encoding="utf-8")
    automation = tmp_path / "automation.yaml"
    automation.write_text(AUTOMATION_YAML, encoding="utf-8")

    settings = build_settings(sensor_config_path=sensors, automation_config_path=automation)
    with TestClient(create_app(settings, relay_service=build_relay_service())) as app_client:
        yield app_client


class TestListRules:
    def test_every_rule_is_reported(self, client: TestClient) -> None:
        response = client.get("/v1/automation/rules", headers=RELAY_HEADERS)

        assert response.status_code == HTTPStatus.OK
        ids = [rule["id"] for rule in response.json()["rules"]]
        assert ids == ["porch-motion-light", "gate-motion-light"]

    def test_a_disabled_rule_is_reported_as_disabled_not_omitted(self, client: TestClient) -> None:
        rules = {
            rule["id"]: rule
            for rule in client.get("/v1/automation/rules", headers=RELAY_HEADERS).json()["rules"]
        }

        assert rules["porch-motion-light"]["enabled"] is True
        assert rules["gate-motion-light"]["enabled"] is False

    def test_a_rule_carries_its_trigger_and_action(self, client: TestClient) -> None:
        rule = client.get("/v1/automation/rules", headers=RELAY_HEADERS).json()["rules"][0]

        assert rule["when"] == {"device": "porch-motion", "motion": True}
        assert rule["then"] == {"relay": "porch-light", "state": "on", "hold_seconds": 120.0}

    def test_the_location_is_not_exposed(self, client: TestClient) -> None:
        # Coordinates identify a home, and listing rules does not need them.
        response = client.get("/v1/automation/rules", headers=RELAY_HEADERS)

        assert "location" not in response.json()
        assert "50.45" not in response.text

    def test_an_empty_file_yields_an_empty_list(self, tmp_path: Path) -> None:
        settings = build_settings(automation_config_path=tmp_path / "absent.yaml")
        with TestClient(create_app(settings, relay_service=build_relay_service())) as client:
            response = client.get("/v1/automation/rules", headers=RELAY_HEADERS)

        assert response.json() == {"rules": []}


class TestAuthentication:
    def test_the_relay_key_is_required(self, client: TestClient) -> None:
        assert client.get("/v1/automation/rules").status_code == HTTPStatus.UNAUTHORIZED

    def test_the_sensor_key_is_refused(self, client: TestClient) -> None:
        # Firmware that pushes readings has no business reading the house's wiring.
        response = client.get("/v1/automation/rules", headers=SENSOR_HEADERS)

        assert response.status_code == HTTPStatus.UNAUTHORIZED
