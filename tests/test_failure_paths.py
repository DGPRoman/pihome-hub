"""What the API answers when something underneath it has failed.

The interesting behaviour of this service is in its error handling, and no test
asserted any 5xx anywhere: across the whole suite the asserted statuses were 401,
200, 404, 422 and 429. Part of why the habit never formed is mechanical —
``TestClient`` defaults to ``raise_server_exceptions=True``, so a 500 arrives as
a test error rather than as a response anyone can make an assertion about.

These failures are not the caller's fault and are not fixed by changing the
request, which is what makes 503 the true answer rather than 500.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pihome_hub.accounts import Role, UserStore
from pihome_hub.app import create_app
from pihome_hub.automation import Location, SunClock
from pihome_hub.config import Settings
from pihome_hub.relays import MockRelayBackend, RelayService
from pihome_hub.storage import prepare_database
from tests.conftest import RELAY_HEADERS, SENSOR_HEADERS, build_relay_service

PASSWORD = "correct-horse-battery"


class StoppedRespondingBackend(MockRelayBackend):
    """A pin claimed successfully at startup that stops answering afterwards.

    The realistic shape of a mid-request hardware failure: nothing was wrong with
    the configuration, so it got through startup, and the fault arrives later.
    """

    def write(self, pin: int, *, on: bool) -> None:
        msg = "the GPIO character device went away"
        raise OSError(msg)


@pytest.fixture
def failing_service() -> RelayService:
    """Claims its pins normally, then refuses every write.

    Only ``write`` is overridden, so startup succeeds exactly as it does in
    production — which is the point: a failure that stopped the service from
    starting would never reach a request handler at all.
    """
    return build_relay_service(StoppedRespondingBackend())


@pytest.fixture
def failing_client(settings: Settings, failing_service: RelayService) -> Iterator[TestClient]:
    prepare_database(settings.database_path)
    app = create_app(settings, relay_service=failing_service)
    # Without this the 500 is re-raised into the test instead of being served, and
    # nothing can be asserted about what a client would actually have received.
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


class TestARelayThatStopsAnswering:
    def test_the_write_is_a_503_rather_than_an_unhandled_500(
        self, failing_client: TestClient
    ) -> None:
        response = failing_client.put(
            "/v1/relays/porch-light", json={"on": True}, headers=RELAY_HEADERS
        )

        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE

    def test_the_body_says_nothing_about_the_cause(self, failing_client: TestClient) -> None:
        """The detail belongs in the log, where the operator is.

        An authenticated caller who cannot fix a failed relay learns nothing from
        a pin number and a driver message except what the inside of the service
        looks like.
        """
        response = failing_client.put(
            "/v1/relays/porch-light", json={"on": True}, headers=RELAY_HEADERS
        )

        detail = response.json()["detail"]
        assert "character device" not in detail
        assert "17" not in detail
        assert "porch-light" not in detail

    def test_the_reported_state_does_not_move_ahead_of_the_hardware(
        self, failing_client: TestClient
    ) -> None:
        """The relay is reported as it was, not as it was asked to be.

        Of the two ways to be wrong about a relay whose write failed, saying it is
        on when it might be off is the one that gets somebody hurt.
        """
        before = failing_client.get("/v1/relays", headers=RELAY_HEADERS).json()

        failing_client.put("/v1/relays/porch-light", json={"on": True}, headers=RELAY_HEADERS)

        after = failing_client.get("/v1/relays", headers=RELAY_HEADERS).json()
        assert after == before

    def test_a_read_still_works(self, failing_client: TestClient) -> None:
        """Only the write path touches the pin, so the rest of the API is unaffected."""
        response = failing_client.get("/v1/relays", headers=RELAY_HEADERS)

        assert response.status_code == HTTPStatus.OK


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the permission bits this relies on")
class TestAStateDirectoryThatWentReadOnly:
    def test_a_login_is_a_503_rather_than_an_unhandled_500(
        self, client: TestClient, settings: Settings
    ) -> None:
        """The database is where a login has to write, so it is where this surfaces.

        A read-only state directory is an ordinary operational accident — a full
        disk, a remount, a restore that got the ownership wrong — and it used to
        leave the login endpoint answering 500.
        """
        UserStore(settings.database_path).create("roman", PASSWORD, Role.ADMIN)

        directory: Path = settings.database_path.parent
        directory.chmod(0o500)
        try:
            with TestClient(client.app, raise_server_exceptions=False) as unraising:
                response = unraising.post(
                    "/v1/session", json={"username": "roman", "password": PASSWORD}
                )
        finally:
            # Restored so pytest can clean the temporary directory up.
            directory.chmod(0o700)

        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE


POLAR_SENSORS_YAML = """
devices:
  - id: porch-motion
    label: "Porch motion"
    stale_after_seconds: 300
"""

#: Svalbard, and a rule that has to know whether it is dark.
POLAR_AUTOMATION_YAML = """
location:
  latitude: 78.2232
  longitude: 15.6267
  timezone: Arctic/Longyearbyen
rules:
  - id: porch-motion-light
    when:
      device: porch-motion
      motion: true
    only_after_dark: true
    then:
      relay: porch-light
      state: on
      hold_seconds: 0.05
"""


class TestALocationWhereTheSunDoesNotSet:
    """Ingestion at a latitude the sun does not rise or set at.

    Not a hardware failure like the two above, and not a 503 either — the request
    is answerable, and the whole fault was that it was not being answered. The
    reading is recorded before any rule runs, so the 500 came back over work the
    hub had already kept, and a device retrying on 5xx re-recorded it every time,
    for the length of the polar day.
    """

    @pytest.fixture
    def midsummer_client(
        self, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Iterator[TestClient]:
        sensors = tmp_path / "sensors.yaml"
        sensors.write_text(POLAR_SENSORS_YAML, encoding="utf-8")
        automation = tmp_path / "automation.yaml"
        automation.write_text(POLAR_AUTOMATION_YAML, encoding="utf-8")

        # The location is in the config; the date cannot be, so the clock the app
        # wires in is replaced with a fixed midsummer one. A subclass rather than a
        # stub, so what answers the request is the real SunClock.
        class Midsummer(SunClock):
            def __init__(self, location: Location) -> None:
                super().__init__(location, clock=lambda: datetime(2026, 6, 21, 1, 0, tzinfo=UTC))

        monkeypatch.setattr("pihome_hub.app.SunClock", Midsummer)

        prepare_database(settings.database_path)
        app = create_app(
            settings.model_copy(
                update={"sensor_config_path": sensors, "automation_config_path": automation}
            ),
            relay_service=build_relay_service(MockRelayBackend()),
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client

    def test_a_reading_is_accepted_rather_than_a_500(self, midsummer_client: TestClient) -> None:
        response = midsummer_client.post(
            "/v1/sensors/porch-motion/readings", json={"motion": True}, headers=SENSOR_HEADERS
        )

        assert response.status_code == HTTPStatus.ACCEPTED

    def test_the_rule_does_not_fire_under_the_midnight_sun(
        self, midsummer_client: TestClient
    ) -> None:
        """01:00 local, and broad daylight. A porch light has no business coming on.

        The status alone would pass with `is_dark` hard-wired to either answer, so
        this asserts the answer as well: the rule asked for darkness and there is
        none.
        """
        midsummer_client.post(
            "/v1/sensors/porch-motion/readings", json={"motion": True}, headers=SENSOR_HEADERS
        )

        relays = midsummer_client.get("/v1/relays", headers=RELAY_HEADERS).json()
        porch = next(relay for relay in relays["relays"] if relay["id"] == "porch-light")
        assert porch["on"] is False
