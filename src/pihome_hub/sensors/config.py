"""Loads sensor device configuration from a YAML file."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import TypeAdapter, ValidationError

from pihome_hub.sensors.errors import SensorConfigError
from pihome_hub.sensors.models import SensorDevice

_device_list_adapter: TypeAdapter[list[SensorDevice]] = TypeAdapter(list[SensorDevice])


def load_sensors(path: Path) -> list[SensorDevice]:
    """Parse and validate a sensor configuration file.

    A missing file is not an error: a deployment with relays but no sensors is a
    perfectly ordinary one, and it returns an empty list. Anything else wrong with
    the file raises :class:`SensorConfigError`.
    """
    if not path.exists():
        return []

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"could not read sensor config at {path}: {exc}"
        raise SensorConfigError(msg) from exc

    try:
        document: Any = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        msg = f"invalid YAML in {path}: {exc}"
        raise SensorConfigError(msg) from exc

    if document is None:
        return []
    if not isinstance(document, dict) or "devices" not in document:
        msg = f"{path} must contain a top-level 'devices' list"
        raise SensorConfigError(msg)

    try:
        return _device_list_adapter.validate_python(document["devices"])
    except ValidationError as exc:
        msg = f"invalid sensor configuration in {path}: {exc}"
        raise SensorConfigError(msg) from exc
