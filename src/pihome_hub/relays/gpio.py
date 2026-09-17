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
from gpiozero.exc import PinFixedPull


class GpioZeroRelayBackend:
    """Drives relays through gpiozero.

    gpiozero auto-selects the pin driver available on the host — lgpio on
    current Raspberry Pi OS, with other drivers as a fallback — so this class
    itself never talks to a specific GPIO library.
    """

    def __init__(self) -> None:
        self._devices: dict[int, OutputDevice] = {}

    def read_level(self, pin: int, *, active_low: bool) -> bool | None:
        """Read what the relay is doing, without telling it what to do.

        ``pull_up=None`` is the whole point. gpiozero's ``InputDevice`` applies a
        pull unless told not to, so ``pull_up=False`` did not mean "leave the line
        alone" — it drove a pull-down and then measured the level it had just
        created. On an undriven pin that reads low, and with the default
        ``active_low`` the inversion turns low into logical *on*: a relay closing
        a mains circuit at startup on the strength of the probe's own pull. BCM 0
        to 8 boot with a pull-*up*, so on those the old probe actively inverted
        what it was trying to observe.

        ``active_state`` has to be given alongside: with no pull there is no
        resting level, and gpiozero refuses to guess which way is active.
        """
        try:
            with DigitalInputDevice(pin, pull_up=None, active_state=True) as probe:
                raw = bool(probe.value)
        except PinFixedPull:
            # GPIO 2 and 3 carry fixed board pull-up resistors, which gpiozero will
            # not let anything override — not even to leave them alone. Their level
            # therefore describes the board rather than the relay, so there is
            # nothing here to preserve. Reported as unknown rather than raised: a
            # relay on one of these pins can still be driven perfectly well, and
            # refusing to start over a reading nobody can take would be worse than
            # starting it de-energised.
            return None
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
