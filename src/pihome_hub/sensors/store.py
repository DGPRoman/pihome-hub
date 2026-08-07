"""In-memory store of the latest reading from each configured device.

Deliberately not persisted. A sensor reading describes the house *now*; a value
recovered from disk after a restart would be presented as current while being
arbitrarily old, which is worse than having nothing. Devices report frequently,
so the gap after a restart closes on its own.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pihome_hub.sensors.errors import SensorConfigError, UnknownDeviceError
from pihome_hub.sensors.models import DeviceSnapshot, SensorDevice, SensorReading


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class _DeviceState:
    """Mutable per-device record. Not exposed; snapshots are handed out instead."""

    device: SensorDevice
    last_seen: datetime | None = field(default=None)
    motion: bool | None = field(default=None)
    motion_updated_at: datetime | None = field(default=None)
    temperature: float | None = field(default=None)
    humidity: float | None = field(default=None)
    climate_updated_at: datetime | None = field(default=None)


class SensorStore:
    """Holds the most recent reading per configured device."""

    def __init__(
        self,
        devices: Iterable[SensorDevice],
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._states: dict[str, _DeviceState] = {}

        for device in devices:
            if device.id in self._states:
                msg = f"duplicate sensor device id {device.id!r}"
                raise SensorConfigError(msg)
            self._states[device.id] = _DeviceState(device=device)

    def _require(self, device_id: str) -> _DeviceState:
        try:
            return self._states[device_id]
        except KeyError:
            raise UnknownDeviceError(device_id) from None

    def _snapshot(self, state: _DeviceState, now: datetime) -> DeviceSnapshot:
        age = None if state.last_seen is None else (now - state.last_seen).total_seconds()
        stale = age is None or age > state.device.stale_after_seconds
        return DeviceSnapshot(
            id=state.device.id,
            label=state.device.label,
            stale=stale,
            last_seen=state.last_seen,
            motion=state.motion,
            motion_updated_at=state.motion_updated_at,
            temperature=state.temperature,
            humidity=state.humidity,
            climate_updated_at=state.climate_updated_at,
        )

    def record(self, device_id: str, reading: SensorReading) -> DeviceSnapshot:
        """Store a reading. Raises :class:`UnknownDeviceError` for an undeclared device."""
        state = self._require(device_id)
        now = self._clock()

        with self._lock:
            state.last_seen = now
            if reading.motion is not None:
                state.motion = reading.motion
                state.motion_updated_at = now
            if reading.temperature is not None:
                state.temperature = reading.temperature
            if reading.humidity is not None:
                state.humidity = reading.humidity
            if reading.has_climate:
                state.climate_updated_at = now

            return self._snapshot(state, now)

    def snapshot(self, device_id: str) -> DeviceSnapshot:
        state = self._require(device_id)
        with self._lock:
            return self._snapshot(state, self._clock())

    def snapshots(self) -> list[DeviceSnapshot]:
        """Every device in configuration order, including ones never heard from."""
        with self._lock:
            now = self._clock()
            return [self._snapshot(state, now) for state in self._states.values()]

    @property
    def configured(self) -> Mapping[str, SensorDevice]:
        return {device_id: state.device for device_id, state in self._states.items()}
