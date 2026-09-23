"""Exceptions raised by the device domain."""

from __future__ import annotations


class DeviceError(Exception):
    """Base class for every error this package raises."""


class UndeclaredDeviceError(DeviceError):
    """Raised when an announcement names a device that is not in configuration.

    Devices are declared rather than discovered, exactly as sensors are. An id
    nobody wrote down is firmware with a typo or somebody with the device key
    trying to make this hub reach an address of their choosing, and neither is
    something to store.
    """

    def __init__(self, device_id: str) -> None:
        super().__init__(f"no device configured with id {device_id!r}")
        self.device_id = device_id


class DeviceConfigError(DeviceError):
    """Raised when device configuration is missing, malformed, or inconsistent."""
