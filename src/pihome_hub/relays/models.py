"""Static configuration for one relay channel."""

from __future__ import annotations

import re
from typing import Annotated, Final, Literal, TypeAlias

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

from pihome_hub.yamlish import switch_word

#: What happens to a relay's pin the moment the process claims it.
#:
#: ``preserve`` reads the pin's current level and keeps it — a service restart
#: should not be a reason for the lights to change. ``on``/``off`` force a known
#: state, useful for a relay whose boot-time level cannot be trusted (see
#: :class:`~pihome_hub.relays.gpio.GpioZeroRelayBackend`).
#: ``BeforeValidator`` because YAML 1.1 turns a bare ``on`` into ``True`` — see
#: :mod:`pihome_hub.yamlish`.
InitialState: TypeAlias = Annotated[Literal["preserve", "on", "off"], BeforeValidator(switch_word)]

#: What this service does to a relay's pin as it shuts down.
#:
#: An important caveat: releasing a GPIO pin turns it back into an input with no
#: pull, so once this process exits the relay follows whatever the board's own idle
#: pull dictates — this service cannot hold a pin after it stops running. What
#: ``shutdown_state`` controls is the state driven *before* the pin is released,
#: which is what a fast restart window sees.
#:
#: ``leave`` releases without driving anything, ``on``/``off`` drive first.
ShutdownState: TypeAlias = Annotated[Literal["leave", "on", "off"], BeforeValidator(switch_word)]

_ID_PATTERN: Final = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

#: BCM numbering — the convention gpiozero, and the rest of the Pi ecosystem, use.
_MIN_BCM_PIN: Final = 0
_MAX_BCM_PIN: Final = 27


class RelayConfig(BaseModel):
    """One controllable relay channel, as declared in ``config/relays.yaml``."""

    # An unrecognised key is refused rather than ignored. Every field here has a
    # safe-looking default or a hyphenated near-miss — ``active-low`` for
    # ``active_low`` reads as nothing at all — and silently accepting one means
    # running a relay at the wrong polarity with a config file that looks correct.
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    pin: int = Field(ge=_MIN_BCM_PIN, le=_MAX_BCM_PIN)
    label: str = Field(min_length=1, max_length=100)
    #: Most cheap opto-isolated relay boards close the relay on a LOW signal —
    #: hence the default. A board that switches on HIGH needs this set to false.
    active_low: bool = True
    #: ``preserve`` is only as trustworthy as the board's idle pull, because a pin
    #: this service does not currently own reads as a floating input. Set ``on`` or
    #: ``off`` explicitly for a relay whose boot-time level you cannot vouch for.
    initial_state: InitialState = "preserve"
    shutdown_state: ShutdownState = "leave"

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
