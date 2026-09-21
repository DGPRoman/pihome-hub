"""RelayService: initial-state resolution, control operations, and config validation."""

from __future__ import annotations

import logging

import pytest

from pihome_hub.relays import (
    MockRelayBackend,
    RelayConfig,
    RelayConfigError,
    RelayService,
    RelayServiceClosedError,
    UnknownRelayError,
)


class TestInitialStateResolution:
    def test_preserve_reads_the_backend(self, backend: MockRelayBackend) -> None:
        backend.seed_level(17, on=True)
        relay = RelayConfig(id="porch-light", pin=17, label="x", initial_state="preserve")

        service = RelayService(backend, [relay])

        assert service.status()["porch-light"] is True
        assert backend.is_on(17) is True

    def test_preserve_does_not_disturb_a_pin_that_was_already_off(
        self, backend: MockRelayBackend
    ) -> None:
        relay = RelayConfig(id="porch-light", pin=17, label="x", initial_state="preserve")

        RelayService(backend, [relay])

        assert backend.is_on(17) is False

    def test_forced_on_ignores_the_backends_seeded_level(self, backend: MockRelayBackend) -> None:
        backend.seed_level(17, on=False)
        relay = RelayConfig(id="porch-light", pin=17, label="x", initial_state="on")

        service = RelayService(backend, [relay])

        assert service.status()["porch-light"] is True

    def test_forced_off_ignores_the_backends_seeded_level(self, backend: MockRelayBackend) -> None:
        backend.seed_level(17, on=True)
        relay = RelayConfig(id="porch-light", pin=17, label="x", initial_state="off")

        service = RelayService(backend, [relay])

        assert service.status()["porch-light"] is False


class TestControlOperations:
    def test_turn_on(self, service: RelayService, backend: MockRelayBackend) -> None:
        service.turn_on("porch-light")
        assert service.status()["porch-light"] is True
        assert backend.is_on(17) is True

    def test_turn_off(self, service: RelayService, backend: MockRelayBackend) -> None:
        service.turn_on("porch-light")
        service.turn_off("porch-light")
        assert service.status()["porch-light"] is False
        assert backend.is_on(17) is False

    def test_toggle_flips_from_off_to_on(self, service: RelayService) -> None:
        assert service.toggle("porch-light") is True
        assert service.status()["porch-light"] is True

    def test_toggle_flips_from_on_to_off(self, service: RelayService) -> None:
        service.turn_on("porch-light")
        assert service.toggle("porch-light") is False

    def test_unknown_relay_raises_on_every_operation(self, service: RelayService) -> None:
        with pytest.raises(UnknownRelayError, match="ghost-relay"):
            service.turn_on("ghost-relay")
        with pytest.raises(UnknownRelayError):
            service.turn_off("ghost-relay")
        with pytest.raises(UnknownRelayError):
            service.toggle("ghost-relay")


class TestGroupOperations:
    def test_turn_on_all(self, service: RelayService) -> None:
        assert service.turn_on_all() == {"porch-light": True, "gate-light": True}

    def test_turn_off_all(self, service: RelayService) -> None:
        service.turn_on_all()
        assert service.turn_off_all() == {"porch-light": False, "gate-light": False}

    def test_toggle_all_inverts_every_relay_independently(self, service: RelayService) -> None:
        service.turn_on("porch-light")
        # porch-light is on, gate-light is off: a desynced pair.

        result = service.toggle_all()

        assert result == {"porch-light": False, "gate-light": True}

    def test_toggle_all_does_not_let_one_relay_dictate_the_rest(
        self, backend: MockRelayBackend
    ) -> None:
        """Deriving one shared new state from a single relay would override any
        relay that had drifted out of sync; each must flip against its own state."""
        already_on = RelayConfig(id="already-on", pin=5, label="x", initial_state="on")
        already_off = RelayConfig(id="already-off", pin=6, label="x", initial_state="off")
        service = RelayService(backend, [already_on, already_off])

        result = service.toggle_all()

        assert result == {"already-on": False, "already-off": True}


class TestConfigValidation:
    def test_rejects_a_duplicate_relay_id(self, backend: MockRelayBackend) -> None:
        first = RelayConfig(id="light", pin=17, label="x")
        second = RelayConfig(id="light", pin=27, label="y")

        with pytest.raises(RelayConfigError, match="duplicate relay id"):
            RelayService(backend, [first, second])

    def test_rejects_two_relays_sharing_a_pin(self, backend: MockRelayBackend) -> None:
        first = RelayConfig(id="porch-light", pin=17, label="x")
        second = RelayConfig(id="gate-light", pin=17, label="y")

        with pytest.raises(RelayConfigError, match="pin 17"):
            RelayService(backend, [first, second])


class TestClose:
    def test_releases_every_relays_pin(self, backend: MockRelayBackend, porch: RelayConfig) -> None:
        service = RelayService(backend, [porch])

        service.close()

        assert backend.claimed == frozenset()
        with pytest.raises(RuntimeError, match="setup_output"):
            backend.write(porch.pin, on=True)

    def test_a_second_close_does_nothing_at_all(
        self, backend: MockRelayBackend, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Three callers release a service — __main__'s finally, the lifespan's owned
        branch, and the self-call that unwinds a part-way startup — so only their
        current ordering kept the second one from writing to a released pin.

        It came back as "failed to drive relay to its shutdown state", with a
        RuntimeError about setup_output underneath: a hardware fault during shutdown,
        reported when nothing whatever is wrong. Asserted on log records rather than
        on state, because the second close leaves the relay exactly where the first
        one put it and the complaint is the only trace it leaves.
        """
        relay = RelayConfig(
            id="porch-light", pin=17, label="Porch", initial_state="on", shutdown_state="off"
        )
        service = RelayService(backend, [relay])
        service.close()
        assert backend.is_on(relay.pin) is False, "the first close did nothing to assert about"

        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="pihome_hub.relays.service"):
            service.close()

        assert caplog.records == []

    def test_close_is_reported_once_it_has_run(
        self, backend: MockRelayBackend, porch: RelayConfig
    ) -> None:
        service = RelayService(backend, [porch])
        assert service.closed is False

        service.close()

        assert service.closed is True

    def test_driving_a_closed_service_says_so(
        self, backend: MockRelayBackend, porch: RelayConfig
    ) -> None:
        """Not a hardware error. The pins are gone; the wiring is fine."""
        service = RelayService(backend, [porch])
        service.close()

        with pytest.raises(RelayServiceClosedError, match="closed"):
            service.turn_on(porch.id)

    @pytest.mark.parametrize("operation", ["turn_on", "turn_off", "toggle"])
    def test_every_write_refuses_once_closed(
        self, backend: MockRelayBackend, porch: RelayConfig, operation: str
    ) -> None:
        service = RelayService(backend, [porch])
        service.close()

        with pytest.raises(RelayServiceClosedError):
            getattr(service, operation)(porch.id)

    def test_reading_still_works_and_reports_the_last_state_set(
        self, backend: MockRelayBackend
    ) -> None:
        """Documented rather than forbidden: shutdown logging and diagnostics run at
        exactly this moment, and raising there would replace a plain answer with a
        traceback. `closed` is how a caller learns not to trust it."""
        relay = RelayConfig(
            id="porch-light", pin=17, label="Porch", initial_state="off", shutdown_state="off"
        )
        service = RelayService(backend, [relay])
        service.turn_on(relay.id)

        service.close()

        assert service.status() == {"porch-light": False}
        assert service.state_of("porch-light") is False
        assert service.closed is True
