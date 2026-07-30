"""Process entry point — this is what the systemd unit executes.

Notable differences from a bare ``uvicorn`` command line:

* No autoreload. Under ``--reload`` any file touch restarts the process, which on
  this service means every relay drops to its power-on state — a development
  convenience with no place near mains wiring.
* ``server_header=False``, so the service does not announce its stack to anyone
  who connects.
* Host, port and log settings come from validated configuration rather than from
  arguments that can drift out of sync with the unit file.
* A misconfiguration produces a readable message in the journal, not a traceback.
"""

from __future__ import annotations

import sys
from typing import Final

import uvicorn
from pydantic import ValidationError

from pihome_hub.app import build_relay_service, create_app
from pihome_hub.config import Settings, get_settings
from pihome_hub.relays import RelayError

#: Exit code for "started with a broken configuration", following the convention
#: that 2 means the operator got the invocation wrong.
EXIT_CONFIGURATION_ERROR: Final = 2


def _environment_variable_for(field: str) -> str:
    prefix = Settings.model_config.get("env_prefix") or ""
    return f"{prefix}{field}".upper()


def render_configuration_error(exc: ValidationError) -> str:
    """Turn a pydantic validation failure into something an operator can act on."""
    lines = ["pihome-hub: configuration is invalid", ""]

    for error in exc.errors():
        location = error["loc"]
        message = error["msg"].removeprefix("Value error, ")
        if location:
            lines.append(f"  {_environment_variable_for(str(location[0]))}: {message}")
        else:
            # A whole-model check rather than a single field, so there is no one
            # variable to blame — print the explanation on its own.
            lines.append(f"  {message}")

    lines += [
        "",
        "Set these in the environment or in a .env file beside the project:",
        "",
        "  cp .env.example .env && chmod 600 .env",
        '  python -c "import secrets; print(secrets.token_urlsafe(48))"',
        "",
        "Every setting is documented in .env.example and in README.md.",
    ]
    return "\n".join(lines)


def main() -> None:
    """Run the HTTP server in the foreground."""
    try:
        settings = get_settings()
    except ValidationError as exc:
        sys.stderr.write(render_configuration_error(exc) + "\n")
        raise SystemExit(EXIT_CONFIGURATION_ERROR) from None

    # Built before the server starts so a bad relay config — or a pin this host will
    # not give us — is reported plainly rather than as a traceback from inside a
    # running event loop. RelayError covers both the configuration and hardware cases;
    # catching only the former let gpiozero's own exceptions escape as a raw traceback
    # with the wrong exit code, which a Restart=always unit turns into a crash loop.
    try:
        relay_service = build_relay_service(settings)
    except RelayError as exc:
        sys.stderr.write(f"pihome-hub: {exc}\n")
        raise SystemExit(EXIT_CONFIGURATION_ERROR) from None

    # This function built the service, so this function releases it. The lifespan
    # only closes a service it created itself, so that an app handed one does not
    # pull GPIO pins out from under whoever still holds a reference.
    try:
        uvicorn.run(
            create_app(settings, relay_service=relay_service),
            host=settings.host,
            port=settings.port,
            server_header=False,
            access_log=settings.access_log,
            # Logging is already configured by create_app(); leave it alone.
            log_config=None,
        )
    finally:
        relay_service.close()


if __name__ == "__main__":
    main()
