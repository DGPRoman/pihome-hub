"""Is it dark right now?

Used to stop a motion rule turning on outdoor lights at midday.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astral import LocationInfo
from astral.sun import elevation, noon, sunrise, sunset

from pihome_hub.automation.errors import AutomationConfigError
from pihome_hub.automation.models import Location

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DarknessOracle(Protocol):
    """Whatever decides that it is dark enough for a rule to act.

    A protocol rather than the concrete class, so the automation engine can be
    tested against fixed daylight without conjuring a plausible latitude, date and
    timezone for every case.
    """

    def is_dark(self) -> bool: ...


class SunClock:
    """Answers ``is_dark`` for a fixed location.

    Sunrise and sunset are computed once per local date and cached: they move by
    minutes a day, and recomputing them on every motion event — as the previous
    implementation did — is arithmetic a Pi Zero has better uses for.

    Above the Arctic circle and below the Antarctic one there are dates with no
    sunrise and no sunset at all, and on those dates ``is_dark`` answers from the
    sun's height at solar noon instead. See :meth:`_classify_day`.
    """

    def __init__(self, location: Location, *, clock: Callable[[], datetime] = _utc_now) -> None:
        try:
            self._zone = ZoneInfo(location.timezone)
        except ZoneInfoNotFoundError as exc:
            # AutomationConfigError, not ValueError. main() catches the domain base
            # classes, so a bare ValueError from here escaped as a traceback with an
            # exit code the unit retries — the exact failure the layer above was
            # written to prevent, arriving through the one door left open.
            msg = (
                f"unknown timezone {location.timezone!r}. Use an IANA name such as "
                "'Europe/Kyiv'; on a minimal system the tzdata package may be missing."
            )
            raise AutomationConfigError(msg) from exc

        self._observer = LocationInfo(
            name="configured",
            region="configured",
            timezone=location.timezone,
            latitude=location.latitude,
            longitude=location.longitude,
        ).observer
        self._clock = clock
        self._cached_for: date | None = None
        self._sunrise: datetime | None = None
        self._sunset: datetime | None = None
        #: The answer for a date the sun does not cross the horizon on, where the
        #: two above stay None. Not a third state: exactly one of this and the pair
        #: is set for a cached date.
        self._all_day: bool | None = None

    def _classify_day(self, day: date) -> bool:
        """Is it dark all of ``day``, on a date the sun does not rise or set?

        Asked only when the date has neither a sunrise nor a sunset. Such a date
        is one where the sun stays on one side of the horizon from midnight to
        midnight, so the whole of it has a single answer and the only question is
        which side.

        Solar noon settles it. It is the moment the sun is highest, it exists on
        every date at every latitude — including both poles, where there is no
        sunrise to speak of for months — and astral computes it as a transit rather
        than by inverting one, so it does not raise where ``sun()`` does. Above the
        horizon at the highest point of a day with no crossing means above it all
        day; below it there means below it all day.

        Sampling the sun wherever the caller happens to be asking would be simpler
        and wrong. On the handful of dates a year when the sun grazes the horizon,
        astral's sunrise solver and its elevation model disagree by up to about a
        degree, so a height taken near local midnight comes out below a horizon
        that the same library says the date never crosses. Whichever hour asked
        first would then settle the answer for the rest of that date.
        """
        highest = elevation(self._observer, noon(self._observer, date=day, tzinfo=self._zone))
        dark = highest < 0.0
        logger.info(
            "sun does not cross the horizon on this date",
            extra={"date": str(day), "noon_elevation": round(highest, 3), "dark_all_day": dark},
        )
        return dark

    def _ensure_cached(self, local_now: datetime) -> None:
        if self._cached_for == local_now.date():
            return

        day = local_now.date()
        try:
            # The two times this class uses, asked for by name. `astral.sun.sun()`
            # returns them in a dict along with dawn, dusk and the twilights, and
            # raises if *any* member of that bundle has no solution — which at 78°N
            # is true of dusk for about a month either side of the midnight sun,
            # on dates where sunrise and sunset are both perfectly well defined.
            # Asking for the bundle threw those dates onto the no-crossing branch
            # below and called them all daylight. It is also four solutions of
            # work this never reads.
            rise = sunrise(self._observer, date=day, tzinfo=self._zone)
            set_ = sunset(self._observer, date=day, tzinfo=self._zone)
        except ValueError:
            # astral signals "the sun does not reach the horizon on this date" by
            # letting the arccosine at the heart of the hour angle fall outside
            # its domain, so it arrives as a bare ValueError. The type is all
            # there is to go on: astral rewords it into something readable only by
            # comparing the message against the literal string "math domain
            # error", which Python 3.14 no longer produces, so the text depends on
            # the interpreter. It is not an error here in any case — it is the
            # polar day and the polar night, which a hub at those latitudes lives
            # through every year. Both calls invert the same zenith on the same
            # date, so either both answer or neither does.
            self._sunrise = None
            self._sunset = None
            self._all_day = self._classify_day(day)
        else:
            self._sunrise = rise
            self._sunset = set_
            self._all_day = None
            logger.debug(
                "sun times computed",
                extra={
                    "date": str(day),
                    "sunrise": rise.isoformat(),
                    "sunset": set_.isoformat(),
                },
            )
        self._cached_for = day

    def is_dark(self) -> bool:
        """True before sunrise or after sunset in the configured timezone.

        On a date with neither — a polar day or a polar night — the answer is the
        same from midnight to midnight: False for the whole of a day the sun never
        sets on, True for the whole of one it never rises on.
        """
        local_now = self._clock().astimezone(self._zone)
        self._ensure_cached(local_now)
        if self._sunrise is None or self._sunset is None:
            assert self._all_day is not None  # noqa: S101 - set by _ensure_cached
            return self._all_day
        return local_now < self._sunrise or local_now >= self._sunset
