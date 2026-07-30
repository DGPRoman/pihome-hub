"""Relay configuration validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pihome_hub.relays import RelayConfig


class TestDefaults:
    def test_active_low_defaults_true(self) -> None:
        assert RelayConfig(id="porch-light", pin=17, label="Porch light").active_low is True

    def test_initial_state_defaults_to_preserve(self) -> None:
        assert (
            RelayConfig(id="porch-light", pin=17, label="Porch light").initial_state == "preserve"
        )

    def test_is_immutable(self) -> None:
        relay = RelayConfig(id="porch-light", pin=17, label="Porch light")
        with pytest.raises(ValidationError):
            relay.pin = 27


class TestIdValidation:
    @pytest.mark.parametrize("valid_id", ["porch-light", "gate", "esp32-n1", "a1"])
    def test_accepts_a_slug(self, valid_id: str) -> None:
        assert RelayConfig(id=valid_id, pin=17, label="x").id == valid_id

    @pytest.mark.parametrize(
        "invalid_id",
        ["Porch-Light", "porch_light", "porch light", "-porch", "porch-", "porch--light", ""],
    )
    def test_rejects_anything_else(self, invalid_id: str) -> None:
        with pytest.raises(ValidationError, match="id"):
            RelayConfig(id=invalid_id, pin=17, label="x")


class TestPinValidation:
    @pytest.mark.parametrize("pin", [-1, 28, 100])
    def test_rejects_pins_outside_bcm_range(self, pin: int) -> None:
        with pytest.raises(ValidationError):
            RelayConfig(id="x", pin=pin, label="x")

    @pytest.mark.parametrize("pin", [0, 17, 27])
    def test_accepts_pins_inside_bcm_range(self, pin: int) -> None:
        assert RelayConfig(id="x", pin=pin, label="x").pin == pin
