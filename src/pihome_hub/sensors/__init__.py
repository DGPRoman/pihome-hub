"""Sensor domain: device configuration, incoming readings, and current state."""

from __future__ import annotations

from pihome_hub.sensors.config import load_sensors
from pihome_hub.sensors.errors import SensorConfigError, SensorError, UnknownDeviceError
from pihome_hub.sensors.models import (
    DeviceSnapshot,
    RecordedReading,
    SensorDevice,
    SensorReading,
)
from pihome_hub.sensors.store import SensorStore

__all__ = [
    "DeviceSnapshot",
    "RecordedReading",
    "SensorConfigError",
    "SensorDevice",
    "SensorError",
    "SensorReading",
    "SensorStore",
    "UnknownDeviceError",
    "load_sensors",
]
