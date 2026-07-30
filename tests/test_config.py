"""Configuration validation.

These tests pin down the security-relevant defaults. If someone later changes the
default bind address or turns the docs on by default, the suite should object.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pihome_hub.config import MIN_API_KEY_LENGTH
from tests.conftest import VALID_KEY, build_settings


class TestSecureDefaults:
    def test_binds_to_loopback_by_default(self) -> None:
        assert build_settings().host == "127.0.0.1"

    def test_docs_are_disabled_by_default(self) -> None:
        assert build_settings().docs_enabled is False

    def test_access_log_is_disabled_by_default(self) -> None:
        assert build_settings().access_log is False

    def test_relay_and_sensor_keys_are_independent(self) -> None:
        settings = build_settings()
        assert (
            settings.relay_api_key.get_secret_value() != settings.sensor_api_key.get_secret_value()
        )


class TestApiKeyValidation:
    def test_accepts_a_sufficiently_long_key(self) -> None:
        settings = build_settings(relay_api_key=VALID_KEY)
        assert settings.relay_api_key.get_secret_value() == VALID_KEY

    def test_rejects_a_key_that_is_too_short(self) -> None:
        short = "a" * (MIN_API_KEY_LENGTH - 1)
        with pytest.raises(ValidationError, match="at least"):
            build_settings(relay_api_key=short)

    @pytest.mark.parametrize(
        "weak",
        [
            "CHANGE_ME_TO_LONG_RANDOM_AAAAAAAAAAAAAAAAAA",
            "changeme-changeme-changeme-changeme-changeme",
            "this-is-an-example-key-do-not-use-in-production",
            "placeholder-placeholder-placeholder-placeho",
        ],
    )
    def test_rejects_example_config_left_in_place(self, weak: str) -> None:
        with pytest.raises(ValidationError, match="example config"):
            build_settings(sensor_api_key=weak)

    def test_keys_are_required(self) -> None:
        with pytest.raises(ValidationError, match="relay_api_key"):
            build_settings(relay_api_key=None)

    def test_secret_is_not_exposed_by_repr(self) -> None:
        settings = build_settings()
        assert VALID_KEY not in repr(settings)
        assert VALID_KEY not in str(settings.relay_api_key)


class TestServerSettings:
    @pytest.mark.parametrize("port", [0, 65536, -1])
    def test_rejects_out_of_range_ports(self, port: int) -> None:
        with pytest.raises(ValidationError):
            build_settings(port=port)

    def test_rejects_blank_host(self) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            build_settings(host="   ")

    def test_settings_are_immutable(self) -> None:
        settings = build_settings()
        with pytest.raises(ValidationError):
            settings.port = 1234
