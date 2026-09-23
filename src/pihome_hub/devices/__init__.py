"""Device domain: which HTTP devices exist, where they are, and what they answered."""

from __future__ import annotations

from pihome_hub.devices.config import load_devices
from pihome_hub.devices.errors import DeviceConfigError, DeviceError, UndeclaredDeviceError
from pihome_hub.devices.models import (
    MAX_STATE_BYTES,
    STATUS_PATH,
    Device,
    DeviceAnnouncement,
    DeviceKind,
    DeviceStatus,
    PollTarget,
    normalise_address,
)
from pihome_hub.devices.registry import DeviceRegistry

__all__ = [
    "MAX_STATE_BYTES",
    "STATUS_PATH",
    "Device",
    "DeviceAnnouncement",
    "DeviceConfigError",
    "DeviceError",
    "DeviceKind",
    "DeviceRegistry",
    "DeviceStatus",
    "PollTarget",
    "UndeclaredDeviceError",
    "load_devices",
    "normalise_address",
]
