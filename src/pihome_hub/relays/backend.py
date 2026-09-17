"""The seam between relay logic and hardware.

Every raw pin level lives behind this interface — callers speak only in logical
on/off, and active-low inversion is resolved once, by the implementation that
owns the pin. Two implementations exist: :class:`~pihome_hub.relays.mock.MockRelayBackend`
for development and tests, and :class:`~pihome_hub.relays.gpio.GpioZeroRelayBackend`
for a real Pi.
"""

from __future__ import annotations

from typing import Protocol


class RelayBackend(Protocol):
    """Hardware access required to drive one relay per GPIO pin."""

    def read_level(self, pin: int, *, active_low: bool) -> bool | None:
        """Best-effort read of ``pin``'s current logical level, or ``None``.

        Called *before* this process claims the pin as an output, so that
        ``initial_state: preserve`` never has to drive a pin blind. Reading only
        after claiming the pin would report whatever this process just wrote, not
        what the relay was actually doing.

        ``None`` means the level could not be established — not that it was low.
        A plain ``bool`` cannot say that, and the difference matters: the caller
        resolves an unknown level by leaving the relay de-energised, where
        reporting a confident ``False`` on a pin whose ``active_low`` inverts it
        would close a mains circuit at startup.
        """

    def setup_output(self, pin: int, *, active_low: bool, initial: bool) -> None:
        """Claim ``pin`` as an output and drive it to ``initial`` immediately.

        There is no intermediate state in which the relay is briefly wrong.
        """

    def write(self, pin: int, *, on: bool) -> None:
        """Drive ``pin``, already claimed by :meth:`setup_output`, to a logical state."""

    def close(self, pin: int) -> None:
        """Release ``pin``. Safe to call on a pin that was never set up."""
