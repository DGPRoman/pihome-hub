"""Automation rule read-back.

Read-only: rules are declared in a YAML file that describes a house, and editing
that over HTTP is a separate decision from being able to see it.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from pihome_hub.api.v1.schemas import AutomationRuleCollection
from pihome_hub.automation import AutomationEngine
from pihome_hub.security import ViewerRequired

router = APIRouter(
    prefix="/v1/automation",
    tags=["automation"],
    dependencies=[ViewerRequired],
    responses={
        401: {"description": "Not authenticated"},
        403: {"description": "The account is not allowed to do that"},
        429: {"description": "Too many failed authentication attempts"},
    },
)


@router.get("/rules", summary="List every configured automation rule")
def list_rules(request: Request) -> AutomationRuleCollection:
    engine: AutomationEngine = request.app.state.automation
    return AutomationRuleCollection(rules=list(engine.configured))
