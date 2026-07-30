"""FailureLimiter: window behaviour, isolation between clients, and memory bounds."""

from __future__ import annotations

import pytest

from pihome_hub.ratelimit import FailureLimiter


class FakeClock:
    """A manually advanced monotonic clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def make_limiter(
    clock: FakeClock, *, max_failures: int = 3, window: float = 60.0
) -> FailureLimiter:
    return FailureLimiter(max_failures=max_failures, window_seconds=window, clock=clock)


class TestConstruction:
    @pytest.mark.parametrize("max_failures", [0, -1])
    def test_rejects_a_non_positive_allowance(self, clock: FakeClock, max_failures: int) -> None:
        with pytest.raises(ValueError, match="max_failures"):
            FailureLimiter(max_failures=max_failures, window_seconds=60.0, clock=clock)

    @pytest.mark.parametrize("window", [0.0, -5.0])
    def test_rejects_a_non_positive_window(self, clock: FakeClock, window: float) -> None:
        with pytest.raises(ValueError, match="window_seconds"):
            FailureLimiter(max_failures=3, window_seconds=window, clock=clock)


class TestBlocking:
    def test_a_fresh_client_is_not_blocked(self, clock: FakeClock) -> None:
        assert make_limiter(clock).is_blocked("10.0.0.1") is False

    def test_blocks_once_the_allowance_is_used_up(self, clock: FakeClock) -> None:
        limiter = make_limiter(clock, max_failures=3)

        for _ in range(2):
            limiter.record_failure("10.0.0.1")
        assert limiter.is_blocked("10.0.0.1") is False

        limiter.record_failure("10.0.0.1")
        assert limiter.is_blocked("10.0.0.1") is True

    def test_clients_are_counted_separately(self, clock: FakeClock) -> None:
        limiter = make_limiter(clock, max_failures=1)
        limiter.record_failure("10.0.0.1")

        assert limiter.is_blocked("10.0.0.1") is True
        assert limiter.is_blocked("10.0.0.2") is False


class TestWindow:
    def test_failures_expire_after_the_window(self, clock: FakeClock) -> None:
        limiter = make_limiter(clock, max_failures=2, window=60.0)
        limiter.record_failure("10.0.0.1")
        limiter.record_failure("10.0.0.1")
        assert limiter.is_blocked("10.0.0.1") is True

        clock.advance(61.0)

        assert limiter.is_blocked("10.0.0.1") is False
        assert limiter.failure_count("10.0.0.1") == 0

    def test_the_window_slides_rather_than_resetting_wholesale(self, clock: FakeClock) -> None:
        limiter = make_limiter(clock, max_failures=2, window=60.0)
        limiter.record_failure("10.0.0.1")
        clock.advance(59.0)
        limiter.record_failure("10.0.0.1")
        assert limiter.is_blocked("10.0.0.1") is True

        # The first failure ages out; the second is still inside the window.
        clock.advance(2.0)

        assert limiter.failure_count("10.0.0.1") == 1
        assert limiter.is_blocked("10.0.0.1") is False


class TestReset:
    def test_reset_clears_a_clients_failures(self, clock: FakeClock) -> None:
        limiter = make_limiter(clock, max_failures=1)
        limiter.record_failure("10.0.0.1")

        limiter.reset("10.0.0.1")

        assert limiter.is_blocked("10.0.0.1") is False

    def test_reset_of_an_unknown_client_does_not_raise(self, clock: FakeClock) -> None:
        make_limiter(clock).reset("10.0.0.99")


class TestMemoryBound:
    def test_tracked_clients_are_capped(self, clock: FakeClock) -> None:
        """An attacker spraying spoofed sources must not be able to grow this forever."""
        limiter = FailureLimiter(
            max_failures=5, window_seconds=600.0, max_tracked_clients=10, clock=clock
        )

        for index in range(100):
            limiter.record_failure(f"10.0.0.{index}")

        # The oldest entries are evicted, so the most recent client is still counted
        # while the first one has been forgotten.
        assert limiter.failure_count("10.0.0.99") == 1
        assert limiter.failure_count("10.0.0.0") == 0
