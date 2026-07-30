"""Relay domain logic.

Resolves configuration into hardware state at startup and exposes logical
on/off/toggle operations — independent of both the transport (HTTP, landing in
Phase 3) and the backend (mock or real GPIO).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping

from pihome_hub.relays.backend import RelayBackend
from pihome_hub.relays.errors import RelayConfigError, UnknownRelayError
from pihome_hub.relays.models import RelayConfig

logger = logging.getLogger(__name__)


class RelayService:
    """Owns every configured relay for the lifetime of the process."""

    def __init__(self, backend: RelayBackend, relays: Iterable[RelayConfig]) -> None:
        self._backend = backend
        self._relays: dict[str, RelayConfig] = {}
        self._state: dict[str, bool] = {}

        seen_pins: dict[int, str] = {}
        for relay in relays:
            if relay.id in self._relays:
                msg = f"duplicate relay id {relay.id!r}"
                raise RelayConfigError(msg)
            if relay.pin in seen_pins:
                msg = f"pin {relay.pin} is used by both {seen_pins[relay.pin]!r} and {relay.id!r}"
                raise RelayConfigError(msg)
            seen_pins[relay.pin] = relay.id
            self._relays[relay.id] = relay

        for relay in self._relays.values():
            initial = self._resolve_initial_state(relay)
            self._backend.setup_output(relay.pin, active_low=relay.active_low, initial=initial)
            self._state[relay.id] = initial
            logger.info(
                "relay ready",
                extra={"relay_id": relay.id, "pin": relay.pin, "initial_on": initial},
            )

    def _resolve_initial_state(self, relay: RelayConfig) -> bool:
        if relay.initial_state == "on":
            return True
        if relay.initial_state == "off":
            return False
        return self._backend.read_level(relay.pin, active_low=relay.active_low)

    def _require(self, relay_id: str) -> RelayConfig:
        try:
            return self._relays[relay_id]
        except KeyError:
            raise UnknownRelayError(relay_id) from None

    def _set(self, relay_id: str, *, on: bool) -> bool:
        relay = self._require(relay_id)
        self._backend.write(relay.pin, on=on)
        self._state[relay.id] = on
        return on

    def turn_on(self, relay_id: str) -> bool:
        return self._set(relay_id, on=True)

    def turn_off(self, relay_id: str) -> bool:
        return self._set(relay_id, on=False)

    def toggle(self, relay_id: str) -> bool:
        relay = self._require(relay_id)
        return self._set(relay.id, on=not self._state[relay.id])

    def status(self) -> Mapping[str, bool]:
        return dict(self._state)

    def turn_on_all(self) -> Mapping[str, bool]:
        for relay_id in self._relays:
            self._set(relay_id, on=True)
        return self.status()

    def turn_off_all(self) -> Mapping[str, bool]:
        for relay_id in self._relays:
            self._set(relay_id, on=False)
        return self.status()

    def toggle_all(self) -> Mapping[str, bool]:
        """Invert every relay independently, based on its own current state.

        The tempting alternative — read one "representative" relay, then apply its
        inverse to all of them — quietly destroys information: any relay that had
        drifted out of sync, through a manual flip or a dropped request, gets
        overridden by an unrelated pin's level. Toggling each relay against its own
        state cannot desync relays that agreed, nor worsen a disagreement.
        """
        for relay_id in self._relays:
            self._set(relay_id, on=not self._state[relay_id])
        return self.status()

    def close(self) -> None:
        for relay in self._relays.values():
            self._backend.close(relay.pin)
