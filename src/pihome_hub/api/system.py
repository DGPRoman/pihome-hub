"""Unversioned operational endpoints.

These sit outside ``/v1`` on purpose: they describe the process, not the home
automation domain, and monitoring should not have to follow API version bumps.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    """Liveness payload."""

    status: Literal["ok"] = "ok"


@router.get(
    "/health",
    summary="Liveness probe",
    description=(
        "Reports that the process is up and serving. Unauthenticated, because systemd "
        "and uptime monitors need it before any credential is in play. Deliberately "
        "returns no version or build detail — an unauthenticated endpoint should not "
        "help a scanner fingerprint the deployment."
    ),
)
def health() -> HealthResponse:
    return HealthResponse()
