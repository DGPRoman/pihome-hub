"""In-memory relay backend used for development, tests and CI.

Nothing here touches real hardware, which is the point: the rest of the service
runs and is tested identically whether or not a Raspberry Pi is attached.
"""

from __future__ import annotations


class MockRelayBackend:
    """A :class:`~pihome_hub.relays.backend.RelayBackend` with no physical pins."""

    def __init__(self) -> None:
        self._levels: dict[int, bool] = {}
        self._claimed: set[int] = set()

    def seed_level(self, pin: int, *, on: bool) -> None:
        """Test hook: pretend ``pin`` was already at this logical level.

        The mock has no physical world to inspect, so unlike the real backend it
        cannot infer what a relay was doing before this process started — a test
        that wants to exercise ``initial_state: preserve`` has to say so explicitly.
        """
        self._levels[pin] = on

    def read_level(self, pin: int, *, active_low: bool) -> bool:
        return self._levels.get(pin, False)

    def setup_output(self, pin: int, *, active_low: bool, initial: bool) -> None:
        self._claimed.add(pin)
        self._levels[pin] = initial

    def write(self, pin: int, *, on: bool) -> None:
        if pin not in self._claimed:
            msg = f"pin {pin} was written to before setup_output()"
            raise RuntimeError(msg)
        self._levels[pin] = on

    def close(self, pin: int) -> None:
        self._claimed.discard(pin)

    def is_on(self, pin: int) -> bool:
        """Test helper: the logical level this backend last recorded for ``pin``."""
        return self._levels.get(pin, False)
