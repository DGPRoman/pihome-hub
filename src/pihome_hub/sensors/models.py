"""Sensor configuration, incoming readings, and stored device state."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Final, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

_ID_PATTERN: Final = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

#: Plausible bounds for a domestic environment sensor. Values outside these are a
#: broken probe or a wiring fault, and recording them would poison the readings a
#: human looks at to decide whether the heating is working.
_MIN_TEMPERATURE_C: Final = -50.0
_MAX_TEMPERATURE_C: Final = 80.0

DEFAULT_STALE_AFTER_SECONDS: Final = 300.0


class SensorDevice(BaseModel):
    """A sensor this service will accept readings from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=100)
    #: How long a reading stays trustworthy. Past this the device is reported as
    #: stale rather than silently serving an old value as if it were current.
    stale_after_seconds: Annotated[float, Field(gt=0)] = DEFAULT_STALE_AFTER_SECONDS

    @model_validator(mode="after")
    def _id_is_a_slug(self) -> Self:
        if not _ID_PATTERN.match(self.id):
            msg = (
                f"id {self.id!r} must be lowercase letters, digits and single hyphens, "
                "e.g. 'porch-motion'"
            )
            raise ValueError(msg)
        return self


class SensorReading(BaseModel):
    """One push from a device. Every field is optional; at least one is required.

    A device may report several quantities in a single request, which matters for
    battery-powered firmware where each extra HTTP round trip costs charge.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Strict: only a JSON boolean. Pydantic would otherwise read "yes" and 1 as
    #: true, and motion is what decides whether a light comes on.
    motion: StrictBool | None = None
    temperature: Annotated[float, Field(ge=_MIN_TEMPERATURE_C, le=_MAX_TEMPERATURE_C)] | None = None
    humidity: Annotated[float, Field(ge=0.0, le=100.0)] | None = None

    @model_validator(mode="after")
    def _at_least_one_value(self) -> Self:
        if self.motion is None and self.temperature is None and self.humidity is None:
            msg = "a reading must carry at least one of: motion, temperature, humidity"
            raise ValueError(msg)
        return self

    @property
    def has_climate(self) -> bool:
        return self.temperature is not None or self.humidity is not None


class DeviceSnapshot(BaseModel):
    """Everything known about one device, as served by the API.

    Timestamps are per quantity: a device that reports motion every few seconds and
    temperature every few minutes has two very different notions of "recent", and
    collapsing them into one ``last_updated`` would hide that.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    label: str
    #: True when nothing has arrived within the device's ``stale_after_seconds``.
    #: Reported rather than hidden, so a dead sensor looks dead instead of looking
    #: like a room that stopped moving.
    stale: bool
    last_seen: datetime | None = None
    motion: bool | None = None
    motion_updated_at: datetime | None = None
    temperature: float | None = None
    humidity: float | None = None
    climate_updated_at: datetime | None = None
