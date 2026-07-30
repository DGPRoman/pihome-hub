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

import time
from concurrent.futures import ThreadPoolExecutor

from pihome_hub.ratelimit import FailureLimiter
from pihome_hub.relays import MockRelayBackend, RelayConfig, RelayService

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
