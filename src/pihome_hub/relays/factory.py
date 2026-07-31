"""Chooses a relay backend from configuration."""

from __future__ import annotations

from typing import Literal

from pihome_hub.relays.backend import RelayBackend
from pihome_hub.relays.errors import RelayConfigError
from pihome_hub.relays.mock import MockRelayBackend

#: Which backend to drive relays through.
#:
#: ``mock`` is the default on purpose. An "auto-detect" default would silently
#: fall back to the mock if the GPIO library failed to load on a real Pi, leaving
#: an operator convinced the service is switching relays when it is switching
#: nothing. Driving real hardware is opt-in.
GpioBackendName = Literal["mock", "gpiozero"]


def create_backend(name: GpioBackendName) -> RelayBackend:
    """Instantiate the named backend, or fail with an actionable message."""
    if name == "mock":
        return MockRelayBackend()

    # Imported lazily and on purpose: gpiozero lives in the optional `rpi` extra,
    # so a top-level import would make this module — and everything that reads
    # configuration — unimportable on a machine without it.
    try:
        from pihome_hub.relays.gpio import GpioZeroRelayBackend  # noqa: PLC0415
    except ImportError as exc:
        msg = (
            "PIHOME_GPIO_BACKEND=gpiozero was requested but gpiozero is not importable. "
            "Install the hardware extra on the Pi: pip install '.[rpi]'"
        )
        raise RelayConfigError(msg) from exc

    return GpioZeroRelayBackend()
