"""AutomationEngine: rule matching, hold timers, and shutdown."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest

from pihome_hub.automation import (
    Action,
    AutomationConfigError,
    AutomationEngine,
    AutomationRule,
    Trigger,
)
from pihome_hub.relays import RelayConfig, RelayService
from pihome_hub.sensors import SensorDevice, SensorReading, SensorStore
from tests.conftest import CountingRelayBackend

#: Short enough to keep the suite fast, long enough not to race the assertions.
HOLD = 0.05

PORCH_PIN = 17
GATE_PIN = 27


def make_relays(backend: CountingRelayBackend | None = None) -> RelayService:
    return RelayService(
        backend if backend is not None else CountingRelayBackend(),
        [
            RelayConfig(id="porch-light", pin=PORCH_PIN, label="Porch"),
            RelayConfig(id="gate-light", pin=GATE_PIN, label="Gate"),
        ],
    )


def make_sensors() -> SensorStore:
    return SensorStore(
        [
            SensorDevice(id="porch-motion", label="Porch motion"),
            SensorDevice(id="gate-camera", label="Gate camera"),
        ]
    )


def motion_rule(
    *,
    rule_id: str = "porch-motion-light",
    device: str = "porch-motion",
    relay: str = "porch-light",
    hold: float | None = None,
    after_dark: bool = False,
    motion: bool = True,
    state: Literal["on", "off"] = "on",
) -> AutomationRule:
    return AutomationRule(
        id=rule_id,
        when=Trigger(device=device, motion=motion),
        then=Action(relay=relay, state=state, hold_seconds=hold),
        only_after_dark=after_dark,
    )


class AlwaysDark:
    def is_dark(self) -> bool:
        return True


class NeverDark:
    def is_dark(self) -> bool:
        return False


class BrokenOracle:
    """An oracle that cannot answer.

    Real once: a hub configured above the Arctic circle raised out of astral for
    the whole of the polar day. That is fixed at the source, but the engine is
    handed a `DarknessOracle` protocol and has no way to know what is behind it.
    """

    def is_dark(self) -> bool:
        msg = "expected a number in range from -1 up to 1, got -2.63"
        raise ValueError(msg)


@pytest.mark.anyio
class TestRuleMatching:
    async def test_a_matching_reading_fires_the_rule(self) -> None:
        relays = make_relays()
        engine = AutomationEngine(relays, [motion_rule()], sensors=make_sensors())

        fired = await engine.handle_reading(
            "porch-motion", SensorReading(motion=True), previous_motion=False
        )

        assert fired == ["porch-motion-light"]
        assert relays.state_of("porch-light") is True

    async def test_a_different_device_does_not_fire(self) -> None:
        relays = make_relays()
        engine = AutomationEngine(relays, [motion_rule()], sensors=make_sensors())

        fired = await engine.handle_reading(
            "gate-camera", SensorReading(motion=True), previous_motion=False
        )

        assert fired == []
        assert relays.state_of("porch-light") is False

    async def test_the_opposite_motion_value_does_not_fire(self) -> None:
        relays = make_relays()
        engine = AutomationEngine(relays, [motion_rule()], sensors=make_sensors())

        assert (
            await engine.handle_reading(
                "porch-motion", SensorReading(motion=False), previous_motion=True
            )
            == []
        )
        assert relays.state_of("porch-light") is False

    async def test_a_climate_only_reading_fires_nothing(self) -> None:
        relays = make_relays()
        engine = AutomationEngine(relays, [motion_rule()], sensors=make_sensors())

        assert (
            await engine.handle_reading(
                "porch-motion", SensorReading(temperature=20.0), previous_motion=None
            )
            == []
        )

    async def test_a_disabled_rule_never_fires(self) -> None:
        relays = make_relays()
        rule = motion_rule().model_copy(update={"enabled": False})
        engine = AutomationEngine(relays, [rule], sensors=make_sensors())

        assert (
            await engine.handle_reading(
                "porch-motion", SensorReading(motion=True), previous_motion=False
            )
            == []
        )


@pytest.mark.anyio
class TestEdgeDetection:
    """A rule answers a change in the house, not a report about it.

    A motion sensor reporting on an interval sends the same value over and over.
    Acting on each one means the house re-issues its command every few seconds,
    so nothing an operator does survives longer than one reporting period.
    """

    async def test_an_unchanged_reading_does_not_fire(self) -> None:
        relays = make_relays()
        engine = AutomationEngine(relays, [motion_rule()], sensors=make_sensors())

        fired = await engine.handle_reading(
            "porch-motion", SensorReading(motion=True), previous_motion=True
        )

        assert fired == []

    async def test_an_unchanged_reading_does_not_write_to_the_relay(self) -> None:
        backend = CountingRelayBackend()
        relays = make_relays(backend)
        engine = AutomationEngine(relays, [motion_rule()], sensors=make_sensors())

        for _ in range(3):
            await engine.handle_reading(
                "porch-motion", SensorReading(motion=True), previous_motion=True
            )

        assert backend.writes_to(PORCH_PIN) == []

    async def test_the_operators_value_survives_an_unchanged_reading(self) -> None:
        """The whole point of the change, end to end at engine level.

        Motion arrives and the light comes on. Somebody turns it off. The sensor,
        which has seen no change, reports the same value again — and used to drive
        the light straight back on, with no way for the operator to win.
        """
        relays = make_relays()
        engine = AutomationEngine(relays, [motion_rule()], sensors=make_sensors())
        await engine.handle_reading(
            "porch-motion", SensorReading(motion=True), previous_motion=False
        )
        assert relays.state_of("porch-light") is True

        relays.turn_off("porch-light")
        await engine.handle_reading(
            "porch-motion", SensorReading(motion=True), previous_motion=True
        )

        assert relays.state_of("porch-light") is False, "an unchanged reading overrode the operator"

    async def test_the_first_reading_from_a_device_fires(self) -> None:
        """``None`` is not a value the sensor ever reported — it means the hub has
        just learned the state of the room. Treating that as "no news" would leave
        the light off with somebody standing under it."""
        relays = make_relays()
        engine = AutomationEngine(relays, [motion_rule()], sensors=make_sensors())

        fired = await engine.handle_reading(
            "porch-motion", SensorReading(motion=True), previous_motion=None
        )

        assert fired == ["porch-motion-light"]
        assert relays.state_of("porch-light") is True

    async def test_motion_returning_after_a_gap_fires_again(self) -> None:
        """Edge detection must not be a one-shot: every genuine transition counts."""
        backend = CountingRelayBackend()
        relays = make_relays(backend)
        engine = AutomationEngine(relays, [motion_rule()], sensors=make_sensors())

        await engine.handle_reading(
            "porch-motion", SensorReading(motion=True), previous_motion=False
        )
        relays.turn_off("porch-light")
        await engine.handle_reading(
            "porch-motion", SensorReading(motion=False), previous_motion=True
        )
        fired = await engine.handle_reading(
            "porch-motion", SensorReading(motion=True), previous_motion=False
        )

        assert fired == ["porch-motion-light"]
        assert backend.writes_to(PORCH_PIN) == [True, False, True]


@pytest.mark.anyio
class TestOperatorOverride:
    async def test_the_revert_is_skipped_when_the_relay_has_moved_since(self) -> None:
        """The rule's claim expires with the state it set.

        Reverting means applying the inverse of a decision that is no longer in
        force — which is how a hold ends up switching off a light somebody asked
        for a moment ago.

        Asserted on writes, not on state. Here the inverse the rule would apply
        happens to be the state the relay is already in, so an unguarded revert
        leaves the house looking exactly the same and only the extra command
        betrays it. That is not a quirk of this test: it is the case the guard
        exists for, because the two situations differ only in who decided.
        """
        backend = CountingRelayBackend()
        relays = make_relays(backend)
        engine = AutomationEngine(relays, [motion_rule(hold=HOLD)], sensors=make_sensors())
        await engine.handle_reading(
            "porch-motion", SensorReading(motion=True), previous_motion=False
        )

        relays.turn_off("porch-light")
        await asyncio.sleep(HOLD * 3)

        assert backend.writes_to(PORCH_PIN) == [True, False], "the revert re-applied its inverse"
        assert relays.state_of("porch-light") is False

    async def test_a_revert_that_is_still_owed_still_happens(self) -> None:
        """The guard must not become an excuse to never revert anything."""
        relays = make_relays()
        engine = AutomationEngine(relays, [motion_rule(hold=HOLD)], sensors=make_sensors())
        await engine.handle_reading(
            "porch-motion", SensorReading(motion=True), previous_motion=False
        )

        await asyncio.sleep(HOLD * 3)

        assert relays.state_of("porch-light") is False

    async def test_an_off_rule_reverts_to_on(self) -> None:
        """The guard compares against what the rule applied, not against a constant."""
        relays = make_relays()
        relays.turn_on("porch-light")
        rule = motion_rule(hold=HOLD, state="off")
        engine = AutomationEngine(relays, [rule], sensors=make_sensors())

        await engine.handle_reading(
            "porch-motion", SensorReading(motion=True), previous_motion=False
        )
        assert relays.state_of("porch-light") is False

        await asyncio.sleep(HOLD * 3)

        assert relays.state_of("porch-light") is True

    async def test_release_hold_calls_off_a_pending_revert(self) -> None:
        relays = make_relays()
        engine = AutomationEngine(relays, [motion_rule(hold=HOLD)], sensors=make_sensors())
        await engine.handle_reading(
            "porch-motion", SensorReading(motion=True), previous_motion=False
        )

        assert engine.release_hold("porch-light") is True
        await asyncio.sleep(HOLD * 3)

        assert engine.pending_holds == frozenset()
        assert relays.state_of("porch-light") is True, "a released hold still fired"

    async def test_release_hold_reports_when_there_was_nothing_to_release(self) -> None:
        relays = make_relays()
        engine = AutomationEngine(relays, [motion_rule(hold=HOLD)], sensors=make_sensors())

        assert engine.release_hold("porch-light") is False
        assert engine.release_hold("gate-light") is False

    async def test_a_released_hold_is_no_longer_reported_as_pending(self) -> None:
        relays = make_relays()
        engine = AutomationEngine(relays, [motion_rule(hold=HOLD)], sensors=make_sensors())
        await engine.handle_reading(
            "porch-motion", SensorReading(motion=True), previous_motion=False
        )
        assert engine.hold_expiry("porch-light") is not None

        engine.release_hold("porch-light")

        assert engine.hold_expiry("porch-light") is None
        await engine.aclose()


@pytest.mark.anyio
class TestHoldExpiryReporting:
    async def test_the_expiry_is_the_hold_measured_from_now(self) -> None:
        relays = make_relays()
        started = datetime(2026, 9, 21, 22, 0, tzinfo=UTC)
        engine = AutomationEngine(
            relays, [motion_rule(hold=90.0)], sensors=make_sensors(), clock=lambda: started
        )

        await engine.handle_reading(
            "porch-motion", SensorReading(motion=True), previous_motion=False
        )

        assert engine.hold_expiry("porch-light") == started + timedelta(seconds=90)
        await engine.aclose()

    async def test_a_relay_with_no_hold_reports_none(self) -> None:
        relays = make_relays()
        engine = AutomationEngine(relays, [motion_rule(hold=HOLD)], sensors=make_sensors())
        await engine.handle_reading(
            "porch-motion", SensorReading(motion=True), previous_motion=False
        )

        assert engine.hold_expiry("gate-light") is None
        await engine.aclose()

    async def test_the_expiry_moves_when_sustained_motion_extends_the_hold(self) -> None:
        relays = make_relays()
        ticks = iter(
            [
                datetime(2026, 9, 21, 22, 0, tzinfo=UTC),
                datetime(2026, 9, 21, 22, 0, 30, tzinfo=UTC),
            ]
        )
        engine = AutomationEngine(
            relays, [motion_rule(hold=90.0)], sensors=make_sensors(), clock=lambda: next(ticks)
        )

        await engine.handle_reading(
            "porch-motion", SensorReading(motion=True), previous_motion=False
        )
        await engine.handle_reading(
            "porch-motion", SensorReading(motion=True), previous_motion=True
        )

        assert engine.hold_expiry("porch-light") == datetime(2026, 9, 21, 22, 2, tzinfo=UTC)
        await engine.aclose()


@pytest.mark.anyio
class TestDarknessCondition:
    async def test_fires_when_dark(self) -> None:
        relays = make_relays()
        engine = AutomationEngine(
            relays, [motion_rule(after_dark=True)], sun=AlwaysDark(), sensors=make_sensors()
        )

        assert (
            await engine.handle_reading(
                "porch-motion", SensorReading(motion=True), previous_motion=False
            )
            != []
        )
        assert relays.state_of("porch-light") is True

    async def test_does_not_fire_in_daylight(self) -> None:
        relays = make_relays()
        engine = AutomationEngine(
            relays, [motion_rule(after_dark=True)], sun=NeverDark(), sensors=make_sensors()
        )

        assert (
            await engine.handle_reading(
                "porch-motion", SensorReading(motion=True), previous_motion=False
            )
            == []
        )
        assert relays.state_of("porch-light") is False

    async def test_a_rule_without_the_condition_fires_in_daylight(self) -> None:
        relays = make_relays()
        engine = AutomationEngine(relays, [motion_rule()], sun=NeverDark(), sensors=make_sensors())

        assert (
            await engine.handle_reading(
                "porch-motion", SensorReading(motion=True), previous_motion=False
            )
            != []
        )

    async def test_an_oracle_that_raises_skips_the_rule_rather_than_the_reading(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The reading is recorded before the engine sees it.

        So an exception escaping here is a 500 over work already kept, and the
        device retries a reading the hub has — for as long as whatever upset the
        oracle lasts. One rule not firing is the cheaper failure, and the rule
        asked to wait for darkness, so unknown is not dark.
        """
        relays = make_relays()
        engine = AutomationEngine(
            relays, [motion_rule(after_dark=True)], sun=BrokenOracle(), sensors=make_sensors()
        )

        with caplog.at_level(logging.WARNING):
            fired = await engine.handle_reading(
                "porch-motion", SensorReading(motion=True), previous_motion=False
            )

        assert fired == []
        assert relays.state_of("porch-light") is False
        # Skipped, not swallowed: the traceback is in the log with the rule named.
        assert "darkness could not be determined" in caplog.text
        assert "ValueError" in caplog.text

    async def test_a_rule_without_the_condition_ignores_a_broken_oracle(self) -> None:
        """Only `only_after_dark` rules consult it, so only they can be stopped."""
        relays = make_relays()
        engine = AutomationEngine(
            relays, [motion_rule()], sun=BrokenOracle(), sensors=make_sensors()
        )

        assert (
            await engine.handle_reading(
                "porch-motion", SensorReading(motion=True), previous_motion=False
            )
            != []
        )


