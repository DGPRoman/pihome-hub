"""One contract, run against both backends.

``backend.py`` says every raw pin level lives behind the interface, that callers
speak only in logical on/off, and that active-low inversion is resolved once by
the implementation that owns the pin. Nothing held either implementation to that.
Each had its own test module asserting its own behaviour, so the mock was free to
define the contract and the real backend was free to differ from it — and the
difference would only ever have shown up on a Pi, with a relay on the end of it.

The two cannot be driven identically, and pretending otherwise is what makes a
contract suite vacuous. The mock has no voltage: it stores logical state, because
there is no pin for a level to be on. The real backend has nothing else: it stores
a pin and computes the logical level from it. So each gets a small adapter for the
two things the protocol does not provide — put this pin in a known state, and say
what its logical level is — and everything below is written once, against those.

For the real backend the adapter goes through the *raw* pin, which is the point:
a ``setup_output`` that inverted ``active_high`` by mistake would drive the wrong
voltage and still report itself as correct. Reading the raw line is the only way
to catch that, and it is the line the relay is actually wired to.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

import pytest

from pihome_hub.relays import MockRelayBackend
from pihome_hub.relays.backend import RelayBackend

#: Not a fixed-pull pin: those are covered in the gpiozero module, where the
#: answer is "unknown", and they would say nothing about this contract.
PIN = 17


class BackendUnderTest(Protocol):
    """A backend, plus what a test needs that the protocol deliberately omits."""

    @property
    def backend(self) -> RelayBackend:
        """The implementation under test.

        Read-only, which is what makes it covariant: an adapter holding a
        ``GpioZeroRelayBackend`` satisfies this, where a plain attribute would be
        invariant and would not.
        """

    def given_level(self, pin: int, *, active_low: bool, on: bool) -> None:
        """Put ``pin`` in a state that means logical ``on``, before it is claimed."""

    def logical_level(self, pin: int, *, active_low: bool) -> bool:
        """What this backend has the relay doing, as the service would see it."""


class MockUnderTest:
    """The mock, which stores logical state because it has no line to hold a level."""

    def __init__(self) -> None:
        self.backend = MockRelayBackend()

    def given_level(self, pin: int, *, active_low: bool, on: bool) -> None:
        # active_low is irrelevant here and that is not an oversight: there is no
        # voltage to invert. Seeding is stated in the same terms the mock stores.
        self.backend.seed_level(pin, on=on)

    def logical_level(self, pin: int, *, active_low: bool) -> bool:
        return self.backend.is_on(pin)


@pytest.fixture
def mock_under_test() -> MockUnderTest:
    return MockUnderTest()


@pytest.fixture
def gpiozero_under_test() -> Iterator[BackendUnderTest]:
    """The real backend on gpiozero's mock pin factory, or skipped.

    Skipped rather than absent so that a run without the hardware extra says the
    contract went unchecked against the implementation that drives mains, instead
    of reporting a full pass on half of it.
    """
    pytest.importorskip("gpiozero", reason="gpiozero lives in the optional rpi extra")

    # Imported here rather than at the top, and the whole module is not guarded by a
    # module-level importorskip the way tests/relays/test_gpio_backend.py is. That
    # would skip this file entirely without the extra — taking the mock half of the
    # contract with it, which needs nothing and must always run.
    from gpiozero import Device  # noqa: PLC0415
    from gpiozero.pins.mock import MockFactory  # noqa: PLC0415

    from pihome_hub.relays.gpio import GpioZeroRelayBackend  # noqa: PLC0415

    class GpioZeroUnderTest:
        def __init__(self, factory: MockFactory) -> None:
            self.backend = GpioZeroRelayBackend()
            self._factory = factory

        def given_level(self, pin: int, *, active_low: bool, on: bool) -> None:
            # Through the raw line. "Logical on" is a low line when active_low,
            # which is the inversion this whole contract exists to pin down.
            if on != active_low:
                self._factory.pin(pin).drive_high()
            else:
                self._factory.pin(pin).drive_low()

        def logical_level(self, pin: int, *, active_low: bool) -> bool:
            raw = self._factory.pin(pin).state == 1
            return (not raw) if active_low else raw

    factory = MockFactory()
    previous = Device.pin_factory
    Device.pin_factory = factory
    try:
        yield GpioZeroUnderTest(factory)
    finally:
        factory.reset()
        Device.pin_factory = previous


@pytest.fixture(params=["mock", "gpiozero"])
def subject(request: pytest.FixtureRequest) -> BackendUnderTest:
    """Both implementations, one after the other."""
    fixture: BackendUnderTest = request.getfixturevalue(f"{request.param}_under_test")
    return fixture


@pytest.mark.parametrize("active_low", [False, True], ids=["active-high", "active-low"])
class TestTheLogicalRoundTrip:
    """Whatever the wiring, what goes in comes out.

    The one property the service depends on everywhere. Every caller above this
    seam says "on" and means energised; a backend that inverts on the way in and
    not on the way out satisfies its own tests and lies to all of them.
    """

    def test_setup_drives_the_relay_to_its_initial_state(
        self, subject: BackendUnderTest, active_low: bool
    ) -> None:
        subject.backend.setup_output(PIN, active_low=active_low, initial=True)
        assert subject.logical_level(PIN, active_low=active_low) is True

    def test_setup_can_leave_the_relay_de_energised(
        self, subject: BackendUnderTest, active_low: bool
    ) -> None:
        subject.backend.setup_output(PIN, active_low=active_low, initial=False)
        assert subject.logical_level(PIN, active_low=active_low) is False

    @pytest.mark.parametrize("wanted", [True, False])
    def test_a_write_reaches_the_relay_as_written(
        self, subject: BackendUnderTest, active_low: bool, wanted: bool
    ) -> None:
        subject.backend.setup_output(PIN, active_low=active_low, initial=not wanted)

        subject.backend.write(PIN, on=wanted)

        assert subject.logical_level(PIN, active_low=active_low) is wanted

    def test_writes_can_be_repeated_and_reversed(
        self, subject: BackendUnderTest, active_low: bool
    ) -> None:
        subject.backend.setup_output(PIN, active_low=active_low, initial=False)

        for wanted in (True, True, False, True, False, False):
            subject.backend.write(PIN, on=wanted)
            assert subject.logical_level(PIN, active_low=active_low) is wanted


@pytest.mark.parametrize("active_low", [False, True], ids=["active-high", "active-low"])
class TestReadingBeforeTheLineIsClaimed:
    """``read_level`` answers in logical terms, whichever way the relay is wired.

    Stated as the round trip of the adapter rather than as a literal: the two
    backends are told to put the pin in the state that means logical *on*, and
    both have to agree that is what it means. A backend that dropped the
    inversion would answer the opposite on one of the two parameters.
    """

    @pytest.mark.parametrize("level", [True, False])
    def test_it_reports_the_level_the_relay_is_at(
        self, subject: BackendUnderTest, active_low: bool, level: bool
    ) -> None:
        subject.given_level(PIN, active_low=active_low, on=level)

        assert subject.backend.read_level(PIN, active_low=active_low) is level


class TestClaimingAndReleasing:
    def test_a_write_before_setup_is_refused(self, subject: BackendUnderTest) -> None:
        """Rather than silently doing nothing, or driving a pin nobody claimed."""
        with pytest.raises(Exception):  # noqa: B017, PT011 - each backend raises its own
            subject.backend.write(PIN, on=True)

    def test_a_write_after_close_is_refused(self, subject: BackendUnderTest) -> None:
        subject.backend.setup_output(PIN, active_low=False, initial=False)
        subject.backend.close(PIN)

        with pytest.raises(Exception):  # noqa: B017, PT011
            subject.backend.write(PIN, on=True)

    def test_closing_a_pin_that_was_never_claimed_is_harmless(
        self, subject: BackendUnderTest
    ) -> None:
        """Shutdown runs after a startup that may have failed part way through."""
        subject.backend.close(PIN)

    def test_closing_twice_is_harmless(self, subject: BackendUnderTest) -> None:
        subject.backend.setup_output(PIN, active_low=False, initial=False)

        subject.backend.close(PIN)
        subject.backend.close(PIN)

    def test_a_pin_can_be_claimed_again_after_being_released(
        self, subject: BackendUnderTest
    ) -> None:
        """The real one holds a device object; releasing has to actually release it."""
        subject.backend.setup_output(PIN, active_low=False, initial=True)
        subject.backend.close(PIN)

        subject.backend.setup_output(PIN, active_low=False, initial=False)

        assert subject.logical_level(PIN, active_low=False) is False
