"""Loads automation configuration from a YAML file."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from pihome_hub.automation.errors import AutomationConfigError
from pihome_hub.automation.models import AutomationConfig


def load_automation(path: Path) -> AutomationConfig:
    """Parse and validate an automation file.

    A missing file means no automation, which is a legitimate configuration — the
    relay API works perfectly well on its own — so it yields an empty rule set
    rather than an error.
    """
    if not path.exists():
        return AutomationConfig()

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"could not read automation config at {path}: {exc}"
        raise AutomationConfigError(msg) from exc

    try:
        document: Any = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        msg = f"invalid YAML in {path}: {exc}"
        raise AutomationConfigError(msg) from exc

    if document is None:
        return AutomationConfig()
    if not isinstance(document, dict):
        msg = f"{path} must contain a mapping with 'rules' and optionally 'location'"
        raise AutomationConfigError(msg)

    try:
        return AutomationConfig.model_validate(document)
    except ValidationError as exc:
        msg = f"invalid automation configuration in {path}: {exc}"
        raise AutomationConfigError(msg) from exc
