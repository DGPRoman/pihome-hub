"""Request and response bodies for the v1 API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pihome_hub.accounts import MAX_PASSWORD_LENGTH, Role
from pihome_hub.automation import AutomationRule
from pihome_hub.devices import DeviceStatus
from pihome_hub.sensors import DeviceSnapshot


class RelayState(BaseModel):
    """A relay and whether its circuit is currently energised."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(description="Stable identifier, as configured", examples=["porch-light"])
    label: str = Field(description="Human-readable name", examples=["Porch light"])
    on: bool = Field(description="True when the circuit is energised")
    hold_expires_at: datetime | None = Field(
        default=None,
        description=(
            "When an automation hold is due to put this relay back, or null when "
            "nothing is scheduled against it. Served so a client can show that a "
            "state has a timer running against it rather than presenting it as "
            "settled. A write to the relay cancels any hold on it, so this is "
            "always null in the response to one."
        ),
    )


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


class DeviceCollection(BaseModel):
    """Every declared HTTP device, whether or not it has ever announced itself.

    Including the ones never heard from, deliberately. A device that was declared
    and has never called is the most interesting row in the list, and a collection
    built from what announced would be one that leaves it out.
    """

    model_config = ConfigDict(frozen=True)

    devices: list[DeviceStatus]


class AutomationRuleCollection(BaseModel):
    """Every configured automation rule.

    Carries no ``location``: the coordinates that block belongs to identify a home,
    and nothing here needs them.
    """

    model_config = ConfigDict(frozen=True)

    rules: list[AutomationRule]


class LoginRequest(BaseModel):
    """Credentials presented at ``POST /v1/session``.

    The lengths are generous bounds on the request rather than the rules an account
    is held to: a username that is too long or a password that is too short is
    answered ``401`` like every other wrong credential, not ``422``. Validating them
    here would make the response say which half was wrong.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    username: str = Field(max_length=200, examples=["roman"])
    password: str = Field(max_length=MAX_PASSWORD_LENGTH)


class SessionResponse(BaseModel):
    """Who the caller is, and when this session runs out.

    No token: it is in the cookie, and repeating it in a body would put it somewhere
    JavaScript can read and a proxy can log.
    """

    model_config = ConfigDict(frozen=True)

    username: str = Field(description="The account this session belongs to")
    role: Role = Field(description="What this account may do")
    expires_at: datetime = Field(description="When the session stops being accepted")


class UserResponse(BaseModel):
    """One account, as an admin sees it.

    Built from :class:`~pihome_hub.accounts.User`, which never held a password hash,
    so there is nothing here that could leak one. The numeric id stays inside the
    hub: the name is what every route and every person uses.
    """

    model_config = ConfigDict(frozen=True)

    username: str = Field(description="The name the account logs in with")
    role: Role = Field(description="What this account may do")
    disabled: bool = Field(description="Whether the account is blocked from logging in")
    created_at: datetime = Field(description="When the account was created")


class UserCollection(BaseModel):
    """Every account, in the order a person reading the list would expect."""

    model_config = ConfigDict(frozen=True)

    users: list[UserResponse]


class UserChangeRequest(BaseModel):
    """A change to an ``operator`` or ``viewer`` account at ``PATCH /v1/users/{username}``.

    Every field is optional and absent means unchanged, but a body naming none of
    them is refused: it can only be a client that meant to send something else.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    # Lax for this one field, because strict mode accepts only an instance of the
    # enum and a JSON body can only ever hold its value. Lax enum validation is still
    # exact — 'VIEWER', 0 and ['viewer'] are all refused — so nothing is guessed at.
    role: Role | None = Field(
        default=None,
        strict=False,
        description="`operator` or `viewer`. `admin` is granted on the hub's console only",
    )
    disabled: bool | None = Field(
        default=None, description="Block the account from logging in, or let it in again"
    )

    @field_validator("role")
    @classmethod
    def _not_admin(cls, role: Role | None) -> Role | None:
        if role is Role.ADMIN:
            msg = "admin is granted with pihome-hub-admin on the hub, not over HTTP"
            raise ValueError(msg)
        return role

    @model_validator(mode="after")
    def _names_something(self) -> UserChangeRequest:
        if self.role is None and self.disabled is None:
            msg = "name at least one of 'role' and 'disabled'"
            raise ValueError(msg)
        return self
