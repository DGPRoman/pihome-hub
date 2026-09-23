"""Loads the declaration of which devices this hub will talk to."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import TypeAdapter, ValidationError

from pihome_hub.devices.errors import DeviceConfigError
from pihome_hub.devices.models import Device

_device_list_adapter: TypeAdapter[list[Device]] = TypeAdapter(list[Device])


def load_devices(path: Path) -> list[Device]:
    """Parse and validate a device configuration file.

    A missing file is not an error — a deployment with no HTTP devices is an
    ordinary one, and it returns an empty list. Anything else wrong with the file
    raises :class:`DeviceConfigError`.
    """
    if not path.exists():
        return []

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"could not read device config at {path}: {exc}"
        raise DeviceConfigError(msg) from exc

    try:
        document: Any = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        msg = f"invalid YAML in {path}: {exc}"
        raise DeviceConfigError(msg) from exc

    if document is None:
        return []
    if not isinstance(document, dict) or "devices" not in document:
        msg = f"{path} must contain a top-level 'devices' list"
        raise DeviceConfigError(msg)

    try:
        return _device_list_adapter.validate_python(document["devices"])
    except ValidationError as exc:
        msg = f"invalid device configuration in {path}: {exc}"
        raise DeviceConfigError(msg) from exc
