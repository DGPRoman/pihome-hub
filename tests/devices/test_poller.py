"""Polling, against a stub device rather than a board.

The device on the other end is an ``httpx2.MockTransport``: a function that decides
what answers. That makes every case below one a real device could produce and none
of them ones anybody has to arrange hardware for — a machine that is off, a key that
was rotated on one side only, a board halfway through a reboot answering nothing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from pydantic import SecretStr

from pihome_hub.devices import (
    MAX_STATE_BYTES,
    Device,
    DeviceAnnouncement,
    DeviceKind,
    DeviceRegistry,
)
from pihome_hub.devices.poller import DevicePoller
from pihome_hub.storage import prepare_database

WORKSHOP = Device(id="workshop-pc", label="Workshop PC", kind=DeviceKind.PC_POWER)
STUDY = Device(id="study-pc", label="Study PC", kind=DeviceKind.PC_POWER)

START = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
KEY = "device-key-value-goes-here"

#: What an esp32c3-pc-power board serves at /v1/power.
ANSWER = {"state": "on", "pending": "none", "observed_at_ms": 412934, "uptime_ms": 498210}

Handler = Callable[[httpx2.Request], httpx2.Response]


class Clock:
    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


@pytest.fixture
def registry(tmp_path: Path) -> DeviceRegistry:
    path = tmp_path / "hub.db"
    prepare_database(path)
    return DeviceRegistry(path, [WORKSHOP, STUDY], clock=Clock())


@pytest.fixture
def announced(registry: DeviceRegistry) -> DeviceRegistry:
    registry.announce(
        "workshop-pc",
        DeviceAnnouncement(address="http://10.0.0.5", api_key=SecretStr(KEY), firmware="0.1.0"),
    )
    return registry


def answering(handler: Handler) -> Callable[[float], httpx2.AsyncClient]:
    """Build the client factory the poller takes, backed by a stub device."""

    def factory(timeout_seconds: float) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler),
            timeout=httpx2.Timeout(timeout_seconds),
        )

    return factory


def poller(
    registry: DeviceRegistry,
    handler: Handler,
    *,
    timeout_seconds: float = 5.0,
    interval_seconds: float = 0.01,
) -> DevicePoller:
    return DevicePoller(
        registry,
        interval_seconds=interval_seconds,
        timeout_seconds=timeout_seconds,
        client_factory=answering(handler),
    )


async def run_one_cycle(device_poller: DevicePoller, handler: Handler) -> None:
    async with answering(handler)(5.0) as client:
        await device_poller.poll_once(client)


@pytest.mark.anyio
class TestADeviceThatAnswers:
    async def test_what_it_said_is_what_is_stored(self, announced: DeviceRegistry) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json=ANSWER)

        await run_one_cycle(poller(announced, handler), handler)
        status = announced.status("workshop-pc")

        assert status.reachable is True
        assert status.state == ANSWER
        assert status.last_error is None

    async def test_it_is_asked_at_the_path_its_kind_publishes(
        self, announced: DeviceRegistry
    ) -> None:
        seen: list[str] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(str(request.url))
            return httpx2.Response(200, json=ANSWER)

        await run_one_cycle(poller(announced, handler), handler)

        assert seen == ["http://10.0.0.5/v1/power"]

    async def test_its_own_key_is_what_is_presented(self, announced: DeviceRegistry) -> None:
        """Not the hub's. Each device generated its own and this is where it goes."""
        seen: list[str | None] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request.headers.get("X-API-Key"))
            return httpx2.Response(200, json=ANSWER)

        await run_one_cycle(poller(announced, handler), handler)

        assert seen == [KEY]


@pytest.mark.anyio
class TestADeviceThatDoesNot:
    async def test_a_refused_connection_is_recorded_not_swallowed(
        self, announced: DeviceRegistry
    ) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectError("All connection attempts failed")

        await run_one_cycle(poller(announced, handler), handler)
        status = announced.status("workshop-pc")

        assert status.reachable is False
        assert status.last_error is not None
        assert "ConnectError" in status.last_error

    async def test_an_unauthorised_answer_says_so(self, announced: DeviceRegistry) -> None:
        """A key rotated on the device and not here. Unreachable is the honest word:
        this hub cannot read that device's state, whatever the reason."""

        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(401, json={"detail": "Invalid or missing API key"})

        await run_one_cycle(poller(announced, handler), handler)
        status = announced.status("workshop-pc")

        assert status.reachable is False
        assert status.last_error == "answered 401"

    async def test_a_device_that_never_answers_is_given_up_on(
        self, announced: DeviceRegistry
    ) -> None:
        """The deadline covers the exchange, so a peer that simply stops writing
        does not hold the cycle open for as long as it likes."""

        async def handler(request: httpx2.Request) -> httpx2.Response:
            await asyncio.sleep(30)
            return httpx2.Response(200, json=ANSWER)

        device_poller = DevicePoller(
            announced,
            interval_seconds=0.01,
            timeout_seconds=0.05,
            client_factory=answering(handler),  # type: ignore[arg-type]  # async handler
        )
        async with answering(handler)(0.05) as client:  # type: ignore[arg-type]
            await asyncio.wait_for(device_poller.poll_once(client), timeout=5)

        status = announced.status("workshop-pc")
        assert status.reachable is False
        assert status.last_error == "did not answer within 0.05s"

    async def test_an_answer_that_is_not_json_is_refused(self, announced: DeviceRegistry) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, text="<html>router login</html>")

        await run_one_cycle(poller(announced, handler), handler)
        status = announced.status("workshop-pc")

        assert status.reachable is False
        assert status.last_error is not None
        assert "not JSON" in status.last_error

    async def test_a_json_array_is_not_a_status_document(self, announced: DeviceRegistry) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json=[1, 2, 3])

        await run_one_cycle(poller(announced, handler), handler)

        assert announced.status("workshop-pc").last_error == (
            "answered a JSON list rather than an object"
        )

    async def test_a_body_that_does_not_stop_is_refused_by_the_byte(
        self, announced: DeviceRegistry
    ) -> None:
        """Counted as it arrives. Content-Length is a claim by the thing being
        bounded, so believing it would be believing exactly the wrong party."""

        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                200,
                content=b'{"pad": "' + b"x" * (MAX_STATE_BYTES * 4) + b'"}',
                headers={"Content-Length": "12"},
            )

        await run_one_cycle(poller(announced, handler), handler)
        status = announced.status("workshop-pc")

        assert status.reachable is False
        assert status.last_error == f"answered more than {MAX_STATE_BYTES} bytes"

    async def test_the_last_reading_survives_the_failure(self, announced: DeviceRegistry) -> None:
        answers: list[httpx2.Response] = [
            httpx2.Response(200, json=ANSWER),
            httpx2.Response(503),
        ]

        def handler(request: httpx2.Request) -> httpx2.Response:
            return answers.pop(0)

        await run_one_cycle(poller(announced, handler), handler)
        await run_one_cycle(poller(announced, handler), handler)

        status = announced.status("workshop-pc")
        assert status.reachable is False
        assert status.state == ANSWER, "a dated reading is more use to a client than none"


