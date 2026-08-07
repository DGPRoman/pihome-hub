"""Exceptions raised by the sensor domain."""

from __future__ import annotations


class SensorError(Exception):
    """Base class for every error this package raises."""


class UnknownDeviceError(SensorError):
    """Raised when a reading names a device that is not configured.

    Devices are declared rather than discovered, so an unrecognised id is a
    misconfigured firmware or a probe — either way not something to record.
    """

    def __init__(self, device_id: str) -> None:
        super().__init__(f"no sensor device configured with id {device_id!r}")
        self.device_id = device_id


class SensorConfigError(SensorError):
    """Raised when sensor configuration is missing, malformed, or inconsistent."""
