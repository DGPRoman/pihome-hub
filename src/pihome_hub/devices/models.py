"""What a device is, what it says about itself, and what this hub serves back.

Three shapes, deliberately kept apart. :class:`Device` is declared by an operator
and never changes at runtime. :class:`DeviceAnnouncement` is what the device itself
sends and is therefore the one that has to be distrusted. :class:`DeviceStatus` is
what a client reads, and it is the only one of the three that is ever serialised
outwards — which is how the announced key stays out of every response.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from ipaddress import IPv6Address, ip_address
from typing import Any, Final, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

_ID_PATTERN: Final = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

#: Longest status document the poller will read before giving up on a device.
#:
#: A device is a small appliance answering a fixed JSON object of a few fields, so
#: this is orders of magnitude more than one needs. It is here because the thing on
#: the other end of the socket is only *probably* that appliance: without a ceiling,
#: anything answering on that address can make this process allocate until it dies,
#: and it would not even have to be malicious to do it.
MAX_STATE_BYTES: Final = 4096

#: Bounds on an announced key. Not a judgement about strength — the device generates
#: it and this hub does not get a say — only a refusal to store something that
#: cannot be a key at all.
_MIN_KEY_LENGTH: Final = 8
_MAX_KEY_LENGTH: Final = 512

#: The port an origin does not need to spell out.
_DEFAULT_HTTP_PORT: Final = 80


class DeviceKind(StrEnum):
    """What a device is.

    The kind decides where its status is read from, so this is not a label: adding
    a value here is adding a device this hub knows how to talk to.
    """

    PC_POWER = "pc-power"


#: Where each kind of device serves its status.
#:
#: Not configurable, and that is the point. The path belongs to the device's own
#: published contract — ``docs/http-api.md`` in esp32c3-pc-power — so writing it in
#: a YAML file would invite an operator to point this hub's authenticated, keyed
#: requests at a path of their choosing on a host it already trusts.
STATUS_PATH: Final[Mapping[DeviceKind, str]] = {
    DeviceKind.PC_POWER: "/v1/power",
}


def normalise_address(raw: str) -> str:
    """Reduce an announced address to an origin this hub is willing to call.

    The rules are narrow on purpose. This is the one value in the system that an
    outside party chooses and that this process then makes an authenticated request
    to, so every clause below closes a way of pointing it somewhere it should not go.

    * **``http`` only.** These devices terminate no TLS and hold no certificate
      anybody could check; accepting ``https`` would promise a guarantee that is not
      there. Anything else — ``file``, ``gopher``, a scheme a future library learns —
      is refused rather than enumerated.
    * **An IP address, never a name.** A hostname is resolved at request time, by a
      resolver this hub does not control, to whatever the answer is *then*. That is
      the whole of DNS rebinding, and refusing names costs a deployment nothing: the
      address is announced by the device itself, which knows its own.
    * **A private address.** ``is_private`` covers the RFC 1918 ranges, the IPv6
      unique-local ones, link-local and loopback. A device is on the same network as
      the hub by construction; an announcement naming a public address is either
      wrong or an attempt to make this hub reach something on somebody's behalf.
    * **No path, query, fragment or userinfo.** The path is the kind's, not the
      announcement's, and credentials in a URL are not how this authenticates.

    Returns the origin in one spelling, so the same device announcing
    ``http://10.0.0.5:80/`` and ``http://10.0.0.5`` does not look like two.
    """
    candidate = raw.strip()
    if not candidate:
        msg = "address must not be empty"
        raise ValueError(msg)

    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError as exc:
        msg = f"address {candidate!r} is not a URL: {exc}"
        raise ValueError(msg) from exc

    if parts.scheme != "http":
        msg = (
            f"address {candidate!r} must use http. These devices hold no certificate, "
            "so https would look like a guarantee that is not there"
        )
        raise ValueError(msg)

    if parts.path not in ("", "/") or parts.query or parts.fragment:
        msg = f"address {candidate!r} must be an origin only, with no path, query or fragment"
        raise ValueError(msg)

    if parts.username is not None or parts.password is not None:
        msg = f"address {candidate!r} must not carry credentials; the key is announced separately"
        raise ValueError(msg)

    host = parts.hostname
    if not host:
        msg = f"address {candidate!r} names no host"
        raise ValueError(msg)

    try:
        address = ip_address(host)
    except ValueError:
        msg = (
            f"address {candidate!r} must name an IP address rather than a hostname, "
            "so that what this hub calls cannot be changed by whoever answers DNS"
        )
        raise ValueError(msg) from None

    if not address.is_private:
        msg = (
            f"address {candidate!r} is not on a private network. A device announces the "
            "address it is reachable at from this hub, which is a local one"
        )
        raise ValueError(msg)

    literal = f"[{address}]" if isinstance(address, IPv6Address) else str(address)
    if port is None or port == _DEFAULT_HTTP_PORT:
        return f"http://{literal}"
    return f"http://{literal}:{port}"


class Device(BaseModel):
    """A device this hub will poll, as an operator declared it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=100)
    kind: DeviceKind

    @model_validator(mode="after")
    def _id_is_a_slug(self) -> Self:
        if not _ID_PATTERN.match(self.id):
            msg = (
                f"id {self.id!r} must be lowercase letters, digits and single hyphens, "
                "e.g. 'workshop-pc'"
            )
            raise ValueError(msg)
        return self

    @property
    def status_path(self) -> str:
        """The path this kind of device serves its status at."""
        return STATUS_PATH[self.kind]


