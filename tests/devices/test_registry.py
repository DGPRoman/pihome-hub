"""What the registry remembers, and what it refuses to.

The interesting cases are all about time: a device that has never announced, a
device that announced while it was being polled, and a run of failures whose start
is the only part worth reporting.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from pihome_hub.devices import (
    Device,
    DeviceAnnouncement,
    DeviceKind,
    DeviceRegistry,
    UndeclaredDeviceError,
)
from pihome_hub.storage import prepare_database

WORKSHOP = Device(id="workshop-pc", label="Workshop PC", kind=DeviceKind.PC_POWER)
STUDY = Device(id="study-pc", label="Study PC", kind=DeviceKind.PC_POWER)

START = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


class Clock:
    """A clock the test moves, so an interval is stated rather than waited for."""

    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "hub.db"
    prepare_database(path)
    return path


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def registry(database: Path, clock: Clock) -> DeviceRegistry:
    return DeviceRegistry(database, [WORKSHOP, STUDY], clock=clock)


def announcement(address: str = "http://10.0.0.5", key: str = "k" * 32) -> DeviceAnnouncement:
    return DeviceAnnouncement(address=address, api_key=SecretStr(key), firmware="0.1.0")


class TestADeviceThatHasNeverAnnounced:
    def test_it_is_listed_anyway(self, registry: DeviceRegistry) -> None:
        """It is the most interesting row in the list, not one to leave out."""
        assert [status.id for status in registry.statuses()] == ["workshop-pc", "study-pc"]

    def test_everything_volatile_about_it_is_none(self, registry: DeviceRegistry) -> None:
        status = registry.status("workshop-pc")

        assert status.address is None
        assert status.announced_at is None
        assert status.reachable is None
        assert status.state is None

    def test_never_polled_is_not_unreachable(self, registry: DeviceRegistry) -> None:
        """``reachable: false`` is a claim about a poll that happened."""
        assert registry.status("workshop-pc").reachable is None

    def test_it_is_not_a_target(self, registry: DeviceRegistry) -> None:
        assert registry.targets() == []


class TestAnnouncing:
    def test_an_undeclared_id_is_refused(self, registry: DeviceRegistry) -> None:
        with pytest.raises(UndeclaredDeviceError):
            registry.announce("kitchen-pc", announcement())

    def test_what_was_announced_is_what_is_served(self, registry: DeviceRegistry) -> None:
        status = registry.announce("workshop-pc", announcement("http://10.0.0.5:8080/"))

        assert status.address == "http://10.0.0.5:8080"
        assert status.firmware == "0.1.0"
        assert status.announced_at == START
        assert status.label == "Workshop PC"

    def test_the_key_is_not_in_what_is_served(self, registry: DeviceRegistry) -> None:
        status = registry.announce("workshop-pc", announcement(key="s3cret-key-value-here"))

        assert "s3cret-key-value-here" not in status.model_dump_json()

    def test_the_key_is_what_the_poller_is_given(self, registry: DeviceRegistry) -> None:
        registry.announce("workshop-pc", announcement(key="s3cret-key-value-here"))
        (target,) = registry.targets()

        assert target.api_key == "s3cret-key-value-here"
        assert target.url == "http://10.0.0.5/v1/power"

    def test_announcing_again_replaces_the_address(
        self, registry: DeviceRegistry, clock: Clock
    ) -> None:
        registry.announce("workshop-pc", announcement("http://10.0.0.5"))
        clock.advance(60)
        status = registry.announce("workshop-pc", announcement("http://10.0.0.9"))

        assert status.address == "http://10.0.0.9"
        assert [target.address for target in registry.targets()] == ["http://10.0.0.9"]

    def test_announcing_again_forgets_what_the_poller_wrote(
        self, registry: DeviceRegistry, clock: Clock
    ) -> None:
        """A device announces because it booted or moved. Both make the old result
        a statement about a situation that no longer exists."""
        registry.announce("workshop-pc", announcement())
        (target,) = registry.targets()
        registry.record_failure(target, "connection refused", at=clock.advance(30))

        status = registry.announce("workshop-pc", announcement("http://10.0.0.9"))

        assert status.reachable is None
        assert status.last_error is None
        assert status.unreachable_since is None
        assert status.last_polled_at is None
        assert status.state is None


class TestRecordingAPoll:
    def test_a_success_is_served_as_the_device_wrote_it(
        self, registry: DeviceRegistry, clock: Clock
    ) -> None:
        registry.announce("workshop-pc", announcement())
        (target,) = registry.targets()
        answered = {"state": "on", "pending": "none", "uptime_ms": 412934}

        at = clock.advance(30)
        registry.record_success(target, answered, at=at)
        status = registry.status("workshop-pc")

        assert status.reachable is True
        assert status.state == answered
        assert status.last_seen_at == at
        assert status.last_polled_at == at
        assert status.last_error is None

    def test_a_failure_says_why_and_keeps_the_last_reading(
        self, registry: DeviceRegistry, clock: Clock
    ) -> None:
        """Dated is more use to a client than blank."""
        registry.announce("workshop-pc", announcement())
        (target,) = registry.targets()
        seen_at = clock.advance(30)
        registry.record_success(target, {"state": "on"}, at=seen_at)

        failed_at = clock.advance(30)
        registry.record_failure(target, "timed out after 5.0s", at=failed_at)
        status = registry.status("workshop-pc")

        assert status.reachable is False
        assert status.last_error == "timed out after 5.0s"
        assert status.state == {"state": "on"}
        assert status.last_seen_at == seen_at
        assert status.last_polled_at == failed_at

    def test_unreachable_since_marks_the_start_of_the_run(
        self, registry: DeviceRegistry, clock: Clock
    ) -> None:
        """Not the most recent failure. One dropped packet is ordinary; an hour of
        them is not, and only the difference is worth waking somebody for."""
        registry.announce("workshop-pc", announcement())
        (target,) = registry.targets()

        first = clock.advance(30)
        registry.record_failure(target, "connection refused", at=first)
        registry.record_failure(target, "connection refused", at=clock.advance(30))
        registry.record_failure(target, "connection refused", at=clock.advance(30))

        assert registry.status("workshop-pc").unreachable_since == first

    def test_a_success_ends_the_run(self, registry: DeviceRegistry, clock: Clock) -> None:
        registry.announce("workshop-pc", announcement())
        (target,) = registry.targets()
        registry.record_failure(target, "connection refused", at=clock.advance(30))

        registry.record_success(target, {"state": "off"}, at=clock.advance(30))

        assert registry.status("workshop-pc").unreachable_since is None
        assert registry.status("workshop-pc").last_error is None

    def test_a_result_for_an_address_the_device_has_left_is_dropped(
        self, registry: DeviceRegistry, clock: Clock
    ) -> None:
        """A poll is not instantaneous. A device can announce a new address while a
        request to the old one is still in flight, and the answer that comes back
        describes an endpoint this hub has already been told to stop using."""
        registry.announce("workshop-pc", announcement("http://10.0.0.5"))
        (in_flight,) = registry.targets()

        registry.announce("workshop-pc", announcement("http://10.0.0.9"))
        registry.record_failure(in_flight, "connection refused", at=clock.advance(30))

        status = registry.status("workshop-pc")
        assert status.address == "http://10.0.0.9"
        assert status.reachable is None
        assert status.last_error is None

    def test_a_long_error_is_truncated_rather_than_stored_whole(
        self, registry: DeviceRegistry, clock: Clock
    ) -> None:
        """The message can come from whatever answered on that address."""
        registry.announce("workshop-pc", announcement())
        (target,) = registry.targets()

        registry.record_failure(target, "x" * 5000, at=clock.advance(30))

        last_error = registry.status("workshop-pc").last_error
        assert last_error is not None
        assert len(last_error) <= 500


class TestDevicesThatLeftConfiguration:
    def test_a_row_for_an_undeclared_id_is_not_served(self, database: Path, clock: Clock) -> None:
        DeviceRegistry(database, [WORKSHOP, STUDY], clock=clock).announce(
            "study-pc", announcement()
        )

        smaller = DeviceRegistry(database, [WORKSHOP], clock=clock)

        assert [status.id for status in smaller.statuses()] == ["workshop-pc"]
        assert smaller.targets() == []

    def test_pruning_stops_the_key_being_handed_out(self, database: Path, clock: Clock) -> None:
        """A device an operator removed is one this hub stops presenting a key for.

        Stops *using* it, which is what this can promise. The row goes; the bytes
        do not necessarily, because SQLite frees a page without overwriting it and
        this service does not run with ``secure_delete`` — that trades SD-card
        writes for a guarantee against somebody who can already read a file holding
        every live key anyway. A device taken out of service should have its key
        rotated on the device, and ``docs/devices.md`` says so.
        """
        DeviceRegistry(database, [WORKSHOP, STUDY], clock=clock).announce(
            "study-pc", announcement(key="s3cret-key-value-here")
        )

        smaller = DeviceRegistry(database, [WORKSHOP], clock=clock)
        assert smaller.forget_undeclared() == 1

        assert smaller.targets() == []
        with pytest.raises(UndeclaredDeviceError):
            smaller.status("study-pc")

    def test_pruning_leaves_the_declared_ones_alone(self, database: Path, clock: Clock) -> None:
        registry = DeviceRegistry(database, [WORKSHOP, STUDY], clock=clock)
        registry.announce("workshop-pc", announcement())

        assert registry.forget_undeclared() == 0
        assert registry.status("workshop-pc").address == "http://10.0.0.5"

    def test_declaring_nothing_empties_the_table(self, database: Path, clock: Clock) -> None:
        """The loop that builds the WHERE clause has no placeholders to write."""
        DeviceRegistry(database, [WORKSHOP], clock=clock).announce("workshop-pc", announcement())

        assert DeviceRegistry(database, [], clock=clock).forget_undeclared() == 1


class TestTheStateDocumentIsStoredWhole:
    def test_a_field_this_hub_has_never_heard_of_survives_the_round_trip(
        self, registry: DeviceRegistry, clock: Clock
    ) -> None:
        """Which is the point of storing JSON rather than columns: the device's
        contract says fields are added, and adding one must not cost a migration."""
        registry.announce("workshop-pc", announcement())
        (target,) = registry.targets()
        answered = json.loads('{"state": "on", "fan_rpm": 1200, "nested": {"a": [1, 2]}}')

        registry.record_success(target, answered, at=clock.advance(30))

        assert registry.status("workshop-pc").state == answered
