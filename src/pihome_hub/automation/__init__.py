"""Automation domain: declarative rules linking sensor readings to relay actions."""

from __future__ import annotations

from pihome_hub.automation.config import load_automation
from pihome_hub.automation.engine import AutomationEngine
from pihome_hub.automation.errors import AutomationConfigError, AutomationError
from pihome_hub.automation.models import (
    Action,
    AutomationConfig,
    AutomationRule,
    Location,
    Trigger,
)
from pihome_hub.automation.sun import DarknessOracle, SunClock

__all__ = [
    "Action",
    "AutomationConfig",
    "AutomationConfigError",
    "AutomationEngine",
    "AutomationError",
    "AutomationRule",
    "DarknessOracle",
    "Location",
    "SunClock",
    "Trigger",
    "load_automation",
]
