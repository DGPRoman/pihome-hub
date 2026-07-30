"""Entry-point behaviour: an unconfigured service must fail loudly and readably."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from pihome_hub.__main__ import (
    EXIT_CONFIGURATION_ERROR,
    main,
    render_configuration_error,
)
from pihome_hub.config import Settings
from tests.conftest import VALID_KEY


def _missing_keys_error() -> ValidationError:
    with pytest.raises(ValidationError) as caught:
        Settings()  # type: ignore[call-arg]  # deliberately omitting required fields
    return caught.value


class TestConfigurationErrorRendering:
    def test_names_the_environment_variables_the_operator_must_set(self) -> None:
        message = render_configuration_error(_missing_keys_error())
        assert "PIHOME_RELAY_API_KEY" in message
        assert "PIHOME_SENSOR_API_KEY" in message

    def test_points_at_the_example_file(self) -> None:
        message = render_configuration_error(_missing_keys_error())
        assert ".env.example" in message

    def test_does_not_include_a_traceback(self) -> None:
        message = render_configuration_error(_missing_keys_error())
        assert "Traceback" not in message
        assert "pydantic_core" not in message

    def test_does_not_echo_a_rejected_secret(self) -> None:
        """A too-short key must not be reflected back into the logs."""
        with pytest.raises(ValidationError) as caught:
            Settings(relay_api_key=SecretStr("short"), sensor_api_key=SecretStr(VALID_KEY))
        assert "short" not in render_configuration_error(caught.value)


class TestMain:
    def test_exits_with_a_configuration_code_when_unconfigured(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as caught:
            main()

        assert caught.value.code == EXIT_CONFIGURATION_ERROR
        assert "PIHOME_RELAY_API_KEY" in capsys.readouterr().err
