"""Static configuration for one relay channel."""

from __future__ import annotations

import re
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: What happens to a relay's pin the moment the process claims it.
#:
#: ``preserve`` reads the pin's current level and keeps it — a service restart
#: should not be a reason for the lights to change. ``on``/``off`` force a known
#: state, useful for a relay whose boot-time level cannot be trusted (see
#: :class:`~pihome_hub.relays.gpio.GpioZeroRelayBackend`).
InitialState = Literal["preserve", "on", "off"]

_ID_PATTERN: Final = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

#: BCM numbering — the convention gpiozero, and the rest of the Pi ecosystem, use.
_MIN_BCM_PIN: Final = 0
_MAX_BCM_PIN: Final = 27


class RelayConfig(BaseModel):
    """One controllable relay channel, as declared in ``config/relays.yaml``."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1, max_length=64)
    pin: int = Field(ge=_MIN_BCM_PIN, le=_MAX_BCM_PIN)
    label: str = Field(min_length=1, max_length=100)
    #: Most cheap opto-isolated relay boards close the relay on a LOW signal —
    #: hence the default. A board that switches on HIGH needs this set to false.
    active_low: bool = True
    initial_state: InitialState = "preserve"

    @field_validator("id")
    @classmethod
    def _id_is_a_slug(cls, value: str) -> str:
        if not _ID_PATTERN.match(value):
            msg = (
                f"id {value!r} must be lowercase letters, digits and single hyphens, "
                "e.g. 'porch-light'"
            )
            raise ValueError(msg)
        return value
