"""The device routes over HTTP.

Two surfaces with two different keys, which is the whole shape of this feature: a
device says where it is, and a client reads what was found there. Neither can do the
other's half, and the tests below are mostly about that boundary holding.
"""

from __future__ import annotations

from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pihome_hub.app import check_configuration, create_app
from pihome_hub.config import Settings
from pihome_hub.devices import DeviceConfigError
from pihome_hub.relays import RelayService
from pihome_hub.storage import prepare_database
from tests.conftest import RELAY_HEADERS, SENSOR_HEADERS, VALID_KEY, build_settings

#: A third key, distinct from the other two so a scope confusion is detectable.
DEVICE_KEY = VALID_KEY[16:] + VALID_KEY[:16]
DEVICE_HEADERS = {"X-API-Key": DEVICE_KEY}

DECLARATION = """
devices:
  - id: workshop-pc
    label: "Workshop PC"
    kind: pc-power
  - id: study-pc
    label: "Study PC"
    kind: pc-power
"""

ANNOUNCEMENT: dict[str, Any] = {
    "address": "http://10.0.0.5",
    "api_key": "device-key-value-goes-here",
    "firmware": "0.1.0",
}


# object rather than Any: this only forwards what it is given, and typing it that
# way keeps the helper out of the per-file ANN401 exemptions.
def declaring(tmp_path: Path, text: str = DECLARATION, **overrides: object) -> Settings:
    path = tmp_path / "devices.yaml"
    path.write_text(text, encoding="utf-8")
    return build_settings(device_config_path=path, **overrides)


@pytest.fixture
def settings_with_devices(tmp_path: Path) -> Settings:
    return declaring(tmp_path, device_api_key=DEVICE_KEY)


@pytest.fixture
def client(settings_with_devices: Settings, relay_service: RelayService) -> Iterator[TestClient]:
    prepare_database(settings_with_devices.database_path)
    with TestClient(create_app(settings_with_devices, relay_service=relay_service)) as test_client:
        yield test_client


class TestReadingTheRegistry:
    def test_a_declared_device_is_listed_before_it_has_ever_called(
        self, client: TestClient
    ) -> None:
        response = client.get("/v1/devices", headers=RELAY_HEADERS)

        assert response.status_code == HTTPStatus.OK
        body = response.json()
        assert [device["id"] for device in body["devices"]] == ["workshop-pc", "study-pc"]
        assert body["devices"][0]["address"] is None
        assert body["devices"][0]["reachable"] is None

    def test_one_device_can_be_read_on_its_own(self, client: TestClient) -> None:
        response = client.get("/v1/devices/workshop-pc", headers=RELAY_HEADERS)

        assert response.status_code == HTTPStatus.OK
        assert response.json()["label"] == "Workshop PC"

    def test_an_undeclared_id_is_not_found(self, client: TestClient) -> None:
        response = client.get("/v1/devices/kitchen-pc", headers=RELAY_HEADERS)

        assert response.status_code == HTTPStatus.NOT_FOUND

    def test_reading_needs_a_key(self, client: TestClient) -> None:
        assert client.get("/v1/devices").status_code == HTTPStatus.UNAUTHORIZED

    def test_the_device_key_does_not_grant_reads(self, client: TestClient) -> None:
        """Announcing where you are is not permission to read the house."""
        assert client.get("/v1/devices", headers=DEVICE_HEADERS).status_code == (
            HTTPStatus.UNAUTHORIZED
        )


