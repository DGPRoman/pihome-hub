"""The mock backend itself — the foundation every other relay test relies on."""

from __future__ import annotations

import pytest

from pihome_hub.relays import MockRelayBackend


class TestReadLevel:
    def test_defaults_to_off_for_an_unseeded_pin(self, backend: MockRelayBackend) -> None:
        assert backend.read_level(17, active_low=True) is False

    def test_reflects_a_seeded_level(self, backend: MockRelayBackend) -> None:
        backend.seed_level(17, on=True)
        assert backend.read_level(17, active_low=True) is True


class TestSetupOutput:
    def test_claims_the_pin_and_drives_the_initial_level(self, backend: MockRelayBackend) -> None:
        backend.setup_output(17, active_low=True, initial=True)
        assert backend.is_on(17) is True

    def test_overrides_any_previously_seeded_level(self, backend: MockRelayBackend) -> None:
        backend.seed_level(17, on=True)
        backend.setup_output(17, active_low=True, initial=False)
        assert backend.is_on(17) is False


class TestWrite:
    def test_updates_the_recorded_level(self, backend: MockRelayBackend) -> None:
        backend.setup_output(17, active_low=True, initial=False)
        backend.write(17, on=True)
        assert backend.is_on(17) is True

    def test_refuses_to_write_before_setup(self, backend: MockRelayBackend) -> None:
        with pytest.raises(RuntimeError, match="setup_output"):
            backend.write(17, on=True)


class TestClose:
    def test_a_closed_pin_can_no_longer_be_written(self, backend: MockRelayBackend) -> None:
        backend.setup_output(17, active_low=True, initial=False)
        backend.close(17)
        with pytest.raises(RuntimeError, match="setup_output"):
            backend.write(17, on=True)

    def test_closing_an_unclaimed_pin_does_not_raise(self, backend: MockRelayBackend) -> None:
        backend.close(17)
