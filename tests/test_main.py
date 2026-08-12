"""Entry-point behaviour: an unconfigured service must fail loudly and readably."""

from __future__ import annotations

from pathlib import Path

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


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, automation: str) -> None:
    """Point a runnable service at one relay and the given automation rules."""
    (tmp_path / "relays.yaml").write_text(
        "relays:\n  - id: porch-light\n    label: Porch light\n    pin: 17\n"
    )
    (tmp_path / "automation.yaml").write_text(automation)

    monkeypatch.setenv("PIHOME_RELAY_API_KEY", VALID_KEY)
    monkeypatch.setenv("PIHOME_SENSOR_API_KEY", VALID_KEY[::-1])
    monkeypatch.setenv("PIHOME_RELAY_CONFIG_PATH", str(tmp_path / "relays.yaml"))
    monkeypatch.setenv("PIHOME_AUTOMATION_CONFIG_PATH", str(tmp_path / "automation.yaml"))

    # Reaching the server means the configuration was accepted. Failing here turns a
    # regression into a failed test rather than a suite that hangs on a bound port.
    monkeypatch.setattr(
        "pihome_hub.__main__.uvicorn.run",
        lambda *args, **kwargs: pytest.fail("the server started on a rejected configuration"),
    )


class TestMain:
    def test_exits_with_a_configuration_code_when_unconfigured(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as caught:
            main()

        assert caught.value.code == EXIT_CONFIGURATION_ERROR
        assert "PIHOME_RELAY_API_KEY" in capsys.readouterr().err

    def test_a_rule_naming_something_absent_is_rejected_before_the_server_starts(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Validating rules inside the lifespan exits 3, which the unit retries forever."""
        _configure(
            monkeypatch,
            tmp_path,
            "rules:\n"
            "  - id: porch-motion-light\n"
            "    when: {device: ghost, motion: true}\n"
            "    then: {relay: porch-light, state: on}\n",
        )

        with pytest.raises(SystemExit) as caught:
            main()

        assert caught.value.code == EXIT_CONFIGURATION_ERROR
        error = capsys.readouterr().err
        assert "porch-motion-light" in error
        assert "ghost" in error
        assert "Traceback" not in error

    def test_a_malformed_automation_file_is_rejected_before_the_server_starts(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _configure(monkeypatch, tmp_path, "rules: [{id: broken}]\n")

        with pytest.raises(SystemExit) as caught:
            main()

        assert caught.value.code == EXIT_CONFIGURATION_ERROR
        assert "Traceback" not in capsys.readouterr().err