@pytest.mark.anyio
class TestHoldTimers:
    async def test_the_relay_reverts_after_the_hold(self) -> None:
        relays = make_relays()
        engine = AutomationEngine(relays, [motion_rule(hold=HOLD)], sensors=make_sensors())

        await engine.handle_reading(
            "porch-motion", SensorReading(motion=True), previous_motion=False
        )
        assert relays.state_of("porch-light") is True

        await asyncio.sleep(HOLD * 3)

        assert relays.state_of("porch-light") is False

    async def test_the_relay_stays_on_without_a_hold(self) -> None:
        relays = make_relays()
        engine = AutomationEngine(relays, [motion_rule()], sensors=make_sensors())

        await engine.handle_reading(
            "porch-motion", SensorReading(motion=True), previous_motion=False
        )
        await asyncio.sleep(HOLD * 3)

        assert relays.state_of("porch-light") is True
        assert engine.pending_holds == frozenset()

    async def test_sustained_motion_keeps_the_countdown_alive(self) -> None:
        """Continued motion must keep the light on, not let the first timer expire.

        Every reading after the first carries the same value, which is what a PIR
        reporting on an interval actually sends. The sleeps are longer than half the
        hold, so by the second assertion more than a full hold has passed since the
        rule fired: without the countdown being restarted the light is already out.
        """
        relays = make_relays()
        engine = AutomationEngine(relays, [motion_rule(hold=HOLD)], sensors=make_sensors())

        await engine.handle_reading(
            "porch-motion", SensorReading(motion=True), previous_motion=False
        )
        for _ in range(4):
            await asyncio.sleep(HOLD * 0.6)
            assert relays.state_of("porch-light") is True, "the light went out mid-motion"
            await engine.handle_reading(
                "porch-motion", SensorReading(motion=True), previous_motion=True
            )

        await asyncio.sleep(HOLD * 3)
        assert relays.state_of("porch-light") is False
        await engine.aclose()

    async def test_sustained_motion_does_not_touch_the_relay(self) -> None:
        """The countdown restarts; the relay command does not get re-issued.

        This is the difference the state cannot show. A relay left on and driven on
        again looks identical afterwards — and yet the second write is precisely
        what overrides an operator who turned it off in between.
        """
        backend = CountingRelayBackend()
        relays = make_relays(backend)
        engine = AutomationEngine(relays, [motion_rule(hold=HOLD)], sensors=make_sensors())

        await engine.handle_reading(
            "porch-motion", SensorReading(motion=True), previous_motion=False
        )
        assert backend.writes_to(PORCH_PIN) == [True]

        for _ in range(4):
            await engine.handle_reading(
                "porch-motion", SensorReading(motion=True), previous_motion=True
            )

        assert backend.writes_to(PORCH_PIN) == [True], "an unchanged reading re-drove the relay"
        assert engine.pending_holds == {"porch-light"}
        await engine.aclose()

    async def test_sustained_motion_does_not_restart_another_rules_hold(self) -> None:
        """A hold belongs to the rule that placed it.

        Both rules here point at the porch light. The long-hold rule fires and owns
        the countdown; repeats of the short-hold rule's trigger say nothing about
        whether that countdown should still be running, so they must leave it alone.
        """
        relays = make_relays()
        mine = motion_rule(rule_id="porch-short", hold=HOLD)
        theirs = motion_rule(
            rule_id="gate-long", device="gate-camera", relay="porch-light", hold=HOLD * 20
        )
        engine = AutomationEngine(relays, [mine, theirs], sensors=make_sensors())

        await engine.handle_reading(
            "gate-camera", SensorReading(motion=True), previous_motion=False
        )
        expiry = engine.hold_expiry("porch-light")

        await engine.handle_reading(
            "porch-motion", SensorReading(motion=True), previous_motion=True
        )

        assert engine.hold_expiry("porch-light") == expiry, "another rule's countdown was reset"
        await engine.aclose()

    async def test_sustained_motion_with_nothing_pending_does_nothing(self) -> None:
        """A rule without a hold has no countdown to restart, and must not start one."""
        backend = CountingRelayBackend()
        relays = make_relays(backend)
        engine = AutomationEngine(relays, [motion_rule()], sensors=make_sensors())

        await engine.handle_reading(
            "porch-motion", SensorReading(motion=True), previous_motion=True
        )

        assert backend.writes_to(PORCH_PIN) == []
        assert engine.pending_holds == frozenset()

    async def test_a_retrigger_does_not_accumulate_timers(self) -> None:
        relays = make_relays()
        engine = AutomationEngine(relays, [motion_rule(hold=HOLD)], sensors=make_sensors())

        await engine.handle_reading(
            "porch-motion", SensorReading(motion=True), previous_motion=False
        )
        for _ in range(4):
            await engine.handle_reading(
                "porch-motion", SensorReading(motion=True), previous_motion=True
            )

        # Counted from the event loop. pending_holds is derived from a dict keyed
        # by relay id, so one relay can only ever yield one entry no matter what
        # the engine does — the previous assertion held even with the cancellation
        # removed and five live revert tasks left running per trigger.
        assert live_hold_tasks() == 1

        await engine.aclose()
        assert live_hold_tasks() == 0

    async def test_holds_on_different_relays_are_independent(self) -> None:
        relays = make_relays()
        rules = [
            motion_rule(rule_id="porch", hold=HOLD),
            motion_rule(rule_id="gate", device="gate-camera", relay="gate-light", hold=HOLD * 20),
        ]
        engine = AutomationEngine(relays, rules, sensors=make_sensors())

        await engine.handle_reading(
            "porch-motion", SensorReading(motion=True), previous_motion=False
        )
        await engine.handle_reading(
            "gate-camera", SensorReading(motion=True), previous_motion=False
        )
        await asyncio.sleep(HOLD * 3)

        assert relays.state_of("porch-light") is False
        assert relays.state_of("gate-light") is True
        await engine.aclose()


