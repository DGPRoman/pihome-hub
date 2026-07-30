"""RelayService: initial-state resolution, control operations, and config validation."""

from __future__ import annotations

import pytest

from pihome_hub.relays import (
    MockRelayBackend,
    RelayConfig,
    RelayConfigError,
    RelayService,
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

        with pytest.raises(RuntimeError, match="setup_output"):
            backend.write(porch.pin, on=True)
