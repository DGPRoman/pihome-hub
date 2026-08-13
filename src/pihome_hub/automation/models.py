"""Declarative automation rules.

The predecessor of this service hardcoded its one rule — which device turns on
which switch, and for how long — inside the sensor handler. Expressing it as data
means the wiring of a house lives in that house's config file rather than in code
everyone else has to read past.
"""

from __future__ import annotations

import re
from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StrictBool, model_validator

from pihome_hub.yamlish import switch_word

_ID_PATTERN: Final = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

#: Upper bound on a hold. Long enough for any lighting use, short enough that a
#: typo cannot leave a circuit energised for a week.
MAX_HOLD_SECONDS: Final = 86_400.0


class Location(BaseModel):
    """Where the house is, for sunrise and sunset.

    Required only when a rule asks about darkness. Coordinates identify a home, so
    they belong in git-ignored configuration and never in source — the previous
    version of this service had them written into a function body.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    latitude: Annotated[float, Field(ge=-90.0, le=90.0)]
    longitude: Annotated[float, Field(ge=-180.0, le=180.0)]
    timezone: str = Field(min_length=1, examples=["Europe/Kyiv"])


class Trigger(BaseModel):
    """What must arrive for a rule to fire."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    device: str
    #: Fire when motion changes to this value. Only motion triggers rules for now;
    #: a climate trigger would need hysteresis to avoid flapping around a threshold,
    #: which is a design question rather than a missing line of code.
    motion: StrictBool


class Action(BaseModel):
    """What a firing rule does to a relay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    relay: str
    #: ``BeforeValidator`` because YAML 1.1 turns a bare ``on`` into ``True``, and
    #: ``state: on`` is exactly how anyone would write this — see
    #: :mod:`pihome_hub.yamlish`.
    state: Annotated[Literal["on", "off"], BeforeValidator(switch_word)]
    #: When set, revert to the opposite state this many seconds after the rule last
    #: fired. A re-trigger while a hold is pending restarts the countdown rather than
    #: queueing a second one, so continued motion keeps a light on.
    hold_seconds: Annotated[float, Field(gt=0, le=MAX_HOLD_SECONDS)] | None = None

    @property
    def turn_on(self) -> bool:
        return self.state == "on"


class AutomationRule(BaseModel):
    """One trigger, its conditions, and what it does."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    when: Trigger
    then: Action
    #: Only fire between dusk and dawn. Requires ``location`` to be configured.
    only_after_dark: bool = False
    enabled: bool = True

    @model_validator(mode="after")
    def _id_is_a_slug(self) -> Self:
        if not _ID_PATTERN.match(self.id):
            msg = (
                f"id {self.id!r} must be lowercase letters, digits and single hyphens, "
                "e.g. 'porch-motion-light'"
            )
            raise ValueError(msg)
        return self


class AutomationConfig(BaseModel):
    """A whole automation file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    location: Location | None = None
    rules: list[AutomationRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _dark_rules_need_a_location(self) -> Self:
        needs_sun = [rule.id for rule in self.rules if rule.only_after_dark and rule.enabled]
        if needs_sun and self.location is None:
            msg = (
                f"rules {needs_sun} use only_after_dark, which needs a 'location' "
                "block (latitude, longitude, timezone) to work out sunset"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _rule_ids_are_unique(self) -> Self:
        seen: set[str] = set()
        for rule in self.rules:
            if rule.id in seen:
                msg = f"duplicate automation rule id {rule.id!r}"
                raise ValueError(msg)
            seen.add(rule.id)
        return self
