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
from astral.sun import sun

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
    """

    def __init__(self, location: Location, *, clock: Callable[[], datetime] = _utc_now) -> None:
        try:
            self._zone = ZoneInfo(location.timezone)
        except ZoneInfoNotFoundError as exc:
            msg = (
                f"unknown timezone {location.timezone!r}. Use an IANA name such as "
                "'Europe/Kyiv'; on a minimal system the tzdata package may be missing."
            )
            raise ValueError(msg) from exc

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

    def _ensure_cached(self, local_now: datetime) -> None:
        if self._cached_for == local_now.date():
            return

        times = sun(self._observer, date=local_now.date(), tzinfo=self._zone)
        self._sunrise = times["sunrise"]
        self._sunset = times["sunset"]
        self._cached_for = local_now.date()
        logger.debug(
            "sun times computed",
            extra={
                "date": str(self._cached_for),
                "sunrise": self._sunrise.isoformat(),
                "sunset": self._sunset.isoformat(),
            },
        )

    def is_dark(self) -> bool:
        """True before sunrise or after sunset in the configured timezone."""
        local_now = self._clock().astimezone(self._zone)
        self._ensure_cached(local_now)
        assert self._sunrise is not None  # noqa: S101 - set by _ensure_cached
        assert self._sunset is not None  # noqa: S101 - set by _ensure_cached
        return local_now < self._sunrise or local_now >= self._sunset
