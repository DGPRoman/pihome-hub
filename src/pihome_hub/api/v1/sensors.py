"""Sensor ingestion and read-back.

Ingestion is guarded by the sensor key, reads by the relay key. The split is the
point of having two keys: firmware that pushes readings should not be able to read
the state of the house, and a phone that reads the house should not be able to
forge a motion event and drive the lights through an automation rule.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request, Response

from pihome_hub.api.v1.schemas import SensorCollection
from pihome_hub.automation import AutomationEngine
from pihome_hub.security import RelayKeyRequired, SensorKeyRequired
from pihome_hub.sensors import DeviceSnapshot, SensorReading, SensorStore

logger = logging.getLogger(__name__)

_AUTH_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"description": "Missing or invalid API key"},
    429: {"description": "Too many failed authentication attempts"},
}

#: Devices push here. Sensor key only.
ingest_router = APIRouter(
    prefix="/v1/sensors",
    tags=["sensors"],
    dependencies=[SensorKeyRequired],
    responses=_AUTH_RESPONSES,
)

#: Clients read here. Relay key, the same one used to control the house.
read_router = APIRouter(
    prefix="/v1/sensors",
    tags=["sensors"],
    dependencies=[RelayKeyRequired],
    responses=_AUTH_RESPONSES,
)


def _store(request: Request) -> SensorStore:
    store: SensorStore = request.app.state.sensors
    return store


def _engine(request: Request) -> AutomationEngine | None:
    engine: AutomationEngine | None = getattr(request.app.state, "automation", None)
    return engine


@read_router.get("", summary="Read every configured sensor")
def list_sensors(request: Request) -> SensorCollection:
    return SensorCollection(sensors=_store(request).snapshots())


@read_router.get(
    "/{device_id}",
    summary="Read one sensor",
    responses={404: {"description": "No device with that id is configured"}},
)
def get_sensor(request: Request, device_id: str) -> DeviceSnapshot:
    return _store(request).snapshot(device_id)


@ingest_router.post(
    "/{device_id}/readings",
    status_code=202,
    summary="Push a reading from a device",
    description=(
        "Accepts one or more quantities in a single request, so battery-powered "
        "firmware need not spend a round trip per value. Returns 202: the reading is "
        "recorded and any matching automation has been applied, but the response "
        "deliberately carries no house state — the sensor key does not grant reads."
    ),
    responses={404: {"description": "No device with that id is configured"}},
)
async def push_reading(request: Request, device_id: str, reading: SensorReading) -> Response:
    store = _store(request)
    snapshot = store.record(device_id, reading)

    engine = _engine(request)
    fired = await engine.handle_reading(device_id, reading) if engine is not None else []

    logger.info(
        "reading recorded",
        extra={
            "device_id": device_id,
            "motion": reading.motion,
            "temperature": reading.temperature,
            "humidity": reading.humidity,
            "rules_fired": fired,
            "stale": snapshot.stale,
        },
    )
    return Response(status_code=202)
