"""SunClock: darkness for a configured location."""

from __future__ import annotations

import datetime as dt
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from astral import LocationInfo, Observer
from astral.sun import sun as astral_sun
from astral.sun import sunrise as astral_sunrise
from astral.sun import sunset as astral_sunset

from pihome_hub.automation import (
    AutomationConfigError,
    AutomationError,
    Location,
    SunClock,
)

# Kyiv in high summer: sunrise around 04:50, sunset around 21:10 local (UTC+3).
KYIV = Location(latitude=50.4501, longitude=30.5234, timezone="Europe/Kyiv")

# Well inside the Arctic circle: no sunset in June, no sunrise in December.
SVALBARD = Location(latitude=78.2232, longitude=15.6267, timezone="Arctic/Longyearbyen")
# The mirror of it, where the same two dates mean the opposite seasons.
MCMURDO = Location(latitude=-77.8419, longitude=166.6863, timezone="Antarctica/McMurdo")
# The limit the Location model permits. There is no sunrise here for half a year
# and the local timezone is a convention rather than a fact, which is exactly why
# it is worth asserting that nothing raises.
NORTH_POLE = Location(latitude=90.0, longitude=0.0, timezone="UTC")
SOUTH_POLE = Location(latitude=-90.0, longitude=0.0, timezone="UTC")


class FrozenClock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


def clock_at(hour_utc: int, *, day: int = 21, month: int = 6) -> FrozenClock:
    return FrozenClock(datetime(2026, month, day, hour_utc, 0, tzinfo=UTC))


def record_sun_calls(monkeypatch: pytest.MonkeyPatch) -> list[dt.date]:
    """The local date of every sunrise computation, in the order they happen.

    A spy rather than a stub: the real function still answers, so these tests keep
    asserting the darkness the cache is there to serve rather than a fixture's idea
    of it.

    Sunrise alone, because it is asked for first. Counting both it and sunset would
    double every entry and say nothing more — what is being asserted is that a date
    is computed once, not how many values that costs.
    """
    computed: list[dt.date] = []

    def recording(observer: Observer, *, date: dt.date, tzinfo: ZoneInfo) -> datetime:
        computed.append(date)
        return astral_sunrise(observer, date=date, tzinfo=tzinfo)

    # Named as a path rather than reached through the module object: sun.py binds
    # the function into its own namespace with a plain `from astral.sun import
    # sunrise`, which strict mode reads as not exported.
    monkeypatch.setattr("pihome_hub.automation.sun.sunrise", recording)
    return computed


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


