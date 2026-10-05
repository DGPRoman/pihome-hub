"""Names and ids are matched whole, never as a line.

``re.match`` with a pattern ending in ``$`` accepts a trailing newline, because ``$``
also matches just before one. Every slug and username check in the hub was written
that way, so ``"porch-light\\n"`` passed as a relay id and ``"olya\\n"`` as a
username — an account that lists exactly like ``olya`` and is not her.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from pihome_hub.accounts import InvalidUsernameError
from pihome_hub.accounts.models import check_username
from pihome_hub.automation.models import Action, AutomationRule, Trigger
from pihome_hub.devices.models import Device, DeviceKind
from pihome_hub.relays import RelayConfig
from pihome_hub.sensors.models import SensorDevice

SOURCE = Path(__file__).resolve().parents[1] / "src" / "pihome_hub"


def _username(value: str) -> object:
    return check_username(value)


def _relay(value: str) -> object:
    return RelayConfig(id=value, pin=17, label="Porch light")


def _sensor(value: str) -> object:
    return SensorDevice(id=value, label="Porch motion")


def _device(value: str) -> object:
    return Device(id=value, label="Workshop PC", kind=DeviceKind.PC_POWER)


def _rule(value: str) -> object:
    return AutomationRule(
        id=value,
        when=Trigger(device="porch-motion", motion=True),
        then=Action(relay="porch-light", state="on"),
    )


CHECKS: list[tuple[str, Callable[[str], object], str]] = [
    ("username", _username, "olya"),
    ("relay id", _relay, "porch-light"),
    ("sensor id", _sensor, "porch-motion"),
    ("device id", _device, "workshop-pc"),
    ("rule id", _rule, "porch-motion-light"),
]


@pytest.mark.parametrize(("_what", "build", "good"), CHECKS, ids=[c[0] for c in CHECKS])
def test_a_good_value_still_passes(_what: str, build: Callable[[str], object], good: str) -> None:
    build(good)


@pytest.mark.parametrize(("_what", "build", "good"), CHECKS, ids=[c[0] for c in CHECKS])
def test_a_trailing_newline_is_refused(
    _what: str, build: Callable[[str], object], good: str
) -> None:
    with pytest.raises((InvalidUsernameError, ValidationError)):
        build(good + "\n")


def test_no_pattern_is_applied_with_match() -> None:
    """The defect was copied five times, so the next copy is the one to stop.

    ``fullmatch`` is the only way these patterns are used. A ``match`` on one of
    them is the bug above, whatever the pattern ends with.
    """
    offenders = [
        f"{path.relative_to(SOURCE)}:{number}"
        for path in sorted(SOURCE.rglob("*.py"))
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if re.search(r"_PATTERN\.match\(", line)
    ]

    assert offenders == []
