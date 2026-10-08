"""Exceptions raised by the automation domain."""

from __future__ import annotations


class AutomationError(Exception):
    """Base class for every error this package raises."""


class AutomationConfigError(AutomationError):
    """Raised when automation configuration is malformed, or refers to something absent."""


class AutomationUnavailableError(AutomationError):
    """Raised when a relay's automation is changed while no engine is running.

    Only in the window around shutdown, when a request can still be in flight after
    the lifespan has closed the engine. Refused rather than written past the engine:
    the choice would be stored, but the hold it has to release and the relay it has
    to switch off belong to an engine that is no longer there to ask.
    """
