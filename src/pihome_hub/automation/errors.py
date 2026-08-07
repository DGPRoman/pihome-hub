"""Exceptions raised by the automation domain."""

from __future__ import annotations


class AutomationError(Exception):
    """Base class for every error this package raises."""


class AutomationConfigError(AutomationError):
    """Raised when automation configuration is malformed, or refers to something absent."""
