"""AutomationEngine: rule matching, hold timers, and shutdown."""

from __future__ import annotations

import asyncio
from typing import Literal

import pytest

from pihome_hub.automation import (
    Action,
    AutomationConfigError,
    AutomationEngine,
    AutomationRule,
    Trigger,
)
from pihome_hub.relays import MockRelayBackend, RelayConfig, RelayService
from pihome_hub.sensors import SensorDevice, SensorReading, SensorStore

#: Short enough to keep the suite fast, long enough not to race the assertions.
HOLD = 0.05


def make_relays() -> RelayService:
    return RelayService(
        MockRelayBackend(),
        [
            RelayConfig(id="porch-light", pin=17, label="Porch"),
            RelayConfig(id="gate-light", pin=27, label="Gate"),
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


@pytest.mark.anyio
class TestRuleMatching:
    async def test_a_matching_reading_fires_the_rule(self) -> None:
        relays = make_relays()
        engine = AutomationEngine(relays, [motion_rule()], sensors=make_sensors())

        fired = await engine.handle_reading("porch-motion", SensorReading(motion=True))

        assert fired == ["porch-motion-light"]
        assert relays.state_of("porch-light") is True

    async def test_a_different_device_does_not_fire(self) -> None:
        relays = make_relays()
        engine = AutomationEngine(relays, [motion_rule()], sensors=make_sensors())

        fired = await engine.handle_reading("gate-camera", SensorReading(motion=True))

        assert fired == []
        assert relays.state_of("porch-light") is False

    async def test_the_opposite_motion_value_does_not_fire(self) -> None:
        relays = make_relays()
        engine = AutomationEngine(relays, [motion_rule()], sensors=make_sensors())

        assert await engine.handle_reading("porch-motion", SensorReading(motion=False)) == []
        assert relays.state_of("porch-light") is False

    async def test_a_climate_only_reading_fires_nothing(self) -> None:
        relays = make_relays()
        engine = AutomationEngine(relays, [motion_rule()], sensors=make_sensors())

        assert await engine.handle_reading("porch-motion", SensorReading(temperature=20.0)) == []

    async def test_a_disabled_rule_never_fires(self) -> None:
        relays = make_relays()
        rule = motion_rule().model_copy(update={"enabled": False})
        engine = AutomationEngine(relays, [rule], sensors=make_sensors())

        assert await engine.handle_reading("porch-motion", SensorReading(motion=True)) == []


@pytest.mark.anyio
class TestDarknessCondition:
    async def test_fires_when_dark(self) -> None:
        relays = make_relays()
        engine = AutomationEngine(
            relays, [motion_rule(after_dark=True)], sun=AlwaysDark(), sensors=make_sensors()
        )

        assert await engine.handle_reading("porch-motion", SensorReading(motion=True)) != []
        assert relays.state_of("porch-light") is True

    async def test_does_not_fire_in_daylight(self) -> None:
        relays = make_relays()
        engine = AutomationEngine(
            relays, [motion_rule(after_dark=True)], sun=NeverDark(), sensors=make_sensors()
        )

        assert await engine.handle_reading("porch-motion", SensorReading(motion=True)) == []
        assert relays.state_of("porch-light") is False

    async def test_a_rule_without_the_condition_fires_in_daylight(self) -> None:
        relays = make_relays()
        engine = AutomationEngine(relays, [motion_rule()], sun=NeverDark(), sensors=make_sensors())

        assert await engine.handle_reading("porch-motion", SensorReading(motion=True)) != []


@pytest.mark.anyio
class TestHoldTimers:
    async def test_the_relay_reverts_after_the_hold(self) -> None:
        relays = make_relays()
        engine = AutomationEngine(relays, [motion_rule(hold=HOLD)], sensors=make_sensors())

        await engine.handle_reading("porch-motion", SensorReading(motion=True))
        assert relays.state_of("porch-light") is True

        await asyncio.sleep(HOLD * 3)

        assert relays.state_of("porch-light") is False

    async def test_the_relay_stays_on_without_a_hold(self) -> None:
        relays = make_relays()
        engine = AutomationEngine(relays, [motion_rule()], sensors=make_sensors())

        await engine.handle_reading("porch-motion", SensorReading(motion=True))
        await asyncio.sleep(HOLD * 3)

        assert relays.state_of("porch-light") is True
        assert engine.pending_holds == frozenset()

    async def test_a_retrigger_restarts_the_countdown(self) -> None:
        """Continued motion must keep the light on, not let the first timer expire."""
        relays = make_relays()
        engine = AutomationEngine(relays, [motion_rule(hold=HOLD)], sensors=make_sensors())

        for _ in range(4):
            await engine.handle_reading("porch-motion", SensorReading(motion=True))
            await asyncio.sleep(HOLD * 0.5)
            assert relays.state_of("porch-light") is True, "the light went out mid-motion"

        await asyncio.sleep(HOLD * 3)
        assert relays.state_of("porch-light") is False

    async def test_a_retrigger_does_not_accumulate_timers(self) -> None:
        relays = make_relays()
        engine = AutomationEngine(relays, [motion_rule(hold=HOLD)], sensors=make_sensors())

        for _ in range(5):
            await engine.handle_reading("porch-motion", SensorReading(motion=True))

        assert len(engine.pending_holds) == 1
        await engine.aclose()

    async def test_holds_on_different_relays_are_independent(self) -> None:
        relays = make_relays()
        rules = [
            motion_rule(rule_id="porch", hold=HOLD),
            motion_rule(rule_id="gate", device="gate-camera", relay="gate-light", hold=HOLD * 20),
        ]
        engine = AutomationEngine(relays, rules, sensors=make_sensors())

        await engine.handle_reading("porch-motion", SensorReading(motion=True))
        await engine.handle_reading("gate-camera", SensorReading(motion=True))
        await asyncio.sleep(HOLD * 3)

        assert relays.state_of("porch-light") is False
        assert relays.state_of("gate-light") is True
        await engine.aclose()


@pytest.mark.anyio
class TestShutdown:
    async def test_aclose_cancels_pending_holds(self) -> None:
        """A thread timer left running past shutdown was a real bug in the
        predecessor: a light scheduled off by a process that no longer existed."""
        relays = make_relays()
        engine = AutomationEngine(relays, [motion_rule(hold=HOLD)], sensors=make_sensors())
        await engine.handle_reading("porch-motion", SensorReading(motion=True))
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
