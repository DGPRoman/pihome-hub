"""Exceptions raised by the relay domain."""

from __future__ import annotations


class RelayError(Exception):
    """Base class for every error this package raises."""


class UnknownRelayError(RelayError):
    """Raised when an operation names a relay id that is not configured."""

    def __init__(self, relay_id: str) -> None:
        super().__init__(f"no relay configured with id {relay_id!r}")
        self.relay_id = relay_id


class RelayConfigError(RelayError):
    """Raised when relay configuration is missing, malformed, or internally inconsistent."""
