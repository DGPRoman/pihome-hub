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
from pihome_hub.automation import AutomationEngine
from pihome_hub.relays import RelayConfig, RelayService
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


def _automation(request: Request) -> AutomationEngine | None:
    engine: AutomationEngine | None = getattr(request.app.state, "automation", None)
    return engine


def _release_holds(request: Request, *relay_ids: str) -> None:
    """Drop any automation revert aimed at these relays.

    An operator's write is the last word. A rule that fired with ``hold_seconds``
    has a countdown running to undo itself; without this, that countdown also
    undoes an instruction that arrived *after* it — the rule switches a light on,
    the operator says "stay on", and the hold switches it off anyway.

    Called before the write rather than after. The relay is being taken out of
    automation's hands the moment the request is accepted, whether or not the
    hardware then cooperates, and doing it first leaves no window in which an
    expiring hold could act on a relay the operator has already claimed.
    """
    engine = _automation(request)
    if engine is None:
        return
    for relay_id in relay_ids:
        if engine.release_hold(relay_id):
            logger.info(
                "automation hold released by an operator write",
                extra={"relay_id": relay_id},
            )


def _state(relay: RelayConfig, *, on: bool, engine: AutomationEngine | None) -> RelayState:
    return RelayState(
        id=relay.id,
        label=relay.label,
        on=on,
        hold_expires_at=None if engine is None else engine.hold_expiry(relay.id),
    )


def _collection(request: Request, service: RelayService) -> RelayCollection:
    state = service.status()
    engine = _automation(request)
    return RelayCollection(
        relays=[
            _state(relay, on=state[relay.id], engine=engine)
            for relay in service.configured.values()
        ]
    )


def _one(request: Request, service: RelayService, relay_id: str) -> RelayState:
    relay = service.config_for(relay_id)
    return _state(relay, on=service.state_of(relay.id), engine=_automation(request))


@router.get("", summary="List every relay and its state")
def list_relays(request: Request) -> RelayCollection:
    return _collection(request, _service(request))


@router.put("", summary="Set every relay to the same state")
def set_all_relays(request: Request, desired: RelayStateRequest) -> RelayCollection:
    service = _service(request)
    _release_holds(request, *service.configured)
    if desired.on:
        service.turn_on_all()
    else:
        service.turn_off_all()
    logger.info("all relays set", extra={"on": desired.on})
    return _collection(request, service)


@router.post("/toggle", summary="Invert every relay independently")
def toggle_all_relays(request: Request) -> RelayCollection:
    service = _service(request)
    _release_holds(request, *service.configured)
    service.toggle_all()
    logger.info("all relays toggled")
    return _collection(request, service)


@router.get(
    "/{relay_id}",
    summary="Read one relay",
    responses={404: {"description": "No relay with that id is configured"}},
)
def get_relay(request: Request, relay_id: str) -> RelayState:
    return _one(request, _service(request), relay_id)


@router.put(
    "/{relay_id}",
    summary="Set one relay to a state",
    responses={404: {"description": "No relay with that id is configured"}},
)
def set_relay(request: Request, relay_id: str, desired: RelayStateRequest) -> RelayState:
    service = _service(request)
    # 404 first: releasing a hold on an id nobody configured would be a no-op, but
    # doing it before the id is checked reads as though unknown ids were expected.
    service.config_for(relay_id)
    _release_holds(request, relay_id)
    if desired.on:
        service.turn_on(relay_id)
    else:
        service.turn_off(relay_id)
    logger.info("relay set", extra={"relay_id": relay_id, "on": desired.on})
    return _one(request, service, relay_id)


@router.post(
    "/{relay_id}/toggle",
    summary="Invert one relay",
    responses={404: {"description": "No relay with that id is configured"}},
)
def toggle_relay(request: Request, relay_id: str) -> RelayState:
    service = _service(request)
    service.config_for(relay_id)
    _release_holds(request, relay_id)
    new_state = service.toggle(relay_id)
    logger.info("relay toggled", extra={"relay_id": relay_id, "on": new_state})
    return _one(request, service, relay_id)
