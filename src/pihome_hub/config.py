"""Application settings, resolved from environment variables and an optional ``.env`` file.

Every setting is prefixed with ``PIHOME_`` so the service cannot accidentally pick up
unrelated variables from the surrounding environment. Secrets are typed as
:class:`~pydantic.SecretStr`, which keeps them out of log lines, tracebacks and
``repr()`` output.
"""

from __future__ import annotations

import ipaddress
import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from pihome_hub.accounts import DEFAULT_SESSION_LIFETIME_SECONDS
from pihome_hub.relays.factory import GpioBackendName

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

#: Shortest API key we are willing to accept. A key this long resists online
#: brute force even when the service is reachable over an untrusted network.
MIN_API_KEY_LENGTH: Final = 32

#: Values that look like a key but are really a copy-paste artefact from example
#: config. Rejecting them at startup turns a silent security hole into a crash.
_REJECTED_KEY_MARKERS: Final = (
    "changeme",
    "change_me",
    "change-me",
    "example",
    "placeholder",
    "replaceme",
    "yourkeyhere",
)


def _state_directory() -> Path:
    """The writable directory systemd made for this unit, or a local stand-in.

    ``STATE_DIRECTORY`` holds one entry per ``StateDirectory=`` in the unit, joined
    by colons. This service declares one; taking the first entry keeps that true
    rather than assuming it.
    """
    exported = os.environ.get("STATE_DIRECTORY", "")
    first = exported.split(":")[0]
    return Path(first) if first else Path("var")


def _default_database_path() -> Path:
    return _state_directory() / "hub.db"


def resolve_database_path() -> Path:
    """Where accounts live, without needing the rest of the configuration.

    For the admin tool. It has no business requiring the two API keys, which on a Pi
    live in a file only root can read — asking it to load :class:`Settings` would
    mean either running the account manager as root or copying secrets around.

    Reads the same variable pydantic-settings would, and falls back the same way. A
    test asserts the two agree, in both the default and the overridden case.
    """
    override = os.environ.get("PIHOME_DATABASE_PATH", "")
    return Path(override) if override else _default_database_path()


class Settings(BaseSettings):
    """Runtime configuration for the service."""

    model_config = SettingsConfigDict(
        env_prefix="PIHOME_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # -- HTTP server ---------------------------------------------------------
    #: Loopback by default. Binding to every interface is an explicit operator
    #: decision, not something that happens because a default was left alone.
    host: str = "127.0.0.1"
    port: Annotated[int, Field(ge=1, le=65535)] = 5002

    # -- Observability -------------------------------------------------------
    log_level: LogLevel = "INFO"
    #: Emit one JSON object per log record instead of human-readable lines.
    log_json: bool = False
    #: Log a line per HTTP request. Off by default; on a permanently exposed host
    #: this is mostly scanner noise and it costs SD-card write cycles.
    access_log: bool = False

    # -- API surface ---------------------------------------------------------
    #: Serve the OpenAPI schema and Swagger UI. Off by default so a public
    #: deployment does not publish its own route map.
    docs_enabled: bool = False

    # -- Hardware ------------------------------------------------------------
    #: Which relay backend to drive. ``mock`` by default: touching real GPIO
    #: pins is something an operator opts into, never a fallback.
    gpio_backend: GpioBackendName = "mock"
    relay_config_path: Path = Path("config/relays.yaml")
    #: Sensors and automation are optional: a missing file means the feature is
    #: simply not in use, not that the deployment is broken.
    sensor_config_path: Path = Path("config/sensors.yaml")
    automation_config_path: Path = Path("config/automation.yaml")

    # -- State ---------------------------------------------------------------
    #: Where accounts and sessions live. The default follows the unit rather than
    #: repeating it: systemd exports STATE_DIRECTORY for every StateDirectory= it
    #: created, so the one declaration in pihome-hub.service decides this on a Pi
    #: and nothing in hub.env can drift away from it. Off systemd it falls back to
    #: a path beside the checkout, which is what a development run wants.
    database_path: Path = Field(default_factory=_default_database_path)

    #: How long a login lasts, counted from the moment it happened rather than from
    #: the last request. Sliding expiry would mean a database write per authenticated
    #: request, which on an SD card is a cost the convenience does not cover. The
    #: default is imported rather than repeated, so the store and the setting cannot
    #: come to disagree.
    session_lifetime_seconds: Annotated[int, Field(gt=0)] = DEFAULT_SESSION_LIFETIME_SECONDS

    #: ``Secure`` on the session cookie. False by default, and that is not an
    #: oversight: this service speaks plain HTTP, and a Secure cookie is one the
    #: browser will not send over it — logging in would appear to work and every
    #: request after it would be anonymous. Set it true behind a TLS proxy, which is
    #: the only arrangement where it is both correct and possible.
    session_cookie_secure: bool = False

    # -- Brute-force protection ----------------------------------------------
    #: Failed authentication attempts one client may make inside the window
    #: before further attempts are refused with 429.
    auth_max_failures: Annotated[int, Field(ge=1)] = 10
    auth_failure_window_seconds: Annotated[float, Field(gt=0)] = 300.0

    # -- Credentials ---------------------------------------------------------
    #: Authenticates clients that control relays. Required: the service is
    #: unusable without it, so failing at startup beats failing at request time.
    relay_api_key: SecretStr
    #: Authenticates sensor devices that push readings. Deliberately separate
    #: from ``relay_api_key`` so firmware flashed onto a sensor cannot also
    #: drive relays directly if that firmware is ever extracted.
    sensor_api_key: SecretStr

    @field_validator("relay_api_key", "sensor_api_key")
    @classmethod
    def _reject_weak_keys(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()

        if len(secret) < MIN_API_KEY_LENGTH:
            msg = f"API key must be at least {MIN_API_KEY_LENGTH} characters, got {len(secret)}"
            raise ValueError(msg)

        normalised = secret.casefold().replace(" ", "")
        for marker in _REJECTED_KEY_MARKERS:
            if marker in normalised:
                msg = (
                    f"API key looks like example config (contains {marker!r}). "
                    'Generate a real one: python -c "import secrets; '
                    'print(secrets.token_urlsafe(48))"'
                )
                raise ValueError(msg)

        return value

    @field_validator("host")
    @classmethod
    def _require_host(cls, value: str) -> str:
        host = value.strip()
        if not host:
            msg = "host must not be empty"
            raise ValueError(msg)
        return host

    @property
    def binds_to_loopback(self) -> bool:
        if self.host == "localhost":
            return True
        try:
            return ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            return False

    @model_validator(mode="after")
    def _docs_stay_on_loopback(self) -> Settings:
        """Refuse to publish the API's own route map on a reachable interface.

        Swagger UI and the OpenAPI schema carry no credential — FastAPI mounts them
        without dependencies, and gating the schema would break the UI that has to
        fetch it. Rather than serve an unauthenticated map of every route and its body
        shape, enabling docs is only permitted while bound to loopback. Reach them from
        elsewhere by forwarding a port over SSH or the VPN.
        """
        if self.docs_enabled and not self.binds_to_loopback:
            msg = (
                f"docs_enabled is true while bound to {self.host!r}, which would serve "
                "/docs and /openapi.json to anyone who can reach that address. Bind to "
                "127.0.0.1, or forward the port (ssh -L 5002:127.0.0.1:5002 pi) instead."
            )
            raise ValueError(msg)
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, reading the environment exactly once."""
    return Settings()  # type: ignore[call-arg]  # values come from env / .env