class TestAnnouncing:
    def test_a_device_says_where_it_is_and_is_believed(self, client: TestClient) -> None:
        response = client.post(
            "/v1/devices/workshop-pc/announcements", headers=DEVICE_HEADERS, json=ANNOUNCEMENT
        )

        assert response.status_code == HTTPStatus.NO_CONTENT
        assert not response.content

        stored = client.get("/v1/devices/workshop-pc", headers=RELAY_HEADERS).json()
        assert stored["address"] == "http://10.0.0.5"
        assert stored["firmware"] == "0.1.0"
        assert stored["announced_at"] is not None

    def test_the_announced_key_is_in_no_response(self, client: TestClient) -> None:
        client.post(
            "/v1/devices/workshop-pc/announcements", headers=DEVICE_HEADERS, json=ANNOUNCEMENT
        )

        collection = client.get("/v1/devices", headers=RELAY_HEADERS)
        single = client.get("/v1/devices/workshop-pc", headers=RELAY_HEADERS)

        assert ANNOUNCEMENT["api_key"] not in collection.text
        assert ANNOUNCEMENT["api_key"] not in single.text

    def test_an_undeclared_id_is_not_found(self, client: TestClient) -> None:
        """Which is what stops the device key being a way to make this hub call
        an address of somebody else's choosing."""
        response = client.post(
            "/v1/devices/kitchen-pc/announcements", headers=DEVICE_HEADERS, json=ANNOUNCEMENT
        )

        assert response.status_code == HTTPStatus.NOT_FOUND

    @pytest.mark.parametrize(
        "address",
        ["http://8.8.8.8", "https://10.0.0.5", "http://pc.local", "http://10.0.0.5/v1/power"],
    )
    def test_an_address_this_hub_will_not_call_is_refused(
        self, client: TestClient, address: str
    ) -> None:
        response = client.post(
            "/v1/devices/workshop-pc/announcements",
            headers=DEVICE_HEADERS,
            json={**ANNOUNCEMENT, "address": address},
        )

        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    def test_a_refused_announcement_changes_nothing(self, client: TestClient) -> None:
        client.post(
            "/v1/devices/workshop-pc/announcements", headers=DEVICE_HEADERS, json=ANNOUNCEMENT
        )
        client.post(
            "/v1/devices/workshop-pc/announcements",
            headers=DEVICE_HEADERS,
            json={**ANNOUNCEMENT, "address": "http://8.8.8.8"},
        )

        stored = client.get("/v1/devices/workshop-pc", headers=RELAY_HEADERS).json()
        assert stored["address"] == "http://10.0.0.5"

    @pytest.mark.parametrize("headers", [RELAY_HEADERS, SENSOR_HEADERS, {}])
    def test_no_other_key_will_do(self, client: TestClient, headers: dict[str, str]) -> None:
        """A key extracted from sensor firmware must not be able to repoint the
        device in a PC case."""
        response = client.post(
            "/v1/devices/workshop-pc/announcements", headers=headers, json=ANNOUNCEMENT
        )

        assert response.status_code == HTTPStatus.UNAUTHORIZED


class TestADeploymentWithNoDeviceKey:
    @pytest.fixture
    def keyless_client(self, tmp_path: Path, relay_service: RelayService) -> Iterator[TestClient]:
        settings = declaring(tmp_path, "devices: []")
        prepare_database(settings.database_path)
        with TestClient(create_app(settings, relay_service=relay_service)) as test_client:
            yield test_client

    def test_the_collection_is_empty_rather_than_absent(self, keyless_client: TestClient) -> None:
        response = keyless_client.get("/v1/devices", headers=RELAY_HEADERS)

        assert response.status_code == HTTPStatus.OK
        assert response.json() == {"devices": []}

    def test_announcing_is_refused_however_it_is_attempted(
        self, keyless_client: TestClient
    ) -> None:
        """With no key configured there is no value to compare against, so the route
        is closed rather than open."""
        for headers in ({}, DEVICE_HEADERS, RELAY_HEADERS):
            response = keyless_client.post(
                "/v1/devices/workshop-pc/announcements", headers=headers, json=ANNOUNCEMENT
            )
            assert response.status_code == HTTPStatus.UNAUTHORIZED


class TestStartingUpWithDevicesAndNoKey:
    def test_it_is_refused_before_the_server_listens(
        self, tmp_path: Path, relay_service: RelayService
    ) -> None:
        """Otherwise the devices sit unpolled with no address, reporting nothing,
        for a reason that is nowhere in the logs."""
        settings = declaring(tmp_path)

        with pytest.raises(DeviceConfigError, match="PIHOME_DEVICE_API_KEY"):
            check_configuration(settings, relay_service)

    def test_the_message_names_what_was_declared(
        self, tmp_path: Path, relay_service: RelayService
    ) -> None:
        settings = declaring(tmp_path)

        with pytest.raises(DeviceConfigError, match="study-pc, workshop-pc"):
            check_configuration(settings, relay_service)

    def test_declaring_nothing_needs_no_key(
        self, tmp_path: Path, relay_service: RelayService
    ) -> None:
        check_configuration(declaring(tmp_path, "devices: []"), relay_service)

    def test_a_key_makes_it_start(self, tmp_path: Path, relay_service: RelayService) -> None:
        check_configuration(declaring(tmp_path, device_api_key=DEVICE_KEY), relay_service)
