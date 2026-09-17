"""The real GPIO backend, against gpiozero's own mock pin factory.

This module was previously asserted to be *absent*: its only appearance anywhere
in the suite was as a name expected to fail importing. Replacing the whole file
with a single comment left every test passing, while it is the only code in the
project that touches a mains circuit.

gpiozero ships ``MockFactory`` so that this needs no Pi. Nothing here is a
substitute for running it against real relays — a mock pin cannot float, cannot
be held by another process and cannot brown out — but it does hold the two things
that were actually wrong: which pull the probe applies, and what happens on a pin
whose pull cannot be changed.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

gpiozero = pytest.importorskip("gpiozero", reason="gpiozero lives in the optional rpi extra")

from gpiozero import Device  # noqa: E402
from gpiozero.pins.mock import MockFactory  # noqa: E402

from pihome_hub.relays import MockRelayBackend, RelayConfig, RelayService  # noqa: E402
from pihome_hub.relays.factory import create_backend  # noqa: E402
from pihome_hub.relays.gpio import GpioZeroRelayBackend  # noqa: E402

#: BCM pins carrying fixed board pull-up resistors, which gpiozero will not let
#: anything override — not even to leave them alone.
FIXED_PULL_PINS = (2, 3)


@pytest.fixture
def pins() -> Iterator[MockFactory]:
    """Route every gpiozero device in this module to a fresh mock pin factory.

    The previous factory is restored rather than dropped: ``Device.pin_factory``
    is module-level state in gpiozero, and leaving a mock in place would follow
    the test session into whatever ran next.
    """
    factory = MockFactory()
    previous = Device.pin_factory
    Device.pin_factory = factory
    try:
        yield factory
    finally:
        factory.reset()
        Device.pin_factory = previous


@pytest.fixture
def backend() -> GpioZeroRelayBackend:
    return GpioZeroRelayBackend()


class TestTheProbe:
    def test_it_does_not_pull_the_line_it_is_reading(
        self, pins: MockFactory, backend: GpioZeroRelayBackend
    ) -> None:
        """The defect, stated directly.

        ``pull_up=False`` does not mean "leave the pull alone" — gpiozero applies
        a pull-down and then measures the level it just created. With the default
        ``active_low`` that inversion reads as logical *on*, so a relay closed a
        mains circuit at startup on the strength of the probe's own pull.
        """
        backend.read_level(17, active_low=True)

        assert pins.pin(17).pull == "floating"

    def test_it_reports_the_level_the_line_is_driven_to(
        self, pins: MockFactory, backend: GpioZeroRelayBackend
    ) -> None:
        pins.pin(17).drive_high()
        assert backend.read_level(17, active_low=False) is True

        pins.pin(17).drive_low()
        assert backend.read_level(17, active_low=False) is False

    def test_active_low_inverts_what_the_line_says(
        self, pins: MockFactory, backend: GpioZeroRelayBackend
    ) -> None:
        """An active-low board energises its relay by pulling the line down."""
        pins.pin(17).drive_low()
        assert backend.read_level(17, active_low=True) is True

        pins.pin(17).drive_high()
        assert backend.read_level(17, active_low=True) is False

    @pytest.mark.parametrize("pin", FIXED_PULL_PINS)
    def test_a_fixed_pull_pin_reads_as_unknown_rather_than_raising(
        self, pins: MockFactory, backend: GpioZeroRelayBackend, pin: int
    ) -> None:
        """GPIO 2 and 3 used to make startup fail outright.

        gpiozero raises ``PinFixedPull``, RelayService wrapped it in
        RelayHardwareError, and the operator was told to check the pin was not in
        use and that they were in the 'gpio' group — none of which was the cause
        and none of which would have helped.
        """
        assert backend.read_level(pin, active_low=True) is None

    def test_it_releases_the_pin_so_the_output_can_claim_it(
        self, pins: MockFactory, backend: GpioZeroRelayBackend
    ) -> None:
        """A probe that held its pin would make every relay fail right after it."""
        backend.read_level(17, active_low=True)

        backend.setup_output(17, active_low=True, initial=False)

        assert pins.pin(17).function == "output"


class TestDrivingThePin:
    def test_setup_output_drives_the_initial_level_immediately(
        self, pins: MockFactory, backend: GpioZeroRelayBackend
    ) -> None:
        """No window in which the pin is claimed but not yet at the right level."""
        backend.setup_output(17, active_low=True, initial=True)

        assert pins.pin(17).function == "output"
        # Active-low: logical on is a low line.
        assert pins.pin(17).state == 0

    def test_setup_output_respects_active_high(
        self, pins: MockFactory, backend: GpioZeroRelayBackend
    ) -> None:
        backend.setup_output(17, active_low=False, initial=True)

        assert pins.pin(17).state == 1

    def test_write_moves_the_line(self, pins: MockFactory, backend: GpioZeroRelayBackend) -> None:
        backend.setup_output(17, active_low=True, initial=False)
        assert pins.pin(17).state == 1

        backend.write(17, on=True)
        assert pins.pin(17).state == 0

        backend.write(17, on=False)
        assert pins.pin(17).state == 1

    def test_close_releases_the_pin(self, pins: MockFactory, backend: GpioZeroRelayBackend) -> None:
        backend.setup_output(17, active_low=True, initial=True)

        backend.close(17)

        assert pins.pin(17).function == "input"

    def test_close_is_safe_on_a_pin_that_was_never_claimed(
        self, pins: MockFactory, backend: GpioZeroRelayBackend
    ) -> None:
        """Startup unwinds by closing everything, including what it never opened."""
        backend.close(17)


class TestCreateBackend:
    def test_gpiozero_is_the_real_backend(self) -> None:
        assert isinstance(create_backend("gpiozero"), GpioZeroRelayBackend)

    def test_mock_is_the_mock_backend(self) -> None:
        assert isinstance(create_backend("mock"), MockRelayBackend)


class TestPreserveThroughTheService:
    """End to end, because the defect was in how the two halves fit together."""

    def test_it_follows_the_line_rather_than_the_probe(
        self, pins: MockFactory, backend: GpioZeroRelayBackend
    ) -> None:
        pins.pin(17).drive_high()  # active-low board: relay open

        service = RelayService(backend, [RelayConfig(id="porch-light", pin=17, label="P")])
        try:
            assert service.status()["porch-light"] is False
        finally:
            service.close()

    def test_it_preserves_an_energised_relay(
        self, pins: MockFactory, backend: GpioZeroRelayBackend
    ) -> None:
        pins.pin(17).drive_low()  # active-low board: relay closed

        service = RelayService(backend, [RelayConfig(id="porch-light", pin=17, label="P")])
        try:
            assert service.status()["porch-light"] is True
        finally:
            service.close()

    @pytest.mark.parametrize("pin", FIXED_PULL_PINS)
    def test_a_fixed_pull_pin_starts_de_energised_instead_of_failing(
        self, pins: MockFactory, backend: GpioZeroRelayBackend, pin: int
    ) -> None:
        """It used to raise, so a relay on GPIO 2 or 3 could not start at all.

        Of the two guesses available when the level cannot be read, only one of
        them closes a mains circuit on a house nobody is watching.
        """
        service = RelayService(backend, [RelayConfig(id="porch-light", pin=pin, label="P")])
        try:
            assert service.status()["porch-light"] is False
        finally:
            service.close()
