"""Relay domain: configuration, hardware backends, and control logic.

:class:`~pihome_hub.relays.gpio.GpioZeroRelayBackend` is deliberately not
re-exported here — importing it requires the ``rpi`` extra, and everything
else in this package must import cleanly on a machine that does not have it.
"""

from __future__ import annotations

from pihome_hub.relays.backend import RelayBackend
from pihome_hub.relays.config import load_relays
from pihome_hub.relays.errors import RelayConfigError, RelayError, UnknownRelayError
from pihome_hub.relays.mock import MockRelayBackend
from pihome_hub.relays.models import RelayConfig
from pihome_hub.relays.service import RelayService

__all__ = [
    "MockRelayBackend",
    "RelayBackend",
    "RelayConfig",
    "RelayConfigError",
    "RelayError",
    "RelayService",
    "UnknownRelayError",
    "load_relays",
]
