"""Evaluates automation rules against incoming sensor readings.

Timers are asyncio tasks rather than ``threading.Timer``. The previous version of
this service started a thread timer per motion event and never cancelled them on
shutdown, so a restart could leave a light scheduled to switch off by a process
that no longer existed. Tasks here are tracked, restarted on re-trigger, and
cancelled when the engine closes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable, Mapping

from pihome_hub.automation.errors import AutomationConfigError
from pihome_hub.automation.models import AutomationRule
from pihome_hub.automation.sun import DarknessOracle
from pihome_hub.relays import RelayService
from pihome_hub.sensors import SensorReading, SensorStore

logger = logging.getLogger(__name__)


class AutomationEngine:
    """Applies rules, and owns the hold timers they schedule."""

    def __init__(
        self,
        relays: RelayService,
        rules: Iterable[AutomationRule],
        *,
        sun: DarknessOracle | None = None,
        sensors: SensorStore | None = None,
    ) -> None:
        self._relays = relays
        self._sun = sun
        self._rules = [rule for rule in rules if rule.enabled]
        #: One pending revert per relay, keyed by relay id — a second rule acting on
        #: the same relay replaces the first one's timer rather than racing it.
        self._holds: dict[str, asyncio.Task[None]] = {}

        self._validate_references(sensors)

    def _validate_references(self, sensors: SensorStore | None) -> None:
        """Fail at startup, not at 3am when a motion event finds a typo."""
        known_relays = set(self._relays.configured)
        known_devices = set(sensors.configured) if sensors is not None else None

        for rule in self._rules:
            if rule.then.relay not in known_relays:
                msg = (
                    f"automation rule {rule.id!r} targets relay {rule.then.relay!r}, "
                    f"which is not configured. Known relays: {sorted(known_relays)}"
                )
                raise AutomationConfigError(msg)
            if known_devices is not None and rule.when.device not in known_devices:
                msg = (
                    f"automation rule {rule.id!r} triggers on device {rule.when.device!r}, "
                    f"which is not configured. Known devices: {sorted(known_devices)}"
                )
                raise AutomationConfigError(msg)
            if rule.only_after_dark and self._sun is None:
                msg = f"automation rule {rule.id!r} uses only_after_dark but no location is set"
                raise AutomationConfigError(msg)

    @property
    def rules(self) -> Mapping[str, AutomationRule]:
        return {rule.id: rule for rule in self._rules}

    @property
    def pending_holds(self) -> frozenset[str]:
        """Relay ids with a revert currently scheduled. Intended for tests and logs."""
        return frozenset(relay_id for relay_id, task in self._holds.items() if not task.done())

    async def handle_reading(self, device_id: str, reading: SensorReading) -> list[str]:
        """Apply every rule this reading satisfies. Returns the ids that fired."""
        if reading.motion is None:
            return []

        fired: list[str] = []
        for rule in self._rules:
            if rule.when.device != device_id or rule.when.motion != reading.motion:
                continue
            if rule.only_after_dark and self._sun is not None and not self._sun.is_dark():
                logger.debug(
                    "rule skipped: not dark yet",
                    extra={"rule_id": rule.id, "device_id": device_id},
                )
                continue

            self._apply(rule)
            fired.append(rule.id)

        return fired

    def _apply(self, rule: AutomationRule) -> None:
        relay_id = rule.then.relay
        target = rule.then.turn_on

        self._cancel_hold(relay_id)

        if target:
            self._relays.turn_on(relay_id)
        else:
            self._relays.turn_off(relay_id)
        logger.info(
            "automation rule fired",
            extra={"rule_id": rule.id, "relay_id": relay_id, "on": target},
        )

        if rule.then.hold_seconds is not None:
            self._holds[relay_id] = asyncio.create_task(
                self._revert_after(rule, relay_id, target),
                name=f"pihome-hold-{relay_id}",
            )

    async def _revert_after(self, rule: AutomationRule, relay_id: str, applied: bool) -> None:
        assert rule.then.hold_seconds is not None  # noqa: S101 - guarded by the caller
        try:
            await asyncio.sleep(rule.then.hold_seconds)
        except asyncio.CancelledError:
            # Either a re-trigger restarted the countdown, or the engine is closing.
            raise

        try:
            if applied:
                self._relays.turn_off(relay_id)
            else:
                self._relays.turn_on(relay_id)
            logger.info(
                "automation hold expired",
                extra={"rule_id": rule.id, "relay_id": relay_id, "on": not applied},
            )
        except Exception:
            # A failing revert must not take the task — and with it any diagnosis —
            # down silently inside the event loop.
            logger.exception(
                "failed to revert relay after hold",
                extra={"rule_id": rule.id, "relay_id": relay_id},
            )
        finally:
            self._holds.pop(relay_id, None)

    def _cancel_hold(self, relay_id: str) -> None:
        task = self._holds.pop(relay_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def aclose(self) -> None:
        """Cancel every pending hold and wait for the tasks to finish unwinding."""
        tasks = list(self._holds.values())
        self._holds.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