def live_hold_tasks() -> int:
    """Hold timers that will still fire, counted from the event loop.

    Cancellation is a request, not an act: a cancelled task stays not-done until
    the loop next runs it, so "not done" alone would count four timers that are
    already on their way out. ``cancelling()`` is what separates a timer still
    waiting to revert a relay from one that has been called off.
    """
    return sum(
        1
        for task in asyncio.all_tasks()
        if task.get_name().startswith("pihome-hold-") and not task.done() and task.cancelling() == 0
    )


@pytest.mark.anyio
class TestShutdown:
    async def test_aclose_cancels_pending_holds(self) -> None:
        """A thread timer left running past shutdown was a real bug in the
        predecessor: a light scheduled off by a process that no longer existed."""
        relays = make_relays()
        engine = AutomationEngine(relays, [motion_rule(hold=HOLD)], sensors=make_sensors())
        await engine.handle_reading(
            "porch-motion", SensorReading(motion=True), previous_motion=False
        )
        assert len(engine.pending_holds) == 1

        await engine.aclose()

        assert engine.pending_holds == frozenset()
        await asyncio.sleep(HOLD * 3)
        assert relays.state_of("porch-light") is True, "a cancelled hold still fired"

    async def test_aclose_is_safe_with_nothing_pending(self) -> None:
        engine = AutomationEngine(make_relays(), [motion_rule()], sensors=make_sensors())
        await engine.aclose()


class TestReferenceValidation:
    def test_a_rule_targeting_an_unknown_relay_is_rejected(self) -> None:
        with pytest.raises(AutomationConfigError, match="ghost-relay"):
            AutomationEngine(
                make_relays(), [motion_rule(relay="ghost-relay")], sensors=make_sensors()
            )

    def test_a_rule_triggered_by_an_unknown_device_is_rejected(self) -> None:
        with pytest.raises(AutomationConfigError, match="ghost-sensor"):
            AutomationEngine(
                make_relays(), [motion_rule(device="ghost-sensor")], sensors=make_sensors()
            )

    def test_after_dark_without_a_location_is_rejected(self) -> None:
        with pytest.raises(AutomationConfigError, match="only_after_dark"):
            AutomationEngine(make_relays(), [motion_rule(after_dark=True)], sensors=make_sensors())

    def test_the_error_names_what_is_available(self) -> None:
        with pytest.raises(AutomationConfigError, match="porch-light"):
            AutomationEngine(
                make_relays(), [motion_rule(relay="ghost-relay")], sensors=make_sensors()
            )
