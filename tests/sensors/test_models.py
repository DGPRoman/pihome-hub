"""Validation of incoming sensor readings."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from pihome_hub.sensors import SensorReading


class TestReadingValidation:
    def test_accepts_motion_alone(self) -> None:
        assert SensorReading(motion=True).motion is True

    def test_accepts_climate_alone(self) -> None:
        reading = SensorReading(temperature=21.5, humidity=48.0)
        assert reading.has_climate is True

    def test_accepts_an_integer_temperature(self) -> None:
        """Firmware sending 21 rather than 21.0 is normal and harmless."""
        assert SensorReading(temperature=21).temperature == 21.0

    def test_rejects_an_empty_reading(self) -> None:
        with pytest.raises(ValidationError, match="at least one"):
            SensorReading()

    def test_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            SensorReading(motion=True, pressure=1013)  # type: ignore[call-arg]

    @pytest.mark.parametrize("bad", ["yes", "true", 1, 0])
    def test_rejects_a_non_boolean_motion(self, bad: Any) -> None:
        """Motion decides whether a light comes on; guessing at intent is wrong."""
        with pytest.raises(ValidationError):
            SensorReading(motion=bad)

    @pytest.mark.parametrize("temperature", [-60.0, 100.0])
    def test_rejects_an_implausible_temperature(self, temperature: float) -> None:
        with pytest.raises(ValidationError):
            SensorReading(temperature=temperature)

    @pytest.mark.parametrize("humidity", [-1.0, 101.0])
    def test_rejects_an_impossible_humidity(self, humidity: float) -> None:
        with pytest.raises(ValidationError):
            SensorReading(humidity=humidity)

    def test_has_climate_is_false_for_motion_only(self) -> None:
        assert SensorReading(motion=True).has_climate is False
