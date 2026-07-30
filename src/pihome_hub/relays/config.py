"""Loads relay configuration from a YAML file."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import TypeAdapter, ValidationError

from pihome_hub.relays.errors import RelayConfigError
from pihome_hub.relays.models import RelayConfig

_relay_list_adapter: TypeAdapter[list[RelayConfig]] = TypeAdapter(list[RelayConfig])


def load_relays(path: Path) -> list[RelayConfig]:
    """Parse and validate a relay configuration file.

    Raises :class:`RelayConfigError` for anything wrong with the file — missing,
    unreadable, malformed YAML, or a schema violation — so a caller has exactly
    one exception type to handle instead of three.
    """
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"could not read relay config at {path}: {exc}"
        raise RelayConfigError(msg) from exc

    try:
        document: Any = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        msg = f"invalid YAML in {path}: {exc}"
        raise RelayConfigError(msg) from exc

    if not isinstance(document, dict) or "relays" not in document:
        msg = f"{path} must contain a top-level 'relays' list"
        raise RelayConfigError(msg)

    try:
        return _relay_list_adapter.validate_python(document["relays"])
    except ValidationError as exc:
        msg = f"invalid relay configuration in {path}: {exc}"
        raise RelayConfigError(msg) from exc
