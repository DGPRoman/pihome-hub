"""Concurrency guarantees for shared mutable state.

Route handlers are sync ``def``, so Starlette runs them in a threadpool and several
requests really do reach ``RelayService`` and ``FailureLimiter`` at once.

Such a test is only worth having if it can actually fail. Short critical sections
usually complete inside a single GIL slice, so a naive "hammer it from eight threads"
test passes with or without a lock and proves nothing. Both tests below therefore
widen the window deliberately — a backend whose write takes as long as real GPIO I/O,
and a clock whose own call yields. Removing either lock makes these fail reliably:
lost toggles in the first case, ``KeyError`` in the second.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable
from concurrent.futures import ThreadPoolExecutor

import pytest

from pihome_hub.automation import Action, AutomationEngine, AutomationRule, Trigger
from pihome_hub.ratelimit import FailureLimiter
from pihome_hub.relays import MockRelayBackend, RelayConfig, RelayService
from pihome_hub.sensors import SensorReading

_THREADS = 8


class SlowRelayBackend(MockRelayBackend):
    """A backend whose write takes about as long as toggling a real relay."""

    def write(self, pin: int, *, on: bool) -> None:
        time.sleep(0.0005)
        super().write(pin, on=on)


def _yielding_clock() -> float:
    """A monotonic clock whose call yields, so threads interleave inside pruning."""
    time.sleep(0.0002)
    return time.monotonic()


class TestRelayServiceUnderThreads:
    def test_concurrent_toggles_are_not_lost(self) -> None:
        """An even number of toggles must leave the relay where it started.

        Unlocked, threads read the same stale state and write the same new value, so
        two presses collapse into one and the circuit ends up energised when the user
        expected it dark.
        """
        service = RelayService(
            SlowRelayBackend(), [RelayConfig(id="porch-light", pin=17, label="Porch")]
        )
        toggles = 100
        assert toggles % 2 == 0

        with ThreadPoolExecutor(max_workers=_THREADS) as pool:
            list(pool.map(lambda _: service.toggle("porch-light"), range(toggles)))

        assert service.state_of("porch-light") is False

    def test_status_never_shows_a_half_applied_group_write(self) -> None:
        """A reader must see every relay on or every relay off, never a mix."""
        relays = [RelayConfig(id=f"relay-{i}", pin=i, label=f"Relay {i}") for i in range(6)]
        service = RelayService(SlowRelayBackend(), relays)
        consistent: list[bool] = []

        def flip(index: int) -> None:
            if index % 2:
                service.turn_on_all()
            else:
                service.turn_off_all()

        def observe(_: int) -> None:
            consistent.append(len(set(service.status().values())) == 1)

        with ThreadPoolExecutor(max_workers=_THREADS) as pool:
            futures = [pool.submit(flip, i) for i in range(20)]
            futures += [pool.submit(observe, i) for i in range(60)]
            for future in futures:
                future.result()

        assert all(consistent), "status() exposed a partially applied group write"


class TestFailureLimiterUnderThreads:
    def test_mixed_operations_never_raise(self) -> None:
        """Unlocked, pruning deletes a client that another thread is re-inserting,
        and ``move_to_end`` raises ``KeyError`` — surfacing as a 500 on an auth check."""
        limiter = FailureLimiter(
            max_failures=3,
            window_seconds=0.001,
            max_tracked_clients=8,
            clock=_yielding_clock,
        )
        errors: list[BaseException] = []

        def churn(index: int) -> None:
            client = f"10.0.0.{index % 4}"
            try:
                limiter.record_failure(client)
                limiter.is_blocked(client)
                limiter.failure_count(client)
                if index % 5 == 0:
                    limiter.reset(client)
            except BaseException as exc:
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=_THREADS) as pool:
            for future in [pool.submit(churn, i) for i in range(400)]:
                future.result()

        assert not errors, f"{len(errors)} errors, first: {errors[0]!r}"

    def test_every_recorded_failure_is_counted(self) -> None:
        """A dropped failure silently widens the brute-force allowance."""
        limiter = FailureLimiter(max_failures=10_000, window_seconds=600.0)
        total = 800

        with ThreadPoolExecutor(max_workers=_THREADS) as pool:
            list(pool.map(lambda _: limiter.record_failure("10.0.0.1"), range(total)))

        assert limiter.failure_count("10.0.0.1") == total


class BlockingRelayBackend(MockRelayBackend):
    """A backend whose write takes long enough to be visible from another task.

    Distinct from :class:`SlowRelayBackend`, whose half-millisecond is tuned to
    widen a lock window without slowing the suite. This one has to be longer than
    the heartbeat interval below by a margin no scheduler jitter can close, because
    what it is measuring is whether the event loop ran at all.
    """

    #: Long enough to dwarf the heartbeat, short enough that a test using it costs
    #: a tenth of a second.
    WRITE_SECONDS = 0.1

    def __init__(self) -> None:
        super().__init__()
        self.thread_names: list[str] = []

    def write(self, pin: int, *, on: bool) -> None:
        self.thread_names.append(threading.current_thread().name)
        time.sleep(self.WRITE_SECONDS)
        super().write(pin, on=on)


async def measure_loop_stall(during: Awaitable[object], *, interval: float = 0.005) -> float:
    """Run ``during`` and report the longest gap between event-loop ticks, in seconds.

    A heartbeat task that does nothing but sleep and note the time. If the loop is
    free it wakes every ``interval``; if something synchronous is running on the
    loop thread it cannot wake at all, and the gap it finds afterwards is how long
    that something held it.

    Measuring rather than asserting a thread name. A write dispatched to a worker
    could still block the loop — by waiting on its result the wrong way, or by
    holding a lock the loop then wants — and the stall is the thing that actually
    matters to every other connection.
    """
    worst = 0.0
    stop = False

    async def heartbeat() -> None:
        nonlocal worst
        previous = time.perf_counter()
        while not stop:
            await asyncio.sleep(interval)
            now = time.perf_counter()
            worst = max(worst, now - previous)
            previous = now

    beat = asyncio.create_task(heartbeat())
    try:
        await during
    finally:
        stop = True
        await beat

    return worst


@pytest.mark.anyio
class TestTheLoopIsNotTheGpioBus:
    """Ingestion is ``async def`` and awaits its way down to a blocking write.

    ``RelayService._set`` takes a ``threading.RLock`` and then writes a GPIO pin.
    Both are fine, and both were happening on the thread that serves every other
    connection: a single relay write measured a 260 ms stall. The mutual exclusion
    was never the problem — only which thread paid for it.
    """

    async def test_a_rule_firing_does_not_stall_the_loop(self) -> None:
        backend = BlockingRelayBackend()
        relays = RelayService(backend, [RelayConfig(id="porch-light", pin=17, label="Porch")])
        rule = AutomationRule(
            id="porch-motion-light",
            when=Trigger(device="porch-motion", motion=True),
            then=Action(relay="porch-light", state="on"),
        )
        engine = AutomationEngine(relays, [rule])

        stall = await measure_loop_stall(
            engine.handle_reading("porch-motion", SensorReading(motion=True), previous_motion=False)
        )

        assert relays.state_of("porch-light") is True, "the write did not happen at all"
        assert stall < BlockingRelayBackend.WRITE_SECONDS / 2, (
            f"the loop stalled for {stall * 1000:.0f} ms while a relay was written; "
            "the write is back on the loop thread"
        )

    async def test_the_write_leaves_the_loop_thread(self) -> None:
        """The same fact stated directly, so a failure says which half is wrong.

        The stall assertion above is the one that matters — a write on a worker can
        still stall the loop — but on its own it cannot distinguish "dispatched to a
        thread" from "the backend got faster".
        """
        backend = BlockingRelayBackend()
        relays = RelayService(backend, [RelayConfig(id="porch-light", pin=17, label="Porch")])
        rule = AutomationRule(
            id="porch-motion-light",
            when=Trigger(device="porch-motion", motion=True),
            then=Action(relay="porch-light", state="on"),
        )
        engine = AutomationEngine(relays, [rule])
        loop_thread = threading.current_thread().name

        await engine.handle_reading(
            "porch-motion", SensorReading(motion=True), previous_motion=False
        )

        assert backend.thread_names, "the backend was never written to"
        assert loop_thread not in backend.thread_names, (
            f"the GPIO write ran on {loop_thread}, which is the event loop's own thread"
        )

    async def test_a_hold_reverting_does_not_stall_the_loop_either(self) -> None:
        """The other path onto the loop: the revert runs inside an asyncio task."""
        backend = BlockingRelayBackend()
        relays = RelayService(backend, [RelayConfig(id="porch-light", pin=17, label="Porch")])
        hold = BlockingRelayBackend.WRITE_SECONDS / 4
        rule = AutomationRule(
            id="porch-motion-light",
            when=Trigger(device="porch-motion", motion=True),
            then=Action(relay="porch-light", state="on", hold_seconds=hold),
        )
        engine = AutomationEngine(relays, [rule])

        async def fire_and_wait() -> None:
            await engine.handle_reading(
                "porch-motion", SensorReading(motion=True), previous_motion=False
            )
            # Past the hold, and past the write the revert then makes.
            await asyncio.sleep(hold + BlockingRelayBackend.WRITE_SECONDS * 2)

        stall = await measure_loop_stall(fire_and_wait())

        assert relays.state_of("porch-light") is False, "the revert did not happen"
        assert stall < BlockingRelayBackend.WRITE_SECONDS / 2, (
            f"the loop stalled for {stall * 1000:.0f} ms while a hold reverted"
        )
        await engine.aclose()
