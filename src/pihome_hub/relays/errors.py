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


#: What to suggest, by what the service was trying to do when it failed.
#:
#: The two moments have nothing useful in common. At startup the likely causes are
#: configuration: another process holding the pin, a missing group, an absent extra.
#: Mid-request none of those apply — the pin was claimed successfully minutes ago —
#: and repeating that advice sends the reader to check three things that are already
#: true.
_REMEDIES = {
    "claim": (
        "Check that the pin is not already in use, that the service user is in "
        "the 'gpio' group, and that the hardware extra is installed "
        "(pip install '.[rpi]')."
    ),
    "drive": (
        "The pin was claimed successfully at startup, so this is not a configuration "
        "problem: look at the wiring, the supply, and whether anything else has "
        "taken the pin since."
    ),
}


class RelayHardwareError(RelayError):
    """Raised when the GPIO layer refuses to claim or drive a pin.

    Exists so that a backend's own exception type — gpiozero raises several, none of
    them ours — does not escape as an unhandled traceback.
    """

    def __init__(
        self, relay_id: str, pin: int, cause: BaseException, *, action: str = "claim"
    ) -> None:
        super().__init__(
            f"could not {action} pin {pin} for relay {relay_id!r}: {cause}\n{_REMEDIES[action]}"
        )
        self.relay_id = relay_id
        self.pin = pin
        self.action = action