@pytest.mark.anyio
class TestACycleWithSeveralDevices:
    async def test_one_device_failing_does_not_hide_another_answering(
        self, registry: DeviceRegistry
    ) -> None:
        registry.announce(
            "workshop-pc", DeviceAnnouncement(address="http://10.0.0.5", api_key=SecretStr(KEY))
        )
        registry.announce(
            "study-pc", DeviceAnnouncement(address="http://10.0.0.6", api_key=SecretStr(KEY))
        )

        def handler(request: httpx2.Request) -> httpx2.Response:
            if request.url.host == "10.0.0.5":
                raise httpx2.ConnectError("All connection attempts failed")
            return httpx2.Response(200, json=ANSWER)

        await run_one_cycle(poller(registry, handler), handler)

        assert registry.status("workshop-pc").reachable is False
        assert registry.status("study-pc").reachable is True

    async def test_a_device_that_has_never_announced_is_not_asked(
        self, announced: DeviceRegistry
    ) -> None:
        asked: list[str] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            asked.append(str(request.url.host))
            return httpx2.Response(200, json=ANSWER)

        await run_one_cycle(poller(announced, handler), handler)

        assert asked == ["10.0.0.5"]
        assert announced.status("study-pc").reachable is None

    async def test_nothing_announced_means_no_requests_at_all(
        self, registry: DeviceRegistry
    ) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            raise AssertionError("a device with no address was polled")

        await run_one_cycle(poller(registry, handler), handler)


@pytest.mark.anyio
class TestTheLoop:
    async def test_it_keeps_polling_until_it_is_closed(self, announced: DeviceRegistry) -> None:
        cycles = 0
        three = asyncio.Event()

        def handler(request: httpx2.Request) -> httpx2.Response:
            nonlocal cycles
            cycles += 1
            if cycles >= 3:
                three.set()
            return httpx2.Response(200, json=ANSWER)

        device_poller = poller(announced, handler, interval_seconds=0.001)
        device_poller.start()
        try:
            await asyncio.wait_for(three.wait(), timeout=5)
        finally:
            await device_poller.aclose()

        assert cycles >= 3

    async def test_a_cycle_that_raises_is_not_the_last_one(
        self, announced: DeviceRegistry, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A poller that stopped on an unexpected error would report a silent device
        by saying nothing, which reads exactly like a device that is fine."""
        calls = 0
        answered = asyncio.Event()

        def handler(request: httpx2.Request) -> httpx2.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("something nobody anticipated")
            answered.set()
            return httpx2.Response(200, json=ANSWER)

        device_poller = poller(announced, handler, interval_seconds=0.001)
        device_poller.start()
        try:
            await asyncio.wait_for(answered.wait(), timeout=5)
        finally:
            await device_poller.aclose()

        # The second request is the proof: the loop came round again. Not what the
        # registry then held — the event fires inside the handler, before the write,
        # so asserting on the row here would be asserting on a race with aclose().
        assert calls >= 2
        assert "device poll cycle failed" in caplog.text

    async def test_starting_twice_does_not_run_two_loops(self, announced: DeviceRegistry) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json=ANSWER)

        device_poller = poller(announced, handler, interval_seconds=10)
        device_poller.start()
        first = device_poller._task
        device_poller.start()
        try:
            assert device_poller._task is first
        finally:
            await device_poller.aclose()

    async def test_closing_one_that_never_started_is_not_an_error(
        self, announced: DeviceRegistry
    ) -> None:
        """The lifespan closes what it built, and it may not have got that far."""

        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json=ANSWER)

        await poller(announced, handler).aclose()

    async def test_closing_leaves_nothing_running(self, announced: DeviceRegistry) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json=ANSWER)

        device_poller = poller(announced, handler, interval_seconds=0.001)
        device_poller.start()
        await asyncio.sleep(0.005)
        await device_poller.aclose()

        assert device_poller._task is None
