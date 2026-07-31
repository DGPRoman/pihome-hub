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


class RelayHardwareError(RelayError):
    """Raised when the GPIO layer refuses to claim or drive a pin.

    Exists so that a backend's own exception type — gpiozero raises several, none of
    them ours — does not escape as an unhandled traceback during startup.
    """

    def __init__(self, relay_id: str, pin: int, cause: BaseException) -> None:
        super().__init__(
            f"could not claim pin {pin} for relay {relay_id!r}: {cause}\n"
            "Check that the pin is not already in use, that the service user is in "
            "the 'gpio' group, and that the hardware extra is installed "
            "(pip install '.[rpi]')."
        )
        self.relay_id = relay_id
        self.pin = pin