class TestPolarDayAndNight:
    """Dates the sun does not cross the horizon on.

    These are the dates `astral.sun.sun()` refuses — it inverts an hour angle to
    find sunrise, and on a date with no sunrise the arccosine has no argument to
    take. Before this was handled, the refusal travelled out of `is_dark()`,
    through the rule being evaluated, and turned every sensor ingest into a 500
    for as long as the polar day lasted.
    """

    @staticmethod
    def observer_of(location: Location) -> Observer:
        observer: Observer = LocationInfo(
            timezone=location.timezone,
            latitude=location.latitude,
            longitude=location.longitude,
        ).observer
        return observer

    @pytest.mark.parametrize(
        ("location", "month", "dark", "what"),
        [
            (SVALBARD, 6, False, "midnight sun"),
            (SVALBARD, 12, True, "polar night"),
            # The same two dates, the opposite way round.
            (MCMURDO, 6, True, "polar night"),
            (MCMURDO, 12, False, "midnight sun"),
            # The limits the Location model permits, where a timezone is a
            # convention rather than a fact.
            (NORTH_POLE, 6, False, "midnight sun"),
            (NORTH_POLE, 12, True, "polar night"),
            (SOUTH_POLE, 6, True, "polar night"),
            (SOUTH_POLE, 12, False, "midnight sun"),
        ],
    )
    def test_astral_refuses_these_dates(
        self, location: Location, month: int, dark: bool, what: str
    ) -> None:
        """The fixtures are the polar case, not merely far north.

        Asserted rather than assumed: if astral ever started answering these
        dates, every case below would still pass while testing the ordinary path
        and nothing would say so.
        """
        # The type and nothing else. astral turns the arccosine's domain error
        # into a readable one by comparing the message against the literal string
        # "math domain error", and Python 3.14 words that error differently — so
        # on 3.11 this reads "Sun is always above the horizon on this day, at
        # this location." and on 3.14 the raw arccosine complaint comes through
        # untranslated. Which is exactly why _ensure_cached catches ValueError
        # and nothing narrower.
        with pytest.raises(ValueError):  # noqa: PT011 - the message is the interpreter's
            astral_sunrise(
                self.observer_of(location),
                date=dt.date(2026, month, 21),
                tzinfo=ZoneInfo(location.timezone),
            )

    @pytest.mark.parametrize(
        ("location", "month", "dark", "what"),
        [
            (SVALBARD, 6, False, "midnight sun"),
            (SVALBARD, 12, True, "polar night"),
            (MCMURDO, 6, True, "polar night"),
            (MCMURDO, 12, False, "midnight sun"),
            (NORTH_POLE, 6, False, "midnight sun"),
            (NORTH_POLE, 12, True, "polar night"),
            (SOUTH_POLE, 6, True, "polar night"),
            (SOUTH_POLE, 12, False, "midnight sun"),
        ],
    )
    def test_the_answer_holds_for_every_hour_of_the_date(
        self, location: Location, month: int, dark: bool, what: str
    ) -> None:
        """This is the documented meaning: one answer, midnight to midnight.

        Every hour rather than one, because the answer is derived from the sun's
        height at solar noon and cached for the date — a version that sampled the
        height at the moment it was asked would pass at noon and fail at 03:00.
        """
        answers = {
            SunClock(location, clock=clock_at(hour, day=21, month=month)).is_dark()
            for hour in range(24)
        }
        assert answers == {dark}, what

    # Svalbard, 3 April 2026: sunrise about 04:58 and sunset about 21:09 local,
    # and twilight that never ends in between.
    DUSKLESS = (4, 3)

    def test_a_date_with_no_dusk_is_not_a_date_with_no_sunset(self) -> None:
        """The distinction the whole error path turns on.

        `astral.sun.sun()` returns dawn, sunrise, noon, sunset and dusk together
        and raises if any one of them has no solution. For roughly a month either
        side of the midnight sun, Svalbard has a sunrise and a sunset and no dusk
        at all — the sky never gets dark enough for one. Reading sunrise and
        sunset out of that bundle put those dates on the no-crossing branch and
        answered "daylight" for the whole of each, including the small hours.
        """
        observer = self.observer_of(SVALBARD)
        zone = ZoneInfo(SVALBARD.timezone)
        month, day = self.DUSKLESS

        with pytest.raises(ValueError, match="dusk"):
            astral_sun(observer, date=dt.date(2026, month, day), tzinfo=zone)

        # Asked for by name, both answer, and they bracket a daylit afternoon.
        when = dt.date(2026, month, day)
        rise = astral_sunrise(observer, date=when, tzinfo=zone)
        goes_down = astral_sunset(observer, date=when, tzinfo=zone)
        assert rise.date() == when
        assert rise < goes_down

        # And the hours either side of that sunrise are told apart, which is the
        # behaviour the bundle's refusal had flattened.
        assert SunClock(SVALBARD, clock=clock_at(0, day=day, month=month)).is_dark() is True
        assert SunClock(SVALBARD, clock=clock_at(12, day=day, month=month)).is_dark() is False

    def test_a_polar_date_is_still_computed_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The error path caches like the ordinary one, or a Pi Zero recomputes
        the whole polar day on every motion event."""
        computed = record_sun_calls(monkeypatch)
        clock = FrozenClock(datetime(2026, 6, 21, 2, 0, tzinfo=UTC))
        sun = SunClock(SVALBARD, clock=clock)

        assert sun.is_dark() is False
        clock.moment = datetime(2026, 6, 21, 14, 0, tzinfo=UTC)
        assert sun.is_dark() is False

        assert computed == [dt.date(2026, 6, 21)]

    def test_the_ordinary_path_returns_once_the_sun_comes_back(self) -> None:
        """A polar location is not a polar location all year.

        Svalbard has ordinary days around the equinox, and the answer there must
        come from the actual sunrise rather than from the branch that handles the
        solstices.
        """
        # Sunrise around 05:45, sunset around 18:30 local (UTC+1).
        assert SunClock(SVALBARD, clock=clock_at(3, day=21, month=3)).is_dark() is True
        assert SunClock(SVALBARD, clock=clock_at(11, day=21, month=3)).is_dark() is False


class TestCaching:
    def test_sun_times_are_computed_once_per_day(self, monkeypatch: pytest.MonkeyPatch) -> None:
        computed = record_sun_calls(monkeypatch)
        clock = FrozenClock(datetime(2026, 6, 21, 9, 0, tzinfo=UTC))
        sun = SunClock(KYIV, clock=clock)

        sun.is_dark()
        # A later hour, same local date: the cache is what must absorb this.
        clock.moment = datetime(2026, 6, 21, 19, 0, tzinfo=UTC)
        sun.is_dark()

        # What matters is that astral is consulted once for a date. The previous
        # assertion compared a private attribute by identity, which pinned how the
        # result is stored rather than that the work is not repeated.
        assert computed == [dt.date(2026, 6, 21)]

    def test_the_cache_refreshes_on_a_new_date(self, monkeypatch: pytest.MonkeyPatch) -> None:
        computed = record_sun_calls(monkeypatch)
        clock = FrozenClock(datetime(2026, 6, 21, 9, 0, tzinfo=UTC))
        sun = SunClock(KYIV, clock=clock)
        sun.is_dark()

        clock.moment = datetime(2026, 12, 21, 14, 0, tzinfo=UTC)

        assert sun.is_dark() is True
        assert computed == [dt.date(2026, 6, 21), dt.date(2026, 12, 21)]


class TestConfiguration:
    def test_an_unknown_timezone_is_rejected_with_a_useful_message(self) -> None:
        with pytest.raises(AutomationConfigError, match="IANA"):
            SunClock(Location(latitude=0.0, longitude=0.0, timezone="Mars/Olympus"))

    def test_an_unknown_timezone_is_a_configuration_error_and_not_a_bare_value_error(
        self,
    ) -> None:
        """main() catches the domain base classes. A ValueError from here went past
        all of them as a traceback, with an exit code the unit retries forever."""
        with pytest.raises(AutomationError):
            SunClock(Location(latitude=0.0, longitude=0.0, timezone="Mars/Olympus"))
