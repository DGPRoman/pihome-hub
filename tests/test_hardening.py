"""Guards for the four design-level issues raised in review."""

from __future__ import annotations

import ipaddress
from typing import Never

import pytest
from pydantic import ValidationError

from pihome_hub.ratelimit import FailureLimiter
from pihome_hub.relays import (
    MockRelayBackend,
    RelayConfig,
    RelayHardwareError,
    RelayService,
)
from pihome_hub.security import normalise_client
from tests.conftest import build_settings


class TestShutdownState:
    def test_leave_does_not_drive_the_relay(self) -> None:
        backend = MockRelayBackend()
        relay = RelayConfig(id="porch-light", pin=17, label="P", initial_state="on")
        service = RelayService(backend, [relay])

        service.close()

        assert service.status()["porch-light"] is True

    def test_off_de_energises_before_releasing_the_pin(self) -> None:
        backend = MockRelayBackend()
        relay = RelayConfig(
            id="porch-light", pin=17, label="P", initial_state="on", shutdown_state="off"
        )
        service = RelayService(backend, [relay])
        assert service.status()["porch-light"] is True

        service.close()

        assert service.status()["porch-light"] is False

    def test_on_energises_before_releasing_the_pin(self) -> None:
        backend = MockRelayBackend()
        relay = RelayConfig(
            id="gate-light", pin=27, label="G", initial_state="off", shutdown_state="on"
        )
        service = RelayService(backend, [relay])

        service.close()

        assert service.status()["gate-light"] is True

    def test_the_pin_is_still_released_afterwards(self) -> None:
        backend = MockRelayBackend()
        relay = RelayConfig(id="porch-light", pin=17, label="P", shutdown_state="off")
        service = RelayService(backend, [relay])

        service.close()

        with pytest.raises(RuntimeError, match="setup_output"):
            backend.write(17, on=True)

    def test_defaults_to_leave(self) -> None:
        assert RelayConfig(id="porch-light", pin=17, label="P").shutdown_state == "leave"


class TestHardwareFailuresAreTranslated:
    def test_a_backend_that_cannot_claim_a_pin_raises_relay_hardware_error(self) -> None:
        """gpiozero raises its own exception types; none of them are ours."""

        class RefusingBackend(MockRelayBackend):
            def setup_output(self, pin: int, *, active_low: bool, initial: bool) -> Never:
                msg = "Unable to load any default pin factory!"
                raise OSError(msg)

        relay = RelayConfig(id="porch-light", pin=17, label="P")

        with pytest.raises(RelayHardwareError) as caught:
            RelayService(RefusingBackend(), [relay])

        message = str(caught.value)
        assert "pin 17" in message
        assert "porch-light" in message
        assert "gpio" in message, "the message should hint at group membership"

    def test_relays_claimed_before_the_failure_are_released(self) -> None:
        """A half-built service must not leave pins claimed behind it."""
        released: list[int] = []

        busy_pin = 27

        class FailsOnSecondPin(MockRelayBackend):
            def setup_output(self, pin: int, *, active_low: bool, initial: bool) -> None:
                if pin == busy_pin:
                    msg = "pin busy"
                    raise OSError(msg)
                super().setup_output(pin, active_low=active_low, initial=initial)

            def close(self, pin: int) -> None:
                released.append(pin)
                super().close(pin)

        relays = [
            RelayConfig(id="porch-light", pin=17, label="P"),
            RelayConfig(id="gate-light", pin=27, label="G"),
        ]

        with pytest.raises(RelayHardwareError):
            RelayService(FailsOnSecondPin(), relays)

        assert released == [17], "the successfully claimed pin should have been released"


class TestLimiterIdentity:
    @pytest.mark.parametrize(
        ("host", "expected"),
        [
            ("192.0.2.10", "192.0.2.10"),
            ("127.0.0.1", "127.0.0.1"),
            ("::ffff:192.0.2.10", "192.0.2.10"),
            ("2001:db8::1", "2001:db8::/64"),
            ("2001:db8::dead:beef", "2001:db8::/64"),
            ("not-an-address", "not-an-address"),
        ],
    )
    def test_addresses_collapse_to_a_stable_bucket(self, host: str, expected: str) -> None:
        assert normalise_client(host) == expected

    def test_rotating_within_an_ipv6_allocation_shares_one_bucket(self) -> None:
        """A routed /64 holds ~1.8e19 addresses; each must not buy a fresh allowance."""
        network = ipaddress.ip_network("2001:db8:abcd:1234::/64")
        buckets = {normalise_client(str(network[offset])) for offset in (1, 2, 500, 99_999)}
        assert len(buckets) == 1


class TestEvictionDoesNotUnblock:
    def test_a_blocked_client_survives_address_churn(self) -> None:
        """LRU eviction would let an attacker push their own block out of the table."""
        limiter = FailureLimiter(max_failures=3, window_seconds=600.0, max_tracked_clients=8)

        for _ in range(3):
            limiter.record_failure("relay:192.0.2.99")
        assert limiter.is_blocked("relay:192.0.2.99")

        # Churn far more distinct sources than the table can hold.
        for index in range(200):
            limiter.record_failure(f"relay:198.51.100.{index % 256}")

        assert limiter.is_blocked("relay:192.0.2.99"), "the blocked client was evicted"

    def test_the_table_stays_bounded(self) -> None:
        limiter = FailureLimiter(max_failures=3, window_seconds=600.0, max_tracked_clients=8)
        for index in range(500):
            limiter.record_failure(f"relay:10.0.{index // 256}.{index % 256}")
        assert limiter.failure_count("relay:10.0.0.0") == 0


class TestDocsStayOnLoopback:
    def test_docs_on_loopback_are_allowed(self) -> None:
        assert build_settings(docs_enabled=True, host="127.0.0.1").docs_enabled is True

    def test_docs_on_localhost_are_allowed(self) -> None:
        assert build_settings(docs_enabled=True, host="localhost").docs_enabled is True

    @pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::"])  # noqa: S104
    def test_docs_on_a_reachable_interface_are_refused(self, host: str) -> None:
        with pytest.raises(ValidationError, match="docs_enabled"):
            build_settings(docs_enabled=True, host=host)

    def test_binding_wide_without_docs_is_fine(self) -> None:
        assert build_settings(host="0.0.0.0").host == "0.0.0.0"  # noqa: S104
