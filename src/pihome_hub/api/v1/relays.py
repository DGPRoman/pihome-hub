"""Relay control routes.

Idempotent operations use ``PUT`` with the desired state; ``toggle`` is a ``POST``
because its result depends on the state it finds, so replaying it is not safe.

Collection-wide routes are registered before the per-relay ones. Both shapes are
unambiguous today — the group routes have one path segment where the per-relay
routes have two — but the ordering keeps that true if a route is ever added.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from pihome_hub.api.v1.schemas import RelayCollection, RelayState, RelayStateRequest
from pihome_hub.relays import RelayService
from pihome_hub.security import RelayKeyRequired

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/relays",
    tags=["relays"],
    dependencies=[RelayKeyRequired],
    responses={
        401: {"description": "Missing or invalid API key"},
        429: {"description": "Too many failed authentication attempts"},
    },
)


def _service(request: Request) -> RelayService:
    service: RelayService = request.app.state.relays
    return service


def _collection(service: RelayService) -> RelayCollection:
    state = service.status()
    return RelayCollection(
        relays=[
            RelayState(id=relay.id, label=relay.label, on=state[relay.id])
            for relay in service.configured.values()
        ]
    )


def _one(service: RelayService, relay_id: str) -> RelayState:
    relay = service.config_for(relay_id)
    return RelayState(id=relay.id, label=relay.label, on=service.state_of(relay.id))


@router.get("", summary="List every relay and its state")
def list_relays(request: Request) -> RelayCollection:
    return _collection(_service(request))


@router.put("", summary="Set every relay to the same state")
def set_all_relays(request: Request, desired: RelayStateRequest) -> RelayCollection:
    service = _service(request)
    if desired.on:
        service.turn_on_all()
    else:
        service.turn_off_all()
    logger.info("all relays set", extra={"on": desired.on})
    return _collection(service)


@router.post("/toggle", summary="Invert every relay independently")
def toggle_all_relays(request: Request) -> RelayCollection:
    service = _service(request)
    service.toggle_all()
    logger.info("all relays toggled")
    return _collection(service)


@router.get(
    "/{relay_id}",
    summary="Read one relay",
    responses={404: {"description": "No relay with that id is configured"}},
)
def get_relay(request: Request, relay_id: str) -> RelayState:
    return _one(_service(request), relay_id)


@router.put(
    "/{relay_id}",
    summary="Set one relay to a state",
    responses={404: {"description": "No relay with that id is configured"}},
)
def set_relay(request: Request, relay_id: str, desired: RelayStateRequest) -> RelayState:
    service = _service(request)
    if desired.on:
        service.turn_on(relay_id)
    else:
        service.turn_off(relay_id)
    logger.info("relay set", extra={"relay_id": relay_id, "on": desired.on})
    return _one(service, relay_id)


@router.post(
    "/{relay_id}/toggle",
    summary="Invert one relay",
    responses={404: {"description": "No relay with that id is configured"}},
)
def toggle_relay(request: Request, relay_id: str) -> RelayState:
    service = _service(request)
    new_state = service.toggle(relay_id)
    logger.info("relay toggled", extra={"relay_id": relay_id, "on": new_state})
    return _one(service, relay_id)
