"""Evaluates automation rules against incoming sensor readings.

Timers are asyncio tasks rather than ``threading.Timer``. The previous version of
this service started a thread timer per motion event and never cancelled them on
shutdown, so a restart could leave a light scheduled to switch off by a process
that no longer existed. Tasks here are tracked, restarted on re-trigger, and
cancelled when the engine closes.

Rules fire on a *change*, never on a repeat. A motion sensor reporting on an
interval sends the same value over and over; re-applying the action on each one
would mean the house overrides its operator every few seconds, and no manual
switch would hold for longer than one reporting period.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import anyio.to_thread

from pihome_hub.automation.errors import AutomationConfigError
from pihome_hub.automation.models import AutomationRule
from pihome_hub.automation.sun import DarknessOracle
from pihome_hub.relays import RelayService
from pihome_hub.sensors import SensorReading, SensorStore

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class _Hold:
    """A scheduled revert, and what is needed to judge, restart or call it off."""

    #: The rule that placed it. Sustained motion restarts only its own rule's
    #: countdown — a hold another rule put on the same relay answers to that
    #: rule's trigger, and this one's reading says nothing about it.
    rule: AutomationRule
    #: The state the rule drove the relay to. The revert is only owed if the relay
    #: is still there when the countdown ends.
    applied: bool
    #: Wall-clock estimate of when the revert fires, for the API to report. The
    #: task itself sleeps on the loop's own clock; this is for humans.
    expires_at: datetime
    task: asyncio.Task[None]
    #: The loop the task belongs to, so a worker thread can cancel it safely.
    loop: asyncio.AbstractEventLoop


class AutomationEngine:
    """Applies rules, and owns the hold timers they schedule."""

    def __init__(
        self,
        relays: RelayService,
        rules: Iterable[AutomationRule],
        *,
        sun: DarknessOracle | None = None,
        sensors: SensorStore | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._relays = relays
        self._sun = sun
        self._clock = clock
        #: Every rule as configured. Kept so the API can report a disabled rule as
        #: disabled rather than omitting it.
        self._configured = list(rules)
        self._rules = [rule for rule in self._configured if rule.enabled]
        #: One pending revert per relay, keyed by relay id — a second rule acting on
        #: the same relay replaces the first one's timer rather than racing it.
        self._holds: dict[str, _Hold] = {}
        #: Guards ``_holds``. Reached from two places at once: the event loop, where
        #: readings are handled, and Starlette's threadpool, where the sync relay
        #: routes run and release a hold the operator has overruled.
        self._lock = threading.Lock()

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
    def configured(self) -> Sequence[AutomationRule]:
        """Every rule, enabled or not, in configuration order."""
        return tuple(self._configured)

    @property
    def pending_holds(self) -> frozenset[str]:
        """Relay ids with a revert currently scheduled. Intended for tests and logs."""
        with self._lock:
            return frozenset(
                relay_id for relay_id, hold in self._holds.items() if not hold.task.done()
            )

    def hold_expiry(self, relay_id: str) -> datetime | None:
        """When a pending revert will put this relay back, or ``None`` if none is due.

        Served in the relay representation so a client can say *why* a light is on
        and when it will go out, instead of showing a state with an invisible timer
        running against it.
        """
        with self._lock:
            hold = self._holds.get(relay_id)
        return None if hold is None or hold.task.done() else hold.expires_at

    def release_hold(self, relay_id: str) -> bool:
        """Call off the pending revert on a relay. Returns whether one was pending.

        For callers outside automation: something has taken control of the relay,
        and a rule that fired earlier no longer has any claim on it.

        Safe from any thread. The relay routes are sync ``def``, so Starlette runs
        them in a worker thread, and ``Task.cancel`` may only be called on the loop
        that owns the task.
        """
        with self._lock:
            hold = self._holds.pop(relay_id, None)
        if hold is None or hold.task.done():
            return False
        hold.loop.call_soon_threadsafe(hold.task.cancel)
        return True

    async def handle_reading(
        self, device_id: str, reading: SensorReading, *, previous_motion: bool | None
    ) -> list[str]:
        """Apply every rule this reading newly satisfies. Returns the ids that fired.

        ``previous_motion`` is what the store held before this reading replaced it.
        It is required rather than defaulted because there is no safe default: a
        caller unable to say what came before would silently get the old behaviour,
        where every repeat of an unchanged value re-issued the relay command.

        A first reading from a device — ``previous_motion is None`` — counts as a
        change. The hub has just learned the state of the room, and treating that
        as "no news" would leave a light off with somebody standing under it until
        the motion stopped and started again.
        """
        if reading.motion is None:
            return []

        changed = reading.motion != previous_motion

        fired: list[str] = []
        for rule in self._rules:
            if rule.when.device != device_id or rule.when.motion != reading.motion:
                continue
            if not changed:
                # Sustained motion keeps the countdown alive but must not touch the
                # relay: whoever set it last may not have been this rule.
                self._extend_hold(rule)
                continue
            if rule.only_after_dark and self._sun is not None and not self._sun.is_dark():
                logger.debug(
                    "rule skipped: not dark yet",
                    extra={"rule_id": rule.id, "device_id": device_id},
                )
                continue

            await self._apply(rule)
            fired.append(rule.id)

        return fired

    async def _drive(self, relay_id: str, *, on: bool) -> None:
        """Set one relay, off the event loop.

        Everything below :class:`RelayService` is synchronous and blocking — a
        ``threading.RLock`` and then a write to a GPIO pin — and this is the only
        caller that reaches it from the loop. The HTTP routes are sync ``def``, so
        Starlette already runs them in its threadpool; ingestion is ``async def``
        and awaits its way down to here, which put the lock and the bus on the
        thread that serves every other connection.

        Measured with the slow backend the concurrency tests already use: a single
        relay write stalled the loop for 260 ms. Nothing about the mutual exclusion
        is wrong — the question was only ever which thread pays for it.
        """
        await anyio.to_thread.run_sync(
            self._relays.turn_on if on else self._relays.turn_off, relay_id
        )

    async def _apply(self, rule: AutomationRule) -> None:
        relay_id = rule.then.relay
        target = rule.then.turn_on

        self._cancel_hold(relay_id)

        await self._drive(relay_id, on=target)
        logger.info(
            "automation rule fired",
            extra={"rule_id": rule.id, "relay_id": relay_id, "on": target},
        )

        self._schedule_hold(rule, applied=target)

    def _extend_hold(self, rule: AutomationRule) -> None:
        """Restart this rule's countdown, leaving the relay alone.

        Only the rule's own hold: a countdown placed by a different rule belongs to
        that rule's trigger, and sustained motion here is no evidence about it.
        """
        relay_id = rule.then.relay
        with self._lock:
            hold = self._holds.get(relay_id)
        if hold is None or hold.task.done() or hold.rule.id != rule.id:
            return

        self._cancel_hold(relay_id)
        self._schedule_hold(rule, applied=hold.applied)
        logger.debug(
            "hold extended by continued motion",
            extra={"rule_id": rule.id, "relay_id": relay_id},
        )

    def _schedule_hold(self, rule: AutomationRule, *, applied: bool) -> None:
        hold_seconds = rule.then.hold_seconds
        if hold_seconds is None:
            return

        relay_id = rule.then.relay
        loop = asyncio.get_running_loop()
        task = loop.create_task(
            self._revert_after(rule, relay_id, applied),
            name=f"pihome-hold-{relay_id}",
        )
        with self._lock:
            self._holds[relay_id] = _Hold(
                rule=rule,
                applied=applied,
                expires_at=self._clock() + timedelta(seconds=hold_seconds),
                task=task,
                loop=loop,
            )

    async def _revert_after(self, rule: AutomationRule, relay_id: str, applied: bool) -> None:
        assert rule.then.hold_seconds is not None  # noqa: S101 - guarded by the caller
        try:
            await asyncio.sleep(rule.then.hold_seconds)
        except asyncio.CancelledError:
            # A re-trigger restarted the countdown, an operator overruled the rule,
            # or the engine is closing.
            raise

        try:
            current = self._relays.state_of(relay_id)
            if current != applied:
                # Something moved the relay after the rule did. Reverting now would
                # apply the inverse of a decision that is no longer in force, which
                # is how a hold ends up switching off a light somebody just asked
                # for. The rule's claim expires with the state it set.
                logger.info(
                    "hold expired, but the relay had already been changed; leaving it",
                    extra={"rule_id": rule.id, "relay_id": relay_id, "on": current},
                )
                return

            await self._drive(relay_id, on=not applied)
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
            with self._lock:
                self._holds.pop(relay_id, None)

    def _cancel_hold(self, relay_id: str) -> None:
        """Call off a pending revert from the loop that owns it."""
        with self._lock:
            hold = self._holds.pop(relay_id, None)
        if hold is not None and not hold.task.done():
            hold.task.cancel()

    async def aclose(self) -> None:
        """Cancel every pending hold and wait for the tasks to finish unwinding."""
        with self._lock:
            holds = list(self._holds.values())
            self._holds.clear()
        for hold in holds:
            hold.task.cancel()
        for hold in holds:
            with contextlib.suppress(asyncio.CancelledError):
                await hold.task
