"""The mock backend itself — the foundation every other relay test relies on."""

from __future__ import annotations

import pytest

from pihome_hub.relays import MockRelayBackend


class TestReadLevel:
    def test_an_unseeded_pin_is_unknown_rather_than_off(self, backend: MockRelayBackend) -> None:
        """Not False: this backend has no physical world to read.

        Answering False would let a test pass in a situation where the real
        backend reports that it cannot tell — which is the same conflation that
        made ``initial_state: preserve`` energise a relay on a level the probe
        itself had created.
        """
        assert backend.read_level(17, active_low=True) is None

    def test_reflects_a_seeded_level(self, backend: MockRelayBackend) -> None:
        backend.seed_level(17, on=True)
        assert backend.read_level(17, active_low=True) is True

    def test_reflects_a_seeded_off_level_distinctly_from_an_unseeded_one(
        self, backend: MockRelayBackend
    ) -> None:
        backend.seed_level(17, on=False)
        assert backend.read_level(17, active_low=True) is False


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
