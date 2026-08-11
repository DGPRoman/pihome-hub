"""Request and response bodies for the v1 API."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from pihome_hub.automation import AutomationRule
from pihome_hub.sensors import DeviceSnapshot


class RelayState(BaseModel):
    """A relay and whether its circuit is currently energised."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(description="Stable identifier, as configured", examples=["porch-light"])
    label: str = Field(description="Human-readable name", examples=["Porch light"])
    on: bool = Field(description="True when the circuit is energised")


class RelayStateRequest(BaseModel):
    """Desired state for a relay, or for every relay.

    Strict: only a JSON boolean is accepted. Pydantic would otherwise read ``"yes"``
    and ``"true"`` as true, and guessing at intent is the wrong instinct for a
    request that closes a mains circuit.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    on: bool = Field(description="True to energise, false to de-energise")


class RelayCollection(BaseModel):
    """Every configured relay.

    Wrapped in an object rather than returned as a bare array so that later
    additions — a timestamp, a count — do not change the response's shape.
    """

    model_config = ConfigDict(frozen=True)

    relays: list[RelayState]


class SensorCollection(BaseModel):
    """Every configured sensor device and its latest reading."""

    model_config = ConfigDict(frozen=True)

    sensors: list[DeviceSnapshot]


class AutomationRuleCollection(BaseModel):
    """Every configured automation rule.

    Carries no ``location``: the coordinates that block belongs to identify a home,
    and nothing here needs them.
    """

    model_config = ConfigDict(frozen=True)

    rules: list[AutomationRule]
