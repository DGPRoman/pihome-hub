"""SunClock: darkness for a configured location."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pihome_hub.automation import Location, SunClock

# Kyiv in high summer: sunrise around 04:50, sunset around 21:10 local (UTC+3).
KYIV = Location(latitude=50.4501, longitude=30.5234, timezone="Europe/Kyiv")


class FrozenClock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


def clock_at(hour_utc: int, *, day: int = 21, month: int = 6) -> FrozenClock:
    return FrozenClock(datetime(2026, month, day, hour_utc, 0, tzinfo=UTC))


class TestIsDark:
    def test_midday_is_not_dark(self) -> None:
        assert SunClock(KYIV, clock=clock_at(9)).is_dark() is False

    def test_midnight_is_dark(self) -> None:
        assert SunClock(KYIV, clock=clock_at(23)).is_dark() is True

    def test_before_sunrise_is_dark(self) -> None:
        # 01:00 UTC is 04:00 local, before a ~04:50 sunrise.
        assert SunClock(KYIV, clock=clock_at(1)).is_dark() is True

    def test_after_sunset_is_dark(self) -> None:
        # 19:00 UTC is 22:00 local, after a ~21:10 sunset.
        assert SunClock(KYIV, clock=clock_at(19)).is_dark() is True

    def test_winter_afternoon_is_dark(self) -> None:
        """The same wall-clock hour differs by season, which is the whole point."""
        assert SunClock(KYIV, clock=clock_at(16, day=21, month=12)).is_dark() is True


class TestCaching:
    def test_sun_times_are_computed_once_per_day(self) -> None:
        clock = FrozenClock(datetime(2026, 6, 21, 9, 0, tzinfo=UTC))
        sun = SunClock(KYIV, clock=clock)

        sun.is_dark()
        first = sun._cached_for

        sun.is_dark()
        assert sun._cached_for is first

    def test_the_cache_refreshes_on_a_new_date(self) -> None:
        clock = FrozenClock(datetime(2026, 6, 21, 9, 0, tzinfo=UTC))
        sun = SunClock(KYIV, clock=clock)
        sun.is_dark()

        clock.moment = datetime(2026, 12, 21, 14, 0, tzinfo=UTC)

        assert sun.is_dark() is True


class TestConfiguration:
    def test_an_unknown_timezone_is_rejected_with_a_useful_message(self) -> None:
        with pytest.raises(ValueError, match="IANA"):
            SunClock(Location(latitude=0.0, longitude=0.0, timezone="Mars/Olympus"))
