"""Real hardware backend, built on gpiozero.

Requires the ``rpi`` extra (``pip install -e '.[rpi]'``). This module is not
imported by :mod:`pihome_hub.relays`, so importing that package — and running
the rest of the service — never requires gpiozero or a Raspberry Pi to be present.

This module has not been exercised against real relays; it has been checked for
API correctness against gpiozero's documented constructors, and no further.
Treat a first deployment as a test, not a given.
"""

from __future__ import annotations

from gpiozero import DigitalInputDevice, OutputDevice


class GpioZeroRelayBackend:
    """Drives relays through gpiozero.

    gpiozero auto-selects the pin driver available on the host — lgpio on
    current Raspberry Pi OS, with other drivers as a fallback — so this class
    itself never talks to a specific GPIO library.
    """

    def __init__(self) -> None:
        self._devices: dict[int, OutputDevice] = {}

    def read_level(self, pin: int, *, active_low: bool) -> bool:
        # A floating read of a pin nothing has claimed yet. `pull_up=False`
        # matches the BCM default pull-down present on most GPIOs; a relay board
        # wired to a pin with a different boot-time pull may read incorrectly
        # here. `initial_state: on`/`off` sidesteps the question entirely by
        # not reading the pin at all.
        with DigitalInputDevice(pin, pull_up=False) as probe:
            raw = bool(probe.is_active)
        return (not raw) if active_low else raw

    def setup_output(self, pin: int, *, active_low: bool, initial: bool) -> None:
        self._devices[pin] = OutputDevice(pin, active_high=not active_low, initial_value=initial)

    def write(self, pin: int, *, on: bool) -> None:
        device = self._devices[pin]
        if on:
            device.on()
        else:
            device.off()

    def close(self, pin: int) -> None:
        device = self._devices.pop(pin, None)
        if device is not None:
            device.close()