class DeviceAnnouncement(BaseModel):
    """What a device sends on boot, and whenever its address changes.

    The key is in the body rather than in configuration because this hub cannot know
    it in advance: the device generates one at first boot and shows it once. That
    makes an announcement a credential handover, which is why the route carrying it
    needs a key of its own.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    address: str = Field(
        max_length=100,
        description="Origin this device is reachable at, e.g. http://10.0.0.5",
        examples=["http://10.0.0.5"],
    )
    api_key: SecretStr = Field(description="The key this hub must present when polling it")
    firmware: str | None = Field(
        default=None,
        max_length=64,
        description="Whatever the device calls its build. Recorded, never interpreted",
    )

    @field_validator("address")
    @classmethod
    def _address_is_a_private_http_origin(cls, value: str) -> str:
        return normalise_address(value)

    @field_validator("api_key")
    @classmethod
    def _key_is_the_length_of_a_key(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if not _MIN_KEY_LENGTH <= len(secret) <= _MAX_KEY_LENGTH:
            msg = (
                f"api_key must be between {_MIN_KEY_LENGTH} and {_MAX_KEY_LENGTH} characters, "
                f"got {len(secret)}"
            )
            raise ValueError(msg)
        return value

    @field_validator("firmware")
    @classmethod
    def _firmware_is_printable(cls, value: str | None) -> str | None:
        """Refuse control characters in a string that ends up in log lines.

        A device chooses this value and nothing else validates it. Newlines and
        escape sequences in a field that is logged are how one record is made to
        look like two.
        """
        if value is not None and not value.isprintable():
            msg = "firmware must not contain control characters"
            raise ValueError(msg)
        return value


class DeviceStatus(BaseModel):
    """Everything this hub will say about one device.

    Carries no key. The announced credential lives in the registry and goes out only
    to the device it came from — a client reading the house has no use for it, and a
    response is the easiest place in a system for a secret to end up in a log.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    label: str
    kind: DeviceKind
    #: Origin the device last announced, or ``None`` if it never has.
    address: str | None = None
    firmware: str | None = None
    announced_at: datetime | None = None
    #: Whether the most recent poll succeeded. ``None`` before the first one, which
    #: is a different thing from a poll that failed and must not be shown as one.
    reachable: bool | None = None
    last_polled_at: datetime | None = None
    #: When a poll last succeeded. Subtract from now for the age of ``state``.
    last_seen_at: datetime | None = None
    #: When the current run of failures began, or ``None`` while the device answers.
    #: A single dropped packet on wifi is ordinary; a streak starting an hour ago is
    #: not, and only the second is worth waking somebody for.
    unreachable_since: datetime | None = None
    #: Why the last poll failed, in one line. Cleared by a poll that succeeds.
    last_error: str | None = None
    #: The device's own status document, exactly as it served it.
    #:
    #: Passed through rather than modelled here. What the fields mean is the
    #: device's contract to state, and re-declaring them in this hub would make
    #: every field it adds a change in two repositories instead of one.
    state: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class PollTarget:
    """Everything the poller needs to reach one device, and nothing else.

    A plain dataclass, and never a model: it holds the announced key, and the point
    of it not being a model is that nothing can serialise it by accident.

    The address is carried separately from the assembled URL because a poll result
    is only written back if the device is still at the address it was polled at —
    see :meth:`~pihome_hub.devices.registry.DeviceRegistry._record`.
    """

    id: str
    #: Origin as the registry holds it, in the one spelling normalise_address gives.
    address: str
    #: Path for this device's kind, which the announcement had no say in.
    path: str
    api_key: str

    @property
    def url(self) -> str:
        return f"{self.address}{self.path}"
