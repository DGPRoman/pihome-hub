"""Automation rule read-back.

Read-only: rules are declared in a YAML file that describes a house, and editing
that over HTTP is a separate decision from being able to see it.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from pihome_hub.api.v1.schemas import AutomationRuleCollection
from pihome_hub.automation import AutomationEngine
from pihome_hub.security import RelayKeyRequired

router = APIRouter(
    prefix="/v1/automation",
    tags=["automation"],
    dependencies=[RelayKeyRequired],
    responses={
        401: {"description": "Missing or invalid API key"},
        429: {"description": "Too many failed authentication attempts"},
    },
)


@router.get("/rules", summary="List every configured automation rule")
def list_rules(request: Request) -> AutomationRuleCollection:
    engine: AutomationEngine = request.app.state.automation
    return AutomationRuleCollection(rules=list(engine.configured))
