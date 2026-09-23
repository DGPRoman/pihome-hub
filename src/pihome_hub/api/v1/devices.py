"""Device announcements and read-back.

Announcing is guarded by the device key, reading by the relay key or any session.
The split is the same one the sensor routes make and it is the same argument: a
device saying where it is should not also be able to read the state of the house,
and a phone reading the house should not be able to tell this hub that the machine
in the study now answers at an address of somebody else's choosing.

An announcement is the one route in this service that hands it a credential. That
is why it has a key of its own rather than borrowing the sensor key, and why the id
has to be one an operator already wrote down.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request, Response

from pihome_hub.api.v1.schemas import DeviceCollection
from pihome_hub.devices import DeviceAnnouncement, DeviceRegistry, DeviceStatus
from pihome_hub.security import DeviceKeyRequired, ViewerRequired

logger = logging.getLogger(__name__)

_AUTH_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"description": "Missing or invalid API key"},
    429: {"description": "Too many failed authentication attempts"},
}

_READ_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"description": "Not authenticated"},
    403: {"description": "The account is not allowed to do that"},
    429: {"description": "Too many failed authentication attempts"},
}

#: Devices announce here. Device key only, and there is no key by default.
announce_router = APIRouter(
    prefix="/v1/devices",
    tags=["devices"],
    dependencies=[DeviceKeyRequired],
    responses=_AUTH_RESPONSES,
)

#: Clients read here. The relay key, or any session — reading the house is the
#: least any account may do.
read_router = APIRouter(
    prefix="/v1/devices",
    tags=["devices"],
    dependencies=[ViewerRequired],
    responses=_READ_RESPONSES,
)


def _registry(request: Request) -> DeviceRegistry:
    registry: DeviceRegistry = request.app.state.devices
    return registry


@read_router.get("", summary="Read every declared device")
def list_devices(request: Request) -> DeviceCollection:
    return DeviceCollection(devices=_registry(request).statuses())


@read_router.get(
    "/{device_id}",
    summary="Read one device",
    responses={404: {"description": "No device with that id is declared"}},
)
def get_device(request: Request, device_id: str) -> DeviceStatus:
    return _registry(request).status(device_id)


@announce_router.post(
    "/{device_id}/announcements",
    status_code=204,
    summary="Say where a device is and what key to ask it with",
    description=(
        "Sent by the device on boot and whenever its address changes. The id must "
        "already be declared in the hub's device configuration: an announcement "
        "naming an unknown one is 404, so the device key cannot be used to make "
        "this hub start calling somewhere it was never told about.\n\n"
        "Everything the poller had recorded is cleared, because a device announces "
        "when it has just booted or just moved and both make the old result a "
        "statement about a situation that no longer holds.\n\n"
        "Returns 204: the device key does not grant reads, and the device already "
        "knows everything in the record it just wrote."
    ),
    responses={404: {"description": "No device with that id is declared"}},
)
def announce(request: Request, device_id: str, announcement: DeviceAnnouncement) -> Response:
    status = _registry(request).announce(device_id, announcement)

    logger.info(
        "device announced",
        extra={
            "device_id": device_id,
            "address": status.address,
            "firmware": status.firmware,
        },
    )
    return Response(status_code=204)
