"""Asking every announced device how it is, on an interval.

A sensor pushes and a device is asked, which is the whole difference between this
and :mod:`pihome_hub.sensors`. Being the one who asks means being the one who has to
decide when to stop waiting — so the timeout, the size ceiling and the refusal to
follow a redirect are all here, and none of them is optional.

The rule the whole module is built around: a device that does not answer is recorded
as not answering. Not skipped, not left showing its last reading as though it were
current, and not allowed to take the cycle down with it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, Final

import httpx2

from pihome_hub.devices.models import MAX_STATE_BYTES, PollTarget
from pihome_hub.devices.registry import DeviceRegistry

logger = logging.getLogger(__name__)

#: Header a device expects its key in. The same name the hub's own API uses, which
#: is a coincidence of convention rather than a shared implementation.
API_KEY_HEADER: Final = "X-API-Key"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DeviceUnreachableError(Exception):
    """One poll did not produce a status document. Never leaves this module."""


def _default_client(timeout_seconds: float) -> httpx2.AsyncClient:
    """The client every poll goes through, configured for a device on a LAN.

    ``trust_env=False`` because ``HTTP_PROXY`` in the unit's environment would send
    a device's key to whatever that variable named, for a request that never needed
    to leave the local network.

    ``follow_redirects=False`` is the default and is written down anyway: a redirect
    is a device — or something answering where a device used to be — choosing the
    next address this hub connects to, and the key would go with it.
    """
    return httpx2.AsyncClient(
        timeout=httpx2.Timeout(timeout_seconds),
        follow_redirects=False,
        trust_env=False,
    )


class DevicePoller:
    """Polls every announced device and writes what it finds to the registry."""

    def __init__(
        self,
        registry: DeviceRegistry,
        *,
        interval_seconds: float,
        timeout_seconds: float,
        client_factory: Callable[[float], httpx2.AsyncClient] = _default_client,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._registry = registry
        self._interval = interval_seconds
        self._timeout = timeout_seconds
        self._client_factory = client_factory
        self._clock = clock
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Begin polling. Idempotent, so a second call does not run two cycles."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="device-poller")

    async def aclose(self) -> None:
        """Stop polling and wait for the cycle in flight to unwind."""
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        # Suppressed rather than handled: the task was cancelled on purpose and this
        # is the await that confirms it finished, which is the whole point of it.
        with suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        async with self._client_factory(self._timeout) as client:
            while True:
                try:
                    await self.poll_once(client)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A cycle that raised must not be the last one. The failure this
                    # module exists to report is a device going quiet, and a poller
                    # that stopped on an unexpected error would report that by
                    # reporting nothing at all — which is the same as saying the
                    # device is fine, forever.
                    logger.exception("device poll cycle failed")
                # Slept after the cycle rather than at a fixed rate, so a slow cycle
                # delays the next one instead of stacking a second on top of it.
                await asyncio.sleep(self._interval)

    async def poll_once(self, client: httpx2.AsyncClient) -> None:
        """Ask every announced device once, concurrently.

        Separate from the loop so a test — and anything wanting a reading now —
        can run one cycle without waiting an interval for it.
        """
        targets = await asyncio.to_thread(self._registry.targets)
        if not targets:
            return
        await asyncio.gather(*(self._poll(client, target) for target in targets))

    async def _poll(self, client: httpx2.AsyncClient, target: PollTarget) -> None:
        try:
            state = await self._read_status(client, target)
        except DeviceUnreachableError as exc:
            # to_thread for the same reason every other write here uses it: the
            # registry is synchronous SQLite, and a BEGIN IMMEDIATE waiting on a
            # busy database would otherwise hold the whole event loop.
            await asyncio.to_thread(
                self._registry.record_failure, target, str(exc), at=self._clock()
            )
            logger.warning(
                "device did not answer",
                extra={"device_id": target.id, "address": target.address, "reason": str(exc)},
            )
            return

        await asyncio.to_thread(self._registry.record_success, target, state, at=self._clock())
        logger.debug(
            "device answered",
            extra={"device_id": target.id, "address": target.address},
        )

    async def _read_status(self, client: httpx2.AsyncClient, target: PollTarget) -> dict[str, Any]:
        """One request, bounded in time and in bytes, or :class:`DeviceUnreachableError`.

        The deadline wraps the whole exchange rather than each operation. httpx's own
        timeouts are per connect, per read and per write, which together bound nothing:
        a peer sending one byte inside every read window keeps a request alive for as
        long as it likes. Cancelling the coroutine is what actually ends it.
        """
        headers = {API_KEY_HEADER: target.api_key, "Accept": "application/json"}
        try:
            async with asyncio.timeout(self._timeout):
                async with client.stream("GET", target.url, headers=headers) as response:
                    if response.status_code != httpx2.codes.OK:
                        # Read nothing. A device that answered 401 has told us all we
                        # can act on, and its body is not ours to trust or to store.
                        msg = f"answered {response.status_code}"
                        raise DeviceUnreachableError(msg)
                    body = await _read_bounded(response)
        except TimeoutError as exc:
            msg = f"did not answer within {self._timeout}s"
            raise DeviceUnreachableError(msg) from exc
        except httpx2.HTTPError as exc:
            msg = f"{type(exc).__name__}: {exc}"
            raise DeviceUnreachableError(msg) from exc

        try:
            document: Any = json.loads(body)
        except ValueError as exc:
            msg = f"answered something that is not JSON: {exc}"
            raise DeviceUnreachableError(msg) from exc

        if not isinstance(document, dict):
            msg = f"answered a JSON {type(document).__name__} rather than an object"
            raise DeviceUnreachableError(msg)

        return document


async def _read_bounded(response: httpx2.Response) -> bytes:
    """Read a response body up to :data:`MAX_STATE_BYTES`, and refuse a longer one.

    Streamed rather than taken whole. ``Content-Length`` is a claim by whoever is
    answering, so trusting it to decide whether to read is trusting the thing being
    bounded; counting what actually arrives is not.
    """
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_STATE_BYTES:
            msg = f"answered more than {MAX_STATE_BYTES} bytes"
            raise DeviceUnreachableError(msg)
        chunks.append(chunk)
    return b"".join(chunks)
