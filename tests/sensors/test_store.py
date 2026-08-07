"""SensorStore: recording readings, staleness, and rejection of unknown devices."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pihome_hub.sensors import (
    SensorConfigError,
    SensorDevice,
    SensorReading,
    SensorStore,
    UnknownDeviceError,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(clock: FakeClock) -> SensorStore:
    return SensorStore(
        [
            SensorDevice(id="porch-motion", label="Porch motion", stale_after_seconds=300),
            SensorDevice(id="hallway-climate", label="Hallway climate", stale_after_seconds=600),
        ],
        clock=clock,
    )


class TestRecording:
    def test_motion_is_stored_with_its_own_timestamp(
        self, store: SensorStore, clock: FakeClock
    ) -> None:
        snapshot = store.record("porch-motion", SensorReading(motion=True))

        assert snapshot.motion is True
        assert snapshot.motion_updated_at == clock.now
        assert snapshot.climate_updated_at is None

    def test_climate_is_stored_with_its_own_timestamp(self, store: SensorStore) -> None:
        snapshot = store.record("hallway-climate", SensorReading(temperature=21.5, humidity=48.0))

        assert snapshot.temperature == 21.5
        assert snapshot.humidity == 48.0
        assert snapshot.motion_updated_at is None

    def test_quantities_keep_independent_timestamps(
        self, store: SensorStore, clock: FakeClock
    ) -> None:
        """A device reporting motion often and temperature rarely has two notions
        of 'recent'; collapsing them would hide a dead thermometer."""
        store.record("porch-motion", SensorReading(temperature=20.0))
        climate_at = clock.now

        clock.advance(120)
        snapshot = store.record("porch-motion", SensorReading(motion=True))

        assert snapshot.climate_updated_at == climate_at
        assert snapshot.motion_updated_at == clock.now

    def test_a_partial_reading_leaves_other_values_alone(self, store: SensorStore) -> None:
        store.record("hallway-climate", SensorReading(temperature=21.5, humidity=48.0))
        snapshot = store.record("hallway-climate", SensorReading(temperature=22.0))

        assert snapshot.temperature == 22.0
        assert snapshot.humidity == 48.0

    def test_an_unknown_device_is_rejected(self, store: SensorStore) -> None:
        with pytest.raises(UnknownDeviceError, match="ghost-sensor"):
            store.record("ghost-sensor", SensorReading(motion=True))

    def test_duplicate_device_ids_are_rejected(self) -> None:
        devices = [
            SensorDevice(id="porch-motion", label="One"),
            SensorDevice(id="porch-motion", label="Two"),
        ]
        with pytest.raises(SensorConfigError, match="duplicate"):
            SensorStore(devices)


class TestStaleness:
    def test_a_device_never_heard_from_is_stale(self, store: SensorStore) -> None:
        snapshot = store.snapshot("porch-motion")
        assert snapshot.stale is True
        assert snapshot.last_seen is None

    def test_a_fresh_reading_is_not_stale(self, store: SensorStore) -> None:
        assert store.record("porch-motion", SensorReading(motion=True)).stale is False

    def test_a_reading_goes_stale_after_its_window(
        self, store: SensorStore, clock: FakeClock
    ) -> None:
        store.record("porch-motion", SensorReading(motion=False))

        clock.advance(301)

        snapshot = store.snapshot("porch-motion")
        assert snapshot.stale is True
        assert snapshot.motion is False, "the value is still reported, just flagged"

    def test_each_device_uses_its_own_window(self, store: SensorStore, clock: FakeClock) -> None:
        store.record("porch-motion", SensorReading(motion=True))
        store.record("hallway-climate", SensorReading(temperature=21.0))

        clock.advance(400)

        assert store.snapshot("porch-motion").stale is True
        assert store.snapshot("hallway-climate").stale is False


class TestSnapshots:
    def test_every_configured_device_is_listed_even_when_silent(self, store: SensorStore) -> None:
        assert [s.id for s in store.snapshots()] == ["porch-motion", "hallway-climate"]

    def test_an_unknown_device_snapshot_is_rejected(self, store: SensorStore) -> None:
        with pytest.raises(UnknownDeviceError):
            store.snapshot("ghost-sensor")
